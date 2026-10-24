"""手机搜的结果要真的进结果板（findings），不是变成种子。

这条链路以前根本不存在：手机点「存进结果板」打的是 /api/mobile/sync 的 imports，
服务端 apply_mobile_sync 走 import_bytes —— 那是建**种子**。结果板列的是 findings，
所以用户点了按钮、提示也出了，结果板里还是空的。
"""
import base64
import io
import os
import shutil
import sys
import tempfile

from _paths import add_repo_to_path

add_repo_to_path()
from PIL import Image

from vsw.service import Workbench, ServiceError

FAILS = []
PASS = [0]


def check(name, ok, detail=""):
    if ok:
        PASS[0] += 1
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} :: {detail}")


def png(color, size=(80, 80)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def data_url(raw):
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def main():
    root = tempfile.mkdtemp(prefix="vsw-mres-")
    try:
        svc = Workbench(root)
        raw = png((10, 20, 30))
        seed = svc.import_bytes(raw, title="原图", kind="test")
        seed_id = int(seed["seed_id"])
        jobs = svc.enqueue_seed(seed_id, engines=["baidu"], kinds=["full"])
        rows = jobs if isinstance(jobs, list) else (jobs.get("created") or jobs.get("jobs") or [])
        first = rows[0] if rows else 0
        job_id = int(first.get("id") or 0) if isinstance(first, dict) else int(first or 0)
        if not job_id:
            print("建不出队列项：", jobs)
            return 1

        print("A 收进结果板")
        r1 = svc.add_mobile_results({"job_id": job_id, "items": [
            {"image_url": data_url(png((200, 30, 40))), "source_url": "https://example.com/p/1", "title": "橘猫"},
        ]})
        check("第一张被收下", r1["new_findings"] == 1, str(r1))
        rows = svc.store.query("SELECT id, title FROM findings WHERE job_id=? AND COALESCE(dup_of,0)=0", (job_id,))
        check("findings 里真的有这条（结果板能列出来）", len(rows) == 1, f"{len(rows)} 行")
        check("保留识别结论当标题", any((r["title"] or "") == "橘猫" for r in rows), str([r["title"] for r in rows]))
        srcs = svc.store.query("SELECT url FROM sources WHERE finding_id=?", (int(rows[0]["id"]),)) if rows else []
        check("来源页记下来了", any("example.com/p/1" in (s["url"] or "") for s in srcs), str([s["url"] for s in srcs]))

        print("B 重复的不重复记")
        r2 = svc.add_mobile_results({"job_id": job_id, "items": [
            {"image_url": data_url(png((200, 30, 40))), "source_url": "https://example.com/p/1"},
        ]})
        check("同一张再收一次不会多出一条", r2["new_findings"] == 0 and r2["duplicates"] >= 1, str(r2))

        print("B2 同一张图的多个尺寸地址只算重复")
        # 真机上这类「同一张首页样图八个尺寸 URL」被当成 8 个对象收进结果板过，
        # 所以这里专门钉住：只差尺寸参数/后缀的地址必须归一成同一个键。
        r4 = svc.add_mobile_results({"job_id": job_id, "items": [
            {"image_url": "https://mms-graph.cdn.bcebos.com/home-beer.jpg~tplv-abc123", "source_url": "https://example.com/a"},
            {"image_url": "https://mms-graph.cdn.bcebos.com/home-beer.jpg", "source_url": "https://example.com/a"},
            {"image_url": "https://mms-graph.cdn.bcebos.com/home-beer.jpg?x-oss-process=image/resize,w_500", "source_url": "https://example.com/a"},
        ]})
        check("尺寸不同的同一张只下载一次", r4["new_findings"] <= 1, str(r4))
        check("另外两笔算重复", r4["duplicates"] >= 2, str(r4))

        print("C 队列项状态")
        job = svc.store.job(job_id)
        check("队列项进入「待复查」（说明这次搜索有货）", (job or {}).get("status") in ("review", "done"), str((job or {}).get("status")))

        print("D 边界")
        try:
            svc.add_mobile_results({"job_id": 0, "items": [{"image_url": "data:image/png;base64,AA"}]})
            check("缺队列项 id 会被拒", False, "居然过了")
        except ServiceError as exc:
            check("缺队列项 id 会被拒", "队列项" in str(exc), str(exc))
        try:
            svc.add_mobile_results({"job_id": job_id, "items": []})
            check("空列表会被拒", False, "居然过了")
        except ServiceError as exc:
            check("空列表会被拒", True, str(exc))
        r3 = svc.add_mobile_results({"job_id": job_id, "items": [{"image_url": "data:image/png;base64,bm90LWFuLWltYWdl", "source_url": "https://x/1"}]})
        check("坏图只记失败，不塞空对象", r3["new_findings"] == 0 and r3["failures"], str(r3["failures"])[:120])
        try:
            svc.store.close()
        except Exception:
            pass
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n结果：{PASS[0]}/{PASS[0] + len(FAILS)} 通过")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
