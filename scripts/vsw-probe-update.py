"""自我更新：服务端那半边（`/api/app/version`、`/api/app/apk`、`vsw/release.py`）。

不碰真实数据目录：临时 root 里自己摆一个 dist/。重点验证的是**手机端会信的那几个数** ——
版本号能不能比、SHA256 对不对、没有安装包时说的是不是人话。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request

from _paths import add_repo_to_path

add_repo_to_path()

from vsw import release as release_mod          # noqa: E402
from vsw.server import build_server              # noqa: E402
from vsw.accounts import AccountStore            # noqa: E402
from vsw.service import Workbench                # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} :: {detail}")
    return ok


def get(base: str, path: str, *, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def make_apk(path: str, tag: bytes) -> str:
    """造一个假 APK：内容只要可辨识就行（这里不需要真能装）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = b"PK\x03\x04" + tag * 400
    with open(path, "wb") as fh:
        fh.write(payload)
    return hashlib.sha256(payload).hexdigest().upper()


def main() -> int:
    print("== A 版本号解析 ==")
    check("r17 → 17", release_mod.build_code("r17") == 17, release_mod.build_code("r17"))
    check("R9 → 9", release_mod.build_code("R9") == 9, release_mod.build_code("R9"))
    check("v3 → 3", release_mod.build_code("v3") == 3, release_mod.build_code("v3"))
    check("空 → 0", release_mod.build_code("") == 0, release_mod.build_code(""))
    check("乱写 → 0", release_mod.build_code("beta") == 0, release_mod.build_code("beta"))
    check("18 比 9 大", release_mod.build_code("r18") > release_mod.build_code("r9"))

    root = tempfile.mkdtemp(prefix="vsw-upd-")
    svc = Workbench(os.path.join(root, "data"))
    base = None
    try:
        srv = build_server(svc, "127.0.0.1", 0)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        print("== B 没有安装包的时候 ==")
        status, body, _ = get(base, "/api/app/version?build=r5")
        data = json.loads(body.decode("utf-8"))
        check("接口 200", status == 200, status)
        check("available 为假", data.get("available") is False, data)
        check("本地源为空", not data.get("sources", {}).get("local"), data.get("sources"))
        status, body, _ = get(base, "/api/app/apk")
        check("没包时取包是 400", status == 400, status)
        check("并且说了人话",
              "dist" in body.decode("utf-8", "replace"),
              body.decode("utf-8", "replace")[:120])

        print("== C 摆一个 r18 的包 ==")
        apk = os.path.join(root, "data", "dist", "vsw-workbench.apk")
        digest = make_apk(apk, b"r18")
        size = os.path.getsize(apk)
        with io.open(os.path.join(root, "data", "dist", "version.json"),
                     "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"build": "r18", "notes": "自更新第一版", "channel": "stable"},
                      fh, ensure_ascii=False)

        status, body, _ = get(base, "/api/app/version?build=r17")
        data = json.loads(body.decode("utf-8"))
        check("有新版本", data.get("update_available") is True, data)
        check("新版本号是 r18", data.get("build") == "r18", data.get("build"))
        check("当前是 r17", data.get("current") == "r17", data.get("current"))
        check("可比（16/17/18 都是数字）", data.get("comparable") is True, data.get("comparable"))
        check("体积对得上", data.get("size") == size, (data.get("size"), size))
        check("SHA256 对得上", str(data.get("sha256", "")).upper() == digest,
              (data.get("sha256"), digest))
        check("带着更新说明", data.get("notes") == "自更新第一版", data.get("notes"))
        check("没让 GitHub 背锅", data.get("source") == "local", data.get("source"))

        status, body, _ = get(base, "/api/app/version?build=r18")
        data = json.loads(body.decode("utf-8"))
        check("已经是 r18 就没有新版", data.get("update_available") is False, data)
        check("仍然告诉我们最新是 r18", data.get("build") == "r18", data.get("build"))

        status, body, _ = get(base, "/api/app/version")
        data = json.loads(body.decode("utf-8"))
        check("不带 build 也能问", status == 200 and data.get("build") == "r18", (status, data))

        print("== D 下载安装包 ==")
        status, body, headers = get(base, "/api/app/apk")
        check("取包 200", status == 200, status)
        check("字节数一致", len(body) == size, (len(body), size))
        check("SHA256 一致", hashlib.sha256(body).hexdigest().upper() == digest, "")
        check("Content-Type 是安装包",
              "android.package-archive" in headers.get("Content-Type", ""),
              headers.get("Content-Type"))
        check("是附件下载", "attachment" in headers.get("Content-Disposition", ""),
              headers.get("Content-Disposition"))
        check("文件名带版本号", "r18" in headers.get("Content-Disposition", ""),
              headers.get("Content-Disposition"))
        check("不让缓存（否则永远装旧的）",
              headers.get("Cache-Control") == "no-store", headers.get("Cache-Control"))
        check("X-VSW-Build 是 r18", headers.get("X-VSW-Build") == "r18",
              headers.get("X-VSW-Build"))

        print("== E 问版本不需要登录（手机在登录前就该知道要不要更新） ==")
        status, body, _ = get(base, "/api/app/version?build=r17")
        check("匿名也能问版本", status == 200, status)
        status, _, _ = get(base, "/api/app/apk")
        check("匿名也能取包", status == 200, status)
        # 有账号的服务器上再验一次：种子要登录，版本号不要
        auth = AccountStore(os.path.join(root, "auth.sqlite3"))
        auth.create_user("甲甲", "secret123", is_admin=True)
        srv3 = build_server(Workbench(os.path.join(root, "data")), "127.0.0.1", 0, auth=auth)
        base3 = f"http://127.0.0.1:{srv3.server_address[1]}"
        threading.Thread(target=srv3.serve_forever, daemon=True).start()
        try:
            status, _, _ = get(base3, "/api/seeds")
            check("有账号时种子 401", status == 401, status)
            status, body, _ = get(base3, "/api/app/version?build=r17")
            check("有账号时版本号照样 200", status == 200, status)
            status, _, _ = get(base3, "/api/app/apk")
            check("有账号时装包照样 200", status == 200, status)
        finally:
            srv3.shutdown()
            auth.close()

        print("== F GitHub 兜底 ==")
        real = release_mod.github_release
        calls = {"n": 0}

        def fake_github(repo: str, timeout: float = 6.0):
            calls["n"] += 1
            return {"build": "r30", "build_code": 30, "size": 1234,
                    "sha256": "ABC", "url": "https://example.com/a.apk",
                    "notes": "云端更狠", "source": "github", "published_at": 0}

        release_mod.github_release = fake_github
        release_mod._gh_cache.clear() if hasattr(release_mod, "_gh_cache") else None
        srv2 = build_server(Workbench(os.path.join(root, "data2")), "127.0.0.1", 0,
                            gh_repo="someone/vsw")
        port2 = srv2.server_address[1]
        base2 = f"http://127.0.0.1:{port2}"
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        try:
            # 这台机器上没有 dist/，所以只能靠 GitHub
            os.makedirs(os.path.join(root, "data2"), exist_ok=True)
            status, body, _ = get(base2, "/api/app/version?build=r17")
            data = json.loads(body.decode("utf-8"))
            check("本地没有包时问 GitHub", calls["n"] >= 1, calls)
            check("用的是 GitHub 那份", data.get("source") == "github", data.get("source"))
            check("版本号是 r30", data.get("build") == "r30", data.get("build"))
            check("仍然报告有新版本", data.get("update_available") is True, data)
            status, body, _ = get(base2, "/api/app/apk")
            check("这台机器上没有包照样是 400", status == 400, status)
        finally:
            srv2.shutdown()
            release_mod.github_release = real

        print("== G 坏掉的情况 ==")
        with io.open(os.path.join(root, "data", "dist", "version.json"),
                     "w", encoding="utf-8") as fh:
            fh.write("{ 这不是 json")
        status, body, _ = get(base, "/api/app/version?build=r17")
        data = json.loads(body.decode("utf-8"))
        check("version.json 坏了也不 500", status == 200, status)
        check("退回到按文件算的版本", data.get("build") in ("", None) or data.get("build") == "",
              data.get("build"))
        check("拿不到版本就不说可以更新", data.get("update_available") is False,
              data.get("update_available"))
    finally:
        try:
            srv.shutdown()          # type: ignore[possibly-undefined]
        except Exception:
            pass
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n{PASS} 通过 · {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
