"""访问密钥（把工作台开放给别人）的回归探针。

为什么单独写一个 Python 探针：这一层几乎全是 HTTP 语义 ——
401/403/429、Cookie 种下去没有、``?key=`` 与头的优先级、撤销之后旧码是否立刻失效。
浏览器探针测这些既慢又绕，直接在进程里起一个真服务器打请求最准。

它自己建一个临时数据目录 + 临时密钥库，不碰你的真实数据。

用法::

    python scripts/vsw-probe-access.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import add_repo_to_path  # noqa: E402

add_repo_to_path()

from vsw.access import AccessStore, ensure_owner_key  # noqa: E402
from vsw.server import build_server  # noqa: E402
from vsw.service import Workbench  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, ok: bool, detail: object = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        print(f"  FAIL {label} :: {text[:400]}")


class Client:
    """极简 HTTP 客户端：记 cookie、可选带密钥头。"""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.cookie = ""
        self.header_key = ""

    def request(self, method: str, path: str, body: object = None,
                key: str = "", use_cookie: bool = True, extra: dict | None = None):
        url = self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if key:
            headers["X-VSW-Key"] = key
        if use_cookie and self.cookie:
            headers["Cookie"] = self.cookie
        headers.update(extra or {})
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                status = resp.status
                got_cookie = resp.headers.get("Set-Cookie") or ""
                content_type = resp.headers.get("Content-Type") or ""
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            got_cookie = exc.headers.get("Set-Cookie") or ""
            content_type = exc.headers.get("Content-Type") or ""
        if got_cookie:
            self.cookie = got_cookie.split(";")[0]
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = raw
        return {"status": status, "json": payload, "cookie": got_cookie,
                "type": content_type, "bytes": len(raw)}


def main() -> int:
    root = tempfile.mkdtemp(prefix="vsw-access-")
    service = Workbench(root, log=lambda msg: None)
    store = AccessStore(os.path.join(root, "workbench.sqlite3"))
    server = build_server(service, "127.0.0.1", 0, access=store)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"临时工作台 {base}（数据目录 {root}）\n")

    # 摆一张图进去，用来测 /media 的鉴权
    os.makedirs(service.media_root, exist_ok=True)
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d4944415478da63f8ffff3f0005fe02fea4b1f2f2"
        "0000000049454e44ae426082")
    with open(os.path.join(service.media_root, "probe.png"), "wb") as fh:
        fh.write(png)
    media_path = "/media/probe.png"

    anon = Client(base)
    try:
        # ---------------------------------------------------------- A 没上锁
        print("A. 还没设密钥（老行为：局域网熟人模式）")
        got = anon.request("GET", "/api/findings?limit=1")
        check("没有密钥时照常可用", got["status"] == 200, got["status"])
        info = anon.request("GET", "/api/access")["json"]
        check("接口自报未上锁", info.get("enabled") is False, info.get("enabled"))

        # ---------------------------------------------------------- B 上锁
        print("\nB. 上了锁之后（一把主人钥匙 + 一把只读钥匙 + 一把协作钥匙）")
        owner = ensure_owner_key(store, "主人")
        owner_code = owner["code"]
        view = store.create("朋友看", "view")
        edit = store.create("协作", "edit")
        check("有密钥后自报已上锁", store.enabled() and
              anon.request("GET", "/api/access")["json"].get("enabled") is True)

        got = anon.request("GET", "/api/findings?limit=1")
        check("不带密钥读接口 → 401", got["status"] == 401, got["status"])
        check("401 里带 need_key 且没泄露数据",
              isinstance(got["json"], dict) and got["json"].get("need_key") is True
              and "findings" not in json.dumps(got["json"]), got["json"])
        got = anon.request("GET", media_path)
        check("不带密钥取图 → 401（图片也是数据）", got["status"] == 401, got["status"])
        got = anon.request("GET", "/api/health")
        check("健康检查不需要密钥（不然脚本探不到活在不在）", got["status"] == 200,
              got["status"])

        # 错码
        bad = Client(base)
        got = bad.request("POST", "/api/access/login", {"key": "VSW-AAAA-AAAA-AAAA"})
        first_left = (got["json"] or {}).get("attempts_left")
        check("错码登录 → 401 并告诉还剩几次", got["status"] == 401 and first_left == 7,
              got["json"])
        got = bad.request("POST", "/api/access/login", {"key": "乱写的"})
        check("形状都不对的码单独提示", got["status"] == 401
              and "格式" in str((got["json"] or {}).get("error", "")), got["json"])

        # 正确码 → cookie
        human = Client(base)
        got = human.request("POST", "/api/access/login", {"key": owner_code.lower()})
        check("正确密钥能登录（大小写不敏感）", got["status"] == 200, got["json"])
        check("登录后种下 HttpOnly cookie",
              "vsw_key=" in got["cookie"] and "HttpOnly" in got["cookie"], got["cookie"][:120])
        got = human.request("GET", "/api/findings?limit=1")
        check("之后靠 cookie 就能读", got["status"] == 200, got["status"])
        got = human.request("GET", media_path)
        check("图片也能靠 cookie 取到（<img> 带不了头）",
              got["status"] == 200 and got["bytes"] == len(png), got["status"])

        # 头 / ?key=
        got = anon.request("GET", "/api/findings?limit=1", key=owner_code)
        check("X-VSW-Key 头也能鉴权", got["status"] == 200, got["status"])
        anon.cookie = ""
        got = anon.request("GET", f"/api/findings?limit=1&key={urllib.parse.quote(owner_code)}")
        check("?key= 分享链接能进", got["status"] == 200, got["status"])
        check("用 ?key= 进来时顺手种 cookie", "vsw_key=" in got["cookie"], got["cookie"][:80])
        got = anon.request("GET", "/api/findings?limit=1&key=VSW-AAAA-AAAA-AAAA")
        check("?key= 是错的 → 401 而不是放行", got["status"] == 401, got["status"])

        # 角色
        print("\nC. 权限分级（只读不能写，协作不能管密钥/不能删文件）")
        viewer = Client(base)
        viewer.request("POST", "/api/access/login", {"key": view["code"]})
        got = viewer.request("GET", "/api/queue?limit=1")
        check("只读能看队列", got["status"] == 200, got["status"])
        got = viewer.request("POST", "/api/settings", {"phash_dup": 5})
        check("只读写设置 → 403", got["status"] == 403, got["json"])
        check("403 里说清当前角色和需要的角色",
              "只看" in str((got["json"] or {}).get("error", "")), got["json"])
        got = viewer.request("POST", "/api/findings/trash", {"ids": [1]})
        check("只读连「移入回收站」都不行 → 403", got["status"] == 403, got["status"])
        got = viewer.request("DELETE", "/api/finding/1")
        check("只读不能删结果 → 403", got["status"] == 403, got["status"])

        editor = Client(base)
        editor.request("POST", "/api/access/login", {"key": edit["code"]})
        got = editor.request("POST", "/api/settings", {"phash_dup": 5})
        check("协作改全局阈值 → 403（那是主人的口径）", got["status"] == 403, got["status"])
        got = editor.request("POST", "/api/reveal", {"path": root})
        check("协作不能在你电脑上弹文件夹 → 403", got["status"] == 403, got["status"])
        got = editor.request("POST", "/api/findings/trash/purge", {"ids": [1]})
        check("协作不能彻底删除 → 403", got["status"] == 403, got["status"])
        got = editor.request("POST", "/api/findings/trash", {"ids": [999999]})
        check("协作能移入回收站（不是 403 就说明放行了）", got["status"] != 403, got["status"])

        # 主人
        boss = Client(base)
        boss.request("POST", "/api/access/login", {"key": owner_code})
        got = boss.request("GET", "/api/access")
        payload = got["json"] or {}
        check("主人能看到密钥列表", got["status"] == 200 and len(payload.get("keys") or []) == 3,
              len(payload.get("keys") or []))
        check("列表里不含明文码（只在创建时返回一次）",
              all("code" not in k for k in (payload.get("keys") or [])), payload.get("keys"))
        check("主人身份来自密钥而不是「本机」", (payload.get("me") or {}).get("role") == "owner",
              payload.get("me"))
        peek = Client(base)
        got = peek.request("GET", "/api/access", key=view["code"])
        check("只读拿不到密钥列表", "keys" not in (got["json"] or {}), got["json"])

        # 建/撤
        got = boss.request("POST", "/api/access/keys", {"label": "临时访客", "role": "view"})
        made = (got["json"] or {}).get("key") or {}
        check("主人能建新密钥且当场拿到码", got["status"] == 200 and "code" in made,
              {"status": got["status"], "body": got["json"]})
        guest = Client(base)
        got = guest.request("POST", "/api/access/login", {"key": made.get("code", "")})
        check("新密钥立刻能用", got["status"] == 200, got["status"])

        got = editor.request("POST", "/api/access/keys", {"label": "我想造一把", "role": "owner"})
        check("协作角色不能建密钥 → 403", got["status"] == 403, got["status"])

        got = boss.request("POST", f"/api/access/keys/{made['id']}/revoke")
        check("撤销一条密钥", got["status"] == 200, got["status"])
        got = guest.request("POST", "/api/access/login", {"key": made.get("code", "")},
                            use_cookie=False)
        check("撤销之后旧码立刻失效", got["status"] == 401, got["status"])

        # 最后一条不能撤
        for row in store.keys():
            if row["active"] and row["id"] != owner["id"]:
                store.revoke(row["id"])
        got = boss.request("POST", f"/api/access/keys/{owner['id']}/revoke")
        check("最后一条有效密钥不许撤（否则没人进得来）",
              got["status"] == 400 and "最后一条" in str((got["json"] or {}).get("error", "")),
              got["json"])

        # ---------------------------------------------------------- D 限速
        print("\nD. 爆破防护")
        store.note_success("127.0.0.1")
        attacker = Client(base)
        codes = []
        for i in range(9):
            got = attacker.request("POST", "/api/access/login",
                                   {"key": f"VSW-AAAA-AAAA-AAA{chr(65 + i)}"},
                                   use_cookie=False)
            codes.append(got["status"])
        check("连续错 8 次之后开始 429", codes[-1] == 429, codes)
        locked = attacker.request("POST", "/api/access/login", {"key": "VSW-AAAA-AAAA-AAAJ"},
                                  use_cookie=False)["json"] or {}
        check("429 会给出 retry_after 与再次解锁时间",
              locked.get("retry_after", 0) > 0 and "error" in locked, locked)
        store.note_success("127.0.0.1")

        # ---------------------------------------------------------- E 边角
        print("\nE. 边角")
        got = boss.request("HEAD", media_path)
        check("HEAD 也要密钥（不能被当成免检通道）", got["status"] == 200, got["status"])
        anon.cookie = ""
        got = anon.request("HEAD", media_path)
        check("没有密钥的 HEAD → 401", got["status"] == 401, got["status"])
        pre = Client(base).request("OPTIONS", "/api/findings")
        check("预检允许 X-VSW-Key 头（手机端靠它）", pre["status"] == 204, pre["status"])
        got = anon.request("GET", "/")
        check("页面本身照发（壳子不含数据，前端自己弹解锁）",
              got["status"] == 200 and got["bytes"] > 1000, got["status"])
        got = anon.request("GET", "/m/")
        check("手机端页面也照发", got["status"] == 200 and got["bytes"] > 1000,
              got["status"])
        share = (boss.request("GET", "/api/access")["json"] or {}).get("share") or {}
        check("分享信息给出手机地址（不带密钥，由前端拼）",
              bool(share.get("phone")) and all("key=" not in u for u in share.get("phone", [])),
              share.get("phone"))
        check("分享信息里有端口与数据目录",
              share.get("port") == port and bool(share.get("root")), share.get("port"))
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        try:
            service.store.close()
        except Exception:  # noqa: BLE001
            pass

    print(f"\n通过 {PASSED} 项，失败 {FAILED} 项")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
