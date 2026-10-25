#!/usr/bin/env python3
"""视觉搜索工作台 —— 看门狗：开机自启 + 挂了就续跑。

它只做三件事：
  1. 把 `python -m vsw.server ...` 当子进程拉起来；
  2. 子进程退出（崩溃、被误关、端口被抢）就等一会儿再拉起，退避 1/2/4/8/15/30/60 秒；
  3. 子进程还活着但 `/api/health` 连续几次不通（卡死），就先杀掉再拉起。

单实例：同一台机器只有一个看门狗在管这个端口。第二个进程启动时会直接退出，
并把「已经在跑」写进日志 —— 免得开机时两条路径各起一个、互相抢 8799。

端口上已经有别人在服务（比如你手动开的那个）时，它不会去抢，而是**接管监控**：
先盯着那个进程，等它不健康了再自己拉起一个新的。

状态写在 `<root>/logs/watchdog-status.json`，日志写 `<root>/logs/watchdog.log`（超过 8MB 轮转）。

用法：
    python scripts/vsw-watchdog.py --root vsw\\data --port 8799
    python scripts/vsw-watchdog.py --root vsw\\data --port 8799 --gh owner/repo
    python scripts/vsw-watchdog.py --root vsw\\data --port 8799 --once   # 只检查一次，给探针用
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

LOG_MAX_BYTES = 8 * 1024 * 1024
HEALTH_PATH = "/api/health"
STILL_ACTIVE = 259


# --------------------------------------------------------------------- 小工具
def now() -> float:
    return time.time()


def stamp(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now() if ts is None else ts))


def pid_alive(pid: int) -> bool:
    """Windows 上 `os.kill(pid, 0)` 会真的把进程杀掉（等价 TerminateProcess），不能用。"""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def tail(text: str, limit: int = 400) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


_JOB_HANDLE = None


def job_attach(pid: int) -> bool:
    """把子进程放进一个「随本进程一起结束」的 Job Object。

    Windows 上 taskkill 看门狗是硬杀（TerminateProcess），Python 的 finally 不会跑，
    子进程就会变成孤儿占着端口。Job Object 由内核负责：看门狗一死，里面的进程一起完蛋。
    """
    global _JOB_HANDLE
    if os.name != "nt" or pid <= 0:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        if _JOB_HANDLE is None:
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return False
            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            class _Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]

            class _IoCounters(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class _Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", _Basic),
                            ("IoInfo", _IoCounters),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            info = _Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000
            if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                kernel32.CloseHandle(handle)
                return False
            _JOB_HANDLE = handle
        # AssignProcessToJobObject 要 PROCESS_SET_QUOTA(0x0100) | PROCESS_TERMINATE(0x0001)
        process = kernel32.OpenProcess(0x0100 | 0x0001, False, int(pid))
        if not process:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(_JOB_HANDLE, process))
        finally:
            kernel32.CloseHandle(process)
    except (AttributeError, OSError, ValueError):
        return False


class Logger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._rotate_if_needed()

    def _rotate_if_needed(self) -> None:
        try:
            if os.path.getsize(self.path) > LOG_MAX_BYTES:
                os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def write(self, message: str) -> None:
        line = f"[{stamp()}] {message}"
        # pythonw 下 sys.stdout 是 None；控制台又可能是 GBK。日志永远不该让看门狗崩。
        if sys.stdout is not None:
            try:
                print(line, flush=True)
            except (UnicodeEncodeError, OSError):
                try:
                    print(line.encode("gbk", "replace").decode("gbk", "replace"), flush=True)
                except Exception:  # noqa: BLE001 - 日志永远不该抛异常
                    pass
        self._rotate_if_needed()
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")


# --------------------------------------------------------------------- 看门狗
class Watchdog:
    def __init__(self, opts: argparse.Namespace):
        self.opts = opts
        self.root = os.path.abspath(opts.root)
        self.log_dir = os.path.abspath(opts.log_dir or os.path.join(self.root, "logs"))
        os.makedirs(self.log_dir, exist_ok=True)
        self.log = Logger(os.path.join(self.log_dir, "watchdog.log"))
        self.status_path = os.path.join(self.log_dir, "watchdog-status.json")
        self.lock_path = os.path.join(self.log_dir, "watchdog.lock")
        self.child: Optional[subprocess.Popen] = None
        self.child_started_at = 0.0
        self.restarts = 0
        self.last_exit: Dict[str, Any] = {}
        self.adopted = False
        self.health_fails = 0
        self.healthy_once = False
        self.stopping = False

    # -- 单实例 ---------------------------------------------------------
    def acquire_lock(self) -> bool:
        if os.path.exists(self.lock_path):
            try:
                with open(self.lock_path, "r", encoding="utf-8") as fh:
                    other = json.load(fh)
            except (OSError, ValueError):
                other = {}
            pid = int(other.get("pid") or 0)
            if pid and pid != os.getpid() and pid_alive(pid):
                self.log.write(f"已经有一个看门狗在跑了（PID {pid}），这个进程退出")
                return False
            self.log.write(f"清掉过期的锁（PID {pid or '?'} 已经不在了）")
        payload = {"pid": os.getpid(), "port": self.opts.port, "started_at": now()}
        with open(self.lock_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return True

    def release_lock(self) -> None:
        try:
            with open(self.lock_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if int(data.get("pid") or 0) == os.getpid():
                os.remove(self.lock_path)
        except (OSError, ValueError):
            pass

    # -- 健康检查 -------------------------------------------------------
    def health(self, timeout: float = 4.0) -> Dict[str, Any]:
        url = f"http://127.0.0.1:{self.opts.port}{HEALTH_PATH}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                body = resp.read()
            data = json.loads(body or b"{}")
            return {"ok": True, "data": data}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def serving_elsewhere(self) -> bool:
        """端口上已经有人在服务（手动开的、或者上一轮遗留的）。"""
        return bool(self.health(timeout=2.0).get("ok"))

    # -- 子进程 ---------------------------------------------------------
    def server_argv(self) -> List[str]:
        argv = [
            self.opts.python, "-u", "-m", "vsw.server",
            "--root", self.root,
            "--host", self.opts.host,
            "--port", str(self.opts.port),
            "--no-browser",
        ]
        if self.opts.gh:
            argv += ["--gh", self.opts.gh]
        return argv

    def start_child(self) -> bool:
        if self.serving_elsewhere():
            if not self.adopted:
                self.log.write(f"端口 {self.opts.port} 上已经有服务在跑 —— 先接管监控，不重复启动")
                self.adopted = True
            return True
        argv = self.server_argv()
        self.log.write("启动：" + " ".join(argv))
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        out = open(os.path.join(self.log_dir, "server.log"), "a", encoding="utf-8", newline="\n")
        try:
            self.child = subprocess.Popen(  # noqa: S603 - 参数是我们自己拼的
                argv,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as exc:
            self.log.write(f"启动失败：{exc}")
            self.child = None
            return False
        attached = job_attach(self.child.pid)
        if not attached:
            self.log.write("（提示）没能把服务挂到 Job Object 上；看门狗被硬杀时服务可能会留下来")
        self.child_started_at = now()
        self.healthy_once = False
        self.health_fails = 0
        self.adopted = False
        self.write_status(state="starting")
        return True

    def stop_child(self) -> None:
        child = self.child
        if child is None or child.poll() is not None:
            return
        try:
            child.terminate()
            child.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            try:
                child.kill()
            except OSError:
                pass

    def child_running(self) -> bool:
        return self.child is not None and self.child.poll() is None

    # -- 状态文件 -------------------------------------------------------
    def write_status(self, state: str) -> None:
        data = {
            "state": state,
            "watchdog_pid": os.getpid(),
            "child_pid": (self.child.pid if self.child_running() else 0),
            "adopted": bool(self.adopted),
            "restarts": self.restarts,
            "port": self.opts.port,
            "root": self.root,
            "child_uptime": round(now() - self.child_started_at, 1) if self.child_started_at else 0,
            "last_exit": self.last_exit,
            "updated_at": now(),
            "updated": stamp(),
        }
        tmp = self.status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.status_path)

    # -- 主循环 ---------------------------------------------------------
    def tick(self) -> None:
        if self.child_running():
            code = self.child.poll()
            data = self.health()
            if data["ok"]:
                self.health_fails = 0
                if not self.healthy_once:
                    self.healthy_once = True
                    self.log.write(f"服务已经能应答了（PID {self.child.pid}）")
                    self.write_status(state="running")
                return
            self.health_fails += 1
            grace = now() - self.child_started_at < self.opts.start_grace
            if grace:
                return
            if self.health_fails >= self.opts.health_fails:
                self.log.write(
                    f"连着 {self.health_fails} 次问不到 {HEALTH_PATH}，判定卡死，重启（{data.get('error')}）"
                )
                self.stop_child()
            else:
                return
        elif self.child is not None:
            code = self.child.returncode
            self.last_exit = {"code": code, "at": stamp(), "uptime": round(now() - self.child_started_at, 1)}
            self.log.write(f"服务退出了（code={code}，跑了 {self.last_exit['uptime']}s），准备续跑")
            self.child = None
            self.write_status(state="restarting")
        elif self.adopted:
            # 接管模式下没有子进程：只等它不健康
            if self.health()["ok"]:
                self.write_status(state="adopted")
                return
            self.log.write("接管的服务不健康了，自己拉起一个")
            self.adopted = False
            self.restarts += 1
            self.start_child()
            return

        if self.serving_elsewhere():
            self.adopted = True
            self.write_status(state="adopted")
            return

        # 退避：跑得久就清零，避免一次偶发崩溃之后永远等 60 秒
        if self.child_started_at and now() - self.child_started_at > 120:
            self.reboot_backoff()
        self.restarts += 1
        delay = self.backoff_delay()
        if delay:
            self.log.write(f"等 {delay:.0f}s 再拉起（第 {self.restarts} 次续跑）")
            self.sleep_while_stopping(delay)
        self.start_child()

    def reboot_backoff(self) -> None:
        self.restarts = 1

    def backoff_delay(self) -> float:
        steps = [1, 2, 4, 8, 15, 30, 60]
        idx = min(max(self.restarts - 1, 0), len(steps) - 1)
        return float(steps[idx]) * self.opts.backoff_scale

    def sleep_while_stopping(self, seconds: float) -> None:
        end = now() + seconds
        while now() < end and not self.stopping:
            time.sleep(min(0.2, max(0.0, end - now())))

    def run(self) -> int:
        if not self.acquire_lock():
            return 3
        self.log.write(
            f"看门狗起来了：root={self.root} port={self.opts.port} python={self.opts.python}"
            + (f" gh={self.opts.gh}" if self.opts.gh else "")
        )
        try:
            while not self.stopping:
                self.tick()
                self.sleep_while_stopping(self.opts.interval)
        except KeyboardInterrupt:
            self.log.write("收到 Ctrl+C，收工")
        finally:
            self.stopping = True
            self.stop_child()
            self.write_status(state="stopped")
            self.release_lock()
            self.log.write("看门狗已退出")
        return 0

    def once(self) -> int:
        """只做一次判断：端口通不通、状态文件在不在 —— 给探针与自检用。"""
        data = self.health()
        print(json.dumps({"health": data.get("ok"), "error": data.get("error")}, ensure_ascii=False))
        return 0 if data.get("ok") else 1


def _relax_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="视觉搜索工作台看门狗（开机自启 + 挂了续跑）")
    ap.add_argument("--root", default=os.path.join("vsw", "data"), help="工作区数据目录")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，手机能连）")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--gh", default=os.environ.get("VSW_GH_REPO", ""), help="GitHub owner/repo（更新兜底）")
    ap.add_argument("--python", default=sys.executable, help="用哪个解释器跑服务")
    ap.add_argument("--log-dir", default="", help="日志目录（默认 <root>/logs）")
    ap.add_argument("--interval", type=float, default=5.0, help="巡检间隔（秒）")
    ap.add_argument("--start-grace", type=float, default=90.0, help="启动宽限（SigLIP2 加载要十几秒）")
    ap.add_argument("--health-fails", type=int, default=3, help="连续几次不健康就重启")
    ap.add_argument("--backoff-scale", type=float, default=1.0, help="退避倍率（探针里调小）")
    ap.add_argument("--once", action="store_true", help="只检查一次健康状态就退出")
    return ap.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    _relax_console()
    opts = parse_args(argv)
    dog = Watchdog(opts)
    if opts.once:
        return dog.once()
    for sig in (signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda *_: setattr(dog, "stopping", True))
        except (OSError, ValueError):
            pass
    return dog.run()


if __name__ == "__main__":
    sys.exit(main())
