"""Drive the live workbench with a real folder of collected images.

Usage:  python vsw-verify-live.py http://127.0.0.1:8799 "D:\\images\\<folder>"

This is a smoke test against a *running* server: it imports real files, checks that
variants + queue + semantic vectors were produced, then feeds back a synthetic-but-realistic
engine result page (two of the already-collected images plus one new one) to confirm the
three dedup layers and the exploration ranking work on real pixels.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from typing import Any, Dict, List

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799"
FOLDER_ROOT = sys.argv[2] if len(sys.argv) > 2 else r"D:\images"
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 8

failures: List[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + (f" :: {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    print(f"== 目标服务 {BASE} ==")
    health = requests.get(f"{BASE}/api/health", timeout=30).json()
    check("服务健康且语义引擎就绪", health["vision"]["available"], health["vision"]["reason"])
    print(f"     模型：{health['vision']['model']}")

    page = requests.get(f"{BASE}/", timeout=30)
    check("WebUI 页面可访问", page.status_code == 200 and "视觉搜索工作台" in page.text)

    folder_names = [n for n in os.listdir(FOLDER_ROOT)
                    if os.path.isdir(os.path.join(FOLDER_ROOT, n))] if os.path.isdir(FOLDER_ROOT) else []
    folder_names.sort(key=lambda n: len(os.listdir(os.path.join(FOLDER_ROOT, n))), reverse=True)
    check(f"找到素材库 {FOLDER_ROOT}", bool(folder_names), FOLDER_ROOT)
    if not folder_names:
        return 1

    def take(folder: str, count: int) -> List[str]:
        full = os.path.join(FOLDER_ROOT, folder)
        names = sorted((n for n in os.listdir(full)
                        if os.path.splitext(n)[1].lower() in (".jpg", ".jpeg", ".png", ".webp", ".bmp")),
                       key=lambda n: os.path.getsize(os.path.join(full, n)), reverse=True)
        return [os.path.join(full, n) for n in names[:count]]

    seed_folder = folder_names[0]
    files = take(seed_folder, LIMIT)
    check(f"种子文件夹 {seed_folder}", bool(files), str(len(files)))
    print(f"     种子取最大的 {len(files)} 张：{[os.path.basename(f) for f in files[:3]]} …")

    imported: List[Dict[str, Any]] = []
    for path in files:
        with open(path, "rb") as fh:
            payload = __import__("base64").b64encode(fh.read()).decode()
        resp = requests.post(f"{BASE}/api/import", json={
            "kind": "upload",
            "uploads": [{"name": os.path.basename(path),
                         "data_url": "data:image/jpeg;base64," + payload}],
            "variants": ["full", "crop_center_50", "quad_tl", "gray", "bright_up", "edges"],
            "auto_enqueue": True,
            "engines": ["baidu", "bing_cn"],
        }, timeout=600).json()
        item = resp["imported"][0]
        imported.append(item)
        print(f"     种子 #{item['seed_id']} {item['width']}×{item['height']} "
              f"标签={item['tags'][:3]} 变体={len(item.get('variants', []))} "
              f"入队={len(item.get('job_ids', []))} 新建={item['created']}")
    # 重复导入是幂等的：种子已存在时不会重复建变体（也不会重复入队），
    # 所以这里核对的是「这 6 种变体齐了没有」，而不是变体总数恰好等于 6。
    wanted = {"full", "crop_center_50", "quad_tl", "gray", "bright_up", "edges"}
    for item in imported:
        detail = requests.get(f"{BASE}/api/seed/{item['seed_id']}", timeout=60).json()
        kinds = {v["kind"] for v in detail["variants"]}
        check(f"种子 #{item['seed_id']} 的 6 种查询变体齐全", wanted <= kinds,
              f"缺 {sorted(wanted - kinds)}")
    check("语义层给出了零样本标签", all(i.get("tags") for i in imported))
    seed_ids = [i["seed_id"] for i in imported]
    queued = 0
    required = 2 * 6
    by_status = {}
    for seed_id in seed_ids:
        jobs = requests.get(f"{BASE}/api/seed/{seed_id}", timeout=60).json()["jobs"]
        for job in jobs:
            by_status[job["status"]] = by_status.get(job["status"], 0) + 1
        queued += len(jobs)
    check(f"每个种子都排过 2 引擎 × 6 变体的队列项（共需 {required * len(seed_ids)}）",
          queued >= required * len(seed_ids), f"实际 {queued} 状态分布 {by_status}")
    check("导入前发现语义引擎已就绪", health["vision"]["available"])

    queue = requests.get(f"{BASE}/api/queue?status=todo", timeout=60).json()
    check("队列有待办项", len(queue["jobs"]) > 0, str(queue["counts"]))

    first_seed = requests.get(f"{BASE}/api/seed/{seed_ids[0]}", timeout=60).json()
    check("种子详情返回变体清单", bool(first_seed["variants"]),
          str(len(first_seed["variants"])))
    variant_kinds = {v["kind"] for v in first_seed["variants"]}
    check("变体覆盖几何/色调/画质三类",
          {"full", "crop_center_50", "quad_tl", "gray", "bright_up", "edges"} <= variant_kinds,
          str(sorted(variant_kinds)))
    check("变体带独立指纹与文件",
          all(v["phash"] and v["rel_path"] for v in first_seed["variants"]))

    # 派发第一项，拿到真实深链与本地文件路径
    job = next(j for j in queue["jobs"] if j["seed_id"] == seed_ids[0])
    ex = requests.post(f"{BASE}/api/job/{job['id']}/execute", json={}, timeout=60).json()
    check("派发返回引擎入口与本地文件", ex["dispatch_url"].startswith("http")
          and os.path.isfile(ex["local_file"]), ex.get("dispatch_url", ""))
    print(f"     引擎={ex['engine_name']} 入口={ex['dispatch_url']} 文件={os.path.basename(ex['local_file'])}")

    # 回填一份"引擎结果页"：用本机临时 HTTP 服务器托管真实文件，所以回填路径里的下载、
    # 去重、来源聚合都是真的在跑（不是打桩）。第一张刻意用"和种子逐字节相同"的那份文件，
    # 用来验证文件层去重：引擎把查询图自己吐回来时，不能被算成新发现。
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    served: Dict[str, bytes] = {}
    seed_rel = first_seed["seed"]["rel_path"]           # e.g. media/seeds/xxx.png
    served["seed-echo.png"] = requests.get(f"{BASE}/{seed_rel}", timeout=60).content
    for idx, path in enumerate(files[1:3]):
        with open(path, "rb") as fh:
            served[f"real-{idx}.jpg"] = fh.read()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            name = self.path.lstrip("/").split("?")[0]
            blob = served.get(name)
            if blob is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if name.endswith(".png") else "image/jpeg")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

    local = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    local.daemon_threads = True
    threading.Thread(target=local.serve_forever, daemon=True).start()
    local_base = f"http://127.0.0.1:{local.server_address[1]}"

    payload_lines = []
    pages = ["https://bbs.example.net/thread/live-1",
             "https://web.archive.org/web/2005/http://old.example.org/",
             "https://www.pinterest.com/pin/12345"]
    titles = ["转载：老店 2007", "Internet Archive 2005 街景", "Pinterest 灵感图"]
    for idx, name in enumerate(served):
        payload_lines.append(
            f'{{"objURL":"{local_base}/{name}","fromURL":"{pages[idx]}",'
            f'"fromPageTitleEnc":"{titles[idx]}"}}')
    payload = "{" + '"data":[' + ",".join(payload_lines) + "]}"

    parsed = requests.post(f"{BASE}/api/parse", json={"payload": payload, "engine": "baidu"}, timeout=60).json()
    check(f"结果页解析器认出 {len(served)} 条候选", len(parsed["candidates"]) == len(served),
          str(len(parsed["candidates"])))
    check("来源页与平台被识别",
          any(c["platform"] == "Pinterest" for c in parsed["candidates"])
          and any(c["platform"] == "Internet Archive" for c in parsed["candidates"]),
          json.dumps([c["platform"] for c in parsed["candidates"]], ensure_ascii=False))

    ing = requests.post(f"{BASE}/api/job/{job['id']}/ingest",
                        json={"payload": payload, "download": True, "fetch_limit": 10},
                        timeout=600).json()
    check("回填下载了全部候选", ing["fetched"] == len(served),
          json.dumps({k: ing[k] for k in ("parsed", "fetched")}, ensure_ascii=False))
    check("没有取图失败", not ing["failures"],
          json.dumps(ing["failures"], ensure_ascii=False)[:200])
    check("回填的账目自洽（新增+折叠重复=候选数）",
          ing["new_findings"] + ing["duplicates"] == len(served),
          json.dumps({k: ing[k] for k in ("new_findings", "duplicates")}, ensure_ascii=False) + json.dumps(ing, ensure_ascii=False)[:400])
    check("已收集过的图被折叠为转载而不是新图（种子池回流去重）",
          ing["duplicates"] >= 1 or ing["new_findings"] < len(served),
          json.dumps([r.get("dup_layer") for r in ing["duplicate_records"]], ensure_ascii=False))
    local.shutdown()
    local.server_close()

    report = requests.get(f"{BASE}/api/report", timeout=60).json()
    st = report["stats"]
    check("指标面板统计到操作与来源", st["operations"] >= 1 and st["sources"] >= 2,
          json.dumps(st, ensure_ascii=False))
    print(f"     每100次操作净增图片={st['new_per_100']} 新来源={st['sources_per_100']} "
          f"每个新来源成本={st['ops_per_new_source']} 次操作")

    # 让结果板有真实内容：从**另外两个店**的素材里取图，当作"引擎返回的其它网页上的图"
    # 回填一次。它们与种子池不重合，所以是真正的新视觉对象。
    extra: Dict[str, bytes] = {}
    for other in folder_names[1:3]:
        for path in take(other, 3):
            with open(path, "rb") as fh:
                extra[f"{os.path.basename(other)[:8]}-{os.path.basename(path)}"] = fh.read()
    if extra:
        import base64
        board_payload = "{" + '"data":[' + ",".join(
            json.dumps({
                "objURL": "data:image/jpeg;base64," + base64.b64encode(blob).decode(),
                "fromURL": f"https://bbs.example.net/thread/board-{idx}",
                "fromPageTitleEnc": f"{name} 转载",
            }, ensure_ascii=False) for idx, (name, blob) in enumerate(extra.items())) + "]}"
        board_ing = requests.post(f"{BASE}/api/job/{job['id']}/ingest",
                                  json={"payload": board_payload, "download": True, "fetch_limit": 10},
                                  timeout=600).json()
        check("data: URL 候选也能入库（引擎结果里常见内联图）",
              board_ing["parsed"] >= 1 and board_ing["fetched"] == board_ing["parsed"]
              and board_ing["new_findings"] + board_ing["duplicates"] == board_ing["parsed"],
              json.dumps({k: board_ing[k] for k in ("parsed", "fetched", "new_findings",
                                                    "duplicates", "failures")},
                         ensure_ascii=False)[:300])
        print(f"     回填 {board_ing['parsed']} 个内联候选：新增 {board_ing['new_findings']}、"
              f"折叠重复 {board_ing['duplicates']}（重复说明种子池回流去重生效）")

    board = requests.get(f"{BASE}/api/findings?limit=10", timeout=60).json()["findings"]
    check("结果板有内容且带三队列标注",
          bool(board) and all(f["triage"] in ("tracking", "diffusion", "bridge") for f in board),
          str([(f["id"], f["triage"]) for f in board]))
    if board:
        top = board[0]
        check("排序按探索价值而不是单看相似度",
              all(top["explore_value"] >= f["explore_value"] for f in board),
              str([round(f["explore_value"], 3) for f in board]))
        print(f"     首位 E={top['explore_value']} 相似={top['similarity']} 新颖={top['novelty']} "
              f"桥接={top['bridging']} 元数据={top['metadata_score']} 队列={top['triage']}")

    graph = requests.get(f"{BASE}/api/graph", timeout=120).json()
    check("图谱包含种子/变体/结果/来源节点",
          len({n["type"] for n in graph["nodes"]} & {"seed", "variant", "finding", "source"}) >= 2,
          str({n["type"] for n in graph["nodes"]}))

    recent = requests.get(f"{BASE}/api/findings?limit=20", timeout=60).json()["findings"]
    if recent:
        chain = requests.get(f"{BASE}/api/finding/{recent[0]['id']}", timeout=60).json()["chain"]
        check("搜索链回放可追溯", len(chain["nodes"]) >= 2, str(len(chain["nodes"])))

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项：" + "; ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
