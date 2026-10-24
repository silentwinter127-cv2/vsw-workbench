"""批量自动搜图（手机上一键搜）的服务端回归。

手机上按一下「自动搜图」，实际干活的是这台机器上的无头浏览器。这条链路有三段容易出错：

  1. 挑任务：只能挑「引擎本身支持无头浏览器」的队列项，别的引擎得说清楚为什么跳过；
  2. 排队：一次只允许一条队（一台机器一次只能开一个识图页），重复点要能被拦住；
  3. 汇报：每个任务的成败与新增张数要落到 auto_state 里，手机轮询就能看到，
     而且**单个任务失败不能中断整批**。

这里不真的开浏览器 —— 把 `browser_search` 换成一个假的，专测这三段逻辑与接口权限。

用法：python scripts/vsw-probe-auto.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vsw.accounts import AccountStore, default_auth_path  # noqa: E402
from vsw.hub import WorkspaceHub  # noqa: E402
from vsw.service import ServiceError, Workbench  # noqa: E402
from vsw.server import ServiceProxy, build_server  # noqa: E402

PASS = 0
FAIL = 0


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}" + (f" :: {detail}" if detail else ""))


class Client:
    """最小 HTTP 客户端：自己管 cookie，也能带自定义头。"""

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.cookies = {}
        self.headers = {}

    def _cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def request(self, method, path, body=None, headers=None):
        url = self.base + path
        data = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if self.cookies:
            hdrs["Cookie"] = self._cookie_header()
        hdrs.update(self.headers)
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                for head in resp.headers.get_all("Set-Cookie") or []:
                    pair = head.split(";", 1)[0]
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        if v:
                            self.cookies[k.strip()] = v.strip()
                        else:
                            self.cookies.pop(k.strip(), None)
                try:
                    return resp.status, json.loads(raw.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    return resp.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode("utf-8"))
            except Exception:  # noqa: BLE001
                return exc.code, raw

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)


def make_png(path):
    from PIL import Image

    Image.new("RGB", (64, 48), (30, 90, 160)).save(path, "PNG")


def main():
    root = tempfile.mkdtemp(prefix="vsw-auto-")
    media = os.path.join(root, "seed.png")
    make_png(media)
    svc = Workbench(root)
    store = svc.store

    # 造两个种子、一批待处理任务：baidu 能自动，tinEye/yandex 是「只能手动」的对照
    with open(media, "rb") as fh:
        raw = fh.read()
    seed_a = svc.import_bytes(raw, title="自动探针甲", origin_url="https://example.com/a.png")
    seed_b = svc.import_bytes(raw + b"\0", title="自动探针乙", origin_url="https://example.com/b.png")
    jobs = svc.enqueue_seed(int(seed_a["seed_id"]), engines=["baidu", "so360"],
                         kinds=["full"])
    jobs_b = svc.enqueue_seed(int(seed_b["seed_id"]), engines=["baidu"], kinds=["full"])
    print(f"造了 {len(jobs) + len(jobs_b)} 个队列项")

    print("\nA 挑任务")
    out = svc.auto_candidates(statuses=("pending",), limit=10)
    engines = {item["engine"] for item in out["items"]}
    check("只挑能自动识图的引擎", engines <= {"baidu", "so360"}, str(engines))
    check("候选里带 job_id / 变体 / 种子", all(i["job_id"] and i["variant_id"] for i in out["items"]),
          json.dumps(out["items"][:2], ensure_ascii=False))
    check("limit 生效", len(svc.auto_candidates(statuses=("pending",), limit=1)["items"]) == 1)

    print("\nB 排队与拒绝")
    check("空 job_ids 且没有候选时报错", _fails(lambda: svc.start_auto_batch(
        [999999], statuses=("pending",))), "应该报错但没有")
    check("不存在的任务被跳过", _fails(lambda: svc.start_auto_batch([999999]),
                                       expect="没找到"))

    real_browser = svc.browser_search
    calls = []

    def fake_browser(job_id, timeout_ms=0):
        job = store.job(job_id)
        calls.append(job_id)
        store.set_job_status(int(job_id), "done", note="探针：假装搜到 3 张")
        if len(calls) == 2:
            raise RuntimeError("探针：第二个故意失败")
        return {"ok": True, "new_findings": 3, "duplicates": 1, "failures": []}

    svc.browser_search = fake_browser
    try:
        started = svc.start_auto_batch([int(j) for j in jobs])
        check("开始一批会返回排队条数与任务 id",
              started.get("started") is True and started.get("queued") == len(jobs),
              json.dumps(started.get("job_ids")))
        check("重复点会被拦住", _fails(lambda: svc.start_auto_batch([int(jobs[0])]),
                                   expect="已经有一批"))
        deadline = time.time() + 60
        while time.time() < deadline and svc.auto_state()["running"]:
            time.sleep(0.2)
        state = svc.auto_state()
        check("跑完后 running 变回 False", state["running"] is False, json.dumps(state["phase"]))
        check("done 等于任务数", state["done"] == len(jobs), str(state["done"]))
        check("每个任务都有结果记录", len(state["results"]) == len(jobs), str(len(state["results"])))
        check("中间那个失败没有中断整批",
              [r["ok"] for r in state["results"]] == [True, False] + [True] * (len(jobs) - 2),
              json.dumps([r["ok"] for r in state["results"]]))
        check("失败项带错误原因",
              any("故意失败" in (r["error"] or "") for r in state["results"]),
              json.dumps([r["error"] for r in state["results"]], ensure_ascii=False))
        check("成功项记了新增张数",
              state["results"][0]["new_findings"] == 3, str(state["results"][0]))
        check("auto_state 里带 browser 快照", "browser" in state)

        print("\nC 不能自动的引擎")
        manual_job = None
        for key in ("tinEye", "yandex"):
            try:
                ids = svc.enqueue_seed(int(seed_a["seed_id"]), engines=[key])
                if ids:
                    manual_job = int(ids[0])
                    break
            except ServiceError:
                continue
        if manual_job:
            check("指定不能自动识图的任务会被拒绝", _fails(
                lambda: svc.start_auto_batch([manual_job]), expect="不支持自动识图"))
        else:
            print("  ..  没有可用的「手动引擎」样本，跳过这组")
    finally:
        svc.browser_search = real_browser

    print("\nD 接口与权限")
    auth = AccountStore(default_auth_path(root))
    hub = WorkspaceHub(auth, root)
    proxy = ServiceProxy(svc)
    server = build_server(proxy, "127.0.0.1", 0, auth=auth, hub=hub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        owner = Client(base)
        status, body = owner.post("/api/auth/setup",
                                  {"name": "探针主人", "password": "secret123",
                                   "workspace": "自动工作区"})
        check("建第一个账号 200", status == 200, json.dumps(body)[:120])
        status, body = owner.get("/api/auto/preview?status=pending&limit=3")
        check("预览接口返回 count / items / skipped",
              status == 200 and {"count", "items", "skipped"} <= set(body), json.dumps(body)[:160])
        anon = Client(base)  # 建号之后的这台机器：没有凭证就当外部人
        status, body = anon.get("/api/auto/preview")
        check("建了账号后，没登录看预览要 401", status == 401, f"{status} {json.dumps(body)[:80]}")
        status, body = anon.post("/api/auto/run", {})
        check("建了账号后，没登录不能开跑", status == 401, f"{status} {json.dumps(body)[:80]}")

        status, body = owner.get("/api/auto/state")
        check("状态接口返回 running / done / results",
              status == 200 and {"running", "done", "results", "browser"} <= set(body),
              json.dumps(sorted(body))[:160])

        status, body = owner.post("/api/auth/workspaces", {"name": "旁观", "kind": "shared"})
        ws_id = int(body["workspace"]["id"]) if status == 200 else 0
        guest = Client(base)
        status, body = guest.post("/api/auth/setup",
                                  {"name": "旁观者", "password": "secret123"})
        # 已经有账号了，所以这里走邀请码
        status, body = owner.post(f"/api/auth/workspaces/{ws_id}/invites",
                                  {"role": "view", "hours": 2})
        code = (body.get("invite") or {}).get("code") or ""
        check("主人能发只读邀请码", status == 200 and bool(code), json.dumps(body)[:120])
        viewer = Client(base)
        if code:
            status, body = viewer.post("/api/auth/register",
                                       {"code": code, "name": "只读的人", "password": "secret123"})
            check("用邀请码注册成功", status == 200, json.dumps(body)[:120])
            status, body = viewer.post("/api/auto/run", {"job_ids": [int(jobs[0])]})
            check("只读身份开跑被拒（403）", status == 403, f"{status} {json.dumps(body)[:80]}")
            status, _ = viewer.get("/api/auto/state")
            check("只读身份可以看进度（200）", status == 200, str(status))
    finally:
        server.shutdown()
        server.server_close()
        auth.close()
        hub.close()
        store.close()
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


def _fails(fn, expect=""):
    try:
        fn()
    except ServiceError as exc:
        return True if not expect else expect in str(exc)
    except Exception:  # noqa: BLE001
        return not expect
    return False


if __name__ == "__main__":
    sys.exit(main())
