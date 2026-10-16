"""Web UI backend for the Visual Search Workbench.

A dependency-free HTTP server (stdlib only, like the rest of this workspace's tools):
JSON API under ``/api``, static single-page UI at ``/``, media at ``/media``.
The heavy lifting lives in :mod:`vsw.service`; this layer only routes.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import engines as engines_mod
from . import imaging
from .service import ServiceError, Workbench

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 64 * 1024 * 1024


def _json_default(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    return str(value)


# 路由处理器是普通函数，self 是 Router 而不是 HTTP handler，所以拿不到监听端口。
# 用一个小绑定表记下真实端口；build_server 建完 server 后写入，main 里也用同一个值。
_BOUND: Dict[str, Any] = {"port": 0}


def bound_port(default: int = 8787) -> int:
    return int(_BOUND.get("port") or default)


class _Raw:
    """Marker for a handler that wants to answer with bytes + an explicit content type."""

    __slots__ = ("data", "content_type")

    def __init__(self, data: bytes, content_type: str):
        self.data = data
        self.content_type = content_type


class Router:
    """Tiny declarative router: ``/api/seed/*`` style patterns, JSON in/out."""

    def __init__(self, service: Workbench):
        self.service = service
        self.routes: List[Tuple[str, re.Pattern, Callable[..., Any]]] = []
        self._register()

    def get(self, pattern: str):
        return self._add("GET", pattern)

    def post(self, pattern: str):
        return self._add("POST", pattern)

    def delete(self, pattern: str):
        return self._add("DELETE", pattern)

    def _add(self, method: str, pattern: str):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            regex = re.compile("^" + pattern.replace("*", "(?P<wild>.*)") + "$")
            self.routes.append((method, regex, func))
            return func
        return decorator

    def dispatch(self, method: str, path: str, query: Dict[str, List[str]],
                 body: Dict[str, Any], raw: bytes) -> Tuple[int, Any]:
        allowed_other = False
        for route_method, regex, handler in self.routes:
            match = regex.match(path)
            if not match:
                continue
            if route_method != method:
                allowed_other = True
                continue
            try:
                return 200, handler(query=query, body=body, raw=raw, **match.groupdict())
            except ServiceError as exc:
                return 400, {"error": str(exc)}
            except ValueError as exc:
                return 400, {"error": f"参数错误：{exc}"}
            except FileNotFoundError as exc:
                return 404, {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                return 500, {"error": f"{type(exc).__name__}: {exc}"}
        if allowed_other:
            return 405, {"error": f"{method} 不支持该接口"}
        return 404, {"error": "未找到该接口", "path": path}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _first(query: Dict[str, List[str]], key: str, default: str = "") -> str:
        values = query.get(key)
        return values[0] if values else default

    def _int(self, query: Dict[str, List[str]], key: str, default: int = 0) -> int:
        raw = self._first(query, key, "")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------- routes
    def _register(self) -> None:
        svc = self.service

        @self.get("/api/health")
        def health(**_kw: Any) -> Dict[str, Any]:
            payload = svc.health()
            payload["browser"] = svc.browser_state()
            payload["node"] = svc._node_path()
            return payload

        @self.get("/api/engines")
        def engines(**_kw: Any) -> Dict[str, Any]:
            return {"engines": svc.engines(), "variant_kinds": svc.variant_kinds()}

        @self.get("/api/settings")
        def get_settings(**_kw: Any) -> Dict[str, Any]:
            return {"settings": svc.settings()}

        @self.post("/api/settings")
        def set_settings(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            return {"settings": svc.update_settings(body.get("settings") or body or {})}

        @self.post("/api/rescore")
        def rescore(**_kw: Any) -> Dict[str, Any]:
            return svc.rescore_all()

        # -------------------------------------------------------------- seeds
        @self.get("/api/seeds")
        def list_seeds(query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            limit = self._int(query, "limit", 300)
            return {"seeds": svc.store.seeds(limit=limit),
                    "stats": svc.store.stats()}

        # 更具体的路径必须排在 /api/seed/* 前面：路由是按注册顺序匹配的，而 /api/seed/*
        # 会把 "/1/image" 整个吞进去（表现为 "invalid literal for int(): '1/image'"）。
        @self.get("/api/seed/*/image")
        def seed_image(wild: str, **_kw: Any) -> Dict[str, Any]:
            """Everything the crop UI needs: the full-size picture and its real dimensions."""
            seed = svc.store.seed(int(wild))
            if not seed:
                raise ServiceError("种子不存在")
            path = svc.abs_media(seed.get("rel_path") or "")
            if not os.path.isfile(path):
                raise ServiceError("种子文件丢失")
            with open(path, "rb") as fh:
                width, height = imaging.image_size(fh.read())
            return {"seed_id": int(wild), "url": f"/{seed['rel_path']}",
                    "width": width, "height": height,
                    "variants": [v for v in svc.store.variants(int(wild))
                                 if v.get("kind") == "crop"]}

        @self.post("/api/seed/*/variants")
        def make_variants(wild: str, body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            kinds = body.get("kinds") or None
            return {"variants": svc.generate_variants(int(wild), kinds=kinds,
                                                      formats=body.get("formats") or ("jpg",))}

        @self.post("/api/seed/*/crop")
        def crop_search(wild: str, body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            """框选搜索：把用户画的那个矩形变成 crop 变体，并可立刻入队/自动识图。

            ``box`` accepts either normalised (0..1) ``x, y, w, h`` or absolute pixels; the
            browser sends normalised values so the client never has to know the original size.
            """
            box = body.get("box") or []
            if len(box) < 4:
                raise ServiceError("需要一个矩形：box=[x, y, w, h]")
            # 默认派发百度：这条路实测能自动跑，所以「仅入队」也应该是可直接运行的队列项，
            # 而不是一堆没有引擎的空变体。
            engines = body.get("engines") or ["baidu"]
            return svc.crop_variant(
                int(wild), box,
                padding=float(body.get("padding") or 0.0),
                scale=float(body.get("scale") or 1.0),
                label=body.get("label") or "",
                engines=engines,
                auto_run=bool(body.get("auto_run")))

        @self.get("/api/seed/*")
        def get_seed(wild: str, **_kw: Any) -> Dict[str, Any]:
            seed_id = int(wild)
            seed = svc.store.seed(seed_id)
            if not seed:
                raise ServiceError("种子不存在")
            return {"seed": seed, "variants": svc.store.variants(seed_id),
                    "jobs": svc.store.jobs(seed_id=seed_id),
                    "findings": svc.store.findings(seed_id=seed_id, limit=200)}

        @self.post("/api/import")
        def do_import(body: Dict[str, Any], raw: bytes, **_kw: Any) -> Dict[str, Any]:
            kind = (body.get("kind") or "file").lower()
            variants = body.get("variants") or None
            engines = body.get("engines") or None
            common = {
                "title": body.get("title") or "",
                "note": body.get("note") or "",
                "variants": variants,
                "auto_enqueue": bool(body.get("auto_enqueue")),
                "engines": engines,
            }
            if kind == "path":
                paths = body.get("paths") or ([body["path"]] if body.get("path") else [])
                if not paths:
                    raise ServiceError("缺少 paths")
                results = []
                for path in paths:
                    results.append(svc.import_path(path, **common))
                return {"imported": results}
            if kind == "directory":
                path = body.get("path") or ""
                if not path:
                    raise ServiceError("缺少 path")
                return svc.import_directory(path, limit=int(body.get("limit") or 500),
                                            recursive=bool(body.get("recursive", True)), **common)
            if kind == "url":
                urls = body.get("urls") or ([body["url"]] if body.get("url") else [])
                if not urls:
                    raise ServiceError("缺少 urls")
                results, errors = [], []
                for url in urls:
                    try:
                        results.append(svc.import_url(url, **common))
                    except Exception as exc:  # noqa: BLE001
                        errors.append({"url": url, "error": str(exc)})
                return {"imported": results, "errors": errors}
            if kind == "upload":
                uploads = body.get("uploads") or []
                if not uploads:
                    raise ServiceError("缺少 uploads（data_url + name）")
                results = []
                import base64
                for item in uploads:
                    data_url = item.get("data_url") or ""
                    if "," not in data_url:
                        continue
                    payload = base64.b64decode(data_url.split(",", 1)[1])
                    item_common = dict(common)
                    item_common["title"] = item.get("name") or common["title"]
                    results.append(svc.import_bytes(payload, **item_common))
                return {"imported": results}
            raise ServiceError(f"未知导入方式：{kind}")

        @self.delete("/api/seed/*")
        def delete_seed(wild: str, **_kw: Any) -> Dict[str, Any]:
            return {"deleted": svc.store.delete_seed(int(wild))}

        # --------------------------------------------------------------- queue
        @self.get("/api/queue")
        def queue(query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            status = self._first(query, "status", "todo")

            def list_param(name: str) -> List[str]:
                raw = self._first(query, name, "")
                return [x.strip() for x in raw.replace(" ", "").split(",") if x.strip()]

            return svc.queue_view(
                status=status,
                seed_id=self._int(query, "seed_id", 0) or None,
                engines=list_param("engines") or None,
                kinds=list_param("kinds") or None,
                text=self._first(query, "q", ""),
                results=self._first(query, "results", ""))

        @self.post("/api/queue/enqueue")
        def enqueue(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            engines = body.get("engines") or ["baidu"]
            kinds = body.get("kinds") or None
            if body.get("seed_ids"):
                job_ids: List[int] = []
                for seed_id in body["seed_ids"]:
                    job_ids.extend(svc.enqueue_seed(int(seed_id), engines=engines, kinds=kinds))
                return {"job_ids": job_ids}
            if body.get("finding_ids"):
                return svc.promote_findings(body["finding_ids"], engines=engines, kinds=kinds,
                                            note=body.get("note") or "")
            if body.get("variant_ids"):
                return {"job_ids": svc.enqueue_variants(body["variant_ids"], engines)}
            raise ServiceError("需要 seed_ids / finding_ids / variant_ids 之一")

        @self.post("/api/job/*/execute")
        def execute(wild: str, body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            out = svc.execute_job(int(wild), public_url=body.get("public_url") or "",
                                  note=body.get("note") or "")
            out["local_file"] = out.get("local_file") or ""
            return out

        @self.post("/api/job/*/browser")
        def browser_search(wild: str, body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            """Run one queue item through the automated headless-browser path.

            The browser run takes tens of seconds, which would blow the HTTP timeout, so it
            starts in the background and the UI polls /api/browser/state.
            """
            job_id = int(wild)
            started = svc.start_browser_search(job_id, timeout_ms=int(body.get("timeout_ms") or 90000))
            return {**started, "state": svc.browser_state()}

        @self.get("/api/browser/state")
        def browser_state(**_kw: Any) -> Dict[str, Any]:
            return svc.browser_state()

        @self.post("/api/job/*/finish")
        def finish(wild: str, body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            return {"job": svc.finish_job(int(wild), status=body.get("status") or "done",
                                          note=body.get("note") or "")}

        @self.delete("/api/job/*")
        def delete_job(wild: str, **_kw: Any) -> Dict[str, Any]:
            svc.store.delete_job(int(wild))
            return {"ok": True}

        @self.post("/api/queue/clear")
        def clear_queue(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            statuses = body.get("statuses") or ["pending"]
            return {"removed": svc.store.clear_jobs(statuses)}

        # -------------------------------------------------------------- ingest
        @self.post("/api/job/*/ingest")
        def ingest(wild: str, body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            return svc.ingest_payload(
                int(wild),
                payload=body.get("payload") or "",
                page_url=body.get("page_url") or "",
                candidates=body.get("candidates") or None,
                download=bool(body.get("download", True)),
                fetch_limit=int(body.get("fetch_limit") or 60),
                note=body.get("note") or "")

        @self.post("/api/parse")
        def parse(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            return svc.parse_result_payload(body.get("payload") or "",
                                            engine=body.get("engine") or "")

        # ------------------------------------------------------------ findings
        @self.get("/api/findings")
        def findings(query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            archived = self._first(query, "archived", "0")
            return {"findings": svc.store.findings(
                triage=self._first(query, "triage", "all"),
                status=self._first(query, "status", "all") or "all",
                cluster_id=self._int(query, "cluster_id", 0) or None,
                seed_id=self._int(query, "seed_id", 0) or None,
                include_dups=self._first(query, "dups", "") == "1",
                limit=self._int(query, "limit", 300),
                offset=self._int(query, "offset", 0),
                archived=(None if archived == "all" else archived == "1"),
                order=self._first(query, "order", "value"))}

        # ---- 入库归档（不是删除：可浏览、可恢复、来源照旧可追）--------------------
        @self.get("/api/mobile/qr.png")
        def mobile_qr(**_kw: Any) -> Any:
            """把当前局域网地址画成二维码（手机端设置页直接显示它，导入快照时再扫一次）。"""
            address = lan_addresses()
            host = address[0] if address else "127.0.0.1"
            url = f"http://{host}:{bound_port()}/m/"
            try:
                import io as _io  # noqa: PLC0415
                import qrcode  # noqa: PLC0415
                # PNG 而不是 SVG：<img> 对 PNG 的支持最稳（iOS Safari 上 SVG 有时不出图）
                image = qrcode.make(url, border=2)
                buffer = _io.BytesIO()
                image.save(buffer, format="PNG")
                return _Raw(buffer.getvalue(), "image/png")
            except ImportError:
                return {"url": url, "note": "未安装 qrcode，无法生成二维码（pip install qrcode）"}

        @self.post("/api/repair/source-labels")
        def repair_labels(**_kw: Any) -> Dict[str, Any]:
            """修掉历史数据里被错贴的「引擎结果页」标题（可反复调用，幂等）。"""
            return svc.repair_source_labels()

        # ---- 手机端 ------------------------------------------------------------
        @self.get("/api/mobile/snapshot")
        def mobile_snapshot(query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            """给手机端打一份可离线使用的快照（纯文本 JSON，图片走独立静态路由）。"""
            mode = self._first(query, "images", "thumbs")
            return svc.mobile_snapshot(
                thumb_side=self._int(query, "thumb", 240),
                findings_limit=self._int(query, "limit", 800),
                include_trash=self._first(query, "trash", "1") == "1",
                include_archives=self._first(query, "archives", "1") == "1",
                image_mode=mode,
                with_sources=self._first(query, "sources", "0") == "1")

        @self.post("/api/mobile/sync")
        def mobile_sync(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            """手机端把离线期间攒下的操作推回来。

            目前支持两类：``imports``（手机上拍/选的图，含裁剪后的字节，base64）和
            ``trash``（手机上删掉的对象 id）。手机跑不了无头浏览器，所以队列类操作
            （自动识图）不在这里，由手机端在联网时直接调既有接口。
            """
            return svc.apply_mobile_sync(body or {})

        @self.get("/api/archives")
        def archives(**_kw: Any) -> Dict[str, Any]:
            return svc.archives()

        @self.get("/api/archives/preview")
        def archive_preview(query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            """`ids=3,7,9` 时只预览这批（对应「只归档选中的图片」）。"""
            raw = self._first(query, "ids", "")
            ids = [int(x) for x in raw.replace(" ", "").split(",") if x.strip().isdigit()]
            return svc.archive_scope_preview(ids if raw else None)

        @self.post("/api/archives")
        def archive_create(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            ids = body.get("finding_ids") or None
            return svc.archive_now(name=body.get("name") or "", note=body.get("note") or "",
                                   finding_ids=[int(i) for i in ids] if ids else None)

        @self.get("/api/archive/*")
        def archive_detail(wild: str, query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            return svc.archive_detail(int(wild), limit=self._int(query, "limit", 300))

        @self.post("/api/archives/unarchive")
        def archive_restore(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            archive_id = body.get("archive_id")
            return svc.unarchive(int(archive_id) if archive_id else None)

        # ---- 回收站（软删除，可恢复；只有这里能把文件真正删掉）------------------
        @self.get("/api/findings/trash")
        def trash_list(query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            return svc.trash_view(limit=self._int(query, "limit", 200),
                                  offset=self._int(query, "offset", 0))

        @self.post("/api/findings/trash")
        def trash_push(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            ids = body.get("finding_ids") or []
            only_unpromoted = bool(body.get("only_unpromoted"))
            if not ids and not body.get("sweep"):
                raise ServiceError("需要 finding_ids，或用 sweep=true 清理全部未选为种子的结果")
            if body.get("sweep"):
                return svc.trash_unpromoted(reason=body.get("reason") or "sweep")
            return svc.trash_findings(ids, reason=body.get("reason") or "manual",
                                      only_unpromoted=only_unpromoted)

        @self.post("/api/findings/trash/restore")
        def trash_restore(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            if body.get("all"):
                return svc.restore_all()
            ids = body.get("finding_ids") or []
            if not ids:
                raise ServiceError("需要 finding_ids，或用 all=true 全部恢复")
            return svc.restore_findings(ids)

        @self.post("/api/findings/trash/purge")
        def trash_purge(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            if not body.get("confirm"):
                raise ServiceError("彻底删除不可恢复，需要 confirm=true")
            if body.get("all"):
                return svc.purge_all()
            ids = body.get("finding_ids") or []
            if not ids:
                raise ServiceError("需要 finding_ids，或用 all=true 清空回收站")
            return svc.purge_findings(ids)

        @self.get("/api/finding/*")
        def finding(wild: str, **_kw: Any) -> Dict[str, Any]:
            finding_id = int(wild)
            record = svc.store.finding(finding_id)
            if not record:
                raise ServiceError("结果不存在")
            return {"finding": record, "sources": svc.store.sources(finding_id=finding_id),
                    "cluster": svc.store.cluster(int(record["cluster_id"] or 0)),
                    "similar": svc.similar_findings(finding_id, limit=18)["results"],
                    "chain": svc.search_chain(finding_id)}

        @self.post("/api/finding/*")
        def update_finding(wild: str, body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            return {"finding": svc.store.update_finding(int(wild), **body)}

        @self.delete("/api/finding/*")
        def delete_finding(wild: str, **_kw: Any) -> Dict[str, Any]:
            svc.store.delete_finding(int(wild))
            return {"ok": True}

        @self.post("/api/findings/promote")
        def promote(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            ids = body.get("finding_ids") or []
            if not ids:
                raise ServiceError("缺少 finding_ids")
            return svc.promote_findings(ids, engines=body.get("engines") or ["baidu"],
                                        kinds=body.get("kinds") or None,
                                        depth_from=body.get("depth_from"),
                                        note=body.get("note") or "")

        # --------------------------------------------------------------- search
        @self.post("/api/search/semantic")
        def semantic(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            return svc.semantic_search(body.get("query") or "", limit=int(body.get("limit") or 60))

        @self.get("/api/clusters")
        def clusters(**_kw: Any) -> Dict[str, Any]:
            return {"clusters": svc.store.clusters()}

        @self.get("/api/graph")
        def graph(**_kw: Any) -> Dict[str, Any]:
            return svc.graph()

        @self.get("/api/report")
        def report(**_kw: Any) -> Dict[str, Any]:
            return svc.report()

        @self.post("/api/reindex")
        def reindex(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            return svc.reindex_missing(limit=int(body.get("limit") or 500))

        @self.get("/api/reindex")
        def reindex_state(**_kw: Any) -> Dict[str, Any]:
            return svc.reindex_state()

        @self.post("/api/ledger")
        def log_manual(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            op_id = svc.store.log_op(engine=body.get("engine") or "manual",
                                     seed_id=int(body.get("seed_id") or 0),
                                     variant_id=int(body.get("variant_id") or 0),
                                     op_kind=body.get("op_kind") or "manual",
                                     findings_new=int(body.get("findings_new") or 0),
                                     dup_images=int(body.get("dup_images") or 0),
                                     new_sources=int(body.get("new_sources") or 0),
                                     new_platforms=int(body.get("new_platforms") or 0),
                                     note=body.get("note") or "")
            return {"op_id": op_id}

        @self.get("/api/ledger")
        def ledger(query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            return {"ledger": svc.store.ledger(limit=self._int(query, "limit", 200))}


class Handler(BaseHTTPRequestHandler):
    server_version = "VisualSearchWorkbench/1.0"
    protocol_version = "HTTP/1.1"

    router: Router
    service: Workbench
    root: str

    # ------------------------------------------------------------------ plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
        if os.environ.get("VSW_VERBOSE"):
            sys.stderr.write("[http] " + fmt % args + "\n")

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8",
              extra: Optional[Dict[str, str]] = None) -> None:
        if isinstance(payload, _Raw):
            content_type, payload = payload.content_type, payload.data
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # 手机端从 http://<电脑IP>:8799/m/ 打开时是同源；留 CORS 头是为了让
        # 「拖到手机上离线运行」的那份文件（file:// 或别的源）也能回连同一条 API。
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _query(self) -> Dict[str, List[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _read_body(self) -> Tuple[Dict[str, Any], bytes]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}, b""
        if length > MAX_BODY:
            raise ServiceError("请求体过大")
        raw = self.rfile.read(length)
        content_type = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            try:
                return json.loads(raw.decode("utf-8") or "{}"), raw
            except json.JSONDecodeError as exc:
                raise ServiceError(f"JSON 解析失败：{exc}") from None
        if "application/x-www-form-urlencoded" in content_type:
            parsed = urllib.parse.parse_qs(raw.decode("utf-8", "ignore"))
            return {k: v[0] for k, v in parsed.items()}, raw
        return {}, raw

    # ------------------------------------------------------------------ methods
    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path.startswith("/api/"):
            status, payload = self.router.dispatch("GET", path, self._query(), {}, b"")
            self._send(status, payload)
            return
        if path.startswith("/media/"):
            self._serve_media(path)
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        try:
            body, raw = self._read_body()
        except ServiceError as exc:
            self._send(400, {"error": str(exc)})
            return
        if path.startswith("/api/"):
            status, payload = self.router.dispatch("POST", path, self._query(), body, raw)
            self._send(status, payload)
            return
        self._send(404, {"error": "未知路径"})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        status, payload = self.router.dispatch("DELETE", path, self._query(), {}, b"")
        self._send(status, payload)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -------------------------------------------------------------------- static
    def _serve_static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            target = os.path.join(STATIC_DIR, "index.html")
        elif path == "/m" or path == "/m/" or path.startswith("/m/"):
            # 手机端（PWA）住在 static/mobile/ 下，但对外挂在 /m/ 前缀上。
            # 只处理 /m 和 /m/… 这两种形状，别把 /media/… 之类的路径也吞进来。
            tail = path[3:] if path.startswith("/m/") else ""
            if tail in ("", "index.html"):
                target = os.path.join(STATIC_DIR, "mobile", "index.html")
            else:
                target = os.path.abspath(os.path.join(STATIC_DIR, "mobile",
                                                      tail.replace("/", os.sep)))
                if not target.startswith(os.path.abspath(os.path.join(STATIC_DIR, "mobile"))):
                    self._send(403, {"error": "越界"})
                    return
        else:
            relative = path.lstrip("/").replace("/", os.sep)
            target = os.path.abspath(os.path.join(STATIC_DIR, relative))
            if not target.startswith(os.path.abspath(STATIC_DIR)):
                self._send(403, {"error": "越界"})
                return
        if not os.path.isfile(target):
            self._send(404, {"error": "文件不存在", "path": path})
            return
        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if target.endswith(".webmanifest"):
            # 有些 Windows 注册表里没有这一项，guess_type 会给出 None/octet-stream，
            # 浏览器就拒绝把它当 manifest 用（PWA 装不上）。
            content_type = "application/manifest+json"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        with open(target, "rb") as fh:
            self._send(200, fh.read(), content_type=content_type)

    def _serve_media(self, path: str) -> None:
        base = os.path.abspath(self.service.media_root)
        relative = urllib.parse.unquote(path[len("/media/"):]).replace("\\", "/")
        # Stored paths are data-root relative ("media/thumbs/x.jpg"), so accept a leading
        # "media/" and also fall back to interpreting the tail straight under the media root.
        candidates = []
        if relative.lower().startswith("media/"):
            candidates.append(os.path.join(base, relative[len("media/"):].replace("/", os.sep)))
        candidates.append(os.path.join(base, relative.replace("/", os.sep)))
        data_root = os.path.abspath(self.service.root)
        candidates.append(os.path.join(data_root, relative.replace("/", os.sep)))
        target = ""
        for candidate in candidates:
            resolved = os.path.abspath(candidate)
            if resolved.startswith(base) and os.path.isfile(resolved):
                target = resolved
                break
        if not target:
            self._send(404, {"error": "媒体不存在", "path": path})
            return
        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            data = fh.read()
        self._send(200, data, content_type=content_type,
                   extra={"Cache-Control": "public, max-age=3600"})

    # ------------------------------------------------------------------- serving
    def handle_one_request(self) -> None:  # keep-alive tolerant
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True


def build_server(service: Workbench, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    router = Router(service)

    class BoundHandler(Handler):
        pass

    BoundHandler.router = router  # type: ignore[attr-defined]
    BoundHandler.service = service  # type: ignore[attr-defined]
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.daemon_threads = True
    _BOUND["port"] = server.server_address[1]
    return server


def create_service(root: str, *, allow_private: bool = False,
                   log: Optional[Callable[[str], None]] = None) -> Workbench:
    """Build the workbench behind the web UI (shared by :func:`main` and tests)."""
    os.makedirs(root, exist_ok=True)
    return Workbench(root, log=log or (lambda msg: print(f"[vsw] {msg}")),
                     allow_private=allow_private)


def pick_port(host: str, preferred: int, tries: int = 20) -> int:
    for offset in range(tries):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise ServiceError(f"{host} 上 {preferred}~{preferred + tries} 端口都被占用")


def _is_cgnat_or_vpn(addr: str) -> bool:
    """Tailscale/部分 VPN 用 100.64.0.0/10（CGNAT）。手机连不上这类地址。"""
    try:
        first, second = (int(x) for x in addr.split(".")[:2])
    except (ValueError, IndexError):
        return False
    return first == 100 and 64 <= second <= 127


def _usable_lan_ip(addr: str) -> bool:
    """排除明显不是"手机能连上的那个网卡"的地址。

    踩过的坑：本机的虚拟网卡/测速代理可能持有 198.18.0.0/15（RFC 2544 保留段）
    或 Tailscale 的 100.64/10，它们会排在真实 WiFi 网卡前面，打印出来的二维码扫了连不上。
    """
    if not addr or addr.startswith("127.") or addr.startswith("169.254."):
        return False
    if addr.startswith("198.18.") or addr.startswith("198.19."):   # 基准测试保留段
        return False
    if _is_cgnat_or_vpn(addr):
        return False
    if addr.startswith("0.") or addr == "255.255.255.255":
        return False
    return True


def _private_rank(addr: str) -> int:
    """家用/办公局域网常见网段优先 —— 手机多半就在这个网段里。

    ``192.168.*`` 最优先（绝大多数家用路由），其次 ``10.*``，再其次 ``172.16-31.*``。
    不在私有段的（例如公网直连或公司复杂网络）排最后。
    """
    try:
        parts = [int(x) for x in addr.split(".")]
        if len(parts) != 4:
            return 9
    except ValueError:
        return 9
    a, b = parts[0], parts[1]
    if a == 192 and b == 168:
        return 0
    if a == 10:
        return 1
    if a == 172 and 16 <= b <= 31:
        return 2
    return 3


def _adapter_ips() -> List[str]:
    """读网卡列表（Windows 用 ipconfig，其它平台退回 hostname 解析）。"""
    found: List[str] = []
    try:
        import subprocess  # noqa: PLC0415
        if os.name == "nt":
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=8,
                                 encoding="utf-8", errors="ignore").stdout
            for line in out.splitlines():
                if "IPv4" in line and ":" in line:
                    # ipconfig 的尾巴可能是 "(首选)" / "(Preferred)"，按后缀切而不是 rstrip
                    value = line.split(":", 1)[1].strip()
                    for suffix in ("(首选)", "(Preferred)", "(Deprecated)", "(已弃用)"):
                        if value.endswith(suffix):
                            value = value[: -len(suffix)].strip()
                    if value and value not in found:
                        found.append(value)
    except Exception:  # noqa: BLE001
        pass
    if not found:
        try:
            import socket as _socket
            for info in _socket.getaddrinfo(_socket.gethostname(), None, _socket.AF_INET):
                addr = info[4][0]
                if addr not in found:
                    found.append(addr)
        except OSError:
            pass
    return found


def lan_addresses() -> List[str]:
    """本机在局域网里的可用 IPv4 地址，**按"手机最可能连得上"排序**。

    排序依据（踩过坑才知道重要性）：
      1. 家用私有网段优先（192.168 > 10 > 172.16-31）；
      2. 同网段地址靠前；
      3. 排除回环 / 链路本地 / 基准测试保留段 / Tailscale 之类 CGNAT 地址。

    只打印第一个是不够的：这台机器上同时有 WLAN、以太网、Docker、Tailscale，
    手机在哪个网段取决于它连的 WiFi，所以启动日志会把候选地址都列出来。
    """
    seen: List[str] = []
    try:
        import socket as _socket
        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            routed = probe.getsockname()[0]
            if _usable_lan_ip(routed) and _private_rank(routed) < 3:
                seen.append(routed)      # 真的能通外网的那个排最前（除非它是 VPN 地址）
        finally:
            probe.close()
    except OSError:
        pass
    for addr in _adapter_ips() + _hostname_ips():
        if _usable_lan_ip(addr) and addr not in seen:
            seen.append(addr)
    # 默认路由出口（真能通外网的那个）永远第一，其余按"家用网段"排序
    if seen:
        head = seen[0]
        rest = sorted(seen[1:], key=_private_rank)
        seen = [head] + rest
    return seen


def _hostname_ips() -> List[str]:
    try:
        import socket as _socket
        return [info[4][0] for info in _socket.getaddrinfo(_socket.gethostname(), None,
                                                           _socket.AF_INET)]
    except OSError:
        return []


def print_phone_hint(host: str, port: int) -> None:
    """把手机访问方式打在启动日志里，并尽量显示一个可扫的二维码。"""
    if host in ("0.0.0.0", "::"):
        addresses = lan_addresses()
    else:
        addresses = [host]
    if not addresses:
        return
    print("[vsw] 手机端（同一 WiFi 下打开）：")
    for index, addr in enumerate(addresses):
        url = f"http://{addr}:{port}/m/"
        mark = "  ← 二维码用这个" if index == 0 else ""
        print(f"[vsw]   {url}{mark}")
    if len(addresses) > 1:
        print("[vsw]   这台机器有多个网卡，手机扫上面的码连不上时，把其它地址手打进去试试")
    if host in ("127.0.0.1", "localhost"):
        print("[vsw]   ⚠ 当前只监听本机，手机连不上；加 --host 0.0.0.0 再启动")
    _print_qr(f"http://{addresses[0]}:{port}/m/")


def _print_qr(url: str) -> None:
    """用纯 ASCII 画一个二维码，省得手机上手打 URL。找不到库就安静跳过。"""
    try:
        import qrcode  # type: ignore
    except ImportError:
        print("[vsw]   （装个 qrcode 就能在这里直接扫码：pip install qrcode）")
        return
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        # 半块字符：一行塞两个像素高度，手机屏幕拍得清、终端也不至于太长
        for row in range(0, len(matrix), 2):
            line = ""
            for col in range(len(matrix[row])):
                top = matrix[row][col]
                bottom = matrix[row + 1][col] if row + 1 < len(matrix) else False
                line += {(True, True): "█", (True, False): "▀",
                         (False, True): "▄", (False, False): " "}[(top, bottom)]
            print("[vsw] " + line)
    except Exception:  # noqa: BLE001
        pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="横向视觉搜索工作台 WebUI")
    parser.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
                        help="数据目录（SQLite + 媒体）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址；手机要访问就填 0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--allow-private", action="store_true",
                        help="允许抓取内网/回环地址的图片（只在本地自测时用；默认拒绝，"
                             "防止工作台被诱导去探测内网）")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    os.makedirs(root, exist_ok=True)
    print(f"[vsw] 数据目录：{root}")
    service = Workbench(root, log=lambda msg: print(f"[vsw] {msg}"),
                        allow_private=args.allow_private)

    port = pick_port(args.host, args.port)
    server = build_server(service, args.host, port)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{port}/"
    print(f"[vsw] 工作台已启动：{url}")
    ok, reason = service.vision.available()
    print(f"[vsw] 语义引擎：{'就绪 ' + service.vision.model_dir if ok else '不可用 - ' + reason}")
    print_phone_hint(args.host, port)
    if not args.no_browser and args.host not in ("0.0.0.0", "::"):
        threading.Thread(target=lambda: (__import__("time").sleep(0.6), webbrowser.open(url)),
                         daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[vsw] 已停止")
    finally:
        server.server_close()
        service.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
