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
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import engines as engines_mod
from . import imaging
from . import release as release_mod
from . import tunnel as tunnel_mod
from .access import (AccessStore, ROLE_EDIT, ROLE_LABEL, ROLE_OWNER, ROLE_RANK,
                     ROLE_VIEW, ensure_owner_key, looks_like_code)
from .accounts import (AccountError, AccountStore, SESSION_COOKIE, SESSION_DAYS,
                       WORKSPACE_COOKIE, default_auth_path, public_user)
from .hub import WorkspaceHub
from .service import ServiceError, Workbench

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 64 * 1024 * 1024

ACCESS_COOKIE = "vsw_key"
COOKIE_MAX_AGE = 30 * 24 * 3600

# 把工作台开放给别人时，哪些动作必须是"主人"。
# 原则：**不可逆的、动到这台机器的、改全局口径的**，一律主人。
_OWNER_WRITES = (
    "/api/reveal",              # 在电脑上打开资源管理器（动的是这台机器）
    "/api/findings/trash/purge",  # 彻底删除文件（不可逆）
    "/api/queue/clear",         # 清空队列
    "/api/reindex",             # 重建索引
    "/api/rescore",             # 重算发散度
    "/api/settings",            # 改阈值/权重（全局口径）
    "/api/tunnel",              # 开关公网通道（等于决定这份数据给谁看）
)
_OWNER_DELETES = ("/api/finding/",)   # 删一条结果


def required_role(method: str, path: str) -> Optional[str]:
    """这条请求至少需要什么角色。``None`` = 不需要密钥（健康检查之类）。

    读一律 ``view``；写一律 ``edit``；上面那张表里的是 ``owner``。
    """
    if path in ("/api/ping", "/api/health"):
        return None
    # 应用自身的版本信息与安装包：不含任何用户数据，而且手机在登录之前就要知道要不要更新。
    if path in ("/api/app/version", "/api/app/apk"):
        return None
    # 页面壳子（index.html / app.js / app.css / 图标 / 手机端单文件）必须能加载：
    # 解锁框就长在这些文件里。它们不含任何数据，真正的数据全在 /api 与 /media 后面。
    if not (path.startswith("/api/") or path.startswith("/media/")):
        return None
    if method in ("GET", "HEAD", "OPTIONS"):
        return ROLE_VIEW
    if method == "DELETE":
        for prefix in _OWNER_DELETES:
            if path.startswith(prefix):
                return ROLE_OWNER
        return ROLE_EDIT
    for prefix in _OWNER_WRITES:
        if path.startswith(prefix):
            return ROLE_OWNER
    return ROLE_EDIT


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

    __slots__ = ("data", "content_type", "headers")

    def __init__(self, data: bytes, content_type: str,
                 headers: Optional[Dict[str, str]] = None):
        self.data = data
        self.content_type = content_type
        self.headers = headers or {}


class ServiceProxy:
    """按请求切换的工作区门面。

    路由是在启动时一次性注册的，处理器闭包里抓的是**一个** ``service``。
    有了工作区之后同一个进程要服务好几套数据，所以这里放一个薄代理：
    处理器照旧写 ``svc.store.seeds(...)``，真正落到哪个工作区由每个请求先 ``bind()``。

    ThreadingHTTPServer 是一请求一线程，所以用 ``threading.local`` 就够隔离。
    """

    def __init__(self, fallback: Workbench) -> None:
        object.__setattr__(self, "_local", threading.local())
        object.__setattr__(self, "_fallback", fallback)

    def bind(self, service: Optional[Workbench]) -> Optional[Workbench]:
        self._local.service = service or self._fallback
        return self._local.service

    def current(self) -> Workbench:
        return getattr(self._local, "service", None) or self._fallback

    def __getattr__(self, name: str) -> Any:
        return getattr(self.current(), name)


