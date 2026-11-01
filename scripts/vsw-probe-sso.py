"""第三方应用免登录（HYW 嫁接）探针：绑定令牌、一次性票据、/sso 落地页。

覆盖三件事：

* 「绑定令牌」本身：生成/规范化/撤销/只存哈希，令牌不能被当会话令牌用；
* 「票据」：60 秒有效、取出即删、重放必失效、签了某个用户就只能是那个用户；
* 「落地页 /sso」：换到 cookie 并 302、票据用过就过期、无效票据不崩、
  以及**第三方换票必须免密钥/免登录**（第一版插在 `_guard` 后面被 401 挡掉过）。

用法::

    python scripts/vsw-probe-sso.py            # 全部
    python scripts/vsw-probe-sso.py -v         # 打印每项
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

from vsw import sso as sso_mod  # noqa: E402
from vsw.access import AccessStore  # noqa: E402
from vsw.accounts import AccountStore, default_auth_path  # noqa: E402
from vsw.hub import WorkspaceHub  # noqa: E402
from vsw.server import ServiceProxy, build_server  # noqa: E402
from vsw.service import Workbench  # noqa: E402

PASS = 0
FAIL = 0
VERBOSE = "-v" in sys.argv


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
    """一个「浏览器」：收 cookie，不自动跟 302（要看清 Location）。"""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
                return None

        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect())

    def raw(self, method: str, path: str, body=None):
        data = None
        head = {}
        if body is not None:
            data = json.dumps(body).encode()
            head["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=head, method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()

    def call(self, method: str, path: str, body=None):
        status, headers, raw = self.raw(method, path, body)
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw[:200].decode("utf-8", "replace")}
        return status, headers, data

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, body=None):
        return self.call("POST", path, body if body is not None else {})

    def cookie(self, name: str) -> str:
        for c in self.jar:
            if c.name == name:
                return c.value or ""
        return ""


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="vsw-sso-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    # ---------------------------------------------------- A 令牌与票据的单元
    key = sso_mod.new_bind_key()
    check("生成的绑定令牌形如 VSWL-XXXX-XXXX-XXXX",
          sso_mod.normalize_bind_key(key) == key and key.startswith("VSWL-"), key)
    check("小写/空格/多横线都能规范化回来",
          sso_mod.normalize_bind_key(f"  {key.lower()}  ") == key, key)
    check("乱写的令牌被拒", sso_mod.normalize_bind_key("hello") == "", "hello")

    store = sso_mod.TicketStore()
    t1 = store.issue(7, "probe")
    check("票据取出即删（第一次能换）", store.redeem(t1) == 7, t1[:8])
    check("同一张票第二次换不到", store.redeem(t1) is None, t1[:8])
    check("没发过的票换不到", store.redeem("nope") is None)
    t9 = store.issue(9)
    check("peek 能看但不消费", store.peek(t9) == 9 and store.peek(t9) == 9, t9[:8])
    check("过期的票换不到", sso_mod.TicketStore(ttl=-1).redeem(
        sso_mod.TicketStore(ttl=-1).issue(3)) is None)
    small = sso_mod.TicketStore(limit=4)
    for i in range(10):
        small.issue(i)
    check("票据表有上限，刷不爆", small.size() <= 4, str(small.size()))
    check("ticket_url 拼出 /sso?ticket=",
          sso_mod.ticket_url("https://x.test", "abc") == "https://x.test/sso?ticket=abc",
          sso_mod.ticket_url("https://x.test", "abc"))
    check("ticket_url 会带上 next",
          sso_mod.ticket_url("https://x.test/", "abc", "/m/") == "https://x.test/sso?ticket=abc&next=%2Fm%2F",
          sso_mod.ticket_url("https://x.test/", "abc", "/m/"))

    # ------------------------------------------------------------ B 起真服务
    root = os.path.join(tmp, "data")
    os.makedirs(root, exist_ok=True)
    svc = Workbench(root)
    auth = AccountStore(default_auth_path(root))
    hub = WorkspaceHub(auth, root)
    access = AccessStore(os.path.join(root, "workbench.sqlite3"))
    proxy = ServiceProxy(svc)
    httpd = build_server(proxy, "127.0.0.1", 0, access=access, auth=auth, hub=hub)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        return probe(base, root, auth)
    finally:
        httpd.shutdown()
        hub.close()
        auth.close()
        access.close()
        svc.store.close()


def probe(base: str, tmp: str, auth: AccountStore) -> int:
    boss = Client(base)
    check("还没账号时 /api/sso 自述可用", boss.get("/api/sso")[0] == 200)
    status, _, data = boss.post("/api/auth/setup",
                                {"name": "sso主人", "password": "secret123", "ws": "SSO 测试"})
    check("建好第一个账号", status == 200 and data.get("logged_in") is True, str(data)[:120])
    check("建号后浏览器拿到 vsw_sid", bool(boss.cookie("vsw_sid")), boss.cookie("vsw_sid")[:8])

    # --------------------------------------------------- C 绑定（账号密码换令牌）
    guest = Client(base)
    status, _, data = guest.post("/api/sso/bind", {"name": "sso主人", "password": "wrong-pass"})
    check("密码错不许绑定", status == 401 and "不对" in str(data.get("error")), f"{status} {data}")
    check("错密码会报还剩几次", "attempts_left" in data, str(data))
    status, _, data = guest.post("/api/sso/bind", {"name": "sso主人", "password": "secret123"})
    bind_key = str(data.get("key") or "")
    check("对密码换到绑定令牌", status == 200 and bind_key.startswith("VSWL-"), f"{status} {data}")
    check("响应里有用户与地址", bool(data.get("user")) and bool(data.get("server")), str(data)[:120])
    check("第三方会话没有拿到 cookie", not guest.cookie("vsw_sid"), guest.cookie("vsw_sid")[:8])

    rows = auth.sso_links()
    check("库里存了一条绑定记录", len(rows) == 1, str(rows))
    check("库里只有哈希，没有明文令牌", bool(rows) and bind_key not in json.dumps(rows), str(rows)[:160])

    # ------------------------------------------- D 换票（第三方服务端调，要免登录）
    anon = Client(base)
    status, _, data = anon.post("/api/sso/ticket", {"key": bind_key})
    ticket = str(data.get("ticket") or "")
    check("绑定令牌能换到票据（免登录免密钥）", status == 200 and bool(ticket), f"{status} {data}")
    check("换票返回的是一次性链接", "/sso?ticket=" in str(data.get("url")), str(data.get("url")))
    check("票据有效期报 60 秒", int(data.get("expires_in") or 0) == 60, str(data.get("expires_in")))
    check("绑定令牌不能当会话令牌用", Client(base).get("/api/seeds")[0] == 401)

    status, _, data = anon.post("/api/sso/ticket", {"key": "VSWL-AAAA-BBBB-CCCC"})
    check("乱写的绑定令牌换不到票", status == 401, f"{status} {data}")

    # --------------------------------------------------------- E 落地页 /sso
    browser = Client(base)
    status, headers, _ = browser.raw("GET", f"/sso?ticket={ticket}")
    check("落地页 302 到主页面", status == 302 and headers.get("Location") == "/",
          f"{status} {headers.get('Location')}")
    check("落地页把会话 cookie 种上了", bool(browser.cookie("vsw_sid")), browser.cookie("vsw_sid")[:8])
    check("换完 cookie 就能读数据", browser.get("/api/seeds")[0] == 200)
    check("带上工作区 cookie", bool(browser.cookie("vsw_ws")), browser.cookie("vsw_ws")[:8])

    replay = Client(base)
    status, headers, _ = replay.raw("GET", f"/sso?ticket={ticket}")
    check("同一张票重放 → 跳到 sso=expired",
          status == 302 and "sso=expired" in str(headers.get("Location")),
          f"{status} {headers.get('Location')}")
    check("重放的浏览器没有拿到会话", not replay.cookie("vsw_sid"), replay.cookie("vsw_sid")[:8])

    status, headers, _ = Client(base).raw("GET", "/sso?ticket=")
    loc = str(headers.get("Location"))
    check("空票据不炸，跳回主页并说明票过期",
          status == 302 and loc.startswith("/") and "sso=" in loc, f"{status} {loc}")

    # ------------------------------------------------------- F 管理端：列表与撤销
    status, _, data = boss.get("/api/sso/links")
    check("主人能看到自己的绑定列表", status == 200 and len(data.get("links") or []) == 1,
          f"{status} {data}")
    link_id = (data.get("links") or [{}])[0].get("id")
    check("列表里不带明文令牌", bind_key not in json.dumps(data), json.dumps(data)[:160])

    check("未登录看不到绑定列表", Client(base).get("/api/sso/links")[0] == 401)
    status, _, _ = boss.post(f"/api/sso/revoke/{link_id}")
    check("主人能撤销绑定", status == 200, str(status))
    status, _, data = Client(base).post("/api/sso/ticket", {"key": bind_key})
    check("撤销之后换不到票", status == 401, f"{status} {data}")
    check("撤销后列表空了", len(boss.get("/api/sso/links")[2].get("links") or []) == 0)
    return report()


def report() -> int:
    total = PASS + FAIL
    print()
    print(f"{PASS} 通过 / {FAIL} 失败（共 {total} 项）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
