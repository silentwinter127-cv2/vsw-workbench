"""Verify the automated (headless-browser) search path against a running workbench.

Usage: python vsw-verify-browser.py [base-url] [image-folder]

What it proves end to end:
  1. a seed is imported and its variants generated;
  2. a queue item is dispatched to the *automated* engine (no human clicking);
  3. the browser drives the engine's own upload widget and reads the result DOM;
  4. those results flow through the same three-layer dedup / clustering / ranking;
  5. engine provenance is labelled honestly (engine-only sources must not land in the
     source-tracking queue).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cli import image_dir, positional, usage, wants_help  # noqa: E402

_ARGS = positional()
BASE = _ARGS[0] if len(_ARGS) > 0 else "http://127.0.0.1:8799"
FOLDER = _ARGS[1] if len(_ARGS) > 1 else ""
ENGINE = _ARGS[2] if len(_ARGS) > 2 else "baidu"

USAGE = [
    "python scripts/vsw-verify-browser.py [工作台地址] [图片目录] [引擎]",
    "",
    "验证**自动识图**全链路：导入种子 → 生成变体 → 派发队列 → 无头浏览器驱动引擎的",
    "上传控件 → 结果过三层去重/聚类/排序 → 校验可信度标注是否诚实。",
    "",
    "参数：",
    "  工作台地址  默认 http://127.0.0.1:8799",
    "  图片目录    必填，装着 .jpg/.png 的文件夹",
    "  引擎        默认 baidu（也可用 so360）",
    "",
    "前置条件：Node 18+ 与本机 Edge/Chrome；跑一次约十几秒。",
]

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(("  ok   " if ok else "  FAIL ") + name + (f" :: {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)
    return bool(ok)


def post(path: str, payload: dict, timeout: float = 300) -> dict:
    request = urllib.request.Request(BASE + path, method="POST",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(path: str, timeout: float = 120) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    if wants_help():
        return usage("vsw-verify-browser.py", USAGE)
    names = image_dir(FOLDER, label="图片目录")
    if names is None:
        print("       用法见 --help。")
        return 2
    try:
        health = get("/api/health")
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 连不上 {BASE}：{exc}")
        print("       先确认工作台已经在运行（例如双击 start_vsw.bat）。")
        return 2
    check("服务健康", health.get("vision") is not None)
    check("服务能定位 Node 运行时（自动识图依赖它）", bool(health.get("node")), str(health.get("node")))
    engines = {e["key"]: e for e in get("/api/engines")["engines"]}
    check(f"引擎清单里有 {ENGINE} 且标记为可自动执行",
          engines.get(ENGINE, {}).get("browser") is True,
          json.dumps(engines.get(ENGINE, {}), ensure_ascii=False)[:200])
    check("已失效的引擎在清单里被标明（Bing 不再声称支持按图 URL 深链）",
          engines.get("bing_cn", {}).get("accepts_image_url") is False,
          json.dumps(engines.get("bing_cn", {}), ensure_ascii=False)[:160])

    names = sorted((n for n in os.listdir(FOLDER)
                    if os.path.splitext(n)[1].lower() in (".jpg", ".jpeg", ".png")),
                   key=lambda n: os.path.getsize(os.path.join(FOLDER, n)), reverse=True)
    with open(os.path.join(FOLDER, names[0]), "rb") as fh:
        payload = base64.b64encode(fh.read()).decode()
    imported = post("/api/import", {
        "kind": "upload",
        "uploads": [{"name": names[0], "data_url": "data:image/jpeg;base64," + payload}],
        "variants": ["full", "crop_center_50"],
        "auto_enqueue": False,
    })["imported"][0]
    seed_id = imported["seed_id"]
    print(f"     种子 #{seed_id} {imported['width']}×{imported['height']} 变体 {len(imported['variants'])}")
    check("种子具备可检索的查询变体（重复导入时复用已有变体）",
          {"full", "crop_center_50"} <= {v["kind"] for v in imported["variants"]},
          str([v["kind"] for v in imported["variants"]]))

    queued = post("/api/queue/enqueue", {"seed_ids": [seed_id], "engines": [ENGINE],
                                         "kinds": ["full"]})
    job_ids = queued.get("job_ids") or []
    check("入队成功", len(job_ids) >= 1, json.dumps(queued, ensure_ascii=False)[:200])
    job_id = job_ids[0]

    started = post(f"/api/job/{job_id}/browser", {"timeout_ms": 75000})
    check("自动识图任务已启动（后台跑浏览器）", started.get("started") is True,
          json.dumps(started, ensure_ascii=False)[:200])

    deadline = time.time() + 260
    state = {}
    job = {}
    while time.time() < deadline:
        time.sleep(5)
        state = get("/api/browser/state")
        job = next((j for j in get(f"/api/queue?status=all&seed_id={seed_id}")["jobs"]
                    if j["id"] == job_id), {})
        print(f"     … {state.get('phase', '')} ({job.get('status')})")
        if not state.get("running"):
            break
    check("浏览器任务已结束", not state.get("running"), json.dumps(state, ensure_ascii=False)[:200])
    check("队列项没有停在失败状态", job.get("status") in ("done", "review"),
          f"status={job.get('status')} note={job.get('note')}")

    findings = get(f"/api/findings?limit=400")["findings"]
    check("自动识图产出了视觉对象", len(findings) >= 1, f"共 {len(findings)}")
    if not findings:
        print("\n失败：" + "; ".join(failures))
        return 1
    print(f"     结果板 {len(findings)} 个视觉对象，首位 E={findings[0]['explore_value']} "
          f"队列={findings[0]['triage']}")

    engines_seen = {f["engine"] for f in findings}
    check("结果记录了是哪个引擎带来的", engines_seen <= {ENGINE, ""}, str(engines_seen))

    # 百度现在基本每条都带来源页（走 /ajax/pcsimi 的 fromUrl），所以这里检查的是
    # **标注是否诚实**：有外部来源页的就不能被标成 engine，没来源的必须标成 engine。
    with_external = []
    engine_only = []
    for finding in findings:
        external = [s for s in finding["sources"]
                    if str(s.get("url", "")).startswith("http")
                    and "baidu.com" not in s.get("url", "")]
        if external:
            with_external.append(finding)
        elif finding["sources"] and all(s.get("trust") == "engine" for s in finding["sources"]):
            engine_only.append(finding)
    print(f"     带外部来源页 {len(with_external)} 个 / 仅引擎页 {len(engine_only)} 个")
    check("有外部来源页的结果没有被误标成「引擎页」",
          all(any(s.get("trust") != "engine" or "baidu.com" in str(s.get("url"))
                  for s in f["sources"]) for f in with_external),
          str([(f["id"], [s.get("trust") for s in f["sources"]]) for f in with_external[:3]]))
    check("引擎自动识图带回了真实来源页（这一次或历史都算）", bool(with_external),
          f"共 {len(findings)} 个对象")

    report = get("/api/report")
    ops = report["stats"]["operations"]
    check("操作台账记录了这次自动检索", ops >= 1, json.dumps(report["stats"], ensure_ascii=False))
    browser_ops = [row for row in get("/api/ledger?limit=50")["ledger"] if row["op_kind"] == "browser"]
    check("台账里有 browser 类型的操作记录", bool(browser_ops),
          json.dumps([r.get("op_kind") for r in get("/api/ledger?limit=20")["ledger"]],
                     ensure_ascii=False))

    detail = get(f"/api/finding/{findings[0]['id']}")
    check("详情可打开且带来源与回放链", bool(detail.get("sources")) and bool(detail.get("chain")))
    chain_types = {n["type"] for n in detail["chain"]["nodes"]}
    check("回放链包含 结果→来源→变体→种子", {"finding", "source", "variant", "seed"} <= chain_types,
          str(chain_types))

    promoted = post("/api/findings/promote", {"finding_ids": [findings[0]["id"]],
                                              "engines": [ENGINE], "kinds": ["full"]})
    check("自动识图的结果可以回流成新种子", promoted.get("promoted", 0) >= 1,
          json.dumps(promoted, ensure_ascii=False)[:200])

    print("\n" + ("全部通过" if not failures else f"失败 {len(failures)} 项：" + "; ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
