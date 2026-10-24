"""把工作台开到公网：借 Tailscale Funnel，不碰路由器、不需要公网 IP。

为什么是这个方案：
  * 家里宽带大多没有公网 IPv4（运营商 NAT），端口转发根本转发不出去；
  * Funnel 由 Tailscale 的边缘节点终止 TLS 再回源到 127.0.0.1:端口，
    所以路由器不用动，证书也不用自己签；
  * 唯一的代价是地址长这样：``https://<机器名>.<tailnet>.ts.net/``。

安全上有一件**必须知道**的事：Funnel 回源时对端永远是 127.0.0.1。也就是说服务器
不能再拿"看起来像本机"当本机用，否则整个公网都能抢先建主人账号 ——
真正来源在 ``X-Forwarded-For`` 里，判定见 ``vsw/server.py`` 的 ``_is_local``。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

# 查一次状态要起个进程，别每次刷新页面都去问；20 秒够新了。
_TTL = 20.0
_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}

# 允许用环境变量指定，其次是 PATH，最后是各平台的默认安装位置。
_CANDIDATES = (
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
    "/usr/bin/tailscale",
    "/usr/local/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)

_URL_RE = re.compile(r"(https://[A-Za-z0-9._\-]+(?:\.ts\.net)?(?::\d+)?(?:/\S*)?)")
_TARGET_RE = re.compile(r"proxy\s+(https?://\S+)")


def exe() -> str:
    """tailscale 可执行文件在哪；没装就返回空串（功能整体优雅降级）。"""
    env = (os.environ.get("VSW_TAILSCALE") or "").strip()
    if env and os.path.exists(env):
        return env
    found = shutil.which("tailscale") or shutil.which("tailscale.exe")
    if found:
        return found
    for cand in _CANDIDATES:
        if cand and os.path.exists(cand):
            return cand
    return ""


def available() -> bool:
    return bool(exe())


def _run(args: List[str], timeout: float = 25.0) -> Dict[str, Any]:
    path = exe()
    if not path:
        return {"ok": False, "out": "", "err": "这台机器没装 Tailscale"}
    try:
        proc = subprocess.run([path] + args, capture_output=True, text=True,
                              timeout=timeout, **({"creationflags": 0x08000000} if os.name == "nt" else {}))
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "", "err": "tailscale 命令超时（是不是没登录？）"}
    except Exception as exc:  # noqa: BLE001 - 任何失败都只是"这个功能用不了"
        return {"ok": False, "out": "", "err": f"{type(exc).__name__}: {exc}"}
    return {"ok": proc.returncode == 0, "out": proc.stdout or "", "err": (proc.stderr or "").strip()}


def parse_status(text: str) -> Dict[str, Any]:
    """把 ``tailscale funnel status`` 的输出解析成结构。

    输出长这样（真的就这么朴素）::

        # Funnel on:
        #     - https://silentwinter.tailf5fb77.ts.net

        https://silentwinter.tailf5fb77.ts.net (Funnel on)
        |-- / proxy http://127.0.0.1:8799
    """
    out: Dict[str, Any] = {"running": False, "url": "", "targets": []}
    text = text or ""
    if "funnel on" in text.lower():
        out["running"] = True
    match = _URL_RE.search(text)
    if match:
        out["url"] = match.group(1).rstrip("/")
    out["targets"] = [m.group(1) for m in _TARGET_RE.finditer(text)]
    if not out["targets"] and "No serve config" in text:
        out["running"] = False
    return out


def status(force: bool = False) -> Dict[str, Any]:
    """当前公网状态：{available, running, url, targets, error}。

    20 秒缓存：分享面板会被频繁重渲染，别每次都开销一个进程。
    """
    now = time.time()
    cached = _CACHE.get("data")
    if not force and cached and now - float(_CACHE.get("at") or 0) < _TTL:
        return dict(cached)  # type: ignore[arg-type]

    path = exe()
    if not path:
        data = {"available": False, "running": False, "url": "", "targets": [],
                "error": "没装 Tailscale：装它才能不碰路由器就把工作台开到公网"}
        _CACHE.update({"at": now, "data": data})
        return dict(data)

    res = _run(["funnel", "status"])
    parsed = parse_status(res["out"] or res["err"])
    data = {"available": True, "running": bool(parsed["running"]), "url": parsed["url"],
            "targets": parsed["targets"], "error": "" if res["ok"] or parsed["running"] else res["err"]}
    _CACHE.update({"at": now, "data": data})
    return dict(data)


def start(port: int) -> Dict[str, Any]:
    """把 443 的 Funnel 指到本机的 ``port`` 上（后台常驻）。"""
    res = _run(["funnel", "--bg", str(int(port))], timeout=60.0)
    data = status(force=True)
    data["ok"] = bool(res["ok"])
    if not res["ok"]:
        data["error"] = res["err"] or res["out"] or "启动失败"
    return data


def stop() -> Dict[str, Any]:
    res = _run(["funnel", "--https=443", "off"], timeout=60.0)
    data = status(force=True)
    data["ok"] = bool(res["ok"])
    if not res["ok"]:
        data["error"] = res["err"] or res["out"] or "关闭失败"
    return data


def node_name() -> str:
    """这台机器在 tailnet 里的名字（只用于界面提示，拿不到就算了）。"""
    res = _run(["status", "--json"], timeout=15.0)
    if not res["ok"]:
        return ""
    import json as _json
    try:
        info = _json.loads(res["out"] or "{}")
    except ValueError:
        return ""
    self_info = info.get("Self") or {}
    return str(self_info.get("DNSName") or "").rstrip(".")
