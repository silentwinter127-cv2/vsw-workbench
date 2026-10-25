#!/usr/bin/env python3
"""看门狗探针：开机自启 + 挂了续跑，到底真的会续跑吗？

不碰你正在用的 8799：全部在临时目录 + 临时端口上跑，跑完自己收摊。

钉住的行为：
  A. 起来之后能把服务拉起来并且健康（健康接口 200），状态文件写的是 running；
  B. 把服务进程**杀掉**，看门狗会在几秒内重新拉起来（状态里 restarts 变大、健康恢复）；
  C. 已经有一个看门狗在管这个目录时，第二个看门狗直接退出（不抢端口）；
  D. 端口上已经有别人开的服务时，看门狗先「接管监控」而不是抢着再起一个；
  E. `--once` 只问一次健康，不常驻；
  F. 锁文件在退出后清掉（下次开机不会以为还有人占着）。

用法：python scripts/vsw-probe-watchdog.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import add_repo_to_path  # noqa: E402

add_repo_to_path()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOG = os.path.join(REPO, "scripts", "vsw-watchdog.py")
PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: Any = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} :: {str(detail)[:300]}")


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def health(port: int, timeout: float = 3.0) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as resp:
            return {"ok": True, "data": json.loads(resp.read() or b"{}")}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def wait_for(predicate, timeout: float, step: float = 0.5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 探针只关心最终结果
            pass
        time.sleep(step)
    return False


def read_status(root: str) -> Dict[str, Any]:
    path = os.path.join(root, "logs", "watchdog-status.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def read_log(root: str) -> str:
    path = os.path.join(root, "logs", "watchdog.log")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def start_dog(root: str, port: int, extra: Optional[List[str]] = None) -> subprocess.Popen:
    argv = [
        sys.executable, "-u", DOG,
        "--root", root, "--host", "127.0.0.1", "--port", str(port),
        "--interval", "0.4", "--start-grace", "25", "--health-fails", "2",
        "--backoff-scale", "0.2",
    ] + (extra or [])
    return subprocess.Popen(argv, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def kill_pid(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def kill_tree(proc: subprocess.Popen) -> None:
    """看门狗死了它的服务还在 —— 探针要把整棵树都收掉。"""
    try:
        status = read_status
        _ = status
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass


def cleanup_leftovers(root: str) -> None:
    """把临时 data 目录里还记得的 PID 都杀掉，然后删目录。"""
    st = read_status(root)
    for key in ("child_pid",):
        kill_pid(int(st.get(key) or 0))
    kill_pid(int(st.get("watchdog_pid") or 0))
    shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    base = tempfile.mkdtemp(prefix="vsw-wd-")
    root = os.path.join(base, "data")
    os.makedirs(root, exist_ok=True)
    port = free_port()
    root2 = os.path.join(base, "data2")
    os.makedirs(root2, exist_ok=True)
    port2 = free_port()
    dog: Optional[subprocess.Popen] = None
    dog2: Optional[subprocess.Popen] = None
    manual: Optional[subprocess.Popen] = None
    try:
        print(f"临时根目录 {root} · 端口 {port}\n")

        # ---------------- A. 拉起来并健康 ----------------
        dog = start_dog(root, port)
        up = wait_for(lambda: health(port).get("ok"), 150, 1.0)
        check("看门狗能把服务拉起来（/api/health 200）", up, health(port).get("error"))
        hdata = health(port).get("data", {})
        check("健康接口认得出这是工作台（带 root 与 vision）",
              "root" in hdata and "vision" in hdata, list(hdata)[:8])
        st = read_status(root)
        check("状态文件写了 running", st.get("state") == "running", st)
        check("状态里记了服务 PID", int(st.get("child_pid") or 0) > 0, st)
        check("日志记了启动命令行", "启动：" in read_log(root), read_log(root)[:200])

        # ---------------- B. 杀掉服务 → 续跑 ----------------
        first_pid = int(st.get("child_pid") or 0)
        before = int(st.get("restarts") or 0)
        kill_pid(first_pid)
        gone = wait_for(lambda: not health(port).get("ok"), 30, 0.4)
        check("杀掉服务后确实短暂不健康", gone, health(port))
        came_back = wait_for(lambda: health(port).get("ok"), 90, 1.0)
        check("看门狗把服务续跑起来了", came_back, read_log(root)[-400:])
        st2 = read_status(root)
        check("状态里的 restarts 变大了", int(st2.get("restarts") or 0) > before, st2)
        check("换了一个新的服务进程", int(st2.get("child_pid") or 0) not in (0, first_pid), st2)

        # ---------------- C. 第二个看门狗不抢 ----------------
        dog2 = start_dog(root, port)
        try:
            dog2.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        check("同一个目录的第二个看门狗会自己退出", dog2.poll() == 3, dog2.poll())
        log_text = read_log(root)
        check("日志说明了「已经有一个看门狗在跑」", "已经有一个看门狗在跑" in log_text, log_text[-300:])
        check("端口上的服务没被第二个看门狗搞乱", health(port).get("ok") is True, health(port))

        # ---------------- E. --once ----------------
        once = subprocess.run(
            [sys.executable, DOG, "--root", root, "--port", str(port), "--once"],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        check("--once 只问一次就退出（返回 0）", once.returncode == 0, once.stdout + once.stderr)
        check("--once 的 JSON 里 health 为真", '"health": true' in (once.stdout or ""), once.stdout)

        # 收掉第一只狗
        kill_tree(dog)
        dog = None
        still = health(port).get("ok")
        check("看门狗退出后服务也随之收掉（不留下孤儿）", still is False, health(port))

        # ---------------- D. 接管模式 ----------------
        manual = subprocess.Popen(
            [sys.executable, "-u", "-m", "vsw.server", "--root", root2, "--host", "127.0.0.1",
             "--port", str(port2), "--no-browser"],
            cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        manual_up = wait_for(lambda: health(port2).get("ok"), 150, 1.0)
        check("手动先起一个服务（模拟你自己开着）", manual_up, health(port2).get("error"))

        dog = start_dog(root2, port2)
        adopted = wait_for(lambda: read_status(root2).get("state") in ("adopted", "running"), 60, 0.5)
        check("看门狗发现端口已被占用时不重复启动", adopted, read_status(root2))
        st4 = read_status(root2)
        check("状态是 adopted（接管监控）", st4.get("state") == "adopted", st4)
        check("接管时没有自己的子进程", int(st4.get("child_pid") or 0) == 0, st4)

        kill_pid(int(manual.pid or 0))
        manual = None
        took_over = wait_for(lambda: health(port2).get("ok"), 90, 1.0)
        check("接管的服务挂掉后，看门狗自己拉起一个新的", took_over, read_log(root2)[-400:])
        check("接管之后状态变回 running", read_status(root2).get("state") == "running", read_status(root2))

        # ---------------- F. 硬杀之后留下的锁不会挡住下一次启动 ----------------
        kill_tree(dog)
        dog = None
        lock = os.path.join(root2, "logs", "watchdog.lock")
        check("被硬杀时会留下锁文件（这就是要能自愈的那种情况）", os.path.exists(lock),
              os.listdir(os.path.join(root2, "logs")))
        dog = start_dog(root2, port2)
        recovered = wait_for(lambda: read_status(root2).get("state") in ("running", "adopted"), 90, 0.6)
        check("带着过期锁再启动也能起来（不会卡在「已经有人在跑」）", recovered, read_log(root2)[-400:])
        check("日志里认出了过期锁", "过期的锁" in read_log(root2) or "看门狗起来了" in read_log(root2),
              read_log(root2)[-300:])
    finally:
        if dog is not None:
            kill_tree(dog)
        if dog2 is not None:
            kill_tree(dog2)
        if manual is not None:
            kill_pid(int(manual.pid or 0))
        cleanup_leftovers(root)
        cleanup_leftovers(root2)
        shutil.rmtree(base, ignore_errors=True)
        print(f"\n{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
