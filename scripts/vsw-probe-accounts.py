"""账号 + 私人工作区 + 协作 + 访客密钥：端到端探针。

自建临时 root（不会碰真实数据），在后台线程里起一个真的 HTTP 服务，然后用
urllib 一步步从「没有账号」走到「两个账号 + 两个工作区 + 邀请码 + 密钥」，
每一步都断言服务端的行为。会话既测 cookie（浏览器）也测 ``X-VSW-Session``
请求头（手机 APK 里页面是 file://，cookie 带不上，只能走头）。

用法::

    python scripts/vsw-probe-accounts.py            # 全部
    python scripts/vsw-probe-accounts.py -v         # 打印每项
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
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    """一个「浏览器」：收 cookie，也可以额外塞请求头。"""

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
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers or {})

    def call(self, method: str, path: str, body=None, headers=None):
        status, raw, head = self.raw(method, path, body, headers)
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"raw": raw[:200].decode("utf-8", "replace")}
        return status, data, head

    def get(self, path: str, headers=None):
        return self.call("GET", path, None, headers)

    def post(self, path: str, body=None, headers=None):
        return self.call("POST", path, body if body is not None else {}, headers)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="vsw-accounts-")
    try:
        return run(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(tmp: str) -> int:
    root = os.path.join(tmp, "data")
    os.makedirs(root, exist_ok=True)
    svc = Workbench(root)
    auth = AccountStore(default_auth_path(root))
    hub = WorkspaceHub(auth, root)
    access = AccessStore(os.path.join(root, "workbench.sqlite3"))
    proxy = ServiceProxy(svc)
    httpd = build_server(proxy, "127.0.0.1", 0, access=access, auth=auth, hub=hub)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()

    try:
        return probe(base, root, svc, auth, access)
    finally:
        httpd.shutdown()
        hub.close()
        auth.close()
        access.close()
        svc.store.close()


def probe(base, root, svc, auth, access) -> int:
    # 造一点数据：一条种子 + 一张 1×1 的图，用来验证「数据目录 = 工作区」。
    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
    media = svc.media_root
    os.makedirs(media, exist_ok=True)
    with open(os.path.join(media, "probe.png"), "wb") as fh:
        fh.write(png)

    anon = Client(base)

    # ---------------------------------------------------------------- A 没账号
    st, data, _ = anon.get("/api/auth")
    check("A 没账号时 accounts=False", st == 200 and data.get("accounts") is False, str(data)[:120])
    st, data, _ = anon.get("/api/seeds")
    check("A 没账号时照旧能用", st == 200 and "seeds" in data, f"{st} {str(data)[:80]}")
    st, data, _ = anon.get("/api/health")
    check("A health 免登录", st == 200, str(st))
    st, data, _ = anon.get("/api/auth")
    check("A 本机可建号（can_setup）", bool(data.get("can_setup")))

    # ---------------------------------------------------------------- B 建第一个账号
    owner = Client(base)
    st, data, _ = owner.post("/api/auth/setup",
                             {"name": "阿甲", "password": "secret123", "workspace": "阿甲的工作区"})
    check("B loopback 建第一个账号", st == 200, f"{st} {str(data)[:160]}")
    token = data.get("token") or ""
    check("B 建号响应带会话令牌", bool(token), str(data)[:120])
    ws0 = (data.get("created") or {}).get("workspace") or {}
    check("B 老数据归到第一个工作区", os.path.abspath(str(ws0.get("dir") or "")) == os.path.abspath(root),
          str(ws0.get("dir")))
    check("B 建号后角色是主人", data.get("role") == "owner", str(data.get("role")))

    st, data, _ = owner.get("/api/seeds")
    check("B cookie 会话可用", st == 200 and "seeds" in data, f"{st} {str(data)[:80]}")

    phone = Client(base, headers={"X-VSW-Session": token})
    st, data, _ = phone.get("/api/seeds")
    check("B X-VSW-Session 头可用（手机端）", st == 200 and "seeds" in data, f"{st} {str(data)[:80]}")

    nobody = Client(base)
    st, data, _ = nobody.get("/api/seeds")
    check("B 无凭证 401 need_login", st == 401 and data.get("need_login") is True, f"{st} {str(data)[:120]}")
    st, data, _ = nobody.get("/api/health")
    check("B health 仍然免登录", st == 200, str(st))
    st, data, _ = nobody.get("/")
    check("B 页面壳子照发（登录卡要显示）", st == 200, str(st))
    st, data, _ = nobody.get("/media/probe.png")
    check("B 图片要鉴权", st == 401, str(st))

    # ---------------------------------------------------------------- C 邀请码 + 第二个账号
    st, data, _ = owner.post("/api/auth/login", {"name": "阿甲", "password": "secret123"})
    check("C 密码登录", st == 200 and data.get("logged_in"), f"{st} {str(data)[:120]}")
    st, data, _ = anon.post("/api/auth/login", {"name": "阿甲", "password": "错的口令"})
    check("C 错密码 401 且给剩余次数", st == 401 and data.get("attempts_left"), f"{st} {str(data)[:120]}")

    ws_owner = int(ws0.get("id") or 0)
    st, data, _ = owner.post(f"/api/auth/workspaces/{ws_owner}/invites", {"role": "owner", "hours": 24})
    check("C 邀请码不能直接发主人", st == 400, f"{st} {str(data)[:120]}")
    st, data, _ = owner.post(f"/api/auth/workspaces/{ws_owner}/invites", {"role": "edit", "hours": 24})
    invite = (data.get("invite") or {}).get("code") or ""
    check("C 主人能发可操作邀请码", st == 200 and invite.startswith("INV-"), f"{st} {invite!r}")

    guest = Client(base)
    st, data, _ = guest.post("/api/auth/register",
                             {"code": invite, "name": "阿乙", "password": "secret456"})
    check("C 用邀请码注册并自动登录", st == 200 and data.get("logged_in"), f"{st} {str(data)[:140]}")
    joined = data.get("joined") or {}
    joined_ws = (joined.get("workspace") or {}).get("id")
    check("C 注册后进的是发码的工作区",
          int(joined_ws or 0) == ws_owner and data.get("role") == "edit",
          f"ws={joined_ws} role={data.get('role')}")
    st, data, _ = guest.post("/api/auth/register",
                             {"code": invite, "name": "阿丙", "password": "secret789"})
    check("C 邀请码只能用一次", st == 400, f"{st} {str(data)[:120]}")
    st, data, _ = owner.post(f"/api/auth/workspaces/{ws_owner}/invites", {"role": "edit", "hours": 24})
    invite2 = (data.get("invite") or {}).get("code") or ""
    st, data, _ = Client(base).post("/api/auth/register",
                                    {"code": invite2, "name": "阿甲", "password": "whatever9"})
    check("C 重名注册被拒", st == 400, f"{st} {str(data)[:120]}")

    # ---------------------------------------------------------------- D 私人工作区隔离
    st, data, _ = owner.post("/api/auth/workspaces", {"name": "阿甲的另一个私人区", "kind": "personal"})
    check("D 能自己开新工作区", st == 200 and data.get("workspace"), f"{st} {str(data)[:140]}")
    ws_new = int((data.get("workspace") or {}).get("id") or 0)
    new_dir = os.path.abspath(str((data.get("workspace") or {}).get("dir") or ""))
    check("D 新工作区有独立目录", bool(new_dir) and new_dir != os.path.abspath(root), new_dir)
    st, data, _ = owner.get("/api/seeds")
    check("D 新工作区是空的（物理隔离）", st == 200 and not (data.get("seeds") or []), str(data.get("seeds"))[:80])

    st, data, _ = owner.post("/api/auth/workspaces/switch", {"ws": ws_owner})
    check("D 切回原工作区", st == 200 and int((data.get("workspace") or {}).get("id") or 0) == ws_owner,
          str(data)[:140])
    st, data, _ = owner.post("/api/auth/workspaces/switch", {"ws": 999999})
    check("D 不能切进不存在/没加入的工作区", st == 403, f"{st} {str(data)[:120]}")
    st, data, _ = guest.post("/api/auth/workspaces/switch", {"ws": ws_new})
    check("D 协作方切不进别人的私人区", st == 403, f"{st} {str(data)[:120]}")

    # ---------------------------------------------------------------- E 协作 / 角色
    st, data, _ = guest.get(f"/api/auth/workspaces/{ws_owner}/members")
    check("E 协作方能看成员表", st == 200 and len(data.get("members") or []) == 2,
          f"{st} {str(data.get('members'))[:120]}")
    check("E 协作方不是主人", data.get("is_owner") is False, str(data.get("is_owner")))
    st, data, _ = guest.post(f"/api/auth/workspaces/{ws_owner}/invites", {"role": "view"})
    check("E 非主人不能发邀请码", st == 403, f"{st} {str(data)[:120]}")

    # 阿乙改成只看
    st, data, _ = owner.get(f"/api/auth/workspaces/{ws_owner}/members")
    uids = {m["name"]: m["user_id"] for m in (data.get("members") or [])}
    st, data, _ = owner.post(f"/api/auth/workspaces/{ws_owner}/members/{uids['阿乙']}",
                             {"role": "view"})
    check("E 主人能把成员降成只看", st == 200, f"{st} {str(data)[:120]}")
    st, data, _ = guest.post("/api/findings/trash/restore", {"all": True})
    check("E 只看的人写操作 403", st == 403, f"{st} {str(data)[:120]}")
    st, data, _ = guest.get("/api/seeds")
    check("E 只看的人还能读", st == 200, f"{st}")

    st, data, _ = owner.post(f"/api/auth/workspaces/{ws_owner}/members/{uids['阿乙']}",
                             {"role": "edit"})
    check("E 主人能把成员升回可操作", st == 200, f"{st}")
    st, data, _ = owner.call("DELETE", f"/api/auth/workspaces/{ws_owner}/members/{uids['阿乙']}")
    check("E 主人能移出成员", st == 200, f"{st} {str(data)[:120]}")
    st, data, _ = guest.get("/api/seeds")
    check("E 被移出后立刻失去访问", st == 403 or st == 401, f"{st} {str(data)[:120]}")

    # 加回来，顺便测「按名字加已有账号」
    st, data, _ = owner.post(f"/api/auth/workspaces/{ws_owner}/members",
                             {"name": "阿乙", "role": "edit"})
    check("E 按名字把已有账号加进工作区", st == 200 and len(data.get("members") or []) == 2,
          f"{st} {str(data)[:120]}")
    st, data, _ = guest.get("/api/seeds")
    check("E 加回来后又能访问", st == 200, f"{st}")

    # ---------------------------------------------------------------- F 账号安全
    st, data, _ = guest.get("/api/auth")
    check("F /api/auth 报身份", st == 200 and (data.get("user") or {}).get("name") == "阿乙",
          str(data)[:140])
    st, data, _ = guest.post("/api/auth/password", {"old": "错的口令", "new": "nope123"})
    check("F 改密码要验原密码", st == 400, f"{st} {str(data)[:120]}")
    st, data, _ = guest.post("/api/auth/logout", {})
    check("F 退出登录", st == 200, f"{st}")
    st, data, _ = guest.get("/api/seeds")
    check("F 退出后 401", st == 401 and data.get("need_login") is True, f"{st} {str(data)[:120]}")

    # 手机端：用请求头登录 + 用请求头退出
    st, data, _ = Client(base).post("/api/auth/login", {"name": "阿乙", "password": "secret456"})
    tok2 = data.get("token") or ""
    check("F 手机端重新登录拿到令牌", bool(tok2), str(data)[:120])
    ph2 = Client(base, headers={"X-VSW-Session": tok2})
    st, data, _ = ph2.get("/api/auth")
    check("F 请求头登录态可读", st == 200 and (data.get("user") or {}).get("name") == "阿乙",
          str(data)[:140])
    st, data, _ = ph2.post("/api/auth/logout", {})
    check("F 请求头也能退出", st == 200, f"{st}")
    st, data, _ = ph2.get("/api/auth")
    check("F 退出后令牌失效", st == 200 and not data.get("logged_in"), str(data)[:140])

    # ---------------------------------------------------------------- G 访客密钥
    st, data, _ = owner.post("/api/access/keys", {"label": "只读访客", "role": "view"})
    code = (data.get("key") or {}).get("code") or ""
    check("G 主人能建访客密钥", st == 200 and code.startswith("VSW-"), f"{st} {code!r}")
    keyed = Client(base, headers={"X-VSW-Key": code})
    st, data, _ = keyed.get("/api/seeds")
    check("G 密钥头能读", st == 200 and "seeds" in data, f"{st} {str(data)[:80]}")
    st, data, _ = Client(base).get("/api/seeds?key=" + urllib.parse.quote(code))
    check("G ?key= 能读（<img> 用）", st == 200, f"{st}")
    st, data, _ = keyed.post("/api/auth/workspaces", {"name": "偷偷开一个"})
    check("G 只读密钥不能开工作区", st == 403, f"{st} {str(data)[:120]}")
    st, data, _ = owner.post("/api/access/keys", {"label": "备份主人", "role": "owner"})
    spare = (data.get("key") or {}).get("code") or ""
    check("G 再建一把主人密钥", st == 200 and spare.startswith("VSW-"), f"{st}")
    st, data, _ = keyed.get(f"/api/auth/workspaces/{ws_owner}/members")
    check("G 密钥进不了账号接口", st in (401, 403), f"{st} {str(data)[:120]}")
    keys = (owner.get("/api/access")[1] or {}).get("keys") or []
    kid = int(next((k["id"] for k in keys if k.get("role") == "view" and k.get("active")), 0) or 0)
    st, data, _ = owner.post(f"/api/access/keys/{kid}/revoke", {})
    check("G 撤销密钥", st == 200, f"{st} {str(data)[:120]}")
    st, data, _ = keyed.get("/api/seeds")
    check("G 撤销后旧密钥 401", st == 401, f"{st}")

    # ---------------------------------------------------------------- H 边角
    st, data, head = nobody.raw("OPTIONS", "/api/seeds")
    allow = head.get("Access-Control-Allow-Headers") or head.get("access-control-allow-headers") or ""
    check("H OPTIONS 允许 X-VSW-Session 与 X-VSW-Workspace",
          "X-VSW-Session" in allow and "X-VSW-Workspace" in allow, allow)
    st, data, _ = owner.call("HEAD", "/api/auth")
    check("H /api/auth 支持 HEAD", st == 200, f"{st}")
    st, data, _ = owner.get("/?key=" + urllib.parse.quote(code))
    check("H 页面壳子公开（哪怕带废密钥）", st == 200, f"{st}")

    print()
    total = PASS + FAIL
    print(f"{'通过' if not FAIL else '有失败'}：{PASS}/{total}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
