"""公网通道（Tailscale Funnel）探针：解析、来源判断、接口与权限。

为什么不直接测真的 tunnel：`tailscale funnel on/off` 是**机器级**开关，
探针要是真去开一次，就会把用户正在用的那条公网地址关掉/改掉。所以这里把
``vsw.tunnel`` 的 ``status/start/stop`` 换成假的，只验证我们自己的逻辑：

* ``status`` 文本怎么解析成 ``{running, url, targets}``；
* 经隧道进来时 ``X-Forwarded-For`` 必须压过对端地址（否则整个公网都被当成
  "本机"，谁都能抢先建主人账号）；
* ``/api/tunnel`` 只有主人能开、没有门槛时拒绝开、非法 action 400；
* 开公网后 ``share.public`` 出现在 /api/access 里。

用法::

    python scripts/vsw-probe-tunnel.py            # 全部
    python scripts/vsw-probe-tunnel.py -v         # 打印每项
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vsw import tunnel as tunnel_mod  # noqa: E402
from vsw.access import AccessStore  # noqa: E402
from vsw.accounts import AccountStore, default_auth_path  # noqa: E402
from vsw.hub import WorkspaceHub  # noqa: E402
from vsw.server import Handler, ServiceProxy, build_server  # noqa: E402
from vsw.service import Workbench  # noqa: E402

PASS = 0
FAIL = 0
VERBOSE = "-v" in sys.argv

PUBLIC_URL = "https://probe-node.tail1a2b3c.ts.net"

SAMPLE_ON = """# Funnel on:
#     - https://probe-node.tail1a2b3c.ts.net
#     - http://127.0.0.1:8799

https://probe-node.tail1a2b3c.ts.net (Funnel on)
|-- / proxy http://127.0.0.1:8799
"""

SAMPLE_OFF = """# Funnel is not enabled
#     - tailscale funnel --bg 8799
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        if VERBOSE:
            print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  ({detail})" if detail else ""))


class Client:
    """一个「浏览器」：收 cookie，也可以额外塞请求头（模拟隧道/手机）。"""

    def __init__(self, base: str, headers: dict | None = None) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.headers = dict(headers or {})

    def raw(self, method: str, path: str, body=None, headers=None):
        data = None
        head = dict(self.headers)
        head.update(headers or {})
        if body is not None:
            data = json.dumps(body).encode()
            head["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=head, method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def call(self, method: str, path: str, body=None, headers=None):
        status, raw = self.raw(method, path, body, headers)
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw[:200].decode("utf-8", "replace")}
        return status, data

    def get(self, path: str, headers=None):
        return self.call("GET", path, None, headers)

    def post(self, path: str, body=None, headers=None):
        return self.call("POST", path, body if body is not None else {}, headers)


class FakeTunnel:
    """替身：记住被怎么调用，返回预设状态，绝不碰真的 tailscale。"""

    def __init__(self) -> None:
        self.running = False
        self.calls: list = []
        self.error = ""

    def status(self, force: bool = False):
        return {
            "available": True,
            "running": self.running,
            "url": PUBLIC_URL if self.running else "",
            "targets": ["http://127.0.0.1:8799"] if self.running else [],
            "error": self.error,
        }

    def start(self, port: int):
        self.calls.append(("start", int(port)))
        self.running = True
        data = self.status()
        data["ok"] = True
        return data

    def stop(self):
        self.calls.append(("stop", 0))
        was = self.running
        self.running = False
        data = self.status()
        data["ok"] = True
        data["was_running"] = was
        return data


