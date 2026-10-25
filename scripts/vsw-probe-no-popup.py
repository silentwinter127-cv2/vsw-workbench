"""验证：服务不再往屏幕上闪黑窗口（r47 修的那个「经常跳出黑色弹窗」）。

背景（这是真机上暴露出来的）：

服务以前跑在 cmd/PowerShell 里，子进程（`ipconfig`）挂到同一个控制台，看不见窗口。
r46 换成看门狗用 **pythonw** 拉起之后，`ipconfig` 每次都被 Windows 新开一个控制台 ——
而 `_adapter_ips()` 又被 `lan_addresses()` → `_is_local()` 牵着，**每个请求都要问一次**，
于是「手机上每同步一次，电脑屏幕上就闪一个黑框」。

这个探针钉住三件事：

A. `_adapter_ips()` 起 `ipconfig` 时带了 `CREATE_NO_WINDOW`；
B. `lan_addresses()` 有缓存 —— 20 秒内重复问不会再起进程；
C. 活着的服务上连打 20 个请求，除第一次（填缓存）之外都不再变慢、也不会起进程。

用法：

    python scripts/vsw-probe-no-popup.py
    python scripts/vsw-probe-no-popup.py --origin http://127.0.0.1:8799

没有服务在跑时只跑 A/B/C 的前两项（C 会跳过并说明）。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

from _paths import REPO_ROOT, add_repo_to_path  # noqa: E402

add_repo_to_path()

from vsw import server as srv  # noqa: E402

PASSED: List[str] = []
FAILED: List[str] = []


def check(name: str, ok: Any, detail: Any = "") -> bool:
    ok = bool(ok)
    (PASSED if ok else FAILED).append(name)
    mark = "ok  " if ok else "FAIL"
    extra = "" if detail in ("", None) else f" :: {detail}"
    print(f"  {mark} {name}{extra}")
    return ok


class Recorder:
    """替身：记录 subprocess.run 的调用（不真的起进程）。"""

    def __init__(self, origin: "Recorder | None" = None) -> None:
        self.calls: List[Tuple[List[str], Dict[str, Any]]] = []
        self.origin = origin

    def __call__(self, argv, **kwargs):  # noqa: ANN001
        self.calls.append((list(argv) if isinstance(argv, (list, tuple)) else [str(argv)], dict(kwargs)))
        if self.origin is not None:
            return self.origin(argv, **kwargs)

        class _Done:
            stdout = ""
            returncode = 0
            stderr = ""

        return _Done()


def part_a_b() -> Dict[str, Any]:
    print("\n[A] 起子进程时带 CREATE_NO_WINDOW")
    real = subprocess.run
    rec = Recorder()
    out: Dict[str, Any] = {}
    try:
        subprocess.run = rec          # type: ignore[assignment]
        srv._LAN_CACHE["at"] = 0.0
        srv._LAN_CACHE["value"] = []
        first = srv.lan_addresses(force=True)
        calls_after_first = len(rec.calls)
        second = srv.lan_addresses()
        calls_after_second = len(rec.calls)
        out = {"first": first, "second": second,
               "calls1": calls_after_first, "calls2": calls_after_second,
               "argv": [c[0] for c in rec.calls], "kwargs": [c[1] for c in rec.calls]}
    finally:
        subprocess.run = real          # type: ignore[assignment]

    ipconfig = [c for c in out.get("kwargs", []) if c.get("creationflags")]
    check("读网卡时确实起过 ipconfig（说明这条路径还在用）",
          any(any("ipconfig" in str(a) for a in argv) for argv in out.get("argv", [])),
          out.get("argv"))
    check("起 ipconfig 时带了 CREATE_NO_WINDOW",
          bool(ipconfig) and all(int(c.get("creationflags") or 0) & 0x08000000 for c in ipconfig),
          ipconfig)
    check("_no_window_flags() 在 Windows 上返回 0x08000000",
          srv._no_window_flags().get("creationflags") == 0x08000000,
          srv._no_window_flags())

    print("\n[B] 结果缓存（20 秒内不再问系统）")
    check("第一次调用只起一次进程", out.get("calls1") == 1, out.get("calls1"))
    check("紧接着再问一次，没有再起进程", out.get("calls2") == out.get("calls1"), out.get("calls2"))
    check("两次结果一致", out.get("first") == out.get("second"),
          [out.get("first"), out.get("second")])

    print("\n[E] 缓存会过期（不是永久钉死的旧地址）")
    srv._LAN_CACHE["at"] = time.time() - (srv._LAN_TTL + 1)
    rec2 = Recorder()
    try:
        subprocess.run = rec2          # type: ignore[assignment]
        srv.lan_addresses()
    finally:
        subprocess.run = real          # type: ignore[assignment]
    check("过了 TTL 会重新读一次网卡", len(rec2.calls) >= 1, len(rec2.calls))
    return out


def part_d() -> None:
    """「手机连过来」这条路径上最容易踩的坑：每个请求都去跑一次 ipconfig。"""
    print("\n[D] 局域网请求只读一次网卡（不是每个请求都 ipconfig）")

    class Stub:
        # 只借真实的 _forwarded_ip / _is_local，其余打桩
        _forwarded_ip = srv.Handler._forwarded_ip
        _is_local = srv.Handler._is_local

        headers: Dict[str, str] = {}      # 服务端读的是 self.headers.get(...)，是个对象不是方法

        def __init__(self, ip: str) -> None:
            self.ip = ip

        def _client_ip(self) -> str:
            return self.ip

        def _is_loopback(self) -> bool:
            return False

    real = subprocess.run
    rec = Recorder()
    try:
        subprocess.run = rec          # type: ignore[assignment]
        srv._LAN_CACHE["at"] = 0.0
        srv._LAN_CACHE["value"] = []
        stub = Stub("192.168.1.99")
        results = [bool(srv.Handler._is_local(stub)) for _ in range(5)]
    finally:
        subprocess.run = real          # type: ignore[assignment]

    check("局域网来源不会被当成本机（.99 不是本机地址）", results == [False] * 5, results)
    check("连问 5 次只读了一次网卡", len(rec.calls) == 1, len(rec.calls))
    check("这一次也带了 CREATE_NO_WINDOW",
          all(int(c[1].get("creationflags") or 0) & 0x08000000 for c in rec.calls),
          [c[1] for c in rec.calls])

    real = subprocess.run
    rec2 = Recorder()
    try:
        subprocess.run = rec2          # type: ignore[assignment]
        srv._LAN_CACHE["at"] = 0.0
        srv._LAN_CACHE["value"] = []
        me = srv.lan_addresses(force=True)[0] if srv.lan_addresses(force=True) else ""
        stub2 = Stub(me)
        hit = bool(srv.Handler._is_local(stub2)) if me else False
    finally:
        subprocess.run = real          # type: ignore[assignment]
    check("本机网卡地址会被当成本机（自己用局域网地址打开也能建号）", hit, me)


def part_c(origin: str) -> None:
    print(f"\n[C] 活着的服务：连打 20 个请求不再「每个都跑 ipconfig」")
    url = origin.rstrip("/") + "/api/auth"
    times: List[float] = []
    codes: List[int] = []
    for _ in range(20):
        t0 = time.time()
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                resp.read()
                codes.append(resp.status)
        except urllib.error.HTTPError as exc:
            exc.read()
            codes.append(exc.code)
        except Exception as exc:  # noqa: BLE001
            codes.append(0)
            print("     请求出错:", exc)
        times.append((time.time() - t0) * 1000.0)

    warm = times[1:]
    med = statistics.median(warm) if warm else 0.0
    print(f"     首次 {times[0]:.1f} ms；之后中位数 {med:.1f} ms；最慢 {max(warm):.1f} ms")
    check("服务有应答（200 或 401 都算活着）", all(c in (200, 401) for c in codes), codes)
    check("除了第一次，每个请求都快（< 120ms，说明没在起进程）", bool(warm) and max(warm) < 120.0,
          f"max {max(warm):.1f} ms")
    check("中位数很快（< 40ms）", med < 40.0, f"{med:.1f} ms")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="确认服务不会往屏幕上闪黑窗口")
    ap.add_argument("--origin", default=os.environ.get("VSW_ORIGIN", "http://127.0.0.1:8799"))
    ap.add_argument("--skip-live", action="store_true", help="只跑离线部分（A/B/E）")
    args = ap.parse_args(argv)

    print(f"仓库：{REPO_ROOT}")
    part_a_b()
    part_d()
    if args.skip_live:
        print("\n[C] 跳过（--skip-live）")
    else:
        part_c(args.origin)

    total = len(PASSED) + len(FAILED)
    print(f"\n{len(PASSED)} 通过 / {len(FAILED)} 失败（共 {total} 项）")
    for name in FAILED:
        print("  失败:", name)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
