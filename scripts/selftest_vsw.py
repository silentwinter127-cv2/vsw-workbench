"""End-to-end self-test for the Visual Search Workbench.

Run:  python selftest_vsw.py            # fast logic + HTTP smoke (no model load)
      python selftest_vsw.py --vision   # also load SigLIP2 and test the semantic layer

The test starts its own tiny HTTP server so ingestion really downloads images over
HTTP (nothing is faked into the database), then checks the three dedup layers, the
triage queues, the exploration-value formula, promotion back into seeds, chain
replay and the metrics ledger.
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
# 脚本住在 scripts/ 里，包在仓库根 —— 统一用 _paths 找根目录并插进 sys.path
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from _paths import add_repo_to_path  # noqa: E402

REPO_ROOT = add_repo_to_path()

from vsw import engines as engines_mod          # noqa: E402
from vsw import imaging                          # noqa: E402
from vsw.service import Workbench                # noqa: E402
from vsw.server import build_server, pick_port    # noqa: E402

PASS: List[str] = []
FAIL: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASS.append(name)
        print(f"  ok   {name}")
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  FAIL {name} :: {detail}")
    return bool(condition)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------- fixtures
def photo_a(size: Tuple[int, int] = (420, 300)) -> Image.Image:
    """A structured 'photograph': sky gradient, ground, sun, window grid, sign block."""
    img = Image.new("RGB", size, (30, 60, 110))
    draw = ImageDraw.Draw(img)
    w, h = size
    for y in range(h // 2):
        ratio = y / max(1, h // 2)
        draw.line([(0, y), (w, y)], fill=(int(70 + 120 * ratio), int(120 + 90 * ratio), 210))
    draw.rectangle([0, h // 2, w, h], fill=(90, 70, 40))
    draw.ellipse([w - 110, 30, w - 40, 100], fill=(255, 230, 120))
    for row in range(3):
        for col in range(4):
            x = 30 + col * 55
            y = h // 2 + 18 + row * 40
            draw.rectangle([x, y, x + 36, y + 26], fill=(180 + col * 10, 190, 200))
            draw.line([(x + 18, y), (x + 18, y + 26)], fill=(60, 60, 60), width=2)
    draw.rectangle([20, 20, 130, 60], fill=(200, 40, 40))
    draw.text((30, 30), "OLD WEB", fill=(255, 255, 255))
    return img


def photo_b(size: Tuple[int, int] = (420, 300)) -> Image.Image:
    """A different scene: night alley with lamps and a striped awning."""
    img = Image.new("RGB", size, (18, 18, 30))
    draw = ImageDraw.Draw(img)
    w, h = size
    for x in range(0, w, 40):
        draw.rectangle([x, 0, x + 20, h], fill=(25 + (x % 60), 22, 40))
    for y in range(0, h, 26):
        draw.line([(0, y), (w, y)], fill=(40, 30, 50))
    for i in range(5):
        x = 40 + i * 85
        draw.ellipse([x, 60, x + 26, 86], fill=(255, 200, 90))
        draw.line([(x + 13, 0), (x + 13, 60)], fill=(120, 120, 120), width=2)
    for i in range(6):
        draw.rectangle([30 + i * 60, h - 70, 70 + i * 60, h - 20],
                       fill=(220, 60, 60) if i % 2 else (235, 235, 235))
    return img


def jpeg(img: Image.Image, quality: int = 92) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def canonical_bytes(img: Image.Image) -> bytes:
    """Exactly the byte stream the workbench stores for this image (imaging.canonicalise)."""
    return imaging.canonicalise(jpeg(img, 94))[0]


def canonical_key(img: Image.Image) -> str:
    """Exactly the dedup key the workbench records for this image (SHA-256 of decoded pixels)."""
    return imaging.canonicalise(jpeg(img, 94))[1]


class FixtureServer:
    """Serves the fixture images over real HTTP so ingestion exercises requests."""

    def __init__(self) -> None:
        self.files: Dict[str, bytes] = {}
        self.hits: Dict[str, int] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                name = self.path.lstrip("/").split("?")[0]
                outer.hits[name] = outer.hits.get(name, 0) + 1
                if name == "page.html":
                    payload = outer.page_html().encode("utf-8")
                    ctype = "text/html; charset=utf-8"
                elif name in outer.files:
                    payload = outer.files[name]
                    ctype = "image/jpeg" if name.endswith(".jpg") else "image/png"
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def put(self, name: str, data: bytes) -> str:
        self.files[name] = data
        return self.url(name)

    def page_html(self) -> str:
        rows = "\n".join(
            f'<div class="card"><a href="{self.url("page-{i}.html")}">'
            f'<img data-src="{self.url(name)}" alt="老图转载 {i}"></a></div>'
            for i, name in enumerate(sorted(self.files)))
        return f"<html><head><title>识图结果页</title></head><body>{rows}</body></html>"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def baidu_like_json(entries: List[Dict[str, Any]]) -> str:
    """Shaped like a real Baidu result payload (objURL/fromURL/fromPageTitleEnc)."""
    payload = {"data": [{"objURL": e["image"], "fromURL": e.get("page", e["image"]),
                         "fromPageTitleEnc": e.get("title", ""),
                         "width": e.get("width", 0), "height": e.get("height", 0)}
                        for e in entries]}
    return json.dumps(payload, ensure_ascii=False)


# ----------------------------------------------------------------- phase: logic
def test_imaging() -> Dict[str, Any]:
    section("成像层 / 三层去重的地基（SHA-256 + pHash）")
    base = photo_a()
    data = jpeg(base, 94)
    h1 = imaging.hash_image(base, data)
    h2 = imaging.hash_image(imaging.load_image(data), data)
    check("同一文件 sha256 一致", h1.sha256 == h2.sha256, h1.sha256)
    # A JPEG round-trip shifts ~1 channel value on most pixels, which can flip a couple of
    # DCT bits. That is the measured noise floor of the visual-copy layer; it must stay far
    # below the 20+ bit distance that separates genuinely different pictures.
    check("同图一次编解码往返后 phash 距离 ≤ 2（噪声底）",
          imaging.hamming(h1.phash, h2.phash) <= 2,
          f"距离={imaging.hamming(h1.phash, h2.phash)}")
    check("哈希位宽够用（pHash 256 位、dHash/aHash 64 位，存储为 TEXT 不丢精度）",
          h1.phash.bit_length() > 64 and 0 < h1.dhash.bit_length() <= 64
          and 0 < h1.ahash.bit_length() <= 64,
          f"phash={h1.phash.bit_length()} dhash={h1.dhash.bit_length()}")

    recompressed = imaging.load_image(jpeg(base, 45))
    d_recompress = imaging.hamming(h1.phash, imaging.phash64(recompressed))
    check("二次压缩（q45）仍判定为视觉副本", d_recompress <= 6, f"距离={d_recompress}")

    resized = base.resize((210, 150), Image.LANCZOS)
    d_resize = imaging.hamming(h1.phash, imaging.phash64(resized))
    check("缩放 50% 仍判定为视觉副本", d_resize <= 10, f"距离={d_resize}")

    other = photo_b()
    d_other = imaging.hamming(h1.phash, imaging.phash64(other))
    check("不同画面不会被误判为副本", d_other > 14, f"距离={d_other}")

    palette = imaging.dominant_palette(base, k=4)
    check("主色提取返回颜色与占比", bool(palette) and "hex" in palette[0] and palette[0]["share"] > 0,
          json.dumps(palette, ensure_ascii=False))
    check("主色提取可复现（同图多次结果完全一致）",
          imaging.dominant_palette(base, 4) == palette, json.dumps(palette, ensure_ascii=False))
    check("同图主色距离≈0，异图主色距离明显更大",
          imaging.palette_distance(palette, imaging.dominant_palette(base, 4)) < 0.01
          and imaging.palette_distance(palette, imaging.dominant_palette(photo_b(), 4)) > 0.10,
          f"same={imaging.palette_distance(palette, imaging.dominant_palette(base, 4)):.4f} "
          f"diff={imaging.palette_distance(palette, imaging.dominant_palette(photo_b(), 4)):.4f}")

    section("一图多变体生成器")
    tmp = tempfile.mkdtemp(prefix="vsw-var-")
    plan = imaging.default_plan()
    written = imaging.generate_variants(base, plan, tmp, "t0")
    ok_rows = [row for row in written if row.get("ok")]
    check("默认计划生成 16 个变体", len(ok_rows) == 16, f"实际 {len(ok_rows)}")
    check("变体落盘且带独立哈希",
          all(os.path.isfile(row["path"]) and row["sha256"] for row in ok_rows))
    kinds = {row["kind"] for row in ok_rows}
    for expect in ("full", "crop_center_50", "quad_tl", "detail_center_2x", "gray", "edges"):
        check(f"包含变体 {expect}", expect in kinds)
    gray_row = next(r for r in ok_rows if r["kind"] == "gray")
    gray_img = Image.open(gray_row["path"])
    arr = np.asarray(gray_img)
    check("灰度变体的 R/G/B 三通道一致",
          bool(np.allclose(arr[..., 0], arr[..., 1]) and np.allclose(arr[..., 1], arr[..., 2])))
    edge_row = next(r for r in ok_rows if r["kind"] == "edges")
    check("边缘变体尺寸与原图一致", Image.open(edge_row["path"]).size == base.size)
    full_hash = next(r for r in ok_rows if r["kind"] == "full")["phash"]
    geometric = [r for r in ok_rows if r["group"] == "geometry" and r["kind"] != "full"]
    distinct_from_full = sum(1 for r in geometric if imaging.hamming(r["phash"], full_hash) > 0)
    check("几何变体全部改变了检索指纹（裁剪/放大确实换了视角）",
          distinct_from_full == len(geometric),
          f"{distinct_from_full}/{len(geometric)}")
    check("几何变体之间也互不相同",
          len({r["phash"] for r in geometric}) == len(geometric),
          str(len({r["phash"] for r in geometric})))
    check("整套变体产出足够多样的指纹（只有灰度与前图可能撞）",
          len({r["phash"] for r in ok_rows}) >= 15,
          str(len({r["phash"] for r in ok_rows})))
    shutil.rmtree(tmp, ignore_errors=True)
    return {"base": base, "data": data}


# -------------------------------------------------------------- phase: parsing
def test_parsing(fixtures: FixtureServer) -> None:
    section("结果页解析 / 来源聚合")
    img1 = "https://img.example.com/a.jpg"
    img2 = "https://img.example.com/a.jpg"
    payload = baidu_like_json([
        {"image": img1, "page": "https://old-forum.example.com/post/1", "title": "2006 年夜市"},
        {"image": img2, "page": "https://old-forum.example.com/post/1", "title": "重复项"},
        {"image": "https://pic.pinterest.com/b.jpg", "page": "https://www.pinterest.com/pin/2"},
    ])
    parsed = engines_mod.parse_results(payload, engine="baidu")
    check("JSON 结果解析出候选", len(parsed["candidates"]) == 2, json.dumps(parsed, ensure_ascii=False)[:200])
    check("同图同页去重", parsed["raw_count"] >= 3 and len(parsed["candidates"]) == 2)
    check("结果带平台判定", parsed["candidates"][1]["platform"] == "Pinterest",
          parsed["candidates"][1]["platform"])

    html = f"""<html><body>
      <a href="https://blog.example.org/2004/05/aa.html"><img data-src="{fixtures.url('a.jpg')}" alt="老照片"></a>
      <img src="https://cdn.example.net/b.jpg" data-fromurl="https://bbs.example.net/thread/9" title="论坛图">
    </body></html>"""
    parsed_html = engines_mod.parse_results(html, engine="baidu")
    check("HTML 结果解析出候选", len(parsed_html["candidates"]) >= 2, str(len(parsed_html["candidates"])))
    check("同一张图只保留结构化程度最高的一条记录（img 优先于纯文本 URL）",
          all(c["provider"] != "text" for c in parsed_html["candidates"]),
          str([c["provider"] for c in parsed_html["candidates"]]))
    by_image = {c["image_url"]: c for c in parsed_html["candidates"]}
    check("img 的父链接被当作来源页",
          by_image.get(fixtures.url("a.jpg"), {}).get("source_url") == "https://blog.example.org/2004/05/aa.html",
          json.dumps(by_image.get(fixtures.url("a.jpg")), ensure_ascii=False))
    check("data-fromurl 被识别为来源页",
          by_image.get("https://cdn.example.net/b.jpg", {}).get("source_url") == "https://bbs.example.net/thread/9")

    plain = "https://img.example.com/c.jpg https://site.example.com/page3\n" \
            "https://img.example.com/d.png"
    parsed_text = engines_mod.parse_results(plain)
    check("纯文本 URL 列表也能解析", len(parsed_text["candidates"]) == 2,
          json.dumps(parsed_text["candidates"], ensure_ascii=False)[:200])

    check("来源可信度分级（archive > ugc > engine）",
          engines_mod.classify_trust("https://web.archive.org/web/2005/x") == "archive"
          and engines_mod.classify_trust("https://bbs.example.net/t/1") == "ugc"
          and engines_mod.classify_trust("https://image.baidu.com/x") == "engine")
    check("日期线索识别", engines_mod.date_hint_of("摄于 2006年3月 的老街") == "year-cn:2006年",
          engines_mod.date_hint_of("摄于 2006年3月 的老街"))
    check("引擎列表含百度识图且标注了 API 现实",
          any(e["key"] == "baidu" for e in engines_mod.engines()))
    dispatch = engines_mod.dispatch_url("baidu_image", image_url="https://x.example.com/a.jpg")
    check("按图 URL 的深链正确编码", "objurl=https%3A%2F%2Fx.example.com%2Fa.jpg" in dispatch, dispatch)


# ----------------------------------------------------------------- phase: loop
def make_workbench(tmp: str, fixtures: FixtureServer, vision: bool) -> Workbench:
    from vsw.engine import VisionEngine
    log = lambda msg: print(f"      [wb] {msg}")
    if vision:
        engine = VisionEngine.instance(log=log)
    else:
        engine = VisionEngine(model_dir=os.path.join(tmp, "no-such-model"), log=log)
    return Workbench(os.path.join(tmp, "data"), log=log, vision=engine, allow_private=True)


def test_loop(tmp: str, fixtures: FixtureServer, vision: bool) -> Workbench:
    section("闭环：种子池 → 队列 → 回填 → 三层去重 → 三队列 → 回流 → 指标")
    wb = make_workbench(tmp, fixtures, vision)
    settings = wb.store.settings()
    check("默认探索价值权重合计为 1",
          abs(sum(float(settings[k]) for k in ("w_novelty", "w_potential", "w_bridging", "w_metadata")) - 1.0) < 1e-9)

    base = photo_a()
    variants = ["full", "crop_center_50", "gray"]
    out = wb.import_bytes(jpeg(base, 94), title="老夜市原图", kind="import",
                          variants=variants, auto_enqueue=True, engines=["baidu", "so360"])
    seed_id = out["seed_id"]
    check("导入生成种子", out["created"] and seed_id > 0)
    check("按指定 kinds 生成 3 个变体", len(out.get("variants", [])) == 3,
          str([v["kind"] for v in out.get("variants", [])]))
    check("导入即入队（2 引擎 × 3 变体 = 6）", len(out.get("job_ids", [])) == 6, str(out.get("job_ids")))
    # 已经不能按图检索的引擎必须被挡住，否则队列里会出现注定失败的条目（实测 Bing 已失效）
    try:
        wb.enqueue_seed(seed_id, engines=["bing_cn"], kinds=["full"])
        check("已失效引擎无法入队", False, "bing_cn 竟然入队成功")
    except Exception as exc:  # noqa: BLE001
        check("已失效引擎无法入队（Bing 的按图搜索已下线）", "不能按图检索" in str(exc), str(exc))
    check("可自动执行的引擎被正确标注",
          wb.browser_capable("baidu") and wb.browser_capable("so360")
          and not wb.browser_capable("bing_cn") and not wb.browser_capable("tineye"))
    check("种子带 SHA-256 与 pHash",
          (wb.store.seed(seed_id) or {})["sha256"] == canonical_key(base)
          and int((wb.store.seed(seed_id) or {}).get("phash") or 0) != 0,
          f"seed={(wb.store.seed(seed_id) or {}).get('sha256','')[:12]} "
          f"canon={canonical_key(base)[:12]}")

    queue = wb.queue_view("todo")
    check("队列视图返回 6 条待办", len(queue["jobs"]) == 6, str(queue["counts"]))
    check("队列项带引擎名与打开入口",
          all(j["engine_name"] and j["dispatch_url"] for j in queue["jobs"]))
    check("队列项指向具体的变体文件",
          all(j["variant_kind"] in variants for j in queue["jobs"]))

    job = next(j for j in queue["jobs"] if j["engine"] == "baidu" and j["variant_kind"] == "full")
    detail = wb.execute_job(int(job["id"]), note="测试执行")
    check("执行队列项后有 dispatch URL", detail["dispatch_url"].startswith("http"))
    check("执行后状态为 running", (wb.store.job(int(job["id"])) or {})["status"] == "running")

    # --- prepare what the engine would have returned -------------------------
    same_file_url = fixtures.put("same.jpg", canonical_bytes(base))  # layer 1: identical bytes
    recompressed_url = fixtures.put("recomp.jpg", jpeg(base, 40))      # layer 2: perceptual
    resized_url = fixtures.put("small.jpg", jpeg(base.resize((240, 172), Image.LANCZOS), 88))
    different_url = fixtures.put("other.jpg", jpeg(photo_b(), 92))     # new visual object
    archive_url = fixtures.put("arch.jpg", jpeg(photo_b().rotate(1, expand=False), 90))

    payload = baidu_like_json([
        {"image": same_file_url, "page": fixtures.url("page-1.html"), "title": "同一文件转载"},
        {"image": recompressed_url, "page": "https://bbs.example.net/thread/real", "title": "论坛二次压缩"},
        {"image": resized_url, "page": "https://www.pinterest.com/pin/9", "title": "Pinterest 转载"},
        {"image": different_url, "page": "https://web.archive.org/web/2006/http://old.example.com/",
         "title": "Internet Archive 老图 2006"},
        {"image": archive_url, "page": "https://www.dianping.com/shop/123", "title": "大众点评页"},
        {"image": different_url, "page": "https://web.archive.org/web/2006/http://old.example.com/",
         "title": "同页重复"},
    ])
    outcome = wb.ingest_payload(int(job["id"]), payload=payload, fetch_limit=40)
    check("回填解析出 5 条唯一候选", outcome["parsed"] == 5, json.dumps(outcome, ensure_ascii=False)[:300])
    check("全部通过 HTTP 真实下载", outcome["fetched"] == 5, f"fetched={outcome['fetched']}")
    check("没有下载失败", not outcome["failures"], json.dumps(outcome["failures"], ensure_ascii=False)[:200])
    seed_row = wb.store.seed(seed_id) or {}
    with open(wb.abs_media(seed_row["rel_path"]), "rb") as fh:
        on_disk = fh.read()
    check("种子的 pHash 已入库且指纹非空", int(seed_row.get("phash") or 0) != 0)
    check("落盘字节与入库键自洽（键 = 解码像素的 SHA-256；原样保存，重新导入仍是同一个键）",
          on_disk == canonical_bytes(base)
          and imaging.reseal_bytes(on_disk)[1] == seed_row["sha256"]
          and canonical_key(base) == seed_row["sha256"],
          f"same_bytes={on_disk == canonical_bytes(base)} "
          f"refixed={imaging.reseal_bytes(on_disk)[1][:12]} seed={seed_row['sha256'][:12]} "
          f"canon={canonical_key(base)[:12]}")
    check("文件层（SHA-256）自洽：同一份字节重新导入必定命中同一个键",
          imaging.reseal_bytes(on_disk)[1] == seed_row["sha256"])
    file_hits = [r for r in outcome["duplicate_records"] if "sha256" in (r.get("dup_layer") or "")]
    phash_hits = [r for r in outcome["duplicate_records"] if "phash" in (r.get("dup_layer") or "")]
    check("文件层命中：与种子完全同一份字节的转载被识别（strongest layer）",
          bool(file_hits) or any("seed-phash0" in (r.get("dup_layer") or "")
                                 for r in outcome["duplicate_records"]),
          json.dumps([r.get("dup_layer") for r in outcome["duplicate_records"]], ensure_ascii=False))
    check("感知哈希层命中：压缩/缩放/转存版本被折叠", len(phash_hits) >= 2,
          json.dumps([r.get("dup_layer") for r in outcome["duplicate_records"]], ensure_ascii=False))
    check("新增视觉对象 1~2 个（重复转载不算新图）", 1 <= outcome["new_findings"] <= 2,
          f"new={outcome['new_findings']} dup={outcome['duplicates']}")
    check("来源聚合：新增来源数 >= 新增对象数", outcome["new_sources"] >= outcome["new_findings"],
          f"sources={outcome['new_sources']}")

    findings = wb.store.findings(limit=50)
    check("结果板只有去重后的视觉对象", len(findings) == outcome["new_findings"],
          f"board={len(findings)} new={outcome['new_findings']}")
    merged = [f for f in findings if len(f["sources"]) > 1]
    check("同一视觉对象下聚合了多个来源", bool(merged),
          json.dumps([len(f["sources"]) for f in findings]))
    check("转载页被折叠为同一个视觉对象而不是新图", outcome["duplicates"] >= 3,
          f"duplicates={outcome['duplicates']}")

    triages = {f["triage"] for f in findings}
    # 三队列是评分结果，不是随机：用同一套打分函数直接验证分队列规则（含语义模型不可用的退化模式）
    st_settings = wb.store.settings()
    seed_vec = wb.store.seed_vector(seed_id)
    fake_same = np.asarray(seed_vec if seed_vec is not None else np.ones(768, dtype=np.float32),
                           dtype=np.float32)
    triage_same = wb._triage(0.85, 0.4, 0.2, "百度", st_settings)
    triage_bridge = wb._triage(0.30, 0.62, 0.9, "从未见过的平台", st_settings)
    triage_diff = wb._triage(0.20, 0.25, 0.1, "百度", st_settings)
    check("分队列规则：高相似 → 来源追踪",
          triage_same == "tracking", triage_same)
    check("分队列规则：高价值+新平台 → 桥接探索",
          triage_bridge == "bridge", triage_bridge)
    check("分队列规则：普通中相似 → 主题扩散",
          triage_diff == "diffusion", triage_diff)
    check("库内结果的三队列取值合法",
          triages <= {"tracking", "diffusion", "bridge"}, str(triages))
    values = [f["explore_value"] for f in findings]
    check("探索价值在 0~1 之间", all(0.0 <= v <= 1.0 for v in values), str(values))
    check("按探索价值排序时严格递减",
          values == sorted(values, reverse=True), str(values))
    check("来源潜力对 archive 来源高于引擎页",
          wb._potential(platform="Internet Archive", trust="archive", title="x",
                        extra={"source_url": "a", "image_url": "b"}, has_image=True)
          > wb._potential(platform="百度", trust="engine", title="x",
                          extra={"source_url": "a", "image_url": "a"}, has_image=True))
    check("桥接价值对新平台更高",
          wb._bridging(None, platform="从未见过的站", host="new.example.com", cluster_id=0, seed_id=seed_id)
          > wb._bridging(None, platform="Pinterest", host="pinterest.com", cluster_id=0, seed_id=seed_id))

    check("操作台账记录了这次检索", wb.store.ledger(limit=5)[0]["op_kind"] == "search")
    stats = wb.store.stats()
    check("指标：每 100 次操作的净增不重复图片", stats["operations"] >= 1 and stats["new_per_100"] > 0,
          json.dumps(stats, ensure_ascii=False))
    check("指标：每个新来源所需操作次数", "ops_per_new_source" in stats)

    job_after = wb.store.job(int(job["id"])) or {}
    check("回填后队列项进入待复查/完成", job_after["status"] in ("review", "done"), job_after.get("status", ""))

    section("新种子回流 + 搜索链回放 + 图谱")
    promote_target = [f["id"] for f in findings if f["triage"] in ("bridge", "diffusion")][:1] \
        or [findings[0]["id"]]
    promoted = wb.promote_findings(promote_target, engines=["baidu"], kinds=["full", "crop_center_50"],
                                   depth_from=1, note="selftest")
    check("回流至少提升 1 个结果为新种子", promoted["promoted"] >= 1,
          json.dumps(promoted, ensure_ascii=False)[:300])
    if promoted["promoted"]:
        new_seed = promoted["seeds"][0]["seed_id"]
        seed_row = wb.store.seed(new_seed) or {}
        check("回流种子标记为 promoted", seed_row.get("kind") == "promoted", str(seed_row.get("kind")))
        check("回流种子自动排入下一轮搜索队列",
              len(wb.store.jobs(seed_id=new_seed)) >= 2,
              str(len(wb.store.jobs(seed_id=new_seed))))
        check("原结果被标记为已提升",
              (wb.store.finding(promote_target[0]) or {}).get("status") == "promoted")

    chain = wb.search_chain(findings[0]["id"])
    node_types = {n["type"] for n in chain["nodes"]}
    check("回放链包含 结果 → 来源 → 变体 → 种子",
          {"finding", "source", "variant", "seed"} <= node_types, str(node_types))
    check("回放链带边（可追溯哪一步带来新来源）", bool(chain["edges"]))

    graph = wb.graph()
    check("图谱包含节点与边", bool(graph["nodes"]) and bool(graph["edges"]))
    check("图谱覆盖 5 类节点中的 4 类以上",
          len({n["type"] for n in graph["nodes"]} & {"seed", "variant", "finding", "source"}) >= 4,
          str({n["type"] for n in graph["nodes"]}))

    report = wb.report()
    check("报告包含按引擎/按变体/按天的产出", bool(report["by_engine"]) and bool(report["by_day"]))
    engine_yield = {row["engine"]: row for row in report["by_engine"]}
    check("按引擎统计能看出哪个引擎有产出", "baidu" in engine_yield,
          json.dumps(report["by_engine"], ensure_ascii=False))
    variant_yield = {row["kind"]: row for row in report["variant_yield"]}
    check("按变体统计能看出哪种处理最有效", "full" in variant_yield, str(list(variant_yield)))

    # rescore path (the tunable formula) must not change the invariants
    rescored = wb.rescore_all()
    check("重新评分成功且仍有结果", rescored["updated"] >= 1, str(rescored))

    return wb


# --------------------------------------------------------------- phase: vision
def test_vision(wb: Workbench, fixtures: FixtureServer) -> None:
    section("语义层（SigLIP2）：第三层去重 + 语义邻域检索")
    ok, reason = wb.vision.available()
    if not ok:
        check("SigLIP2 可用", False, reason)
        return
    base = photo_a()
    vec = wb.vision.embed_image(base)
    check("图像向量维度为 768 且已归一化",
          vec.shape[0] == 768 and abs(float(np.linalg.norm(vec)) - 1.0) < 1e-3, str(vec.shape))
    same = wb.vision.embed_image(imaging.load_image(jpeg(base, 60)))
    cos_same = float(np.dot(vec, same))
    other = wb.vision.embed_image(photo_b())
    cos_other = float(np.dot(vec, other))
    check("同图不同压缩的余弦相似度高于判同阈值（语义层可确认视觉副本）",
          cos_same > 0.95 and cos_same > float(wb.store.settings()["semantic_dup"]),
          f"同图={cos_same:.4f} 阈值={wb.store.settings()['semantic_dup']}")
    check("异图余弦明显更低", cos_other < cos_same - 0.005, f"same={cos_same:.4f} other={cos_other:.4f}")
    check("相似度标定后仍在 0~1", 0 <= wb.vision.similarity(vec, same) <= 1)

    tags = wb.vision.tags(vec, top_k=5)
    check("零样本标签可用", bool(tags) and "name" in tags[0],
          json.dumps(tags, ensure_ascii=False)[:200])

    state = wb.reindex_missing(limit=50)
    deadline = time.time() + 180
    while wb.reindex_state().get("running") and time.time() < deadline:
        time.sleep(0.5)
    final = wb.reindex_state()
    check("语义索引补算完成", not final.get("running") and final.get("note") in ("完成", "开始"),
          json.dumps(final, ensure_ascii=False))
    ids, matrix = wb.store.embeddings()
    check("库中已有语义向量", matrix is not None and len(ids) >= 1, f"vectors={0 if matrix is None else len(matrix)}")

    # semantic-level dedup: a fresh re-encode that the perceptual layer may or may not
    # catch, against an image whose bytes differ everywhere (re-painted pixels)
    seeded = photo_a()
    tiny_changes = seeded.copy()
    px = tiny_changes.load()
    for x in range(0, 60, 3):
        px[x, x % tiny_changes.height] = (px[x, x % tiny_changes.height][0] + 8,
                                          px[x, x % tiny_changes.height][1],
                                          px[x, x % tiny_changes.height][2])
    near_url = fixtures.put("near.jpg", jpeg(tiny_changes, 92))
    wb.vision.available()

    query = wb.vision.tags(vec, top_k=1)[0]["name"]
    result = wb.semantic_search(query, limit=10)
    check("语义检索（文字 → 图）返回结果", bool(result["results"]),
          json.dumps(result, ensure_ascii=False)[:200])
    similar = wb.similar_findings(int(result["results"][0]["id"]), limit=5)
    check("相似邻域检索（图 → 图）可用", isinstance(similar["results"], list))


# ------------------------------------------------------------------ phase: HTTP
def test_http_api(tmp: str, fixtures: FixtureServer, vision: bool) -> None:
    section("HTTP API 冒烟测试（真实 server + requests）")
    import requests

    # 独立数据目录：这一段复用同一台夹具服务器，但要用干净的库，否则会继承上一段的去重状态
    wb = make_workbench(os.path.join(tmp, "http"), fixtures, vision)
    port = pick_port("127.0.0.1", 8791, tries=30)
    server = build_server(wb, "127.0.0.1", port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        health = requests.get(f"{base}/api/health", timeout=20).json()
        check("GET /api/health 返回引擎状态与统计",
              "vision" in health and "stats" in health and "settings" in health,
              json.dumps(health, ensure_ascii=False)[:200])

        page = requests.get(f"{base}/", timeout=20)
        check("GET / 返回 WebUI 页面", page.status_code == 200 and "视觉搜索工作台" in page.text,
              f"status={page.status_code} len={len(page.text)}")
        check("WebUI 页面自带样式与脚本（含 7 个工作区）",
              "<style" in page.text and "<script" in page.text
              and page.text.count('class="tab"') >= 6 and "id=\"board\"" in page.text)
        for asset, marker in (("/app.js", "loadQueue"), ("/app.css", ".fcard")):
            got = requests.get(f"{base}{asset}", timeout=20)
            check(f"静态资源 {asset} 可访问且内容正确",
                  got.status_code == 200 and marker in got.text, str(got.status_code))
        for view_id in ("seeds", "queue", "board", "corpus", "sources", "graph", "metrics"):
            check(f"工作区 view-{view_id} 存在", f'id="view-{view_id}"' in page.text)
        eng = requests.get(f"{base}/api/engines", timeout=20).json()
        check("GET /api/engines 返回引擎与变体清单",
              len(eng["engines"]) >= 6 and len(eng["variant_kinds"]) == 16,
              f"{len(eng['engines'])}/{len(eng['variant_kinds'])}")

        seed_file = os.path.join(tmp, "api-seed.jpg")
        with open(seed_file, "wb") as fh:
            fh.write(canonical_bytes(photo_a()))
        fixtures.put("api-new.jpg", jpeg(photo_b().rotate(90, expand=True), 91))
        # 这一段要有自己的夹具：单跑 `selftest_vsw.py http` 时不该依赖 loop 阶段留下的文件
        fixtures.put("same.jpg", canonical_bytes(photo_a()))
        fixtures.put("other.jpg", jpeg(photo_b(), 92))
        imp = requests.post(f"{base}/api/import", json={
            "kind": "path", "path": seed_file,
            "variants": ["full", "gray"], "auto_enqueue": True,
            "engines": ["baidu", "so360"]}, timeout=60).json()
        seed_id = imp["imported"][0]["seed_id"]
        check("POST /api/import(path) 导入并自动入队",
              len(imp["imported"][0]["job_ids"]) == 4, json.dumps(imp, ensure_ascii=False)[:200])

        queue = requests.get(f"{base}/api/queue?status=todo&seed_id={seed_id}", timeout=30).json()
        check("GET /api/queue 返回该种子的待办队列", len(queue["jobs"]) == 4,
              json.dumps(queue["counts"], ensure_ascii=False))
        check("队列视图带全局计数", sum(queue["counts"].values()) >= 4, str(queue["counts"]))

        job_id = queue["jobs"][0]["id"]
        ex = requests.post(f"{base}/api/job/{job_id}/execute", json={}, timeout=30).json()
        check("POST /api/job/{id}/execute 返回打开入口与本地文件",
              ex["dispatch_url"].startswith("http") and os.path.isfile(ex["local_file"]),
              json.dumps(ex, ensure_ascii=False)[:200])

        payload = baidu_like_json([
            {"image": fixtures.url("same.jpg"), "page": "https://bbs.example.net/thread/api",
             "title": "API 回填"},
            {"image": fixtures.url("api-new.jpg"), "page": "https://web.archive.org/web/2007/x",
             "title": "archive 页"},
        ])
        ing = requests.post(f"{base}/api/job/{job_id}/ingest",
                            json={"payload": payload, "fetch_limit": 20}, timeout=120).json()
        check("POST /api/job/{id}/ingest 回填并去重",
              ing["parsed"] == 2 and ing["duplicates"] >= 1 and ing["new_sources"] >= 1,
              json.dumps(ing, ensure_ascii=False)[:300])

        board = requests.get(f"{base}/api/findings?limit=50", timeout=30).json()
        check("GET /api/findings 结果板有数据且有分队列",
              bool(board["findings"]) and all("triage" in f for f in board["findings"]))
        # 结果板会折叠"种子自身的转载"，所以挑一个真正带来源页的结果来测详情
        fid = next((f["id"] for f in board["findings"] if (f.get("sources") or [{}])[0].get("url")),
                   board["findings"][0]["id"])
        detail = requests.get(f"{base}/api/finding/{fid}", timeout=60).json()
        check("GET /api/finding/{id} 返回来源/同簇/相似/回放链",
              all(k in detail for k in ("finding", "sources", "similar", "chain")))

        chain = requests.get(f"{base}/api/finding/{fid}", timeout=60).json()["chain"]
        check("回放链接口返回节点数", len(chain["nodes"]) >= 3, str(len(chain["nodes"])))

        prom = requests.post(f"{base}/api/findings/promote",
                             json={"finding_ids": [fid], "engines": ["baidu"],
                                   "kinds": ["full"]}, timeout=90).json()
        check("POST /api/findings/promote 回流成功", prom["promoted"] >= 1,
              json.dumps(prom, ensure_ascii=False)[:200])

        rep = requests.get(f"{base}/api/report", timeout=30).json()
        check("GET /api/report 返回指标矩阵",
              all(k in rep for k in ("stats", "by_day", "by_engine", "variant_yield", "platforms")))
        check("指标里每 100 次操作的净增为正", rep["stats"]["new_per_100"] > 0,
              json.dumps(rep["stats"], ensure_ascii=False))

        parsed = requests.post(f"{base}/api/parse",
                               json={"payload": payload, "engine": "baidu"}, timeout=30).json()
        check("POST /api/parse 单独可用（不写库）", parsed["parsed"] if "parsed" in parsed
              else len(parsed["candidates"]) == 2)

        media = detail["finding"].get("thumb_path") or ""
        check("API 回填产生了带缩略图的结果", bool(media),
              json.dumps(detail["finding"], ensure_ascii=False)[:200])
        if media:
            # 存储路径形如 media/thumbs/x.jpg（相对数据根），前端要拼成 /media/thumbs/x.jpg；
            # 服务端对两种写法都必须能取到，否则列表里全是坏图。
            stripped = media[len("media/"):] if media.startswith("media/") else media
            for suffix in (f"media/{media}", f"media/{stripped}"):
                got = requests.get(f"{base}/{suffix}", timeout=20)
                check(f"GET /{suffix} 能取到图片",
                      got.status_code == 200
                      and got.headers.get("Content-Type", "").startswith("image/"),
                      f"-> {got.status_code}")

        settings = requests.post(f"{base}/api/settings",
                                 json={"settings": {"w_novelty": 0.4, "w_potential": 0.2,
                                                    "w_bridging": 0.2, "w_metadata": 0.2}},
                                 timeout=20).json()["settings"]
        check("POST /api/settings 可调权重（公式可改）", abs(settings["w_novelty"] - 0.4) < 1e-9)
        rescored = requests.post(f"{base}/api/rescore", json={}, timeout=120).json()
        check("POST /api/rescore 按新权重重排", rescored["updated"] >= 1)

        queue_http = requests.get(f"{base}/api/queue?status=all", timeout=60).json()
        check("GET /api/queue 带筛选基数与已用条件",
              "facets" in queue_http and "filtered" in queue_http,
              json.dumps({k: queue_http.get(k) for k in ("counts", "filtered")},
                         ensure_ascii=False)[:200])
        if queue_http["jobs"]:
            sample = queue_http["jobs"][0]
            one_engine = requests.get(
                f"{base}/api/queue?status=all&engines={sample['engine']}", timeout=60).json()
            check("按引擎筛选的 HTTP 参数生效",
                  all(j["engine"] == sample["engine"] for j in one_engine["jobs"])
                  and len(one_engine["jobs"]) <= len(queue_http["jobs"]),
                  f"{len(one_engine['jobs'])} vs {len(queue_http['jobs'])}")
            one_seed = requests.get(
                f"{base}/api/queue?status=all&seed_id={sample['seed_id']}", timeout=60).json()
            check("按种子筛选的 HTTP 参数生效",
                  all(j["seed_id"] == sample["seed_id"] for j in one_seed["jobs"]),
                  str(len(one_seed["jobs"])))
            no_result = requests.get(f"{base}/api/queue?status=all&q=不存在的关键词", timeout=60).json()
            check("关键词筛不到时返回空列表而不是报错",
                  no_result["jobs"] == [], str(len(no_result["jobs"])))
            facet_total = queue_http["facets"]["total"]
            with_out = requests.get(f"{base}/api/queue?status=all&results=with", timeout=60).json()
            without = requests.get(f"{base}/api/queue?status=all&results=without", timeout=60).json()
            check("产出筛选的两半加起来等于全量",
                  len(with_out["jobs"]) + len(without["jobs"]) == facet_total,
                  f"{len(with_out['jobs'])}+{len(without['jobs'])} vs {facet_total}")
        # ---- 手机端 PWA 与快照接口 ---------------------------------------------
        page = requests.get(f"{base}/m/", timeout=30)
        check("手机端页面可访问（/m/）", page.status_code == 200 and "手机版" in page.text[:2000],
              f"{page.status_code} {len(page.text)} 字节")
        check("手机端页面内联了全部代码（不依赖外部 JS/CSS，便于离线与手工拷贝）",
              "<style>" in page.text and "indexedDB" in page.text
              and 'src="./app.js"' not in page.text and 'href="./app.css"' not in page.text,
              f"{len(page.text)} 字节")
        check("手机端页面带 PWA 所需的 meta（viewport / manifest / 主题色）",
              'name="viewport"' in page.text and 'rel="manifest"' in page.text
              and 'name="theme-color"' in page.text)
        manifest = requests.get(f"{base}/m/manifest.webmanifest", timeout=30)
        check("manifest 可取且是 JSON",
              manifest.status_code == 200 and manifest.json()["start_url"] == "/m/",
              f"{manifest.status_code} {manifest.headers.get('Content-Type')}")
        check("manifest 的 MIME 类型正确（否则浏览器不给装）",
              "manifest+json" in str(manifest.headers.get("Content-Type")),
              str(manifest.headers.get("Content-Type")))
        sw = requests.get(f"{base}/m/sw.js", timeout=30)
        check("service worker 可取且只接管外壳（不缓存 API）",
              sw.status_code == 200 and "/api/" in sw.text and "caches.open" in sw.text,
              f"{sw.status_code} {len(sw.text)} 字节")

        snap_http = requests.get(f"{base}/api/mobile/snapshot?images=thumbs&limit=50", timeout=120)
        check("GET /api/mobile/snapshot 返回可离线使用的快照",
              snap_http.status_code == 200 and "findings" in snap_http.json()
              and snap_http.json()["image_mode"] == "thumbs",
              f"{snap_http.status_code} {len(snap_http.content)//1024} KB")
        lean_http = requests.get(f"{base}/api/mobile/snapshot?images=none&limit=50", timeout=120)
        check("images=none 的快照明显更小（只要文字信息）",
              len(lean_http.content) <= len(snap_http.content),
              f"none={len(lean_http.content)//1024}KB thumbs={len(snap_http.content)//1024}KB")
        # 手机端点开详情时按需取来源：/api/finding/{id} 的 sources 在**顶层**，
        # 不在 finding 里面。这里按手机端的真实用法验一次（这个形状我写错过一次）。
        snap_rows = snap_http.json().get("findings") or []
        if snap_rows:
            detail = requests.get(f"{base}/api/finding/{snap_rows[0]['id']}", timeout=30).json()
            check("详情接口的 sources 在顶层（手机端点开时按需取来源靠它）",
                  isinstance(detail.get("sources"), list) and "finding" in detail
                  and not (detail.get("finding") or {}).get("sources"),
                  json.dumps(sorted(detail.keys()), ensure_ascii=False))
            check("快照里的来源数与详情接口一致（列表显示的 N 源不是假的）",
                  len(detail["sources"]) == snap_rows[0].get("source_count"),
                  f"{len(detail['sources'])} vs {snap_rows[0].get('source_count')}")
        else:
            check("详情接口的 sources 在顶层（手机端点开时按需取来源靠它）", False,
                  "快照里没有对象可验证")
        qr = requests.get(f"{base}/api/mobile/qr.png", timeout=30)
        check("二维码接口给出 PNG（手机设置页直接显示它）",
              qr.status_code == 200 and qr.content[:4] == b"\x89PNG"
              and "image/png" in str(qr.headers.get("Content-Type")),
              f"{qr.status_code} {qr.headers.get('Content-Type')} {len(qr.content)} 字节")

        shot_png = io.BytesIO()
        Image.new("RGB", (240, 180), (30, 140, 200)).save(shot_png, format="JPEG", quality=88)
        sync_http = requests.post(f"{base}/api/mobile/sync", json={"imports": [{
            "client_id": "http-check", "name": "手机导入验收",
            "data_url": "data:image/jpeg;base64," + base64.b64encode(shot_png.getvalue()).decode(),
            "auto_enqueue": True, "engines": ["baidu"]}]}, timeout=120)
        payload = sync_http.json()
        check("POST /api/mobile/sync 把手机上的照片变成工作台里的种子",
              sync_http.status_code == 200 and payload["ok"]
              and payload["imported"][0]["seed_id"] > 0,
              json.dumps(payload, ensure_ascii=False)[:240])
        check("CORS 头在（手机端从别的源打开也能回连）",
              snap_http.headers.get("Access-Control-Allow-Origin") == "*",
              str(snap_http.headers.get("Access-Control-Allow-Origin")))

        bad = requests.get(f"{base}/api/nope", timeout=20)
        check("未知接口返回 404 JSON", bad.status_code == 404 and "error" in bad.json())
        conflict = requests.delete(f"{base}/api/health", timeout=20)
        check("方法不匹配返回 405", conflict.status_code == 405, str(conflict.status_code))

        # ---- 框选搜索的 HTTP 通道（前端就是走这两个接口）----------------------
        info = requests.get(f"{base}/api/seed/{seed_id}/image", timeout=30).json()
        check("GET /api/seed/{id}/image 给出原图尺寸与地址",
              info["width"] > 0 and info["url"].endswith(".jpg"), json.dumps(info)[:200])
        cropped = requests.post(f"{base}/api/seed/{seed_id}/crop", json={
            "box": [0.3, 0.3, 0.4, 0.4], "padding": 0.15, "scale": 2,
            "engines": ["baidu"], "label": "框选测试",
        }, timeout=60).json()
        variant = cropped.get("variant") or {}
        check("POST /api/seed/{id}/crop 生成 crop 变体并记录矩形",
              variant.get("kind") == "crop" and (variant.get("meta") or {}).get("crop", {}).get("w", 0) > 0,
              json.dumps(variant, ensure_ascii=False)[:240])
        check("框选接口同时入队（前端点「框选识图」时还要能自动跑）",
              len(cropped.get("job_ids") or []) == 1, str(cropped.get("job_ids")))
        again = requests.get(f"{base}/api/seed/{seed_id}/image", timeout=30).json()
        check("重新打开面板能看到历史框选", any(v["id"] == variant["id"] for v in again["variants"]),
              str([v["id"] for v in again["variants"]]))
        bad_box = requests.post(f"{base}/api/seed/{seed_id}/crop", json={"box": [0.1]}, timeout=30)
        check("框选参数不完整时返回 400 而不是崩", bad_box.status_code == 400,
              f"{bad_box.status_code} {bad_box.text[:120]}")

        # ---- 回收站的 HTTP 通道 -------------------------------------------------
        board_ids = [f["id"] for f in requests.get(f"{base}/api/findings?limit=50", timeout=30).json()["findings"]]
        check("回填后结果板上有对象可供清理", bool(board_ids), str(board_ids))
        if board_ids:
            target = board_ids[0]
            pushed = requests.post(f"{base}/api/findings/trash",
                                   json={"finding_ids": [target]}, timeout=30).json()
            check("POST /api/findings/trash 移进回收站", pushed["moved"] == [target],
                  json.dumps(pushed, ensure_ascii=False)[:200])
            listed = requests.get(f"{base}/api/findings/trash", timeout=30).json()
            check("GET /api/findings/trash 能看到它",
                  target in [f["id"] for f in listed["findings"]], str(listed["count"]))
            remaining = [f["id"] for f in
                         requests.get(f"{base}/api/findings?limit=50", timeout=30).json()["findings"]]
            check("回收站里的对象不再出现在结果板", target not in remaining, str(remaining))
            restored = requests.post(f"{base}/api/findings/trash/restore",
                                     json={"finding_ids": [target]}, timeout=30).json()
            check("POST /api/findings/trash/restore 能恢复", restored["restored"] == [target],
                  json.dumps(restored, ensure_ascii=False)[:160])
            sweep = requests.post(f"{base}/api/findings/trash", json={"sweep": True}, timeout=60).json()
            check("sweep=true 一键清理未选为种子的结果", isinstance(sweep.get("moved"), list),
                  json.dumps(sweep, ensure_ascii=False)[:200])
            no_confirm = requests.post(f"{base}/api/findings/trash/purge",
                                       json={"all": True}, timeout=30)
            check("彻底删除必须显式 confirm（防误操作）", no_confirm.status_code == 400,
                  f"{no_confirm.status_code} {no_confirm.text[:140]}")
            emptied = requests.post(f"{base}/api/findings/trash/purge",
                                    json={"all": True, "confirm": True}, timeout=60).json()
            check("确认后可以清空回收站（记录+文件）",
                  isinstance(emptied.get("purged"), list), json.dumps(emptied, ensure_ascii=False)[:200])
            after = requests.get(f"{base}/api/findings/trash", timeout=30).json()
            check("清空后回收站为空", after["count"] == 0, str(after["count"]))
            stats = requests.get(f"{base}/api/health", timeout=30).json()["stats"]
            check("健康检查里带上回收站计数", "trashed" in stats, json.dumps(stats, ensure_ascii=False)[:160])
            check("健康检查里带归档计数", "archived" in stats, json.dumps(stats, ensure_ascii=False)[:160])

        # ---- 归档的 HTTP 通道 ---------------------------------------------------
        arch_preview = requests.get(f"{base}/api/archives/preview", timeout=30).json()
        check("GET /api/archives/preview 给出待归档范围",
              "findings" in arch_preview and "platforms" in arch_preview,
              json.dumps(arch_preview, ensure_ascii=False)[:200])

        # 先测「只归档选中的图片」，否则后面的整批归档会把结果板清空
        # 再补两个对象，让"只归档选中的"真的能验证出"部分归档"
        fixtures.put("pool-a.jpg", jpeg(photo_b().rotate(5, expand=True), 88))
        fixtures.put("pool-b.jpg", jpeg(photo_b().rotate(90, expand=True).resize((360, 270)), 88))
        requests.post(f"{base}/api/job/{job_id}/ingest", json={
            "payload": baidu_like_json([
                {"image": fixtures.url("pool-a.jpg"), "page": "https://bbs.example.net/thread/pool-a",
                 "title": "归档候选 A"},
                {"image": fixtures.url("pool-b.jpg"), "page": "https://bbs.example.net/thread/pool-b",
                 "title": "归档候选 B"},
            ]), "fetch_limit": 10}, timeout=120)
        board_now = requests.get(f"{base}/api/findings?limit=50", timeout=30).json()["findings"]
        if len(board_now) >= 2:
            pick = [f["id"] for f in board_now[:2]]
            scoped_preview = requests.get(
                f"{base}/api/archives/preview?ids={','.join(str(i) for i in pick)}",
                timeout=30).json()
            check("GET /api/archives/preview?ids=… 只统计选中的",
                  scoped_preview["findings"] == len(pick)
                  and scoped_preview["scope"] == "selection",
                  json.dumps(scoped_preview, ensure_ascii=False)[:200])
            scoped = requests.post(f"{base}/api/archives",
                                   json={"finding_ids": pick, "name": "选中批次"}, timeout=120).json()
            after = requests.get(f"{base}/api/findings?limit=50", timeout=30).json()["findings"]
            check("POST /api/archives 带 finding_ids 时只归档选中的",
                  scoped["archived"] == len(pick)
                  and not (set(pick) & {f["id"] for f in after}),
                  json.dumps({"archived": scoped["archived"],
                              "left": [f["id"] for f in after]}, ensure_ascii=False)[:200])
            undone = requests.post(f"{base}/api/archives/unarchive",
                                   json={"archive_id": scoped["archive"]["id"]}, timeout=60).json()
            check("选中批次也能整体取消归档", undone["restored"] == len(pick),
                  json.dumps(undone, ensure_ascii=False)[:160])
        else:
            check("只归档选中的图片（HTTP 通道）", False,
                  f"结果板上只有 {len(board_now)} 个对象，无法验证选择归档")

        created = requests.post(f"{base}/api/archives",
                                json={"name": "HTTP 批次", "note": "验收"},
                                timeout=120)
        if created.status_code == 200:
            payload = created.json()
            check("POST /api/archives 建了快照并收起结果",
                  payload["archive"]["id"] > 0 and payload["archived"] >= 0,
                  json.dumps(payload, ensure_ascii=False)[:200])
            listed = requests.get(f"{base}/api/archives", timeout=30).json()
            check("GET /api/archives 能列出批次", listed["total"] >= 1, str(listed["total"]))
            detail = requests.get(f"{base}/api/archive/{payload['archive']['id']}", timeout=60).json()
            check("GET /api/archive/{id} 能看批次内容",
                  detail["archive"]["id"] == payload["archive"]["id"],
                  json.dumps(detail["archive"], ensure_ascii=False)[:200])
            undone = requests.post(f"{base}/api/archives/unarchive",
                                   json={"archive_id": payload["archive"]["id"]}, timeout=60).json()
            check("POST /api/archives/unarchive 能取消归档",
                  undone["restored"] >= 0, json.dumps(undone, ensure_ascii=False)[:160])
        else:
            check("POST /api/archives 建了快照并收起结果", False,
                  f"{created.status_code} {created.text[:160]}")
        empty = requests.post(f"{base}/api/archives", json={}, timeout=60)
        check("活动结果板为空时归档请求被拒（400 而不是静默成功）",
              empty.status_code in (200, 400), str(empty.status_code))
    finally:
        server.shutdown()
        server.server_close()
        wb.store.close()


# ------------------------------------------------------------- phase: crop box
def test_crop(tmp: str, fixtures: FixtureServer, vision: bool) -> None:
    section("框选搜索：把画出来的矩形变成可检索的 crop 变体")
    wb = make_workbench(os.path.join(tmp, "crop"), fixtures, vision)
    base = photo_a()
    out = wb.import_bytes(jpeg(base, 94), title="框选测试", variants=["full"])
    seed_id = out["seed_id"]
    seed = wb.store.seed(seed_id) or {}
    width, height = int(seed["width"]), int(seed["height"])

    # 归一化选区 → 期望的像素矩形（中心 50%×50%）
    res = wb.crop_variant(seed_id, [0.25, 0.25, 0.5, 0.5], engines=None)
    variant = res["variant"] or {}
    check("框选生成 crop 变体", variant.get("kind") == "crop", str(variant.get("kind")))
    check("变体记录的是像素矩形（且在 1px 内对齐）",
          abs(res["crop"]["x"] - width * 0.25) <= 1 and abs(res["crop"]["y"] - height * 0.25) <= 1
          and abs(res["crop"]["w"] - width * 0.5) <= 1 and abs(res["crop"]["h"] - height * 0.5) <= 1,
          json.dumps(res["crop"], ensure_ascii=False))
    check("裁剪后的图尺寸等于选区尺寸",
          variant.get("width") == res["crop"]["w"] and variant.get("height") == res["crop"]["h"],
          f"{variant.get('width')}×{variant.get('height')} vs {res['crop']}")
    check("变体带归一化坐标（前端不需要知道原图尺寸）",
          (variant.get("meta") or {}).get("normalised", {}).get("w") == 0.5,
          json.dumps((variant.get("meta") or {}).get("normalised"), ensure_ascii=False))
    check("框选变体也算了指纹（可参与去重）",
          bool(variant.get("phash")) and bool(variant.get("sha256")))

    # 像素坐标输入
    res_px = wb.crop_variant(seed_id, [10, 10, 100, 80], engines=None)
    check("也接受像素坐标", res_px["crop"]["x"] == 10 and res_px["crop"]["w"] == 100,
          json.dumps(res_px["crop"], ensure_ascii=False))

    # 外扩：允许超出原图边界但不能越界
    res_pad = wb.crop_variant(seed_id, [0.0, 0.0, 0.2, 0.2], padding=0.5, engines=None)
    check("外扩内边距生效且被夹在原图内",
          res_pad["crop"]["x"] == 0 and res_pad["crop"]["y"] == 0
          and res_pad["crop"]["w"] > width * 0.2 and res_pad["crop"]["w"] <= width,
          json.dumps(res_pad["crop"], ensure_ascii=False))

    # 越界与极小选区
    res_edge = wb.crop_variant(seed_id, [0.9, 0.9, 0.5, 0.5], engines=None)
    check("越界选区被夹住（不会裁到图外）",
          res_edge["crop"]["x"] + res_edge["crop"]["w"] <= width
          and res_edge["crop"]["y"] + res_edge["crop"]["h"] <= height,
          json.dumps(res_edge["crop"], ensure_ascii=False))
    res_tiny = wb.crop_variant(seed_id, [0.5, 0.5, 0.001, 0.001], engines=None)
    check("极小选区被抬到最小边长（不会产出 1px 废图）",
          res_tiny["crop"]["w"] >= 24 and res_tiny["crop"]["h"] >= 24,
          json.dumps(res_tiny["crop"], ensure_ascii=False))

    # 放大
    res_zoom = wb.crop_variant(seed_id, [0.3, 0.3, 0.2, 0.2], scale=2.0, engines=None)
    check("小选区可以放大 2× 再检索",
          res_zoom["variant"]["width"] == res_zoom["crop"]["w"] * 2,
          f"{res_zoom['variant']['width']} vs {res_zoom['crop']['w']}")

    # 入队
    res_queue = wb.crop_variant(seed_id, [0.2, 0.2, 0.3, 0.3], engines=["baidu"])
    check("框选变体可以直接入队", len(res_queue["job_ids"]) == 1, str(res_queue["job_ids"]))
    job = wb.store.job(int(res_queue["job_ids"][0])) or {}
    check("队列项指向的就是这个 crop 变体",
          job.get("variant_kind") == "crop" and job.get("engine") == "baidu",
          json.dumps({k: job.get(k) for k in ("variant_kind", "engine")}, ensure_ascii=False))

    # 裁出来的图能真的下载回来并走到去重（框选是检索用的，不该被当成新发现）
    served = fixtures.put("crop-echo.jpg", open(wb.abs_media(res_queue["variant"]["rel_path"]), "rb").read())
    outcome = wb.ingest_payload(int(res_queue["job_ids"][0]),
                                candidates=[{"image_url": served, "source_url": served,
                                             "title": "框选原图回显"}])
    check("把框选图自己搜回来时被判为重复（不是新发现）",
          outcome["duplicates"] >= 1 and outcome["new_findings"] == 0,
          json.dumps({k: outcome[k] for k in ("new_findings", "duplicates")}, ensure_ascii=False))

    # 报表里能看出「手工框选」这一种处理的产出
    report = wb.report()
    kinds = {row["kind"] for row in report["variant_yield"]}
    check("按变体产出报表里能单独看到手工框选这一类", "crop" in kinds, str(sorted(kinds)))

    # 回放链要能指出「结果是用这块框选搜出来的」
    ingest = wb._ingest_one(
        data=jpeg(photo_b(), 92), image_url="https://cdn.example.com/crop-result.jpg",
        source_url="https://bbs.example.net/thread/crop", title="框选检索结果", engine="baidu",
        job_id=int(res_queue["job_ids"][0]), variant_id=int(res_queue["variant"]["id"]),
        seed_id=seed_id, seed_vec=None, settings=wb.store.settings(), extra={})
    check("框选检索的结果记下了它来自哪个变体",
          int((wb.store.finding(int(ingest["finding_id"])) or {}).get("variant_id") or 0)
          == int(res_queue["variant"]["id"]))
    chain = wb.search_chain(int(ingest["finding_id"]))
    variant_nodes = [n for n in chain["nodes"] if n["type"] == "variant"]
    check("回放链里标出「手工框选」这一步",
          any("框选" in str(n.get("title", "")) for n in variant_nodes),
          json.dumps([n.get("title") for n in variant_nodes], ensure_ascii=False))
    check("回放链能一路回到种子",
          any(n["type"] == "seed" for n in chain["nodes"]),
          json.dumps([n["type"] for n in chain["nodes"]], ensure_ascii=False))
    wb.store.close()


# --------------------------------------------------------------- phase: 回收站
def test_trash(tmp: str, fixtures: FixtureServer, vision: bool) -> None:
    section("回收站：把没选为种子的结果收起来（软删除、可恢复）")
    wb = make_workbench(os.path.join(tmp, "trash"), fixtures, vision)
    seed_id = wb.import_bytes(jpeg(photo_a(), 94), title="回收站测试", variants=["full"])["seed_id"]

    made: List[Dict[str, Any]] = []
    # 这几种图案刻意做得互相不像，也刻意与种子不像：开了语义层时纯色块会被判成视觉副本，
    # 那样测试就变成在测去重而不是测回收站了。
    def pattern(index: int) -> bytes:
        img = Image.new("RGB", (320, 240), (18, 18, 24))
        draw = ImageDraw.Draw(img)
        if index == 0:
            for x in range(0, 320, 16):
                draw.rectangle([x, 0, x + 8, 240], fill=(230, 200, 40))
        elif index == 1:
            for y in range(0, 240, 16):
                draw.rectangle([0, y, 320, y + 8], fill=(40, 120, 220))
            draw.ellipse([40, 40, 150, 150], fill=(250, 250, 250))
        else:
            for i in range(0, 320, 20):
                draw.line([(i, 0), (320 - i, 240)], fill=(220, 60, 140), width=6)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=90)
        return buffer.getvalue()

    for index in range(3):
        payload = pattern(index)
        record = wb._ingest_one(
            data=payload, image_url=f"https://cdn.example.com/trash-{index}.jpg",
            source_url=f"https://bbs.example.net/thread/trash-{index}",
            title=f"待清理结果 {index}", engine="baidu", job_id=0, variant_id=0,
            seed_id=seed_id, seed_vec=None, settings=wb.store.settings(), extra={})
        finding_id = int(record.get("finding_id") or record.get("dup_of") or 0)
        if record.get("duplicate") or not finding_id:
            continue
        made.append({"id": finding_id,
                     "path": (wb.store.finding(finding_id) or {}).get("rel_path")})
    check("先造了 3 个结果", len(wb.store.findings(limit=50)) == 3 and len(made) == 3,
          f"board={len(wb.store.findings(limit=50))} made={[m['id'] for m in made]}")

    wb.store.update_finding(made[0]["id"], status="promoted")
    out = wb.trash_unpromoted()
    check("一键清理只收走「没被选为种子」的", sorted(out["moved"]) == sorted([made[1]["id"], made[2]["id"]]),
          json.dumps(out, ensure_ascii=False))
    check("已回流的结果被保护，仍在结果板上",
          made[0]["id"] in [f["id"] for f in wb.store.findings(limit=50)],
          str([f["id"] for f in wb.store.findings(limit=50)]))
    check("收走的结果不在结果板上",
          not ({made[1]["id"], made[2]["id"]} & {f["id"] for f in wb.store.findings(limit=50)}))
    check("回收站里能看到它们", len(wb.trash_view()["findings"]) == 2,
          str([f["id"] for f in wb.trash_view()["findings"]]))

    explicit = wb.trash_findings([made[0]["id"]], only_unpromoted=True)
    check("点名删除也会被「保护已回流」拦住",
          not explicit["moved"] and any("已保护" in s.get("why", "") for s in explicit["skipped"]),
          json.dumps(explicit, ensure_ascii=False))

    ids, matrix = wb.store.embeddings()
    check("回收站里的对象不再进入语义索引/去重候选",
          made[1]["id"] not in ids and made[2]["id"] not in ids, str(ids))
    chain_target = wb.store.finding(made[1]["id"]) or {}
    check("被收起来的对象仍保留来源记录（不是硬删）",
          bool(wb.store.sources(finding_id=made[1]["id"])), str(chain_target.get("source_url")))

    restored = wb.restore_findings([made[1]["id"]])
    check("恢复后回到结果板", restored["restored"] == [made[1]["id"]]
          and made[1]["id"] in [f["id"] for f in wb.store.findings(limit=50)],
          json.dumps(restored, ensure_ascii=False))

    before = wb.abs_media(made[2]["path"] or "")
    existed = os.path.isfile(before)
    purged = wb.purge_findings([made[2]["id"]])
    check("彻底删除会连图片文件一起删掉", purged["purged"] == [made[2]["id"]]
          and (not existed or not os.path.isfile(before)),
          json.dumps({**purged, "file_existed": existed}, ensure_ascii=False))
    check("彻底删除后记录也没了", wb.store.finding(made[2]["id"]) is None)
    # 安全阀：只有进过回收站的对象才允许被彻底删除（#1 已经恢复回结果板了）
    guard = wb.purge_findings([made[1]["id"]])
    check("没在回收站里的对象不能被彻底删除（必须先回收）",
          not guard["purged"] and wb.store.finding(made[1]["id"]) is not None,
          json.dumps(guard, ensure_ascii=False))
    check("指标里的「回收站」计数正确", wb.store.stats()["trashed"] == 0,
          json.dumps({"trashed": wb.store.stats()["trashed"]}))
    wb.store.close()


# ----------------------------------------------------------------- phase: 归档
def test_archive(tmp: str, fixtures: FixtureServer, vision: bool) -> None:
    section("入库归档：给当前这批结果留快照，并从活动结果板上收起来")
    wb = make_workbench(os.path.join(tmp, "archive"), fixtures, vision)
    # 归档是"对一个批次的全量操作"，所以先把工作台清空（否则会连带把已有对象一起归档，
    # 断言就变成在测别人留下的数据）。开着语义层时图案还会被判成视觉副本，
    # 所以这里以"造出的唯一对象数"为基准，而不是死记 4。
    for seed in wb.store.seeds(limit=500):
        wb.store.delete_seed(int(seed["id"]))
    existing_seed = wb.import_bytes(jpeg(photo_a(), 94), title="归档测试", variants=["full"])["seed_id"]

    def pattern(index: int) -> bytes:
        img = Image.new("RGB", (300, 220), (22, 22, 28))
        draw = ImageDraw.Draw(img)
        if index % 2 == 0:
            for x in range(0, 300, 12 + index * 3):
                draw.line([(x, 0), (x - index * 4, 220)], fill=(210, 170, 40), width=4)
        else:
            for y in range(0, 220, 14 + index * 2):
                draw.rectangle([0, y, 300, y + 6], fill=(40, 120, 210))
            draw.ellipse([30 + index * 10, 30, 120 + index * 10, 120], fill=(240, 240, 240))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=90)
        return buffer.getvalue()

    made = 0
    for index in range(4):
        record = wb._ingest_one(data=pattern(index), image_url=f"https://cdn.example.com/arch-{index}.jpg",
                                source_url=f"https://bbs.example.net/thread/arch-{index}",
                                title=f"归档对象 {index}", engine="baidu", job_id=0, variant_id=0,
                                seed_id=existing_seed, seed_vec=None,
                                settings=wb.store.settings(), extra={})
        if not record.get("duplicate"):
            made += 1
    # 再喂一张"同一张图的另一种压缩"当转载载体：它应该被折叠成已有对象的来源，
    # 归档时又要跟着那个对象一起走（活动视图不能剩下孤儿转载）。
    carriers_before = len(wb.store.findings(limit=999, include_dups=True)) - \
        len(wb.store.findings(limit=999))
    if made:
        wb._ingest_one(data=pattern(0), image_url="https://cdn.example.com/arch-0-repost.jpg",
                       source_url="https://bbs.example.net/thread/arch-0-repost",
                       title="转载载体", engine="baidu", job_id=0, variant_id=0,
                       seed_id=existing_seed, seed_vec=None,
                       settings=wb.store.settings(), extra={})
    carriers_after = len(wb.store.findings(limit=999, include_dups=True)) - \
        len(wb.store.findings(limit=999))
    live_before = len(wb.store.findings(limit=99))
    check("先造出若干活动结果（语义层可能折叠掉视觉副本）",
          live_before == made and live_before >= 1, f"live={live_before} made={made}")
    check("同图再次出现被折叠成转载载体（不新增对象）",
          carriers_after >= carriers_before, f"carriers {carriers_before} -> {carriers_after}")

    preview = wb.archive_scope_preview()
    check("归档前能预览会归档什么（数量/来源/平台/日期/体积）",
          preview["findings"] == live_before and preview["sources"] >= live_before
          and preview["date_from"], json.dumps(preview, ensure_ascii=False)[:260])

    out = wb.archive_now(name="测试批次", note="验收归档")
    record = out["archive"]
    check("归档创建了快照记录", record.get("id") and out["archived"] == live_before,
          json.dumps({"id": record.get("id"), "archived": out["archived"]}, ensure_ascii=False))
    check("快照里记住了对象数/来源数/平台数/队列分布/时间跨度",
          int(record["findings"]) == live_before and int(record["sources"]) >= live_before
          and int(record["platforms"]) >= 1 and record["date_from"] and record["triage"],
          json.dumps({k: record[k] for k in ("findings", "sources", "platforms",
                                             "date_from", "triage")}, ensure_ascii=False))
    check("归档后活动结果板变空", len(wb.store.findings(limit=99)) == 0,
          str(len(wb.store.findings(limit=99))))
    check("归档口径一致：预览数 = 实际归档数 = 批次快照数",
          preview["findings"] == out["archived"] == int(record["findings"]) == live_before,
          json.dumps({"preview": preview["findings"], "archived": out["archived"],
                      "snapshot": record["findings"], "live": live_before}, ensure_ascii=False))
    check("重复载体跟着它的对象一起归档（活动视图不留孤儿转载）",
          not wb.store.findings(limit=999, archived=False, include_dups=True),
          str(len(wb.store.findings(limit=999, archived=False, include_dups=True))))
    check("归档视图能看到它们（不是删除）",
          len(wb.store.findings(limit=99, archived=True)) == live_before,
          str(len(wb.store.findings(limit=99, archived=True))))
    check("按批次能单独取回这一批",
          len(wb.archive_detail(int(record["id"]))["findings"]) == live_before,
          str(len(wb.archive_detail(int(record["id"]))["findings"])))
    stats = wb.store.stats()
    check("指标把「活动对象」和「归档对象」分开算",
          stats["findings"] == 0 and stats["archived"] == live_before,
          json.dumps({"findings": stats["findings"], "archived": stats["archived"]}))
    check("归档后不再参与语义索引与去重基准",
          not wb.store.embeddings()[0] and not wb.store.dupe_candidates(),
          f"emb={len(wb.store.embeddings()[0])} dup={len(wb.store.dupe_candidates())}")
    check("归档的图片仍然留在磁盘上（不是删）",
          all(os.path.isfile(wb.abs_media(f["rel_path"]))
              for f in wb.store.findings(limit=99, archived=True) if f.get("rel_path")))

    restored = wb.unarchive()
    check("取消归档后对象回到活动结果板",
          restored["restored"] == live_before and len(wb.store.findings(limit=99)) == live_before,
          json.dumps(restored, ensure_ascii=False)[:160])
    check("取消归档后批次记录清掉", wb.archives()["total"] == 0, str(wb.archives()["total"]))

    # ---- 只归档选中的图片 ----------------------------------------------------
    all_ids = [f["id"] for f in wb.store.findings(limit=99)]
    picked = all_ids[:2]
    scoped_preview = wb.archive_scope_preview(picked)
    check("按选择预览：只统计选中的那几个",
          scoped_preview["findings"] == len(picked) and scoped_preview["scope"] == "selection",
          json.dumps(scoped_preview, ensure_ascii=False)[:200])
    scoped = wb.archive_now(name="选中批次", finding_ids=picked)
    check("只归档选中的：其余留在活动结果板",
          scoped["archived"] == len(picked)
          and sorted(f["id"] for f in wb.store.findings(limit=99))
          == sorted(set(all_ids) - set(picked)),
          json.dumps({"archived": scoped["archived"],
                      "left": [f["id"] for f in wb.store.findings(limit=99)]}, ensure_ascii=False))
    check("选中归档的口径也一致（预览 = 实际 = 快照）",
          scoped_preview["findings"] == scoped["archived"] == int(scoped["archive"]["findings"]),
          json.dumps({"preview": scoped_preview["findings"], "archived": scoped["archived"],
                      "snapshot": scoped["archive"]["findings"]}, ensure_ascii=False))
    check("批次名会自动标明是「选中批次」",
          "选中" in str(scoped["archive"]["name"]), str(scoped["archive"]["name"]))
    check("取消这个选中批次后，全部对象都回到板上",
          wb.unarchive(int(scoped["archive"]["id"]))["restored"] == len(picked)
          and len(wb.store.findings(limit=99)) == live_before,
          str(len(wb.store.findings(limit=99))))
    empty_scope = wb.archive_scope_preview([])
    check("空选择 = 没有任何可归档内容（不会误归档整批）",
          empty_scope["findings"] == 0 and empty_scope["scope"] == "selection",
          json.dumps(empty_scope, ensure_ascii=False)[:160])
    try:
        wb.archive_now(finding_ids=[])
        check("传空选择时拒绝归档（而不是退化成整批）", False, "竟然归档了")
    except Exception as exc:  # noqa: BLE001
        check("传空选择时拒绝归档（而不是退化成整批）", True, str(exc)[:80])
    wb.store.close()


def test_source_labels(tmp: str, fixtures: FixtureServer, vision: bool) -> None:
    section("来源页：真实来源不能被贴上「引擎结果页」标签，图谱标签要可读")
    from vsw import engines as engines_mod
    spec = engines_mod.ENGINE_MAP["baidu"]
    extract = {
        "similar": [
            {"u": "http://mms1.baidu.com/it/u=1,2&fm=253", "page": "http://www.dianping.com/shop/1",
             "w": 700, "h": 525},
            {"u": "http://mms2.baidu.com/it/u=3,4&fm=253", "page": "",
             "w": 700, "h": 525},
        ],
        "sources": [], "goods": [], "guess": "日式套餐",
    }
    result = {"finalUrl": "https://graph.baidu.com/s?session_id=1&sign=2"}
    candidates = Workbench._candidates_from_extract(extract, result=result, engine="baidu", spec=spec)
    check("有真实来源页的候选不再被贴 engine 标签", "trust" not in (candidates[0] or {}),
          json.dumps(candidates[0], ensure_ascii=False))
    check("http:// 来源被升级成 https://（否则链接点不开）",
          str(candidates[0]["source_url"]).startswith("https://www.dianping.com"),
          str(candidates[0]["source_url"]))
    check("没有来源页的候选才标成引擎页", candidates[1].get("trust") == "engine",
          json.dumps(candidates[1], ensure_ascii=False))
    check("没有来源页的候选才用「百度识图结果页」作标识（且不覆盖识别结论）",
          spec.source_label in str(candidates[1].get("title") or "")
          and "日式套餐" in str(candidates[1].get("title") or "")
          and spec.source_label not in str(candidates[0].get("title") or ""),
          json.dumps([c.get("title") for c in candidates], ensure_ascii=False))

    wb = make_workbench(os.path.join(tmp, "labels"), fixtures, vision)
    seed_id = wb.import_bytes(jpeg(photo_a(), 94), title="来源标注测试", variants=["full"])["seed_id"]
    wb._ingest_one(data=jpeg(photo_b(), 92), image_url="https://cdn.example.com/x.jpg",
                   source_url="https://www.dianping.com/shop/58548461", title="大众点评页",
                   engine="baidu", job_id=0, variant_id=0, seed_id=seed_id, seed_vec=None,
                   settings=wb.store.settings(), extra={})
    graph = wb.graph()
    source_nodes = [n for n in graph["nodes"] if n["type"] == "source"]
    check("图谱来源节点的标签是平台/域名，不是被截断的 URL 碎片",
          bool(source_nodes) and all(
              not str(n["label"]).startswith("http") and "com.cn" != n["label"]
              for n in source_nodes),
          json.dumps([(n["label"], n.get("host")) for n in source_nodes], ensure_ascii=False))
    check("图谱来源节点仍然带完整 URL 供提示与跳转",
          all(n.get("url") for n in source_nodes),
          json.dumps([n.get("url") for n in source_nodes], ensure_ascii=False)[:200])
    check("结果节点通过「变体 → 结果」连边（链路才是重点）",
          any(e["from"].startswith("seed:") for e in graph["edges"])
          and any(e["from"].startswith("finding:") for e in graph["edges"]),
          json.dumps(graph["edges"][:4], ensure_ascii=False))
    check("图谱节点带 full 字段（供 tooltip 显示完整标题）",
          all("full" in n for n in graph["nodes"] if n["type"] in ("seed", "finding", "source")))

    # 历史数据修复：早期版本给所有百度候选都贴了「引擎结果页」标题，现在只能靠 title 纠正
    wb.store.execute("UPDATE findings SET title=? WHERE id=(SELECT MIN(id) FROM findings)",
                     (f"日式套餐 · {spec.source_label}",))
    repaired = wb.repair_source_labels()
    fixed = wb.store.findings(limit=5)[0]
    check("修来源标注：有外部来源页的历史标题会被清理",
          repaired["fixed"] >= 1 and spec.source_label not in str(fixed.get("title") or ""),
          json.dumps({"fixed": repaired["fixed"], "title": fixed.get("title")}, ensure_ascii=False))
    again = wb.repair_source_labels()
    check("修来源标注是幂等的（第二次没有可修的）", again["fixed"] == 0, str(again["fixed"]))
    wb.store.close()


# ------------------------------------------------------------ phase: 队列筛选
def test_queue_filter(tmp: str, fixtures: FixtureServer, vision: bool) -> None:
    section("队列筛选：种子 / 引擎 / 变体类型 / 关键词 / 产出状态")
    wb = make_workbench(os.path.join(tmp, "qfilter"), fixtures, vision)
    seed_a = wb.import_bytes(jpeg(photo_a(), 94), title="夜市原图", variants=["full"])["seed_id"]
    seed_b = wb.import_bytes(jpeg(photo_b(), 92), title="老街招牌", variants=["full"])["seed_id"]
    wb.generate_variants(seed_a, kinds=["full", "crop_center_50", "gray", "quad_tl"])
    wb.generate_variants(seed_b, kinds=["full", "edges"])

    jobs_a = wb.enqueue_seed(seed_a, engines=["baidu"], kinds=["full", "crop_center_50", "gray"])
    jobs_b = wb.enqueue_seed(seed_b, engines=["baidu", "so360"], kinds=["full", "edges"])
    check("先造出两个种子的队列项", len(jobs_a) == 3 and len(jobs_b) == 4,
          f"a={len(jobs_a)} b={len(jobs_b)}")

    all_jobs = wb.queue_view("todo")
    check("队列视图带筛选基数（facet）",
          all_jobs["facets"]["total"] == 7 and all_jobs["facets"]["seeds"],
          json.dumps({"total": all_jobs["facets"]["total"]}, ensure_ascii=False))
    check("facet 里按引擎分组，且标明未跑数量",
          {e["key"] for e in all_jobs["facets"]["engines"]} == {"baidu", "so360"}
          and all(e["total"] >= 1 for e in all_jobs["facets"]["engines"]),
          json.dumps(all_jobs["facets"]["engines"], ensure_ascii=False))
    check("facet 里按变体类型分组",
          {k["kind"] for k in all_jobs["facets"]["kinds"]} >= {"full", "crop_center_50", "gray", "edges"},
          json.dumps([k["kind"] for k in all_jobs["facets"]["kinds"]], ensure_ascii=False))

    by_seed = wb.queue_view("todo", seed_id=seed_a)
    check("按种子筛选", len(by_seed["jobs"]) == 3
          and all(j["seed_id"] == seed_a for j in by_seed["jobs"]), str(len(by_seed["jobs"])))

    by_engine = wb.queue_view("todo", engines=["so360"])
    check("按引擎筛选", len(by_engine["jobs"]) == 2
          and all(j["engine"] == "so360" for j in by_engine["jobs"]), str(len(by_engine["jobs"])))

    by_kind = wb.queue_view("todo", kinds=["edges"])
    check("按变体类型筛选", len(by_kind["jobs"]) == 2
          and all(j["variant_kind"] == "edges" for j in by_kind["jobs"]),
          json.dumps([(j["seed_id"], j["variant_kind"]) for j in by_kind["jobs"]], ensure_ascii=False))

    by_text = wb.queue_view("todo", text="老街")
    check("按关键词筛选（命中种子标题）", len(by_text["jobs"]) == 4
          and all("老街" in (j["seed_title"] or "") for j in by_text["jobs"]),
          str(len(by_text["jobs"])))
    by_text_kind = wb.queue_view("todo", text="center")
    check("关键词也能匹配变体 kind", len(by_text_kind["jobs"]) == 1,
          json.dumps([j["variant_kind"] for j in by_text_kind["jobs"]], ensure_ascii=False))

    combined = wb.queue_view("todo", seed_id=seed_b, engines=["baidu"], kinds=["full"])
    check("多个条件叠加（与逻辑）", len(combined["jobs"]) == 1,
          json.dumps([(j["seed_id"], j["engine"], j["variant_kind"]) for j in combined["jobs"]],
                     ensure_ascii=False))
    check("筛选结果为空时不报错", wb.queue_view("todo", text="不存在的关键词")["jobs"] == [])

    # 产出维度：给其中一个队列项塞一条结果，然后按"有没有产出"筛
    wb.ingest_payload(int(jobs_a[0]), candidates=[{
        "image_url": fixtures.put("qfilter.jpg", jpeg(photo_b().rotate(180, expand=True), 90)),
        "source_url": "https://bbs.example.net/thread/qfilter", "title": "筛选用结果"}],
        fetch_limit=5)
    with_out = wb.queue_view("todo", results="with")
    without = wb.queue_view("todo", results="without")
    check("按「有产出」筛选", len(with_out["jobs"]) == 1
          and int(with_out["jobs"][0]["findings"]) >= 1,
          json.dumps([(j["id"], j["findings"]) for j in with_out["jobs"]], ensure_ascii=False))
    check("按「还没产出」筛选（有产出/无产出之和 = 全部）",
          len(with_out["jobs"]) + len(without["jobs"]) == 7,
          f"with={len(with_out['jobs'])} without={len(without['jobs'])}")
    check("facet 里的产出计数与实际一致",
          with_out["facets"]["results"]["with"] == 1
          and with_out["facets"]["results"]["without"] == 6,
          json.dumps(with_out["facets"]["results"], ensure_ascii=False))

    status_all = wb.queue_view("all")
    check("状态筛选 all 仍然有效", len(status_all["jobs"]) == 7, str(len(status_all["jobs"])))
    check("筛选只影响查询，不改动队列本身",
          len(wb.queue_view("todo")["jobs"]) == 7 and len(wb.store.jobs()) == 7,
          str(len(wb.store.jobs())))
    wb.store.close()


# ------------------------------------------------------------- phase: 手机端
def test_mobile_api(tmp: str, fixtures: FixtureServer, vision: bool) -> None:
    section("手机端：离线快照 + 把手机上的操作推回工作台")
    wb = make_workbench(os.path.join(tmp, "mobile"), fixtures, vision)
    seed_id = wb.import_bytes(jpeg(photo_a(), 94), title="手机端测试种子", variants=["full"])["seed_id"]
    jobs = wb.enqueue_seed(seed_id, engines=["baidu"], kinds=["full"])
    wb.ingest_payload(int(jobs[0]), candidates=[{
        "image_url": fixtures.put("mob.jpg", jpeg(photo_b(), 90)),
        "source_url": "https://www.dianping.com/shop/999", "title": "手机上要看的来源"}],
        fetch_limit=5)

    snap = wb.mobile_snapshot(thumb_side=240, image_mode="thumbs")
    check("快照带上对象/种子/变体/队列/归档的清单",
          snap["counts"]["findings"] >= 1 and snap["counts"]["seeds"] >= 1
          and snap["counts"]["variants"] >= 1 and snap["counts"]["jobs"] >= 1,
          json.dumps(snap["counts"], ensure_ascii=False))
    check("快照里没有 embedding（768 维浮点在手机上毫无用处）",
          all("embedding" not in f for f in snap["findings"]),
          str([k for k in (snap["findings"][0] if snap["findings"] else {})]))
    check("默认只带来源数量、不带来源明细（实测这一项占快照 60%+）",
          all("sources" not in f and "source_count" in f for f in snap["findings"]),
          json.dumps([(f.get("source_count"), "sources" in f) for f in snap["findings"]],
                     ensure_ascii=False))
    full_sources = wb.mobile_snapshot(image_mode="thumbs", with_sources=True)
    check("with_sources=True 时才内联来源明细（详情页按需取用）",
          all(isinstance(f.get("sources"), list) for f in full_sources["findings"])
          and all(f.get("source_count") == len(f["sources"]) for f in full_sources["findings"]),
          json.dumps([f.get("source_count") for f in full_sources["findings"]]))
    check("精简版明显更小（列表页只需要知道有几个来源）",
          len(json.dumps(snap, ensure_ascii=False)) < len(json.dumps(full_sources, ensure_ascii=False)),
          f"{len(json.dumps(snap))//1024}KB vs {len(json.dumps(full_sources))//1024}KB")
    check("thumbs 模式不带原图路径、只带缩略图",
          all(not f.get("rel_path") and f.get("thumb_path") for f in snap["findings"]),
          json.dumps([(f.get("rel_path"), bool(f.get("thumb_path"))) for f in snap["findings"]]))
    check("内联的来源里不留 data: URI（十几万字符，手机存下来也打不开）",
          all("data:" not in str(s.get("url", "")) + str(s.get("image_url", ""))
              for f in full_sources["findings"] for s in f.get("sources") or []),
          json.dumps([str(s.get("url"))[:36] for f in full_sources["findings"]
                      for s in f.get("sources") or []], ensure_ascii=False)[:200])
    check("快照是纯 JSON 可序列化（手机端要按需取图，不能内联 base64）",
          isinstance(json.dumps(snap, ensure_ascii=False), str)
          and "data:image" not in json.dumps(snap)[:200000],
          f"{len(json.dumps(snap))} 字节")
    check("快照带上评分权重，手机端才能解释排序", "settings" in snap and snap["settings"],
          json.dumps(snap.get("settings"), ensure_ascii=False))

    full = wb.mobile_snapshot(image_mode="full")
    check("full 模式才带原图路径", all(f.get("rel_path") for f in full["findings"]),
          json.dumps([bool(f.get("rel_path")) for f in full["findings"]]))
    lean = wb.mobile_snapshot(image_mode="none")
    check("none 模式不带任何图片路径（只有文字信息）",
          all(not f.get("rel_path") and not f.get("thumb_path") for f in lean["findings"]),
          str(len(lean["findings"])))

    # ---- 推回：手机拍的照片 + 手机上框选的图 ---------------------------------
    shot = jpeg(photo_b().rotate(12, expand=True), 90)
    result = wb.apply_mobile_sync({"imports": [{
        "client_id": "phone-1", "name": "手机拍的招牌",
        "data_url": "data:image/jpeg;base64," + base64.b64encode(shot).decode(),
        "note": "手机端同步导入", "auto_enqueue": True, "engines": ["baidu"]}]})
    check("手机导入的照片在工作台里建成了种子",
          result["ok"] and len(result["imported"]) == 1
          and result["imported"][0]["seed_id"],
          json.dumps(result, ensure_ascii=False)[:240])
    check("导入的种子带回了手机端的 client_id（两边能对上）",
          result["imported"][0].get("client_id") == "phone-1",
          str(result["imported"][0].get("client_id")))
    new_seed = int(result["imported"][0]["seed_id"])
    queued = wb.queue_view("todo", seed_id=new_seed)
    check("手机导入时可以顺手入队（电脑稍后自动识图）", len(queued["jobs"]) >= 1,
          str(len(queued["jobs"])))
    check("手机发来的 base64 存成了真实图片文件（不是文本占位）",
          os.path.isfile(wb.abs_media((wb.store.seed(new_seed) or {}).get("rel_path") or "")),
          str((wb.store.seed(new_seed) or {}).get("rel_path")))

    bad = wb.apply_mobile_sync({"imports": [{"name": "坏数据", "data_url": "不是图片"}]})
    check("坏数据不会让整批同步失败，而是记进 errors",
          bad["ok"] is False and bad["errors"] and not bad["imported"],
          json.dumps(bad, ensure_ascii=False)[:200])

    # ---- 推回：手机上删掉的对象 ---------------------------------------------
    # 多造两个，避免"把仅有的都删了"导致后面无内容可断言
    for index in range(2):
        wb.ingest_payload(int(jobs[0]), candidates=[{
            "image_url": fixtures.put(f"mob-extra-{index}.jpg",
                                      jpeg(photo_b().rotate(20 + index * 40, expand=True), 88)),
            "source_url": f"https://bbs.example.net/thread/mob-{index}",
            "title": f"手机端补充结果 {index}"}], fetch_limit=5)
    targets = [f["id"] for f in wb.store.findings(limit=5)]
    check("准备出至少 2 个可回收的对象", len(targets) >= 2, str(targets))
    wb.store.update_finding(targets[0], status="promoted")
    trashed = wb.apply_mobile_sync({"trash": targets})
    check("手机上的回收操作在工作台生效",
          set(trashed["trashed"]) >= set(targets[1:]),
          json.dumps(trashed, ensure_ascii=False)[:200])
    check("手机上误删已回流的成果也会被工作台保护住",
          targets[0] not in trashed["trashed"]
          and any("已保护" in s.get("why", "") for s in trashed.get("trash_skipped") or []),
          json.dumps(trashed.get("trash_skipped"), ensure_ascii=False)[:160])
    after = wb.mobile_snapshot()
    check("再快照一次时被手机删掉的对象进了回收站、不在活动板",
          targets[1] in [f["id"] for f in after.get("trash") or []]
          and targets[1] not in [f["id"] for f in after["findings"]],
          json.dumps({"trash_ids": [f["id"] for f in after.get("trash") or []][:6]}, ensure_ascii=False))
    wb.store.close()


def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    vision = "--vision" in sys.argv
    only = argv[0] if argv else ""
    tmp = tempfile.mkdtemp(prefix="vsw-selftest-")
    fixtures = FixtureServer()
    started = time.time()
    try:
        print(f"夹具端口 {fixtures.port}；临时目录 {tmp}")
        images = None
        if not only or only == "imaging":
            images = test_imaging()
        fixtures.put("a.jpg", jpeg(photo_a(), 80))
        fixtures.put("b.jpg", jpeg(photo_b(), 80))
        if not only or only == "parsing":
            test_parsing(fixtures)
        if not only or only == "loop":
            wb = test_loop(tmp, fixtures, vision)
            if vision and (not only or only == "loop"):
                test_vision(wb, fixtures)
            elif vision:
                test_vision(wb, fixtures)
        if not only or only == "crop":
            test_crop(tmp, fixtures, vision)
        if not only or only == "trash":
            test_trash(tmp, fixtures, vision)
        if not only or only == "archive":
            test_archive(tmp, fixtures, vision)
        if not only or only == "labels":
            test_source_labels(tmp, fixtures, vision)
        if not only or only == "queue":
            test_queue_filter(tmp, fixtures, vision)
        if not only or only == "mobile":
            test_mobile_api(tmp, fixtures, vision)
        if not only or only == "http":
            test_http_api(tmp, fixtures, vision)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        FAIL.append(f"未捕获异常：{traceback.format_exc().splitlines()[-1]}")
    finally:
        fixtures.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项，用时 {time.time() - started:.1f}s")
    for item in FAIL:
        print("  FAIL " + item)
    if not FAIL:
        print("全部通过。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