class StubHandler:
    """给 Handler 的方法打桩：只关心 IP 判断，不需要真 socket。"""

    def __init__(self, peer: str, xff: str = "") -> None:
        self.peer = peer
        self.headers = {"X-Forwarded-For": xff} if xff else {}

    _client_ip = lambda self: self.peer  # noqa: E731
    _is_loopback = lambda self: self.peer.startswith("127.") or self.peer == "::1"  # noqa: E731
    _forwarded_ip = Handler._forwarded_ip
    _is_local = Handler._is_local


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="vsw-tunnel-")
    real = (tunnel_mod.status, tunnel_mod.start, tunnel_mod.stop)
    fake = FakeTunnel()
    tunnel_mod.status = fake.status  # type: ignore[assignment]
    tunnel_mod.start = fake.start  # type: ignore[assignment]
    tunnel_mod.stop = fake.stop  # type: ignore[assignment]
    try:
        return run(tmp, fake)
    finally:
        tunnel_mod.status, tunnel_mod.start, tunnel_mod.stop = real
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str, fake: FakeTunnel) -> int:
    # ---------------------------------------------------------------- A 解析
    off = tunnel_mod.parse_status(SAMPLE_OFF)
    check("关闭态：running 为 False", not off.get("running"), str(off))
    on = tunnel_mod.parse_status(SAMPLE_ON)
    check("开启态：running 为 True", bool(on.get("running")), str(on))
    check("开启态：抠出 https 地址", on.get("url") == PUBLIC_URL, str(on.get("url")))
    check("开启态：抠出回源目标", on.get("targets") == ["http://127.0.0.1:8799"],
          str(on.get("targets")))
    check("空文本不炸", tunnel_mod.parse_status("").get("running") is False)
    check("available() 返回布尔", isinstance(tunnel_mod.available(), bool))

    # ------------------------------------------------- B 来源判断（XFF 优先）
    local_peer = StubHandler("127.0.0.1", "8.8.8.8")
    check("隧道外部：对端 127 但 XFF 是公网 → 不算本机", not local_peer._is_local(),
          str(local_peer._forwarded_ip()))
    check("隧道回环：XFF 127.0.0.1 → 算本机", StubHandler("127.0.0.1", "127.0.0.1")._is_local())
    check("直连回环：无 XFF、对端 127 → 算本机", StubHandler("127.0.0.1")._is_local())
    check("局域网外部设备：无 XFF、对端 192.168.1.250 → 不算本机",
          not StubHandler("192.168.1.250")._is_local())
    check("XFF 带多跳时只看第一段",
          not StubHandler("127.0.0.1", "8.8.8.8, 10.0.0.1")._is_local())

    # ------------------------------------------------ C 接口与权限（临时实例）
    root = os.path.join(tmp, "data")
    os.makedirs(root, exist_ok=True)
    svc = Workbench(root)
    auth = AccountStore(default_auth_path(root))
    hub = WorkspaceHub(auth, root)
    access = AccessStore(os.path.join(root, "workbench.sqlite3"))
    proxy = ServiceProxy(svc)
    httpd = build_server(proxy, "127.0.0.1", 0, access=access, auth=auth, hub=hub)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        anon = Client(base)
        owner = Client(base)
        guest = Client(base, {"X-Forwarded-For": "8.8.8.8"})

        status, data = anon.get("/api/tunnel")
        check("没账号时 /api/tunnel 可读但 blocked 有话说",
              status == 200 and bool(data.get("blocked")), f"{status} {data.get('blocked')}")

        status, data = anon.post("/api/tunnel", {"action": "start"})
        check("没门槛时拒绝开公网 → 400", status == 400, str(status))
        check("拒绝时说明「先建第一个账号」",
              "先建第一个账号" in (data.get("error") or ""), str(data.get("error")))
        check("拒绝时没有真的去开", not any(c[0] == "start" for c in fake.calls),
              str(fake.calls))

        status, data = owner.post("/api/auth/setup",
                                  {"name": "探针主人", "password": "secret123"})
        check("建第一个账号 200", status == 200, f"{status} {data}")
        ws_id = int(((data.get("created") or {}).get("workspace") or {}).get("id") or 0)
        check("账号建好", bool(data.get("user")), str(data)[:160])

        status, data = owner.get("/api/tunnel")
        check("主人看公网状态 → 200 且 blocked 清空",
              status == 200 and not data.get("blocked"), f"{status} {data}")
        check("状态里有 available 字段", data.get("available") is True, str(data))

        status, data = owner.post("/api/tunnel", {"action": "start"})
        check("主人开公网 → 200", status == 200, f"{status} {data}")
        check("开的时候把端口传下去了", any(c[0] == "start" and c[1] > 0 for c in fake.calls),
              str(fake.calls))
        check("返回里带公网地址", data.get("url") == PUBLIC_URL and data.get("running") is True,
              str(data))

        status, data = owner.get("/api/access")
        pub = ((data.get("share") or {}).get("public") or {})
        check("/api/access 的 share.public 有公网地址", pub.get("url") == PUBLIC_URL, str(pub))
        check("share.public.running 为 True", pub.get("running") is True, str(pub))

        status, data = owner.post("/api/tunnel", {"action": "stop"})
        check("主人关公网 → 200 且不再 running",
              status == 200 and data.get("running") is False, f"{status} {data}")

        status, data = owner.post("/api/tunnel", {"action": "on"})
        check("非法 action → 400", status == 400, str(status))

        owner.post("/api/tunnel", {"action": "start"})
        invite = owner.post(f"/api/auth/workspaces/{ws_id}/invites", {"role": "view", "hours": 24})
        code = ((invite[1].get("invite") or {}).get("code") or "")
        check("主人能发只读邀请码", invite[0] == 200 and bool(code), str(invite[1])[:160])
        reg = guest.post("/api/auth/register",
                         {"code": code, "name": "探针访客", "password": "secret123"})
        check("外部设备用邀请码注册 → 200", reg[0] == 200, str(reg[1])[:160])
        token = (reg[1].get("token") or "")
        # 访客是只读身份：能看状态，不能开关通道（_OWNER_WRITES 里有 /api/tunnel）
        g2 = Client(base, {"X-Forwarded-For": "8.8.8.8", "X-VSW-Session": token})
        status, data = g2.post("/api/tunnel", {"action": "stop"})
        check("只读身份不能关公网 → 403", status == 403, f"{status} {data}")
        status, data = g2.get("/api/tunnel")
        check("只读身份能看公网状态 → 200", status == 200, str(status))

        # 经隧道进来的外部设备：不能建账号、不能读数据，但页面壳子要能打开
        # 注意要一个**全新的**客户端 —— guest 刚才注册过，手里已经有会话 cookie 了。
        outsider = Client(base, {"X-Forwarded-For": "8.8.8.8"})
        status, data = outsider.get("/api/auth")
        check("经隧道的外部设备 can_setup 为 False", data.get("can_setup") is False,
              str({k: data.get(k) for k in ("logged_in", "accounts", "can_setup")}))
        status, data = outsider.get("/api/seeds?limit=1")
        check("经隧道的外部设备未登录读数据 → 401", status == 401, str(status))
        status, raw = outsider.raw("GET", "/")
        check("经隧道的外部设备能打开页面壳子（解锁框在里面）", status == 200, str(status))
    finally:
        httpd.shutdown()
        hub.close()
        auth.close()
        access.close()
        svc.store.close()

    # ------------------------------- D 只有密钥、没有账号时也能开（门槛也算）
    root2 = os.path.join(tmp, "keys-only")
    os.makedirs(root2, exist_ok=True)
    svc2 = Workbench(root2)
    access2 = AccessStore(os.path.join(root2, "workbench.sqlite3"))
    made = access2.create("访客", "owner")
    proxy2 = ServiceProxy(svc2)
    httpd2 = build_server(proxy2, "127.0.0.1", 0, access=access2)
    base2 = f"http://127.0.0.1:{httpd2.server_address[1]}"
    threading.Thread(target=httpd2.serve_forever, daemon=True).start()
    try:
        fake.calls.clear()
        key_client = Client(base2, {"X-VSW-Key": made.get("code", "")})
        status, data = key_client.get("/api/tunnel")
        check("有密钥没账号：blocked 也清空（密钥算门槛）", status == 200 and not data.get("blocked"),
              f"{status} {data}")
        status, data = key_client.post("/api/tunnel", {"action": "start"})
        check("有密钥的主人能开公网", status == 200 and data.get("running") is True,
              f"{status} {data}")
    finally:
        httpd2.shutdown()
        access2.close()
        svc2.store.close()

    print(f"\n公网通道探针：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