class Router:
    """Tiny declarative router: ``/api/seed/*`` style patterns, JSON in/out."""

    def __init__(self, service: Any, gh_repo: str = ""):
        self.service = service
        self.routes: List[Tuple[str, re.Pattern, Callable[..., Any]]] = []
        self.gh_repo = gh_repo.strip()
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
            self.routes.append((method, regex, func, "*" in pattern))
            return func
        return decorator

    def dispatch(self, method: str, path: str, query: Dict[str, List[str]],
                 body: Dict[str, Any], raw: bytes) -> Tuple[int, Any]:
        allowed_other = False
        # 字面路由优先于通配路由：`/api/archive/merge/zip` 不能被 `/api/archive/*/zip`
        # 先抢走（真踩过：通配把 "merge" 当成 id，接口直接 400）。所以先只跑不带 * 的路由，
        # 没命中再跑带 * 的。
        for literal_only in (True, False):
            for route_method, regex, handler, has_wild in self.routes:
                if has_wild == literal_only:
                    continue
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

    @staticmethod
    def _ids(query: Dict[str, List[str]]) -> List[int]:
        raw = (query.get("ids") or [""])[0]
        out: List[int] = []
        for part in str(raw).replace(" ", "").split(","):
            if part.isdigit():
                out.append(int(part))
        if not out:
            raise ServiceError("需要 ids=1,2,3 指定批次")
        return out

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

        # ------------------------------------------------------ 批量自动搜图
        # 手机上没有浏览器内核可以替它上传文件，所以「自动搜图」是手机下命令、这台机器
        # 的无头浏览器干活。排队与串行都在服务端，手机可以关屏走人，回来再看进度。
        @self.get("/api/auto/preview")
        def auto_preview(query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            statuses = tuple(x for x in self._first(query, "status", "pending,review").split(",") if x)
            engines = [x for x in self._first(query, "engine", "").split(",") if x] or None
            return svc.auto_candidates(statuses=statuses, engines=engines,
                                       limit=int(self._first(query, "limit", "12") or 12))

        @self.get("/api/auto/state")
        def auto_state(**_kw: Any) -> Dict[str, Any]:
            return svc.auto_state()

        @self.post("/api/auto/run")
        def auto_run(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            job_ids = [int(x) for x in (body.get("job_ids") or [])]
            statuses = tuple(body.get("statuses") or ("pending", "review"))
            engines = body.get("engines") or None
            return svc.start_auto_batch(job_ids or None, statuses=statuses, engines=engines,
                                        limit=int(body.get("limit") or 12),
                                        timeout_ms=int(body.get("timeout_ms") or 90000))

        # ---------------------------------------------------------- 应用自我更新
        # 手机端问「有没有新版本」，再决定要不要下载安装包。两个接口都不需要登录：
        # 版本号与二进制不是用户数据，而且手机在登录之前就该知道要不要更新。
        @self.get("/api/app/version")
        def app_version(query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            current = self._first(query, "build", "") or self._first(query, "current", "")
            root = getattr(self.service, "root", "") or ""
            info = release_mod.latest(root, gh_repo=self.gh_repo, current=current)
            return info

        @self.get("/api/app/apk")
        def app_apk(**_kw: Any) -> Any:
            root = getattr(self.service, "root", "") or ""
            path = release_mod.apk_path(root)
            if not path:
                raise ServiceError("这台机器上没有放安装包（data/dist/vsw-workbench.apk）")
            with open(path, "rb") as fh:
                data = fh.read()
            name = f"vsw-workbench-{release_mod.local_release(root).get('build') or 'latest'}.apk"
            return _Raw(data, release_mod.APK_MIME, {
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": "no-store",
                "X-VSW-Build": str(release_mod.local_release(root).get("build") or ""),
            })

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

        @self.post("/api/queue/delete")
        def delete_queue_items(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            """批量删除队列项（手机端「删除选中 / 删除筛选结果」用）。

            传 job_ids 就删指定的那些；传 statuses 则按状态清空（等价于 /api/queue/clear）。
            两者都给时先删 id、再按状态清，返回删掉的总数。
            """
            ids = [int(i) for i in (body.get("job_ids") or [])]
            statuses = body.get("statuses") or []
            if not ids and not statuses:
                raise ServiceError("需要 job_ids，或 statuses 指定要清空的状态")
            removed = svc.store.delete_jobs(ids) if ids else 0
            if statuses:
                removed += svc.store.clear_jobs(statuses)
            return {"removed": removed, "ids": ids[:50]}

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
        @self.get("/api/variant/*/thumb")
        def variant_thumb(wild: str, query: Dict[str, List[str]], **_kw: Any) -> Any:
            """变体缩略图（按需生成 + 磁盘缓存），给手机队列页显示"这条派发的是哪个变体"。

            变体表只有原尺寸图片，没有缩略图；队列动辄几百条，下原图太重，所以现生成小图。
            """
            payload = svc.variant_thumb(int(wild), box=self._int(query, "box", 320))
            return _Raw(payload, "image/jpeg")

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
                with_sources=self._first(query, "sources", "0") == "1",
                include_archived=self._first(query, "archived", "1") == "1")

        @self.post("/api/mobile/sync")
        def mobile_sync(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            """手机端把离线期间攒下的操作推回来。

            目前支持两类：``imports``（手机上拍/选的图，含裁剪后的字节，base64）和
            ``trash``（手机上删掉的对象 id）。手机跑不了无头浏览器，所以队列类操作
            （自动识图）不在这里，由手机端在联网时直接调既有接口。
            """
            return svc.apply_mobile_sync(body or {})

        @self.post("/api/mobile/results")
        def mobile_results(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            """手机「手机搜」抓到的相似图 → 收进结果板（findings），不再是种子。"""
            return svc.add_mobile_results(body or {})

        @self.get("/api/archive/*/zip")
        def archive_zip(wild: str, **_kw: Any) -> Any:
            """把归档批次打包成 zip —— 手机端「保存到手机」下载的就是它。"""
            info = svc.archive_zip(int(wild))
            with open(info["path"], "rb") as fh:
                payload = fh.read()
            # 文件名里可能有中文，HTTP 头只能是 latin-1：ASCII 兜底 + RFC 5987 的 filename*
            ascii_name = f"archive-{int(wild)}.zip"
            quoted = urllib.parse.quote(info["name"], safe="")
            return _Raw(payload, "application/zip", {
                "Content-Disposition":
                    f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'})

        @self.get("/api/archive/merge")
        def archive_merge(query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            """合并多个归档批次看（合并相册）。ids=1,2,3"""
            ids = self._ids(query)
            return svc.merge_info(ids)

        @self.get("/api/archive/merge/zip")
        def archive_merge_zip(query: Dict[str, List[str]], **_kw: Any) -> Any:
            info = svc.merge_zip(self._ids(query))
            with open(info["path"], "rb") as fh:
                payload = fh.read()
            ascii_name = "archive-merge.zip"
            quoted = urllib.parse.quote(info["name"], safe="")
            return _Raw(payload, "application/zip", {
                "Content-Disposition":
                    f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'})

        @self.post("/api/archive/merge/export")
        def archive_merge_export(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            ids = [int(i) for i in (body.get("ids") or [])]
            if not ids:
                raise ServiceError("需要 ids 指定要合并的批次")
            return svc.export_merge(ids)

        @self.get("/api/archive/*/paths")
        def archive_paths(wild: str, query: Dict[str, List[str]], **_kw: Any) -> Dict[str, Any]:
            """批次在磁盘上的位置（带 export=1 时先把图片落到那个文件夹）。"""
            export = self._first(query, "export", "0") == "1"
            return svc.archive_paths(int(wild), export=export)

        @self.post("/api/reveal")
        def reveal(body: Dict[str, Any], **_kw: Any) -> Dict[str, Any]:
            """在**服务器这台电脑**上打开一个文件夹 / 选中一个文件（资源管理器）。

            手机端点了「在电脑上打开文件夹」也走这里 —— 手机自己打不开电脑的目录，
            但可以让电脑弹出来。只允许打开数据目录以内的路径，避免变成任意程序启动器。
            """
            target = os.path.abspath(str(body.get("path") or ""))
            root = os.path.abspath(svc.root)
            if not target.startswith(root):
                raise ServiceError("只允许打开工作台数据目录以内的路径")
            if not os.path.exists(target):
                raise ServiceError("路径不存在（可能还没导出，请先生成归档文件夹）")
            return {"opened": open_in_file_manager(target), "path": target}

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
    service: Any                     # ServiceProxy（多工作区）或单个 Workbench（自测/老调用）
    root: str
    access: Optional[AccessStore] = None      # 单工作区模式的访客密钥库（兼容老入口）
    auth: Optional[AccountStore] = None       # 账号库；没有它就是"没开账号"的熟人模式
    hub: Optional[WorkspaceHub] = None        # 工作区仓库；按请求给不同的 Workbench
    share: Callable[[], Dict[str, Any]] = staticmethod(lambda: {})

    # ------------------------------------------------------------------ plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
        if os.environ.get("VSW_VERBOSE"):
            sys.stderr.write("[http] " + fmt % args + "\n")

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8",
              extra: Optional[Dict[str, str]] = None) -> None:
        head_only = getattr(self, "_head_only", False)
        extra = dict(extra or {})
        if isinstance(payload, _Raw):
            # 先取头再换 payload —— 顺序反了就会变成 bytes.headers 直接 AttributeError
            extra.update(payload.headers or {})
            content_type, payload = payload.content_type, payload.data
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = payload
        pending: List[str] = []
        key_cookie = getattr(self, "_set_cookie", "")
        if key_cookie:
            pending.append(f"{ACCESS_COOKIE}={urllib.parse.quote(key_cookie)}; "
                           f"Path=/; HttpOnly; SameSite=Lax; Max-Age={COOKIE_MAX_AGE}")
            self._set_cookie = ""
        pending.extend(getattr(self, "_cookies", []) or [])
        self._cookies = []
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # 手机端从 http://<电脑IP>:8799/m/ 打开时是同源；留 CORS 头是为了让
        # 「拖到手机上离线运行」的那份文件（file:// 或别的源）也能回连同一条 API。
        self.send_header("Access-Control-Allow-Origin", "*")
        # 跨源脚本默认读不到这些头 —— 手机端要靠 Content-Length 显示大小、
        # 靠 Content-Disposition 判断下载的文件名。
        self.send_header("Access-Control-Expose-Headers",
                         "Content-Disposition, Content-Length, Content-Type")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        for cookie in pending:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if head_only:
            # HEAD 请求：头照发（长度、类型、文件名都对），体不发
            return
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ------------------------------------------------------------------- 密钥
    def _client_ip(self) -> str:
        # 反代后面才需要看 X-Forwarded-For；直连时它可伪造，所以只认对端地址。
        return str((self.client_address or ("?",))[0] or "?")

    def _query(self) -> Dict[str, List[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _code_from_request(self) -> str:
        """从请求里捞出密钥。四种携带方式，覆盖三种客户端：

        - ``X-VSW-Key`` / ``Authorization: Bearer``：手机 APK（file:// 跨源）与脚本；
        - ``Cookie``：电脑浏览器打开一次之后，``<img src>`` 这种带不了头的请求靠它；
        - ``?key=``：分享链接（扫码/转发）第一次打开时用。
        """
        for name in ("X-VSW-Key", "X-Access-Key"):
            value = (self.headers.get(name) or "").strip()
            if value:
                return value
        auth = (self.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        query = self._query()
        for name in ("key", "k", "access_key"):
            values = query.get(name) or []
            if values and values[0].strip():
                return values[0].strip()
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == ACCESS_COOKIE and value:
                return urllib.parse.unquote(value)
        return ""

    def _cookie_value(self, name: str) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return urllib.parse.unquote(value or "")
        return ""

    def _has_cookie(self) -> bool:
        cookie = self.headers.get("Cookie") or ""
        return any(part.strip().partition("=")[0] == ACCESS_COOKIE for part in cookie.split(";"))

    def _wants_key_auth(self) -> bool:
        """请求里带了访客密钥的痕迹吗（头 / ?key= / 密钥 cookie）。

        有账号以后密钥只是"访客通道"：带着它的请求就要按密钥判身份，
        不能再当成"浏览器里已登录的人"。
        """
        if (self.headers.get("X-VSW-Key") or self.headers.get("X-Access-Key")
                or self.headers.get("Authorization") or "").strip():
            return True
        if (self._query().get("key") or self._query().get("access_key")):
            return True
        return bool(self._cookie_value(ACCESS_COOKIE))

    # ------------------------------------------------------------------- 身份
    def _legacy_workspace(self) -> Dict[str, Any]:
        """没开账号时的那个"唯一工作区"，就是老的数据目录。"""
        return {"id": 0, "name": "本机工作台", "dir": self.root, "kind": "personal"}

    def _workspaces_for(self, user_id: int) -> Dict[int, str]:
        """这人能进哪些工作区、各自什么角色。

        注意 ``memberships_of`` 返回的是 ``workspaces.*`` 加一列 role，
        工作区 id 在 ``id`` 上，不是 ``ws_id``（这里踩过一次 KeyError）。
        """
        if self.auth is None:
            return {}
        out: Dict[int, str] = {}
        for row in self.auth.memberships_of(int(user_id)):
            out[int(row["id"])] = row.get("role") or ROLE_VIEW
        return out

    def _pick_workspace(self, mine: Dict[int, str], user_id: int) -> Optional[int]:
        """这次请求该看哪个工作区：头/Query 指名的 > cookie 记住的 > 默认的。"""
        wanted = 0
        raw = (self.headers.get("X-VSW-Workspace") or "").strip() \
            or (self._query().get("ws") or [""])[0].strip() \
            or self._cookie_value(WORKSPACE_COOKIE)
        if raw:
            try:
                wanted = int(raw)
            except ValueError:
                wanted = 0
        if wanted and wanted in mine:
            return wanted
        if self.auth is None:
            return None
        row = self.auth.default_workspace(int(user_id))
        if row and int(row["id"]) in mine:
            return int(row["id"])
        return next(iter(mine), None)

    def _session_token(self) -> str:
        """会话令牌的来源：cookie 优先，其次是请求头和 ?sid=。

        手机端（尤其是 APK 里 file:// 起的页面）拿不到服务器源的 cookie，
        只能把登录时拿到的令牌存本地、每次塞进 ``X-VSW-Session`` 头。
        """
        token = self._cookie_value(SESSION_COOKIE)
        if token:
            return token
        for name in ("X-VSW-Session", "X-Session", "X-Auth-Token"):
            got = (self.headers.get(name) or "").strip()
            if got:
                return got
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
            if got:
                return got
        query = urllib.parse.urlparse(self.path).query
        for key in ("sid", "session"):
            vals = urllib.parse.parse_qs(query).get(key) or []
            if vals and vals[0].strip():
                return vals[0].strip()
        return ""

    def _resolve_user(self) -> Optional[Dict[str, Any]]:
        if self.auth is None or not self.auth.has_users():
            return None
        token = self._session_token()
        if not token:
            return None
        user = self.auth.session_user(token)
        if not user:
            return None
        mine = self._workspaces_for(int(user["id"]))
        if not mine:
            return None
        ws_id = self._pick_workspace(mine, int(user["id"]))
        if not ws_id:
            return None
        row = self.auth.workspace(ws_id) or {}
        return {
            "mode": "user",
            "user": public_user(user),
            "ws_id": ws_id,
            "ws": row,
            "role": mine.get(ws_id, ROLE_VIEW),
            "label": user.get("name") or "",
        }

    def _key_store_for(self, ws_id: int) -> Optional[AccessStore]:
        if self.hub is not None and ws_id:
            return self.hub.access(ws_id)
        return self.access

    def _resolve_key(self) -> Optional[Dict[str, Any]]:
        """访客通道：拿一把工作区密钥进来。

        密钥是**工作区局部**的 —— 逐个已知工作区试，命中哪个就进哪个。
        只对"这个人本来进不去的工作区"生效，所以主人不用密钥也不会被挡住。
        """
        code = self._code_from_request()
        if not code:
            return None
        ip = self._client_ip()
        candidates: List[Dict[str, Any]] = []
        if self.auth is not None and self.auth.has_users():
            candidates = self.auth.workspaces()
        else:
            candidates = [self._legacy_workspace()]
        for row in candidates:
            ws_id = int(row["id"])
            store = self._key_store_for(ws_id)
            if store is None or not store.enabled():
                continue
            if store.blocked_for(ip) > 0:
                continue
            who = store.verify(code, ip)
            if who is None:
                continue
            if not self._has_cookie():
                # 用 ?key= / 头进来的，顺手种一颗 cookie：接下来 <img src="/media/...">
                # 这类带不了自定义头的请求才有身份。
                self._set_cookie = code
            return {"mode": "key", "key_id": who.get("id"), "label": who.get("label") or "访客",
                    "role": who.get("role") or ROLE_VIEW, "ws_id": ws_id, "ws": row}
        return None

    def _key_locked(self) -> float:
        for row in (self.auth.workspaces() if (self.auth and self.auth.has_users())
                    else [self._legacy_workspace()]):
            store = self._key_store_for(int(row["id"]))
            if store is not None and store.enabled():
                wait = store.blocked_for(self._client_ip())
                if wait > 0:
                    return wait
        return 0.0

    def _resolve(self) -> Optional[Dict[str, Any]]:
        """这次请求是谁：账号会话 / 访客密钥 / 没开锁的本机。None = 没有身份。"""
        if self._wants_key_auth():
            return self._resolve_key()
        who = self._resolve_user()
        if who is not None:
            return who
        if self.auth is None or not self.auth.has_users():
            # 还没建账号：跟以前一样，本机（或同一局域网）直接就是主人 —— 除非
            # 这个工作区自己上了锁（--protect / 访客密钥），那还是要密钥。
            store = self._key_store_for(0) or self.access
            if store is not None and store.enabled():
                return self._resolve_key()
            return {"mode": "open", "role": ROLE_OWNER, "label": "本机",
                    "ws_id": 0, "ws": self._legacy_workspace(), "open": True}
        if self._key_store_for(0) is None and self.access is None:
            return None
        # 开了账号、但手里只有一个浏览器：看是不是"这个浏览器以前拿密钥进来过"。
        return self._resolve_key()

    def _bind(self, ctx: Optional[Dict[str, Any]]) -> None:
        """把这次请求的工作区挂到 service 代理上；后面所有 `svc.xxx` 都落到它。"""
        self._ctx = ctx or {}
        ws_id = int(self._ctx.get("ws_id") or 0)
        chosen: Optional[Workbench] = None
        if ctx and self.hub is not None and ws_id:
            try:
                chosen = self.hub.service(ws_id)
            except Exception:  # noqa: BLE001 - 目录坏了也要给个能解释的错误
                chosen = None
        if isinstance(self.service, ServiceProxy):
            self.service.bind(chosen)

    def _access_store(self) -> Optional[AccessStore]:
        ws_id = int((getattr(self, "_ctx", None) or {}).get("ws_id") or 0)
        return self._key_store_for(ws_id)

    def _identity(self) -> Optional[Dict[str, Any]]:
        """当前请求是谁（给密钥面板和权限判断用）。"""
        ctx = getattr(self, "_ctx", None) or {}
        mode = ctx.get("mode")
        if mode == "user":
            user = ctx.get("user") or {}
            return {"id": user.get("id", 0), "label": user.get("name") or "", "user": user,
                    "role": ctx.get("role") or ROLE_VIEW}
        if mode == "key":
            return {"id": ctx.get("key_id") or 0, "label": ctx.get("label") or "访客",
                    "role": ctx.get("role") or ROLE_VIEW}
        if mode == "open":
            return {"id": 0, "label": "本机", "role": ROLE_OWNER, "open": True}
        return None

    def _guard(self, method: str, path: str) -> bool:
        """鉴权闸门。返回 False 表示已经把响应写回去了（401/403/429）。"""
        need = required_role(method, path)
        if need is None:
            # 页面壳子本身不设防：解锁框、登录框就长在这些文件里。
            self._bind(self._resolve())
            return True
        wait = self._key_locked()
        if wait > 0:
            self._send(429, {"error": f"试错太多次了，{int(wait) + 1} 秒后再试",
                             "retry_after": int(wait) + 1})
            return False
        ctx = self._resolve()
        if ctx is None:
            accounts = bool(self.auth is not None and self.auth.has_users())
            self._bind({"mode": "", "ws_id": 0, "role": ""})
            payload: Dict[str, Any] = {"error": "需要登录" if accounts else "需要访问密钥"}
            if accounts:
                payload["need_login"] = True
                payload["hint"] = "这个工作台已经开了账号；用邀请码注册一个，或者用主人给的密码登录。"
            else:
                payload["need_key"] = True
                payload["hint"] = "这个工作台已经设了密钥；在「共享」里拿一条，或者在地址后面加 ?key=…"
            self._send(401, payload)
            return False
        self._bind(ctx)
        who = self._identity() or {}
        if ROLE_RANK.get(who.get("role", ""), 0) < ROLE_RANK.get(need, ROLE_VIEW):
            self._send(403, {
                "error": f"当前身份是「{ROLE_LABEL.get(who.get('role'), who.get('role'))}」，"
                         f"这个操作需要「{ROLE_LABEL.get(need, need)}」",
                "role": who.get("role"), "need": need, "read_only": who.get("role") == ROLE_VIEW,
            })
            return False
        self._who = who
        return True

    def _tunnel_guard(self) -> str:
        """开公网之前必须先有门槛，否则等于把整个库交给互联网。

        Funnel 一开，拿到地址的人就能读、能写；如果这时还没账号也没密钥，
        连"谁是主人"都拦不住。所以宁可拒绝开，并把话说明白。
        """
        if self.auth is not None and self.auth.has_users():
            return ""
        store = self._access_store()
        if store is not None and store.enabled():
            return ""
        return ("先建第一个账号（或 --protect 上锁）再开公网 —— "
                "现在库里没有任何门槛，开到公网等于谁拿到地址都能读写。")

    def _handle_tunnel(self, method: str, path: str) -> None:
        """公网通道（Tailscale Funnel）。GET 看状态，POST 开/关。

        为什么不自己实现：家宽多半没有公网 IPv4，端口转发根本转发不出去；
        Funnel 由 Tailscale 边缘节点回源，路由器不用动、证书也不用自己签。
        """
        from . import tunnel as _tunnel

        if method == "GET":
            data = _tunnel.status()
            data["blocked"] = self._tunnel_guard()
            self._send(200, data)
            return

        body = getattr(self, "_body_cache", None)
        if body is None:
            body, _ = self._read_body()
        action = str(body.get("action") or "").strip().lower()
        if action not in ("start", "stop"):
            self._send(400, {"error": "action 只能是 start 或 stop"})
            return
        if action == "start":
            blocked = self._tunnel_guard()
            if blocked:
                self._send(400, {"error": blocked, "blocked": blocked})
                return
            data = _tunnel.start(bound_port())
        else:
            data = _tunnel.stop()
        data["blocked"] = self._tunnel_guard()
        self._send(200 if data.get("ok", True) else 400, data)

    def _handle_access(self, method: str, path: str) -> None:
        """密钥相关的接口。放在 Handler 里（而不是 Router），因为要读写这颗 cookie。"""
        self._bind(self._resolve())
        store = self._access_store()
        accounts = bool(self.auth is not None and self.auth.has_users())
        query = self._query()
        tail = path[len("/api/access"):].strip("/")
        if store is None:
            self._send(200, {"enabled": False, "accounts": accounts, "keys": [],
                             "me": self._identity(), "roles": [], "share": self.share()})
            return

        if method == "GET" and tail == "":
            who = self._identity()
            payload: Dict[str, Any] = {
                "enabled": store.enabled() or accounts,
                "accounts": accounts,
                "keys_on": store.enabled(),
                "me": who,
                "roles": store.roles(),
                "share": self.share(),
                "cookie": self._has_cookie(),
            }
            if who is None:
                payload["need_key"] = True
            elif ROLE_RANK.get(who.get("role", ""), 0) >= ROLE_RANK[ROLE_OWNER]:
                payload["keys"] = store.keys()
            self._send(200, payload)
            return

        if method == "POST" and tail == "login":
            body, _ = self._read_body()
            code = str(body.get("key") or query.get("key", [""])[0] or "").strip()
            ip = self._client_ip()
            wait = store.blocked_for(ip)
            if wait > 0:
                self._send(429, {"error": f"试错太多次了，{int(wait) + 1} 秒后再试",
                                 "retry_after": int(wait) + 1})
                return
            who = store.verify(code, ip) if code else None
            if who is None:
                left = store.note_failure(ip)
                self._send(401, {"error": "密钥不对" if looks_like_code(code)
                                 else "密钥格式不对（形如 VSW-XXXX-XXXX-XXXX）",
                                 "remaining": left, "attempts_left": left})
                return
            store.note_success(ip)
            self._set_cookie = code
            self._send(200, {"me": who, "enabled": True})
            return

        if method == "POST" and tail == "logout":
            self._set_cookie = ""
            self._send(200, {"ok": True},
                       extra={"Set-Cookie": f"{ACCESS_COOKIE}=; Path=/; HttpOnly; "
                                            "SameSite=Lax; Max-Age=0"})
            return

        # 以下都是主人的活
        who = self._identity()
        if store.enabled() and (who is None or
                                ROLE_RANK.get(who.get("role", ""), 0) < ROLE_RANK[ROLE_OWNER]):
            self._send(401 if who is None else 403,
                       {"error": "只有主人能管理密钥", "need": ROLE_OWNER,
                        "need_key": who is None})
            return

        if method == "POST" and tail == "keys":
            body, _ = self._read_body()
            label = str(body.get("label") or query.get("label", [""])[0] or "").strip()
            role = str(body.get("role") or query.get("role", [ROLE_VIEW])[0]
                       or ROLE_VIEW).strip()
            made = store.create(label, role)
            # 第一次开锁的人不该被自己关在门外：建完直接把这条种进 cookie。
            if who is None or who.get("open"):
                self._set_cookie = made["code"]
            self._send(200, {"key": made, "keys": store.keys(), "enabled": True})
            return

        parts = tail.split("/")
        if method in ("POST", "DELETE") and len(parts) >= 2 and parts[0] == "keys":
            try:
                key_id = int(parts[1])
            except ValueError:
                self._send(400, {"error": "密钥 id 不对"})
                return
            if method == "DELETE":
                ok = store.delete(key_id)
                self._send(200 if ok else 404,
                           {"ok": ok, "keys": store.keys(),
                            "enabled": store.enabled()} if ok else {"error": "没有这条密钥"})
                return
            action = parts[2] if len(parts) > 2 else "revoke"
            body, _ = self._read_body()
            if action == "revoke":
                if store.active_count() <= 1 and store.enabled():
                    row = next((k for k in store.keys() if k["id"] == key_id), None)
                    if row and row["active"]:
                        self._send(400, {"error": "这是最后一条有效密钥，撤掉就没人能进来了；"
                                                  "先建一条新的再撤"})
                        return
                ok = store.revoke(key_id)
            elif action == "restore":
                ok = store.restore(key_id)
            elif action == "rename":
                ok = store.rename(key_id, str(body.get("label") or ""))
            else:
                self._send(404, {"error": "未知动作"})
                return
            self._send(200 if ok else 404,
                       {"ok": ok, "keys": store.keys(), "enabled": store.enabled()})
            return

        self._send(404, {"error": "未知的密钥接口"})

    # ------------------------------------------------------------------- 账号
    def _is_loopback(self) -> bool:
        host = self._client_ip()
        return host in ("::1", "localhost", "0:0:0:0:0:0:0:1") or host.startswith("127.")

    def _forwarded_ip(self) -> str:
        """隧道/反代写进来的真实来源地址（没有就空串）。

        Tailscale Funnel 回源时对端永远是 ``127.0.0.1``，真正的调用者在
        ``X-Forwarded-For`` 里 —— 实测确实带（还带 ``Tailscale-User-Login``）。
        """
        for name in ("X-Forwarded-For", "X-Real-IP"):
            raw = (self.headers.get(name) or "").strip()
            if raw:
                return raw.split(",")[0].strip()
        return ""

    def _is_local(self) -> bool:
        """请求是不是**跑服务的这台机器**自己发出来的。

        为什么不只认回环：在这台机器上用局域网地址打开（``http://192.168.1.46:8799/``）
        非常常见 —— 用户可能就是从分享面板里点进去的。那时对端 IP 正是本机的一块网卡
        地址，安全性上和 127.0.0.1 没有任何区别（都是同一台机器、同一个用户）。

        所以「第一台电脑能建账号」这句话要按**机器**判断，而不是按**回环**判断：
        否则用户照着面板提示点开局域网地址，反而被挡住。

        **但经过隧道/反代时必须先看 X-Forwarded-For**：Funnel 回源的对端固定是
        127.0.0.1，若只认对端地址，整个公网都会被当成"本机"，谁都能抢先建出主人账号。
        """
        forwarded = self._forwarded_ip()
        if forwarded:
            if forwarded == "::1" or forwarded.startswith("127."):
                return True
            try:
                return forwarded in set(lan_addresses())
            except Exception:
                return False
        if self._is_loopback():
            return True
        host = self._client_ip()
        try:
            return host in set(lan_addresses())
        except Exception:  # 网卡枚举失败时保守起来，当作外部请求
            return False

    def _set_session(self, token: str) -> None:
        self._cookies = getattr(self, "_cookies", []) or []
        self._cookies.append(f"{SESSION_COOKIE}={urllib.parse.quote(token)}; Path=/; HttpOnly; "
                            f"SameSite=Lax; Max-Age={SESSION_DAYS * 86400}")

    def _set_ws_cookie(self, ws_id: int) -> None:
        self._cookies = getattr(self, "_cookies", []) or []
        self._cookies.append(f"{WORKSPACE_COOKIE}={int(ws_id)}; Path=/; SameSite=Lax; "
                            f"Max-Age={SESSION_DAYS * 86400}")

    def _me_payload(self, user: Optional[Dict[str, Any]] = None,
                    ws_id: Optional[int] = None) -> Dict[str, Any]:
        """当前身份 + 能进的工作区 + 这个工作区里的角色。前端登录后全靠它渲染。

        ``user`` / ``ws_id`` 可以显式传：登录、建账号这类"刚种下 cookie 还没回环"的
        请求里，本次请求自己还读不到那颗 cookie。
        """
        ctx = getattr(self, "_ctx", None) or {}
        if user is None:
            user = ctx.get("user") or None
        if not user:
            return {"logged_in": False, "accounts": bool(self.auth and self.auth.has_users()),
                    "can_setup": bool(self.auth and not self.auth.has_users()
                                      and self._is_local())}
        mine: List[Dict[str, Any]] = []
        for row in self.auth.memberships_of(int(user["id"])) if self.auth else []:
            mine.append({"id": int(row["id"]), "name": row.get("name"), "kind": row.get("kind"),
                         "role": row.get("role"), "role_label": ROLE_LABEL.get(row.get("role"), ""),
                         "members": int(row.get("members") or 1)})
        if ws_id is None:
            ws_id = int((ctx.get("ws") or {}).get("id") or 0)
        role = self.auth.role_of(int(ws_id), int(user["id"])) if (self.auth and ws_id) else None
        ws = (self.auth.workspace(int(ws_id)) if (self.auth and ws_id) else None) or {}
        return {
            "logged_in": True,
            "accounts": True,
            "user": public_user(user),
            "workspace": {"id": int(ws.get("id") or ws_id or 0), "name": ws.get("name"),
                          "kind": ws.get("kind"), "role": role},
            "role": role,
            "role_label": ROLE_LABEL.get(role, role or ""),
            "workspaces": mine,
            "share": self.share(),
        }

    def _ws_role(self, ws_id: int, user_id: int) -> Optional[str]:
        return self.auth.role_of(int(ws_id), int(user_id)) if self.auth else None

    def _deny(self, status: int, message: str, **extra: Any) -> None:
        payload: Dict[str, Any] = {"error": message}
        payload.update(extra)
        self._send(status, payload)

    def _handle_auth(self, method: str, path: str) -> None:
        """登录 / 注册 / 工作区 / 成员 / 邀请码。

        放在 Handler 里而不是 Router：它要种会话 cookie，还要按"当前这个人"决定返回什么。
        """
        tail = path[len("/api/auth"):].strip("/")
        parts = [p for p in tail.split("/") if p]
        self._bind(self._resolve())
        if self.auth is None:
            self._send(200, {"accounts": False, "off": True, "can_setup": False})
            return

        if method == "GET" and (not parts or parts[0] in ("me", "status")):
            payload = self._me_payload()
            payload["users"] = self.auth.user_count()
            self._send(200, payload)
            return

        # 剩下的都是「得有个账号」的接口。拿访客密钥进来的人没法开工作区、管成员，
        # 别回 401 need_login（他压根没有密码可登），直接说清楚该找谁。
        if self._ctx is not None and self._ctx.get("mode") == "key":
            self._deny(403, "访客密钥不够用：这个操作要账号登录（找主人给你开一个账号）")
            return

        if method == "POST" and parts[:1] == ["setup"]:
            if self.auth.has_users():
                self._deny(400, "已经有账号了：让主人给你开一个，或者用邀请码注册")
                return
            if not self._is_local():
                self._deny(403, "第一次建账号只能在跑服务的那台电脑上做"
                                "（在那台机器上用 127.0.0.1 或它自己的局域网地址打开）")
                return
            body, _ = self._read_body()
            name = str(body.get("name") or "").strip()
            password = str(body.get("password") or "")
            label = str(body.get("workspace") or "").strip() or f"{name}的工作区"
            user = self.auth.create_user(name, password, is_admin=True)
            row = self.auth.create_workspace(label, self.root, owner_id=int(user["id"]),
                                             kind="personal")
            self.auth.set_workspace_dir(int(row["id"]), self.root)
            self.auth.add_member(int(row["id"]), int(user["id"]), ROLE_OWNER)
            self.auth.mark_login(int(user["id"]))
            token = self.auth.create_session(int(user["id"]), self._client_ip(),
                                            self.headers.get("User-Agent") or "")
            self._set_session(token)
            self._set_ws_cookie(int(row["id"]))
            payload = self._me_payload(user=user, ws_id=int(row["id"]))
            payload["token"] = token
            payload["created"] = {"user": public_user(user), "workspace": row,
                                  "adopted": os.path.abspath(self.root)}
            self._send(200, payload)
            return

        if method == "POST" and parts[:1] == ["register"]:
            body, _ = self._read_body()
            name = str(body.get("name") or "").strip()
            password = str(body.get("password") or "")
            code = str(body.get("code") or body.get("invite") or "").strip()
            if not self.auth.peek_invite(code):
                self._deny(400, "邀请码不对、已经用过，或者过期了")
                return
            if self.auth.user_by_name(name):
                self._deny(400, "这个名字已经有人用了，换一个")
                return
            user = self.auth.create_user(name, password)
            joined = self.auth.use_invite(code, int(user["id"]))
            self.auth.mark_login(int(user["id"]))
            token = self.auth.create_session(int(user["id"]), self._client_ip(),
                                            self.headers.get("User-Agent") or "")
            self._set_session(token)
            self._set_ws_cookie(int(joined["ws_id"]))
            payload = self._me_payload(user=user, ws_id=int(joined["ws_id"]))
            payload["token"] = token
            payload["joined"] = {"workspace": joined["workspace"], "role": joined["role"]}
            self._send(200, payload)
            return

        if method == "POST" and parts[:1] == ["login"]:
            body, _ = self._read_body()
            name = str(body.get("name") or body.get("user") or "").strip()
            password = str(body.get("password") or "")
            ip = self._client_ip()
            wait = self.auth.blocked_for(ip)
            if wait > 0:
                self._send(429, {"error": f"试错太多次了，{int(wait) + 1} 秒后再试",
                                 "retry_after": int(wait) + 1})
                return
            user = self.auth.verify_login(name, password)
            if not user:
                left = self.auth.note_failure(ip)
                self._send(401, {"error": "名字或密码不对", "attempts_left": left,
                                 "remaining": left})
                return
            self.auth.note_success(ip)
            token = self.auth.create_session(int(user["id"]), ip,
                                            self.headers.get("User-Agent") or "")
            self._set_session(token)
            auto = self.auth.default_workspace(int(user["id"])) or {}
            if auto:
                self._set_ws_cookie(int(auto["id"]))
            payload = self._me_payload(user=user, ws_id=int(auto.get("id") or 0))
            payload["token"] = token
            self._send(200, payload)
            return

        if method == "POST" and parts[:1] == ["logout"]:
            token = self._session_token()
            if token:
                self.auth.drop_session(token)
            self._send(200, {"ok": True}, extra={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; "
                                                               "HttpOnly; SameSite=Lax; Max-Age=0"})
            return

        # ---- 以下都要已登录
        ctx = getattr(self, "_ctx", None) or {}
        user = ctx.get("user") or None
        if ctx.get("mode") != "user" or not user:
            accounts = self.auth.has_users()
            self._send(401, {"error": "需要登录" if accounts else "还没建账号",
                             "need_login": accounts, "need_setup": not accounts,
                             "can_setup": bool(not accounts and self._is_local()),
                             "hint": "用邀请码注册，或者用主人给的密码登录"})
            return

        uid = int(user["id"])

        if method == "POST" and parts[:1] == ["password"]:
            body, _ = self._read_body()
            old = str(body.get("old") or body.get("old_password") or "")
            new = str(body.get("new") or body.get("new_password") or "")
            if not self.auth.verify_login(user["name"], old):
                self._deny(400, "原密码不对")
                return
            self.auth.set_password(uid, new)
            token = self._cookie_value(SESSION_COOKIE)
            if token:
                self.auth.drop_session(token)
            fresh = self.auth.create_session(uid, self._client_ip(),
                                             self.headers.get("User-Agent") or "")
            self._set_session(fresh)
            self._send(200, {"ok": True, "hint": "其它设备上的登录已经全部失效"})
            return

        if not parts or parts[0] != "workspaces":
            self._send(404, {"error": "未知的账号接口"})
            return

        # GET /api/auth/workspaces
        if method == "GET" and len(parts) == 1:
            self._send(200, self._me_payload())
            return

        # POST /api/auth/workspaces  —— 自己开一个新的私人工作区
        if method == "POST" and len(parts) == 1:
            body, _ = self._read_body()
            name = str(body.get("name") or "").strip()
            kind = "personal" if str(body.get("kind") or "personal") == "personal" else "shared"
            row = self.hub.create_workspace(name, owner_id=uid, kind=kind) if self.hub else None
            if row is None:
                self._deny(500, "工作区仓库没接上")
                return
            self._set_ws_cookie(int(row["id"]))
            self._send(200, {"ok": True, "workspace": row, "me": self._me_payload()})
            return

        if method == "POST" and len(parts) >= 2 and parts[1] == "switch":
            body, _ = self._read_body()
            try:
                ws_id = int(body.get("ws") or body.get("workspace") or 0)
            except (TypeError, ValueError):
                self._deny(400, "工作区 id 不对")
                return
            role = self._ws_role(ws_id, uid)
            if not role:
                self._deny(403, "你不在这个工作区里")
                return
            self._set_ws_cookie(ws_id)
            self._send(200, self._me_payload(user=user, ws_id=ws_id))
            return

        # /api/auth/workspaces/<id>/...
        try:
            ws_id = int(parts[1])
        except (IndexError, ValueError):
            self._deny(400, "工作区 id 不对")
            return
        role = self._ws_role(ws_id, uid)
        if not role:
            self._deny(403, "你不在这个工作区里")
            return
        sub = parts[2] if len(parts) > 2 else ""

        if method == "GET" and sub == "members":
            self._send(200, {"workspace": self.auth.workspace(ws_id), "role": role,
                             "members": self.auth.members(ws_id),
                             "roles": self.auth.roles(),
                             "is_owner": ROLE_RANK.get(role, 0) >= ROLE_RANK[ROLE_OWNER]})
            return

        if sub in ("members", "invites") and ROLE_RANK.get(role, 0) < ROLE_RANK[ROLE_OWNER]:
            self._deny(403, "只有这个工作区的主人能管成员和邀请码")
            return

        if method == "GET" and sub == "invites":
            self._send(200, {"invites": self.auth.invites(ws_id),
                             "workspace": self.auth.workspace(ws_id)})
            return

        if method == "POST" and sub == "invites":
            body, _ = self._read_body()
            want = str(body.get("role") or ROLE_EDIT).strip()
            hours = float(body.get("hours") or 72)
            made = self.auth.create_invite(ws_id, want, created_by=uid, hours=hours)
            self._send(200, {"invite": made, "invites": self.auth.invites(ws_id)})
            return

        if method == "POST" and sub == "members" and len(parts) == 3:
            body, _ = self._read_body()
            name = str(body.get("name") or "").strip()
            want = str(body.get("role") or ROLE_EDIT).strip()
            target = self.auth.user_by_name(name)
            if not target:
                self._deny(404, f"没有叫「{name}」的账号：先给他一条邀请码让他注册")
                return
            self.auth.add_member(ws_id, int(target["id"]), want)
            self._send(200, {"members": self.auth.members(ws_id)})
            return

        if sub == "members" and len(parts) >= 4:
            try:
                target_id = int(parts[3])
            except ValueError:
                self._deny(400, "成员 id 不对")
                return
            if method == "POST":
                body, _ = self._read_body()
                self.auth.set_member_role(ws_id, target_id,
                                          str(body.get("role") or ROLE_VIEW).strip())
            elif method == "DELETE":
                self.auth.remove_member(ws_id, target_id)
            else:
                self._deny(405, "不支持的方法")
                return
            self._send(200, {"members": self.auth.members(ws_id)})
            return

        self._send(404, {"error": "未知的工作区接口"})

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
        if path.startswith("/api/access") or path.startswith("/api/auth"):
            self._safely(self._handle_access if path.startswith("/api/access")
                         else self._handle_auth, "GET", path)
            return
        if not self._guard("GET", path):
            return
        if path.startswith("/api/tunnel"):
            self._safely(self._handle_tunnel, "GET", path)
            return
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
        if path.startswith("/api/access") or path.startswith("/api/auth"):
            self._safely(self._handle_access if path.startswith("/api/access")
                         else self._handle_auth, "POST", path)
            return
        try:
            body, raw = self._read_body()
        except ServiceError as exc:
            self._send(400, {"error": str(exc)})
            return
        # 体已经读完了：后面自己接管请求的处理器（/api/tunnel）要用同一份，
        # 再调一次 _read_body() 会卡在那里等永远不会到来的数据，直到超时。
        self._body_cache = body
        if not self._guard("POST", path):
            return
        if path.startswith("/api/tunnel"):
            self._safely(self._handle_tunnel, "POST", path)
            return
        if path.startswith("/api/"):
            status, payload = self.router.dispatch("POST", path, self._query(), body, raw)
            self._send(status, payload)
            return
        self._send(404, {"error": "未知路径"})

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path.startswith("/api/access") or path.startswith("/api/auth"):
            self._safely(self._handle_access if path.startswith("/api/access")
                         else self._handle_auth, "DELETE", path)
            return
        if not self._guard("DELETE", path):
            return
        status, payload = self.router.dispatch("DELETE", path, self._query(), {}, b"")
        self._send(status, payload)

    def do_HEAD(self) -> None:
        """HEAD = 走一遍 GET 的路由，但只回头不回体。

        客户端（手机端「保存到手机」、下载器）常用 HEAD 先探大小与文件名；
        以前没实现，一律 501，于是"先探一下"的代码直接失败。
        """
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def _safely(self, handler: Callable[[str, str], None], method: str, path: str) -> None:
        """账号/密钥接口的统一兜底：业务错误回 400，别把栈直接甩给浏览器。"""
        try:
            handler(method, path)
        except (AccountError, ServiceError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("VSW_VERBOSE"):
                traceback.print_exc()
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-VSW-Key, X-VSW-Session, X-VSW-Workspace, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
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


def open_in_file_manager(path: str) -> bool:
    """在服务器这台机器上打开文件管理器（Windows 用资源管理器）。

    手机端没法直接打开电脑的目录，所以「在电脑上打开文件夹」这个动作只能由电脑执行 ——
    这就是它存在的原因。失败不抛异常，只返回 False（服务可能跑在无桌面的环境里）。
    """
    try:
        if os.name == "nt":
            if os.path.isdir(path):
                os.startfile(path)          # 打开文件夹
            else:
                subprocess.Popen(["explorer", "/select,", path])
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
            return True
        subprocess.Popen(["xdg-open", path])
        return True
    except Exception:  # noqa: BLE001
        return False


def build_server(service: Workbench, host: str = "127.0.0.1", port: int = 8787,
                 access: Optional[AccessStore] = None,
                 auth: Optional[AccountStore] = None,
                 hub: Optional[WorkspaceHub] = None,
                 gh_repo: str = "") -> ThreadingHTTPServer:
    """起一个 HTTP 服务。

    ``service`` 可以是单个 Workbench（自测、单工作区老调用），也可以装在 ServiceProxy 里
    （多工作区：每个请求按身份绑到自己的工作区）。
    """
    content = service if isinstance(service, ServiceProxy) else service
    router = Router(content, gh_repo=gh_repo)

    class BoundHandler(Handler):
        pass

    BoundHandler.router = router  # type: ignore[attr-defined]
    BoundHandler.service = content  # type: ignore[attr-defined]
    BoundHandler.access = access  # type: ignore[attr-defined]
    BoundHandler.auth = auth  # type: ignore[attr-defined]
    BoundHandler.hub = hub  # type: ignore[attr-defined]
    fallback = content if isinstance(content, Workbench) else content.current()
    BoundHandler.root = getattr(fallback, "root", "")  # type: ignore[attr-defined]
    BoundHandler.share = staticmethod(  # type: ignore[attr-defined]
        lambda: _share_info(host, fallback))
    server = ThreadingHTTPServer((host, port), BoundHandler)
    server.daemon_threads = True
    _BOUND["port"] = server.server_address[1]
    _BOUND["host"] = host
    return server


def _share_info(host: str, service: Workbench) -> Dict[str, Any]:
    """分享给别人的那几条地址（手机端 + 电脑端）。

    只做填空题：给的是**不带密钥**的基础地址，密钥由界面拼上去 ——
    这样"复制邀请链接"才不会把别人的密钥写进服务器日志/页面源码。
    """
    port = bound_port()
    if host in ("0.0.0.0", "::", ""):
        addresses = lan_addresses()
        base = [f"http://{addr}:{port}" for addr in addresses] or [f"http://127.0.0.1:{port}"]
        wildcard = True
    else:
        base = [f"http://{host}:{port}"]
        wildcard = False
    return {
        "port": port,
        "host": host,
        "wildcard": wildcard,
        "desktop": base,
        "phone": [f"{url}/m/" for url in base],
        "lan": lan_addresses() if wildcard else [],
        "public": tunnel_mod.status(),
        "root": service.root,
    }


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


def _no_window_flags() -> Dict[str, int]:
    """起控制台子进程时用的 creationflags：有它才不会在你屏幕上闪黑框。

    服务现在多半是 **pythonw**（看门狗拉起的，本身没有控制台）。这种进程再 spawn
    ``ipconfig`` 之类控制台程序时，Windows 会给它**新开一个控制台窗口** —— 用户看到
    的就是「经常会跳出黑色弹窗」。以前服务跑在 cmd 里时子进程挂到同一个控制台，
    所以这个坑一直没露出来（r46 换了看门狗才暴露）。
    """
    if os.name != "nt":
        return {}
    return {"creationflags": 0x08000000}          # CREATE_NO_WINDOW


def _adapter_ips() -> List[str]:
    """读网卡列表（Windows 用 ipconfig，其它平台退回 hostname 解析）。"""
    found: List[str] = []
    try:
        import subprocess  # noqa: PLC0415
        if os.name == "nt":
            out = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=8,
                                 encoding="utf-8", errors="ignore",
                                 **_no_window_flags()).stdout
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


_LAN_CACHE: Dict[str, Any] = {"at": 0.0, "value": []}
_LAN_TTL = 20.0


def lan_addresses(*, force: bool = False) -> List[str]:
    """本机在局域网里的可用 IPv4 地址，**按"手机最可能连得上"排序**。

    结果缓存 20 秒：``_is_local()`` 每个请求都要问它一次（判断「是不是本机」），
    不缓存就等于**每个请求都去跑一次 ipconfig** —— 换网句柄不说，在 pythonw 下
    每次都会闪一个黑窗口（而且每次还要几十到几百毫秒）。

    排序依据（踩过坑才知道重要性）：
      1. 家用私有网段优先（192.168 > 10 > 172.16-31）；
      2. 同网段地址靠前；
      3. 排除回环 / 链路本地 / 基准测试保留段 / Tailscale 之类 CGNAT 地址。

    只打印第一个是不够的：这台机器上同时有 WLAN、以太网、Docker、Tailscale，
    手机在哪个网段取决于它连的 WiFi，所以启动日志会把候选地址都列出来。
    """
    now = time.time()
    cached = _LAN_CACHE.get("value") or []
    if not force and cached and now - float(_LAN_CACHE.get("at") or 0.0) < _LAN_TTL:
        return list(cached)

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
    _LAN_CACHE["at"] = now
    _LAN_CACHE["value"] = list(seen)
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


def _relax_console() -> None:
    """让启动日志在任何控制台编码下都不会把服务搞崩。

    真事：Windows 控制台默认 GBK，启动日志里那句「🔒 已上锁：N 条有效密钥」
    带英文码点之外的字符，`print` 直接抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\U0001f512'`，
    服务**还没开始监听就退出**了 —— 而且只在「库里有密钥」时出现，非常容易漏测。
    这里把 stdout/stderr 改成 UTF-8 且 errors=replace：编码不认识的字变成 ?，
    但服务照常起来。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _relax_console()
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
    parser.add_argument("--protect", "--share", dest="protect", action="store_true",
                        help="开门就要密钥：没有密钥就先建一条主人密钥并打印出来。"
                             "一旦有过密钥，之后一律带锁启动（不设这个参数也一样）")
    parser.add_argument("--key", default="",
                        help="指定主人密钥的码（形如 VSW-XXXX-XXXX-XXXX）；"
                             "只在第一次建密钥时生效。不填就随机生成")
    parser.add_argument("--unprotect", action="store_true",
                        help="临时拆锁：本次启动不校验密钥（密钥仍留在库里，"
                             "下次不带这个参数又会上锁）")
    parser.add_argument("--tunnel", action="store_true",
                        help="同时开到公网（Tailscale Funnel）：给一个 https 地址，"
                             "不在同一局域网的人也能打开。需要先有账号或密钥")
    parser.add_argument("--no-tunnel", action="store_true",
                        help="启动时把已经开着的公网通道关掉")
    parser.add_argument("--gh", default=os.environ.get("VSW_GH_REPO", ""),
                        metavar="OWNER/REPO",
                        help="可选的 GitHub 仓库：本机 dist/ 里没有安装包时退到这里查最新 Release")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    os.makedirs(root, exist_ok=True)
    print(f"[vsw] 数据目录：{root}")
    service = Workbench(root, log=lambda msg: print(f"[vsw] {msg}"),
                        allow_private=args.allow_private)

    access = AccessStore(os.path.join(root, "workbench.sqlite3"))
    auth = AccountStore(default_auth_path(root))
    hub = WorkspaceHub(auth, root, log=lambda msg: print(f"[vsw] {msg}"),
                       allow_private=args.allow_private)
    proxy = ServiceProxy(service)

    if auth.has_users():
        people = auth.users()
        bosses = [u for u in people if u.get("is_admin")]
        print(f"[vsw] 👤 账号模式：{len(people)} 个账号 / {len(auth.workspaces())} 个工作区；"
              f"第一个主人是「{bosses[0]['name'] if bosses else '?'}」")
        print("[vsw]      新账号只能由主人发邀请码注册；访客密钥仍然可用（工作区局部）")
    else:
        print("[vsw] 还没建账号 —— 现在和以前一样，本机/局域网直接可用。")
        print("[vsw]      在页面上建第一个账号，就变成「各人私人工作区 + 可协作」的工作台。")

    if args.unprotect:
        print("[vsw] ⚠ --unprotect：本次启动不校验密钥（局域网内任何人可读写）")
    elif access.enabled():
        print(f"[vsw] 🔒 已上锁：{access.active_count()} 条有效密钥；"
              "浏览器第一次打开要在页面上填一次密钥（或点分享链接）")
    elif args.protect:
        made = ensure_owner_key(access, "主人", args.key)
        print("[vsw] 🔑 已开门并上锁，主人密钥：")
        print(f"[vsw]      {made.get('code', '')}")
        print("[vsw]      这条码只显示这一次；请记下来（或用界面里的「复制邀请链接」）")
    elif not auth.has_users():
        print("[vsw] 未设密钥（仅本机/熟人局域网用）；想开放给别人：--protect")
    elif not access.enabled():
        print("[vsw] 没有访客密钥：对外只有账号登录这一条路（想发访客链接就在「共享」里建一把）")

    port = pick_port(args.host, args.port)
    server = build_server(proxy, args.host, port, access=access, auth=auth, hub=hub,
                         gh_repo=args.gh)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{port}/"
    print(f"[vsw] 工作台已启动：{url}")
    ok, reason = service.vision.available()
    print(f"[vsw] 语义引擎：{'就绪 ' + service.vision.model_dir if ok else '不可用 - ' + reason}")
    print_phone_hint(args.host, port)

    # ------------------------------------------------------------- 公网通道
    if args.no_tunnel:
        off = tunnel_mod.stop()
        print("[vsw] 已关闭公网通道" + ("（原来就没开）" if not off.get("running") else ""))
    elif args.tunnel:
        if not tunnel_mod.available():
            print("[vsw] ⚠ --tunnel 需要 Tailscale：https://tailscale.com/download "
                  "装好并登录后重试（它能在没有公网 IP 的情况下给出 https 地址）")
        elif not auth.has_users() and not access.enabled():
            print("[vsw] ⚠ 拒绝开公网：库里没有任何门槛。先建第一个账号（或 --protect 上锁）——"
                  "否则谁拿到地址都能读写")
        else:
            pub = tunnel_mod.start(port)
            if pub.get("running") and pub.get("url"):
                print(f"[vsw] 🌍 公网地址：{pub['url']}/")
                print("[vsw]      这条地址不在局域网也能打开；关掉它：重启时加 --no-tunnel")
                print("[vsw]      注意：走公网的人是「外部设备」，不能替你建第一个账号")
            else:
                print(f"[vsw] ⚠ 公网通道没起来：{pub.get('error') or '未知原因'}")
                print("[vsw]      常见原因：Tailscale 没登录，或后台没在管理台启用 Funnel/HTTPS")

    if args.host in ("0.0.0.0", "::") and not access.enabled() and not args.unprotect \
            and not auth.has_users():
        print("[vsw] ⚠ 已监听 0.0.0.0 但没有密钥：同一网络里的人都能读写你的工作台。"
              "要收口就重启并加 --protect")
    if not args.no_browser and args.host not in ("0.0.0.0", "::"):
        threading.Thread(target=lambda: (__import__("time").sleep(0.6), webbrowser.open(url)),
                         daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[vsw] 已停止")
    finally:
        server.server_close()
        hub.close()
        service.store.close()
        auth.close()
        access.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
