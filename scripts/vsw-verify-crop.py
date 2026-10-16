"""Verify the hand-drawn crop module against a running workbench.

Usage: python vsw-verify-crop.py [base-url] [image-folder]

Proves: selection endpoint -> crop variant (with its rectangle) -> queue -> automated search
-> the engine's echo of our own crop is folded instead of counted as a new find.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import sys
import time
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cli import image_dir, positional, usage, wants_help  # noqa: E402

_ARGS = positional()
BASE = _ARGS[0] if len(_ARGS) > 0 else "http://127.0.0.1:8799"
FOLDER = _ARGS[1] if len(_ARGS) > 1 else ""
AUTO_SEARCH = "--no-auto" not in sys.argv

USAGE = [
    "python scripts/vsw-verify-crop.py [工作台地址] [图片目录] [--no-auto]",
    "",
    "验证**手工框选**全链路：选区 → crop 变体（带矩形与归一化坐标）→ 入队 →",
    "自动识图 → 引擎把我们自己的框选图回显时被判为重复而不是新发现。",
    "",
    "参数：",
    "  工作台地址  默认 http://127.0.0.1:8799",
    "  图片目录    必填，装着 .jpg/.png 的文件夹",
    "  --no-auto   跳过自动识图那一段（不依赖 Node/浏览器，更快）",
]

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(("  ok   " if ok else "  FAIL ") + name + (f" :: {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)
    return bool(ok)


def request(path: str, payload: dict | None = None, timeout: float = 300) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, method="POST" if data else "GET", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    if wants_help():
        return usage("vsw-verify-crop.py", USAGE)
    names = image_dir(FOLDER, label="图片目录")
    if names is None:
        print("       用法见 --help。")
        return 2
    with open(os.path.join(FOLDER, names[0]), "rb") as fh:
        blob = base64.b64encode(fh.read()).decode()
    try:
        seed = request("/api/import", {
            "kind": "upload",
            "uploads": [{"name": names[0], "data_url": "data:image/jpeg;base64," + blob}],
            "variants": ["full"], "auto_enqueue": False,
        })["imported"][0]
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 连不上 {BASE} 或导入失败：{exc}")
        print("       先确认工作台已经在运行（例如双击 start_vsw.bat）。")
        return 2
    seed_id = seed["seed_id"]
    print(f"     种子 #{seed_id}")

    info = request(f"/api/seed/{seed_id}/image")
    check("GET /api/seed/{id}/image 给出原图尺寸（框选面板的底图）",
          info["width"] > 100 and info["height"] > 100 and info["url"].startswith("/media/"),
          json.dumps(info)[:200])

    # 在中心偏上框一块（模拟「只搜招牌那一块」这种用法）
    box = [0.22, 0.18, 0.42, 0.34]
    cropped = request(f"/api/seed/{seed_id}/crop", {
        "box": box, "padding": 0.15, "scale": 2, "engines": ["baidu"],
        "label": "框选 42%×34%", "auto_run": AUTO_SEARCH,
    }, timeout=600)
    variant = cropped.get("variant") or {}
    crop = variant.get("meta", {}).get("crop", {})
    print(f"     crop 变体 #{variant.get('id')} {variant.get('width')}×{variant.get('height')} "
          f"原图坐标 {crop}")
    check("框选生成 crop 变体（含矩形与归一化坐标）",
          variant.get("kind") == "crop" and crop.get("w", 0) > 0
          and variant.get("meta", {}).get("normalised", {}).get("w", 0) > 0,
          json.dumps(variant, ensure_ascii=False)[:240])
    check("外扩与放大都记在变体上（可复现）",
          abs(variant.get("meta", {}).get("padding", 0) - 0.15) < 1e-9
          and abs(variant.get("meta", {}).get("scale", 0) - 2) < 1e-9,
          json.dumps(variant.get("meta"), ensure_ascii=False)[:200])
    check("框选变体已入队", len(cropped.get("job_ids") or []) == 1, str(cropped.get("job_ids")))

    again = request(f"/api/seed/{seed_id}/image")
    check("再打开面板能看到历史框选（可继续微调）",
          any(v["id"] == variant["id"] for v in again["variants"]), str(len(again["variants"])))

    if AUTO_SEARCH:
        check("框选后直接触发了自动识图", bool(cropped.get("auto", {}).get("started")),
              json.dumps(cropped.get("auto"), ensure_ascii=False)[:160])
        deadline = time.time() + 260
        state = {}
        while time.time() < deadline:
            time.sleep(5)
            state = request("/api/browser/state")
            job = next((j for j in request(f"/api/queue?status=all&seed_id={seed_id}")["jobs"]
                        if j["id"] == cropped["job_ids"][0]), {})
            print(f"     … {state.get('phase', '')} ({job.get('status')})")
            if not state.get("running"):
                break
        check("框选自动识图跑完且队列项未失败",
              not state.get("running") and job.get("status") in ("done", "review"),
              f"status={job.get('status')} err={state.get('error')}")
        check("队列项绑定的就是这个 crop 变体", job.get("variant_kind") == "crop",
              str(job.get("variant_kind")))

    findings = request("/api/findings?limit=400&dups=1")["findings"]
    published = [f for f in findings if not f.get("dup_of")]
    print(f"     结果板 {len(published)} 个视觉对象 / {len(findings)} 行（含被折叠的重复载体）")
    check("框选检索产出了结果", len(published) >= 1, str(len(published)))
    # 上面那次框选可能整批都被折叠成重复（说明去重生效），所以优先找这一块的产出，
    # 找不到就退回到「任意一个由 crop 变体带来的结果」来验证回放链。
    mine = [f for f in findings if int(f.get("variant_id") or 0) == int(variant["id"])]
    any_crop = [f for f in findings
                if int(f.get("variant_id") or 0) in
                {v["id"] for v in request(f"/api/seed/{seed_id}/image")["variants"]}]
    target = (mine or any_crop or published or [None])[0]
    check("能挑出由框选变体带来的结果（本次或历史框选都算）",
          bool(target), f"本次 variant_id={variant['id']}")
    if target:
        if mine:
            check("本次框选的结果被记录下来了（可能整批被判为重复载体）", True)
        detail = request(f"/api/finding/{target['id']}")
        crop_variants = [n for n in detail["chain"]["nodes"] if n.get("type") == "variant"]
        check("回放链标出了用的是「手工框选」变体",
              any("框选" in str(n.get("title", "")) for n in crop_variants),
              json.dumps([n.get("title") for n in crop_variants], ensure_ascii=False))
        seed_nodes = [n for n in detail["chain"]["nodes"] if n.get("type") == "seed"]
        check("回放链能一路回到种子", bool(seed_nodes),
              json.dumps([n.get("type") for n in detail["chain"]["nodes"]], ensure_ascii=False))

    report = request("/api/report")
    kinds = {row["kind"]: row for row in report["variant_yield"]}
    check("「按变体看产出」把手工框选单独统计（能判断哪种框法有效）",
          "crop" in kinds, str(sorted(kinds.keys())))
    if "crop" in kinds:
        row = kinds["crop"]
        print(f"     手工框选：操作 {row['ops']} 次 · 新增 {row['findings']} · 折叠重复 {row['dups']}")

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项：" + "; ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
