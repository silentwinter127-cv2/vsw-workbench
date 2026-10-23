"""Workbench service: intake -> variants -> queue -> dedup/cluster -> rank -> promote.

This is the closed loop the design asks for::

    seed pool -> same-image search -> neighbourhood search -> merge & dedup
              -> promote valuable results into the next round of seeds

The engine's job is discovery; this module owns scheduling, dedup, ranking and
accumulation. Every function here is plain Python on top of :mod:`vsw.store`, so the
whole loop is testable without a browser (see ``selftest_vsw.py``).
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
import urllib.parse
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from . import engines as engines_mod
from . import imaging
from .engine import VisionEngine, similarity_from_cosine, vector_to_blob
from .store import Store, norm_vec

FETCH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
MAX_FETCH_BYTES = 24 * 1024 * 1024
BLOCKED_HOST_PATTERNS = (r"^localhost$", r"^127\.", r"^0\.", r"^10\.", r"^192\.168\.",
                         r"^172\.(1[6-9]|2\d|3[01])\.", r"^169\.254\.", r"^\[?::1\]?$")


class ServiceError(RuntimeError):
    pass


def _safe_name(text: str, fallback: str = "img") -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", (text or "").strip())
    text = text.strip("._-")
    return (text or fallback)[:60]


def _host_blocked(url: str) -> bool:
    host = engines_mod.host_of(url)
    return any(re.search(p, host) for p in BLOCKED_HOST_PATTERNS)


class Workbench:
    """High level operations exposed by the HTTP API."""

    def __init__(self, root: str, log: Optional[Callable[[str], None]] = None,
                 vision: Optional[VisionEngine] = None, allow_private: bool = False):
        self.root = os.path.abspath(root)
        # Private-address fetching stays off unless a caller (selftest) opts in; the
        # workbench is a web-facing process, so it must not be talked into probing LAN.
        self.allow_private = allow_private
        self.media_root = os.path.join(self.root, "media")
        self.thumb_dir = os.path.join(self.media_root, "thumbs")
        self.seed_dir = os.path.join(self.media_root, "seeds")
        self.variant_dir = os.path.join(self.media_root, "variants")
        self.result_dir = os.path.join(self.media_root, "results")
        for path in (self.media_root, self.thumb_dir, self.seed_dir, self.variant_dir, self.result_dir):
            os.makedirs(path, exist_ok=True)
        self.store = Store(os.path.join(self.root, "workbench.sqlite3"), self.media_root)
        self._log = log or (lambda msg: None)
        self.vision = vision or VisionEngine.instance(log=self._log)
        self._lock = threading.RLock()
        self._reindex_state: Dict[str, Any] = {"running": False, "done": 0, "total": 0, "note": ""}
        self._browser_state: Dict[str, Any] = {"running": False, "job_id": 0, "engine": "",
                                              "phase": "", "started": 0.0, "error": "", "found": 0}
        self._tag_pool: List[Tuple[int, np.ndarray]] = []
        self._tag_matrix: Optional[np.ndarray] = None
        self.reap_stale_jobs()

    # ------------------------------------------------------------------ logging
    def log(self, message: str) -> None:
        self._log(message)

    def reap_stale_jobs(self) -> int:
        """A job left in ``running`` when the process starts is abandoned, not running.

        The queue persists across restarts, so anything still marked "搜索中" belongs to a
        session that no longer exists — leaving it there makes the queue look busy forever
        and hides it from the 待处理 filter.
        """
        rows = self.store.query("SELECT id FROM jobs WHERE status='running'")
        for row in rows:
            self.store.set_job_status(int(row["id"]), "failed",
                                      note="上次会话中断，任务未完成（重启后自动标记）")
        if rows:
            self.log(f"把 {len(rows)} 个残留的「搜索中」队列项标记为失败")
        return len(rows)

    # ------------------------------------------------------------------ health
    def health(self) -> Dict[str, Any]:
        ok, reason = self.vision.available()
        cap_ok, cap_reason = (False, "未检测")
        if ok:
            try:
                cap_ok, cap_reason = self.vision.caption_available()
            except Exception as exc:  # noqa: BLE001
                cap_reason = str(exc)
        return {
            "root": self.root,
            "vision": {"available": ok, "reason": reason,
                       "model": getattr(self.vision, "_dir", "") or "",
                       "caption_available": cap_ok, "caption_reason": cap_reason},
            "settings": self.store.settings(),
            "stats": self.store.stats(),
            "reindex": dict(self._reindex_state),
        }

    def settings(self) -> Dict[str, Any]:
        return self.store.settings()

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        allowed = set(self.store.settings().keys())
        for key, value in (patch or {}).items():
            if key in allowed:
                self.store.set_setting(key, value)
        return self.store.settings()

    def engines(self) -> List[Dict[str, Any]]:
        return engines_mod.engines()

    def variant_kinds(self) -> List[Dict[str, str]]:
        return imaging.variant_kinds()

    # ------------------------------------------------------------------- intake
    def import_bytes(self, data: bytes, *, title: str = "", kind: str = "import",
                     origin_url: str = "", note: str = "", variants: Optional[Sequence[str]] = None,
                     auto_enqueue: bool = False, engines: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        img = imaging.load_image(data)
        # Storage keeps the incoming bytes verbatim (no quality loss, no size inflation);
        # the dedup key is the SHA-256 of their decoded pixels. See imaging.canonicalise.
        payload, digest = imaging.canonicalise(data)
        ext = self._guess_ext(payload, img)
        stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{digest[:10]}-{_safe_name(title or 'seed')}"
        seed_path = os.path.join(self.seed_dir, f"{stem}{ext}")
        with open(seed_path, "wb") as fh:
            fh.write(payload)
        thumb_path = os.path.join(self.thumb_dir, f"seed-{digest[:16]}.jpg")
        with open(thumb_path, "wb") as fh:
            fh.write(imaging.thumb_bytes(img))
        hashes = imaging.hash_image(img, payload)
        palette = imaging.dominant_palette(img)
        embedding = None
        tags: List[Dict[str, Any]] = []
        caption = ""
        ok, _ = self.vision.available()
        if ok:
            try:
                embedding = self.vision.embed_image(img)
                tags = self.vision.tags(embedding, top_k=6)
            except Exception as exc:  # noqa: BLE001
                self.log(f"语义向量失败：{exc}")
        if ok and self.store.get_setting("caption_on_import"):
            try:
                caption = self.vision.caption(img)
            except Exception as exc:  # noqa: BLE001
                self.log(f"描述失败：{exc}")

        seed_id, created = self.store.create_seed(
            sha256=digest, title=title or os.path.basename(seed_path), note=note, kind=kind,
            origin_url=origin_url, rel_path=self._rel(seed_path), thumb_path=self._rel(thumb_path),
            width=hashes.width, height=hashes.height, size=len(payload), phash=str(hashes.phash),
            dhash=str(hashes.dhash), ahash=str(hashes.ahash), embedding=embedding, palette=palette,
            tags=tags, caption=caption)
        result = {"seed_id": seed_id, "created": created, "sha256": digest,
                  "width": hashes.width, "height": hashes.height,
                  "tags": [t["name"] for t in tags], "caption": caption}
        if created:
            result["variants"] = self.generate_variants(seed_id, kinds=variants)
        else:
            # 同一张图再次导入（像素哈希命中）：变体补一下（早期导入的数据可能没有），
            # **但入队不能跳过** —— 这正是"手机上导入的图没进队列"的原因：
            # 之前只有 created 为真才入队，于是重新导一张已存在的图就静默什么都不做。
            result["variants"] = self.store.variants(seed_id) or \
                self.generate_variants(seed_id, kinds=variants)
        if auto_enqueue:
            # store.enqueue 对 (变体, 引擎) 是幂等的：已存在就复用（失败状态还会被重置为待搜索），
            # 所以重复导入不会制造重复任务。
            job_ids = self.enqueue_seed(seed_id, engines=engines or ["baidu", "bing_cn"],
                                        kinds=variants)
            result["job_ids"] = job_ids
            result["enqueued_new"] = bool(created)
        return result

    def import_path(self, path: str, **kwargs: Any) -> Dict[str, Any]:
        if not os.path.isfile(path):
            raise ServiceError(f"文件不存在：{path}")
        with open(path, "rb") as fh:
            data = fh.read()
        kwargs.setdefault("title", os.path.basename(path))
        kwargs.setdefault("kind", "import")
        return self.import_bytes(data, **kwargs)

    def import_url(self, url: str, **kwargs: Any) -> Dict[str, Any]:
        data, content_type = self.fetch_bytes(url)
        if not content_type.startswith("image/") and imaging.image_size(data) == (0, 0):
            raise ServiceError(f"该地址不是图片（Content-Type: {content_type or '未知'}）")
        kwargs.setdefault("title", os.path.basename(urllib.parse.urlsplit(url).path) or url)
        kwargs.setdefault("kind", "import-url")
        kwargs.setdefault("origin_url", url)
        return self.import_bytes(data, **kwargs)

    def import_directory(self, path: str, *, limit: int = 500, recursive: bool = True,
                         **kwargs: Any) -> Dict[str, Any]:
        found: List[str] = []
        if recursive:
            for base, _dirs, files in os.walk(path):
                for name in files:
                    if os.path.splitext(name)[1].lower() in imaging.__dict__.get("_EXT", ()) or \
                            os.path.splitext(name)[1].lower() in (
                                ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"):
                        found.append(os.path.join(base, name))
                        if len(found) >= limit:
                            break
                if len(found) >= limit:
                    break
        else:
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                if os.path.isfile(full) and os.path.splitext(name)[1].lower() in (
                        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"):
                    found.append(full)
                    if len(found) >= limit:
                        break
        imported, skipped = 0, 0
        ids: List[int] = []
        for file_path in found:
            try:
                out = self.import_path(file_path, **kwargs)
                ids.append(out["seed_id"])
                imported += 1 if out["created"] else 0
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                self.log(f"跳过 {file_path}：{exc}")
        return {"scanned": len(found), "imported": imported, "skipped": skipped, "seed_ids": ids}

    def _rel(self, abs_path: str) -> str:
        try:
            return os.path.relpath(abs_path, self.root).replace("\\", "/")
        except ValueError:
            return abs_path.replace("\\", "/")

    @staticmethod
    def _guess_ext(data: bytes, img: Optional[Image.Image] = None) -> str:
        """Pick a file extension that matches the bytes we are about to write."""
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if data[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        if data[:2] == b"BM":
            return ".bmp"
        return ".img"

    def abs_media(self, rel_path: str) -> str:
        if not rel_path:
            return ""
        cleaned = rel_path.replace("/", os.sep)
        candidate = os.path.abspath(os.path.join(self.root, cleaned))
        if not candidate.startswith(os.path.abspath(self.media_root)) and not candidate.startswith(self.root):
            raise ServiceError("越界的媒体路径")
        return candidate

    # --------------------------------------------------------------- variants
    def variant_thumb(self, variant_id: int, box: int = 320) -> bytes:
        """按需生成变体缩略图（磁盘缓存），给手机端队列页用。

        为什么需要它：变体表只有 ``rel_path``（原尺寸的变体图），没有缩略图列。
        手机队列页要显示"这一条派发的是哪个变体"，下原图太重（几十上百 KB × 几百条），
        所以这里现生成一张小图并缓存。缓存键带 box，换尺寸不会串图。
        """
        variant = self.store.variant(int(variant_id))
        if not variant:
            raise ServiceError("变体不存在")
        source = self.abs_media(variant.get("rel_path") or "")
        if not source or not os.path.isfile(source):
            raise ServiceError("变体图片文件不存在")
        cache_dir = os.path.join(self.thumb_dir, "variants")
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"v{variant_id}-{int(box)}.jpg")
        if os.path.isfile(cache_path):
            with open(cache_path, "rb") as fh:
                return fh.read()
        with Image.open(source) as img:
            payload = imaging.thumb_bytes(img.convert("RGB"), box=int(box))
        tmp = cache_path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, cache_path)
        return payload

    def generate_variants(self, seed_id: int, kinds: Optional[Sequence[str]] = None,
                          formats: Sequence[str] = ("jpg",)) -> List[Dict[str, Any]]:
        seed = self.store.seed(seed_id)
        if seed is None:
            raise ServiceError("种子不存在")
        path = self.abs_media(seed["rel_path"])
        if not os.path.isfile(path):
            raise ServiceError("种子文件丢失")
        with open(path, "rb") as fh:
            img = imaging.load_image(fh.read())

        known = {v["kind"] for v in self.store.variants(seed_id)}
        plan = [item for item in imaging.default_plan(kinds or [])
                if item.enabled and (kinds is None or item.kind in set(kinds))]
        plan = [item for item in plan if item.kind not in known]
        if not plan:
            return self.store.variants(seed_id)

        stem = f"s{seed_id}"
        out_dir = os.path.join(self.variant_dir, str(seed_id))
        written = imaging.generate_variants(img, plan, out_dir, stem, formats=formats)
        saved: List[Dict[str, Any]] = []
        for item in written:
            if not item.get("ok"):
                self.log(f"变体失败 {item.get('kind')}: {item.get('error')}")
                continue
            variant_id = self.store.add_variant(
                seed_id, kind=item["kind"], label=item["label"], note=item["note"],
                group=item["group"], rel_path=self._rel(item["path"]), width=item["width"],
                height=item["height"], bytes=item["bytes"], sha256=item["sha256"],
                phash=str(item["phash"]), dhash=str(item["dhash"]), ahash=str(item["ahash"]))
            record = self.store.variant(variant_id) or {}
            record["seed_id"] = seed_id
            saved.append(record)
        return self.store.variants(seed_id)

    # -------------------------------------------------------------------- queue
    def enqueue_seed(self, seed_id: int, *, engines: Optional[Sequence[str]] = None,
                     kinds: Optional[Sequence[str]] = None, group_key: str = "") -> List[int]:
        variant_rows = self.store.variants(seed_id)
        if kinds:
            wanted = set(kinds)
            variant_rows = [v for v in variant_rows if v["kind"] in wanted]
        if not variant_rows:
            variant_rows = self.generate_variants(seed_id)
        engine_keys = list(engines or ["baidu"])
        unknown = [k for k in engine_keys if k not in engines_mod.ENGINE_MAP]
        if unknown:
            raise ServiceError(f"未知引擎：{', '.join(unknown)}")
        # Refuse engines that can no longer search by image at all (measured: Bing dropped the
        # upload endpoint and turns imgurl deep links into plain text searches). Queuing them
        # would manufacture failed queue items and hide the real work.
        dead = [k for k in engine_keys
                if not engines_mod.ENGINE_MAP[k].browser
                and not engines_mod.ENGINE_MAP[k].accepts_image_url
                and not engines_mod.ENGINE_MAP[k].manual_upload]
        if dead:
            names = "、".join(engines_mod.ENGINE_MAP[k].name for k in dead)
            raise ServiceError(f"这些引擎已经不能按图检索，无法入队：{names}"
                               f"（详见 vsw/NOTES-engines.md）")
        return self.store.enqueue(seed_id, [int(v["id"]) for v in variant_rows], engine_keys,
                                  group_key=group_key or f"seed-{seed_id}")

    def enqueue_variants(self, variant_ids: Sequence[int], engines: Sequence[str]) -> List[int]:
        for key in engines:
            spec = engines_mod.ENGINE_MAP.get(key)
            if spec is None:
                raise ServiceError(f"未知引擎：{key}")
            if not spec.browser and not spec.accepts_image_url and not spec.manual_upload:
                raise ServiceError(f"{spec.name} 已经不能按图检索，无法入队")
        job_ids: List[int] = []
        for variant_id in variant_ids:
            variant = self.store.variant(int(variant_id))
            if not variant:
                continue
            job_ids.extend(self.store.enqueue(int(variant["seed_id"]), [int(variant_id)],
                                              engines, group_key=f"batch-{int(time.time())}"))
        return job_ids

    def queue_view(self, status: str = "todo", seed_id: Optional[int] = None,
                   engines: Optional[Sequence[str]] = None, kinds: Optional[Sequence[str]] = None,
                   text: str = "", results: str = "") -> Dict[str, Any]:
        jobs = self.store.jobs(status=status, seed_id=seed_id, engines=engines, kinds=kinds,
                               text=text, results=results)
        counts = {}
        for row in self.store.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
            counts[row["status"]] = int(row["n"])
        for job in jobs:
            spec = engines_mod.ENGINE_MAP.get(job["engine"])
            job["engine_name"] = spec.name if spec else job["engine"]
            job["dispatch_url"] = engines_mod.dispatch_url(job["engine"], image_url="")
            job["variant_file"] = self.abs_media(job.get("variant_path") or "")
            job["browser"] = bool(spec and spec.browser)
        return {"jobs": jobs, "counts": counts, "facets": self.store.job_facets(),
                "filtered": {k: v for k, v in
                             (("status", status), ("seed_id", seed_id),
                              ("engines", list(engines or [])), ("kinds", list(kinds or [])),
                              ("text", text), ("results", results)) if v}}

    # ------------------------------------------------------------------ running
    def execute_job(self, job_id: int, *, public_url: str = "", note: str = "") -> Dict[str, Any]:
        """Mark one queue item as running and hand back everything the user needs."""
        job = self.store.job(job_id)
        if job is None:
            raise ServiceError("队列项不存在")
        variants = self.store.variant(int(job["variant_id"])) or {}
        seed = self.store.seed(int(job["seed_id"])) or {}
        url = engines_mod.dispatch_url(job["engine"], image_url=public_url)
        self.store.set_job_status(job_id, "running", note=note, external_url=url)
        job = self.store.job(job_id) or job
        spec = engines_mod.ENGINE_MAP.get(job["engine"])
        return {
            "job": job,
            "dispatch_url": url,
            "engine": job["engine"],
            "engine_name": spec.name if spec else job["engine"],
            "engine_note": spec.note if spec else "",
            "accepts_image_url": bool(spec and spec.accepts_image_url),
            "local_file": self.abs_media(variants.get("rel_path", "")),
            "seed_file": self.abs_media(seed.get("rel_path", "")),
            "hint": "把结果页整页复制（或复制结果页 URL）后回填到「回填结果」，工作台负责去重、聚类和排序。",
        }

    def finish_job(self, job_id: int, status: str = "done", note: str = "") -> Dict[str, Any]:
        if status not in ("done", "failed", "review", "pending", "running"):
            raise ServiceError("非法状态")
        return self.store.set_job_status(job_id, status, note=note)

    # ------------------------------------------------------- hand-picked crop variant
    def crop_variant(self, seed_id: int, box: Sequence[float], *, padding: float = 0.0,
                     scale: float = 1.0, label: str = "",
                     kinds: Optional[Sequence[str]] = None,
                     engines: Optional[Sequence[str]] = None,
                     auto_run: bool = False) -> Dict[str, Any]:
        """Turn a hand-drawn selection on a seed into a query variant (and optionally run it).

        This is the interactive counterpart of the automatic crop plan: the user frames exactly
        the region that matters (a sign, a face, a menu item) and the workbench records the
        rectangle as a normal variant, so it flows through the same queue, dedup, scoring and
        replay as every other variant — and the "which processing produced a useful result"
        report stays honest.
        """
        seed = self.store.seed(int(seed_id))
        if seed is None:
            raise ServiceError("种子不存在")
        path = self.abs_media(seed["rel_path"])
        if not os.path.isfile(path):
            raise ServiceError("种子文件丢失")
        with open(path, "rb") as fh:
            img = imaging.load_image(fh.read(), max_side=0)
        if len(box) < 4:
            raise ServiceError("框选参数不完整（需要 x, y, w, h）")
        left, top, right, bottom = imaging.resolve_crop_box(box, img.size, padding=padding)
        cropped = img.crop((left, top, right, bottom))
        if scale and scale > 1.0:
            cropped = cropped.resize((int(cropped.width * scale), int(cropped.height * scale)),
                                     Image.LANCZOS)
        out_dir = os.path.join(self.variant_dir, str(seed_id))
        os.makedirs(out_dir, exist_ok=True)
        stamp = str(int(time.time() * 1000))[-8:]
        out_path = os.path.join(out_dir, f"s{seed_id}__crop-{stamp}.jpg")
        with open(out_path, "wb") as fh:
            fh.write(imaging.to_jpeg_bytes(cropped))
        payload = imaging.to_jpeg_bytes(cropped)
        hashes = imaging.hash_image(cropped, payload)
        note_bits = [f"原图 {seed['width']}×{seed['height']} 上框选 ({left},{top})-({right},{bottom})"]
        if padding:
            note_bits.append(f"外扩 {int(padding * 100)}%")
        if scale and scale > 1.0:
            note_bits.append(f"放大 {scale:g}×")
        meta = {
            "crop": {"x": left, "y": top, "w": right - left, "h": bottom - top},
            "normalised": {"x": round(left / img.width, 5), "y": round(top / img.height, 5),
                           "w": round((right - left) / img.width, 5),
                           "h": round((bottom - top) / img.height, 5)},
            "source_size": {"w": img.width, "h": img.height},
            "padding": padding,
            "scale": scale,
            "hand_picked": True,
        }
        variant_id = self.store.add_variant(
            seed_id, kind=imaging.CROP_KIND, label=label or "手工框选",
            note=" · ".join(note_bits), group="geometry", rel_path=self._rel(out_path),
            width=cropped.width, height=cropped.height, bytes=len(payload),
            sha256=hashes.sha256, phash=str(hashes.phash), dhash=str(hashes.dhash),
            ahash=str(hashes.ahash), meta=meta)
        result: Dict[str, Any] = {
            "variant": self.store.variant(variant_id),
            "crop": meta["crop"],
            "source_size": meta["source_size"],
            "job_ids": [],
        }
        if engines:
            result["job_ids"] = self.store.enqueue(seed_id, [variant_id], list(engines),
                                                   group_key=f"crop-{variant_id}")
        if auto_run and result["job_ids"]:
            result["auto"] = self.start_browser_search(int(result["job_ids"][0]))
        return result

    def start_browser_search(self, job_id: int, timeout_ms: int = 90000) -> Dict[str, Any]:
        """Kick off the automated browser search for one queue item (non-blocking)."""
        job = self.store.job(job_id)
        if job is None:
            raise ServiceError("队列项不存在")
        if self._browser_state.get("running"):
            raise ServiceError("已有一个自动识图任务在跑，请等它结束")
        if not self.browser_capable(job["engine"]):
            raise ServiceError("该引擎不支持自动识图，请用「打开识图」手动跑")

        def worker() -> None:
            try:
                self.browser_search(job_id, timeout_ms=timeout_ms)
            except Exception as exc:  # noqa: BLE001
                self.log(f"自动识图失败 #{job_id}: {exc}")

        threading.Thread(target=worker, name=f"vsw-browser-{job_id}", daemon=True).start()
        return {"started": True, "job_id": job_id}

    # --------------------------------------------------- automated browser search
    def browser_capable(self, engine: str) -> bool:
        spec = engines_mod.ENGINE_MAP.get(engine)
        return bool(spec and spec.browser)

    def _driver_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "cdp_driver.js")

    def _node_path(self) -> str:
        """Node executable: an explicit override, then PATH, then the known runtimes."""
        override = os.environ.get("VSW_NODE") or ""
        if override and os.path.isfile(override):
            return override
        found = shutil.which("node")
        if found:
            return found
        candidates = [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs", "node.exe"),
            r"C:\Program Files\nodejs\node.exe",
            os.path.join(os.path.expanduser("~"), ".dsh", "dsh-runtimes", "dsh-primary-runtime",
                         "dependencies", "node", "bin", "node.exe"),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return ""

    def browser_search(self, job_id: int, *, timeout_ms: int = 90000) -> Dict[str, Any]:
        """Run one queue item through the real browser and ingest what comes back.

        This is the automated path the engine boundary forced on us: no usable HTTP API
        exists, so the workbench drives headless Chromium, hands the engine's own upload
        widget the variant file (CDP DOM.setFileInputFiles) and reads the result DOM. The
        extracted image/source list then flows through the exact same three-layer dedup,
        clustering and ranking as a manually pasted result page.
        """
        with self._lock:
            if self._browser_state.get("running"):
                raise ServiceError("已有一个自动识图任务在跑，请等它结束（浏览器是单实例的）")

        job = self.store.job(job_id)
        if job is None:
            raise ServiceError("队列项不存在")
        spec = engines_mod.ENGINE_MAP.get(job["engine"])
        if spec is None:
            raise ServiceError(f"未知引擎：{job['engine']}")
        if not spec.browser:
            raise ServiceError(f"{spec.name} 不支持自动识图，请用「打开识图」手动跑或回填结果")

        node = self._node_path()
        driver = self._driver_path()
        if not node or not os.path.isfile(driver):
            raise ServiceError("缺少 Node 运行时或 cdp_driver.js，无法自动识图"
                               "（可用 VSW_NODE 环境变量指定 node.exe）")

        variant = self.store.variant(int(job["variant_id"])) or {}
        image_path = self.abs_media(variant.get("rel_path") or "")
        if not os.path.isfile(image_path):
            raise ServiceError("变体文件不在本地，无法交给浏览器")

        payload = {
            "engine": job["engine"],
            "url": spec.browser_entry or spec.url_template,
            "file": image_path,
            "selector": spec.upload_selector,
            "waitText": list(spec.wait_text),
            "waitFailText": list(spec.wait_fail_text),
            "waitJs": spec.wait_ready_js,
            "waitMs": max(15000, min(int(timeout_ms), 180000)),
            "settleMs": 6000,
            "extractSettleMs": 2500,
            "extractJs": spec.extract_js,
            "captureNetwork": bool(spec.capture_network),
            "captureFilter": "graph\\.baidu\\.com/ajax" if spec.capture_network else "",
            "captureBodyLimit": 6000,
            "browserPath": str(self.store.get_setting("browser_path") or ""),
            "userAgent": FETCH_UA,
            "dumpProbe": True,
        }
        self.store.set_job_status(job_id, "running", note=f"{spec.name} 自动识图中…")
        self._browser_state.update({"running": True, "job_id": job_id, "engine": job["engine"],
                                    "phase": "启动浏览器", "started": time.time(), "error": "",
                                    "found": 0})
        try:
            result = self._invoke_driver(node, driver, payload)
        except Exception as exc:  # noqa: BLE001
            self.store.set_job_status(job_id, "failed", note=f"自动识图失败：{exc}"[:200])
            self._browser_state.update({"running": False, "error": str(exc), "phase": "失败"})
            raise
        try:
            if not result.get("ok"):
                reason = result.get("error") or "引擎没有给出结果"
                probe = result.get("probe") or {}
                if probe.get("text"):
                    reason = f"{reason}；页面提示：{str(probe['text'])[:120]}"
                self.store.log_op(engine=job["engine"], seed_id=int(job["seed_id"]),
                                  variant_id=int(job["variant_id"]), job_id=job_id,
                                  op_kind="browser", note=f"失败：{reason}"[:200])
                self.store.set_job_status(job_id, "failed", note=reason[:200])
                self._browser_state.update({"running": False, "error": reason, "phase": "失败"})
                return {"ok": False, "error": reason, "engine": job["engine"],
                        "final_url": result.get("finalUrl", ""), "probe": result.get("probe")}

            extract = result.get("extract") or {}
            candidates = self._candidates_from_extract(extract, result=result, engine=job["engine"],
                                                       spec=spec)
            if spec.source_label and result.get("finalUrl"):
                for candidate in candidates:
                    if not candidate.get("title"):
                        candidate["title"] = spec.source_label
            self._browser_state.update({"phase": f"入库 {len(candidates)} 条", "found": len(candidates)})
            outcome = self._ingest_candidates(candidates, job=job, download=True,
                                              op_kind="browser", started=time.time(),
                                              note=f"自动识图（{spec.name}）")
            summary = (f"自动识图完成：候选 {len(candidates)}，新增 {outcome['new_findings']}，"
                       f"折叠重复 {outcome['duplicates']}，失败 {len(outcome['failures'])}")
            self.store.set_job_status(job_id, "done" if candidates else "review", note=summary[:200])
            self._browser_state.update({"running": False, "phase": "完成", "error": ""})
            outcome.update({"ok": True, "engine": job["engine"], "engine_name": spec.name,
                            "final_url": result.get("finalUrl", ""), "guess": extract.get("guess", ""),
                            "goods": extract.get("goods") or [], "sources": extract.get("sources") or [],
                            "summary": summary, "probe": result.get("probe")})
            return outcome
        finally:
            self._browser_state["running"] = False

    def _invoke_driver(self, node: str, driver: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        import subprocess

        with tempfile.TemporaryDirectory(prefix="vsw-cdp-") as work:
            job_path = os.path.join(work, "job.json")
            out_path = os.path.join(work, "out.json")
            with open(job_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            proc = subprocess.run([node, driver, "--job", job_path, "--out", out_path],
                                  capture_output=True, timeout=(payload.get("waitMs", 90000) / 1000) + 90,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.path.isfile(out_path):
                with open(out_path, encoding="utf-8") as fh:
                    return json.load(fh)
            stderr = (proc.stderr or b"").decode("utf-8", "ignore")[-400:]
            stdout = (proc.stdout or b"").decode("utf-8", "ignore")[-400:]
            raise ServiceError(f"驱动没有返回结果：{stderr or stdout or 'unknown'}")

    @staticmethod
    def _candidates_from_extract(extract: Dict[str, Any], *, result: Dict[str, Any], engine: str,
                                 spec: Any) -> List[Dict[str, Any]]:
        """Turn an engine's extracted payload into ingest candidates.

        Provenance is handled per engine, because it is not the same thing everywhere:

        * engines that expose real source pages (their card links leave the engine's domain)
          give each similar image a genuine source URL, so those results feed the **source
          tracking** queue;
        * Baidu's carousel was measured to carry no attribution at all — every card links
          back to ``graph.baidu.com/pcpage/similar`` with just the image and a session token.
          Its outputs are therefore recorded with the engine's own result page as provenance
          and a trust label of ``engine``, which the scoring already treats as low source
          potential. Lying about that would put unsourced neighbours at the top of the source
          queue, which is worse than saying "this came from a search engine".

        The engine's recognition conclusion ("图中可能是…") and any related goods are parked
        on the candidate title so the board keeps that context (and its colour is visible in
        the metadata score, which rewards a title).
        """
        final_url = result.get("finalUrl") or ""
        pages = [p for p in (extract.get("sources") or []) if p.get("url")] if spec.contributes_sources else []
        guess = (extract.get("guess") or "").strip()
        goods = extract.get("goods") or []
        similar = list(extract.get("similar") or [])[:max(1, int(spec.candidate_limit))]
        candidates: List[Dict[str, Any]] = []
        for index, item in enumerate(similar):
            url = item.get("u") if isinstance(item, dict) else str(item)
            if not url:
                continue
            page = (item.get("page") if isinstance(item, dict) else "") or ""
            if page.startswith("http://"):
                # engines still emit http:// for sites that are https-only today; keeping the
                # http form would make every one of those source links fail to open.
                page = "https://" + page[len("http://"):]
            if not page and pages:
                page = pages[index % len(pages)]["url"]
            title_parts = [guess] if guess else []
            if isinstance(item, dict) and item.get("alt"):
                title_parts.append(str(item["alt"]))
            candidates.append({
                "image_url": url,
                "source_url": page or final_url or url,
                "title": " / ".join(title_parts)[:120],
                "width": (item or {}).get("w", 0) if isinstance(item, dict) else 0,
                "height": (item or {}).get("h", 0) if isinstance(item, dict) else 0,
                "provider": "browser",
            })
        # A result page that yields no similar images but does list pages is still worth keeping.
        if not candidates and pages:
            for page in pages:
                candidates.append({"image_url": page["url"], "source_url": page["url"],
                                   "title": page.get("title") or guess, "provider": "browser"})
        for candidate in candidates:
            candidate.setdefault("engine", engine)
            url = str(candidate.get("source_url") or "")
            host = engines_mod.host_of(url) if url else ""
            own_suffixes = ("baidu.com", "baidu.cn", "bcebos.com", "so.com", "360.cn", "qhimg.com")
            engine_page = (not url) or url == final_url or any(
                host == suffix or host.endswith("." + suffix) for suffix in own_suffixes)
            if engine_page:
                candidate["trust"] = spec.trust_label
                if spec.source_label:
                    # 引擎页的信息量比"没有来源"高，所以把标识**附在**识别结论后面，
                    # 而不是拿它覆盖掉识别结论。
                    base = str(candidate.get("title") or "").strip()
                    candidate["title"] = (f"{base} · {spec.source_label}" if base
                                          else spec.source_label)[:200]
            else:
                # 真实来源页不能顶着引擎的信任标签，交给 classify_trust 按域名判
                candidate.pop("trust", None)
        if goods:
            joined = "；".join(f"¥{g.get('price')}{('(' + g['platform'] + ')') if g.get('platform') else ''} "
                              f"{g.get('title', '')}".strip() for g in goods[:6])
            for candidate in candidates:
                candidate["title"] = (candidate["title"] + " | 相关商品: " + joined)[:200]
        return candidates

    def browser_state(self) -> Dict[str, Any]:
        return dict(self._browser_state)

    # ----------------------------------------------------------------- fetching
    def fetch_bytes(self, url: str, timeout: float = 20.0) -> Tuple[bytes, str]:
        import requests  # local import: the workbench works without it for pure-LAN use

        if _host_blocked(url) and not self.allow_private:
            raise ServiceError("拒绝抓取内网地址")
        headers = {"User-Agent": FETCH_UA, "Accept": "image/*,text/html;q=0.9,*/*;q=0.5",
                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8", "Referer": url}
        with requests.get(url, headers=headers, timeout=timeout, stream=True) as resp:
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            chunks: List[bytes] = []
            size = 0
            for chunk in resp.iter_content(65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_FETCH_BYTES:
                    raise ServiceError("图片超过 24MB 上限")
                chunks.append(chunk)
            return b"".join(chunks), content_type

    def fetch_page_text(self, url: str, timeout: float = 25.0, limit: int = 6_000_000) -> str:
        import requests

        if _host_blocked(url) and not self.allow_private:
            raise ServiceError("拒绝抓取内网地址")
        headers = {"User-Agent": FETCH_UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.5",
                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        if not resp.encoding or resp.encoding.lower() in ("iso-8859-1",):
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text[:limit]

    # ------------------------------------------------------------- result intake
    def parse_result_payload(self, payload: str, *, engine: str = "") -> Dict[str, Any]:
        parsed = engines_mod.parse_results(payload, engine=engine)
        for item in parsed["candidates"]:
            if not item.get("platform"):
                item["platform"] = engines_mod.platform_of(item.get("source_url", ""))
        return parsed

    def ingest_payload(self, job_id: int, *, payload: str = "", page_url: str = "",
                       candidates: Optional[Sequence[Dict[str, Any]]] = None,
                       download: bool = True, fetch_limit: int = 60,
                       note: str = "") -> Dict[str, Any]:
        """Import one search's results: parse (or accept) -> download -> dedup -> rank."""
        started = time.time()
        job = self.store.job(job_id)
        if job is None:
            raise ServiceError("队列项不存在")

        source_kind = "payload"
        if not candidates:
            if payload:
                parsed = self.parse_result_payload(payload, engine=job["engine"])
                candidates = parsed["candidates"]
            elif page_url:
                text = self.fetch_page_text(page_url)
                parsed = self.parse_result_payload(text, engine=job["engine"])
                candidates = parsed["candidates"]
                source_kind = "page"
            else:
                raise ServiceError("需要给出结果页内容、结果页 URL 或候选列表")

        candidates = list(candidates)[:max(1, fetch_limit)]
        if page_url:
            self.store.upsert_source(url=page_url, host=engines_mod.host_of(page_url),
                                     platform=engines_mod.platform_of(page_url),
                                     engine=job["engine"], trust="engine", title="检索引擎结果页")
        outcome = self._ingest_candidates(candidates, job=job, download=download,
                                          op_kind="search", started=started, note=note or source_kind)
        self.store.set_job_status(job_id, "review" if outcome["parsed"] else "done",
                                  note=f"回填 {outcome['parsed']} 条，新增 {outcome['new_findings']} 个视觉对象")
        outcome["job"] = self.store.job(job_id)
        return outcome

    def _ingest_candidates(self, candidates: Sequence[Dict[str, Any]], *, job: Optional[Dict[str, Any]],
                           download: bool, op_kind: str, started: float, note: str = "") -> Dict[str, Any]:
        settings = self.store.settings()
        seed_id = int(job["seed_id"]) if job else 0
        variant_id = int(job["variant_id"]) if job else 0
        engine = job["engine"] if job else ""
        seed_vec = self.store.seed_vector(seed_id) if seed_id else None

        new_findings = 0
        dup_images = 0
        duplicates: List[Dict[str, Any]] = []
        created: List[Dict[str, Any]] = []
        new_sources = 0
        new_platforms = 0
        parsed = 0
        fetched = 0
        failures: List[Dict[str, str]] = []
        seen_urls = set()
        known_platforms = {p["platform"] for p in self.store.platform_counts()}

        for item in candidates:
            image_url = (item.get("image_url") or "").strip()
            if not image_url or image_url in seen_urls:
                continue
            seen_urls.add(image_url)
            parsed += 1
            data: Optional[bytes] = None
            fetch_error = ""
            if download:
                if str(image_url).startswith("data:image/"):
                    try:
                        import base64
                        data = base64.b64decode(image_url.split(",", 1)[1])
                        if not data:
                            fetch_error = "内联图片数据为空"
                    except Exception as exc:  # noqa: BLE001
                        fetch_error = f"内联图片解码失败：{exc}"
                else:
                    try:
                        blob, ctype = self.fetch_bytes(image_url)
                        if blob and (ctype.startswith("image/") or imaging.image_size(blob) != (0, 0)):
                            data = blob
                        else:
                            fetch_error = f"返回内容不是可用图片（Content-Type: {ctype or '未知'}）"
                    except Exception as exc:  # noqa: BLE001
                        fetch_error = str(exc)[:200]
            # A candidate we could not obtain must be reported as a failure, never inserted as a
            # blank "finding" (that filled the board with 0×0 entries that could not be promoted).
            if download and data is None:
                failures.append({"image_url": image_url, "error": fetch_error or "取图失败"})
                continue
            try:
                record = self._ingest_one(
                    data=data, image_url=image_url,
                    source_url=item.get("source_url") or image_url,
                    title=item.get("title") or "", engine=engine, job_id=int(job["id"]) if job else 0,
                    variant_id=variant_id, seed_id=seed_id, seed_vec=seed_vec,
                    settings=settings, extra=item)
            except Exception as exc:  # noqa: BLE001
                failures.append({"image_url": image_url, "error": f"入库失败 {exc}"[:200]})
                continue
            if data is not None:
                fetched += 1
            if record["duplicate"]:
                dup_images += 1
                duplicates.append(record)
            else:
                new_findings += 1
                created.append(record)
            if record["source_created"]:
                new_sources += 1
                platform = record["platform"] or "(未识别)"
                if record["source_created"] and platform not in known_platforms:
                    new_platforms += 1
                    known_platforms.add(platform)

        op_id = self.store.log_op(
            engine=engine, seed_id=seed_id, variant_id=variant_id,
            job_id=int(job["id"]) if job else 0, op_kind=op_kind, findings_new=new_findings,
            dup_images=dup_images, new_sources=new_sources, new_platforms=new_platforms,
            duration_s=round(time.time() - started, 2), note=note)
        if job:
            self.store.execute("UPDATE jobs SET op_id=? WHERE id=?", (op_id, int(job["id"])))
        return {"parsed": parsed, "fetched": fetched, "new_findings": new_findings,
                "duplicates": dup_images, "new_sources": new_sources,
                "new_platforms": new_platforms, "failures": failures[:40],
                "created": created[:80], "duplicate_records": duplicates[:80], "op_id": op_id}

    def _ingest_one(self, *, data: Optional[bytes], image_url: str, source_url: str, title: str,
                    engine: str, job_id: int, variant_id: int, seed_id: int,
                    seed_vec: Optional[np.ndarray], settings: Dict[str, Any],
                    extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        extra = extra or {}
        img: Optional[Image.Image] = None
        digest = ""
        hashes = None
        payload: Optional[bytes] = None
        if data:
            payload, digest = imaging.canonicalise(data)
            img = imaging.load_image(payload)
            hashes = imaging.hash_image(img, payload)

        platform = extra.get("platform") or engines_mod.platform_of(source_url)
        host = extra.get("host") or engines_mod.host_of(source_url)
        trust = extra.get("trust") or engines_mod.classify_trust(source_url)

        # ---------------- layer 1: exact file (observed truth, always computed) -----
        # The seed itself is the strongest duplicate target: an engine returning the very
        # image used as the query must not be counted as "a new picture discovered".
        file_layer = ""
        if digest:
            if self.store.one("SELECT id FROM findings WHERE sha256=? AND dup_of=0 LIMIT 1", (digest,)):
                file_layer = "file:sha256"
            elif self.store.one("SELECT id FROM seeds WHERE sha256=? LIMIT 1", (digest,)):
                file_layer = "file:seed-sha256"
        dup_of = 0
        dup_of_seed = 0
        dup_layer = file_layer
        if file_layer == "file:seed-sha256":
            row = self.store.one("SELECT id FROM seeds WHERE sha256=?", (digest,))
            dup_of_seed = int(row["id"]) if row else 0
        elif file_layer == "file:sha256":
            row = self.store.one("SELECT id FROM findings WHERE sha256=? AND dup_of=0 LIMIT 1", (digest,))
            dup_of = int(row["id"]) if row else 0
        # ---------------- layer 2/3: perceptual + semantic -------------------
        vec = None
        if (not dup_layer) and img is not None:
            ok, _ = self.vision.available()
            if ok:
                try:
                    vec = self.vision.embed_image(img)
                except Exception as exc:  # noqa: BLE001
                    self.log(f"结果图向量失败：{exc}")
        if (not dup_layer) and hashes is not None:
            best = None
            for fid, ph, dh, ah, cluster_id in self.store.dupe_candidates():
                dist = imaging.hamming(hashes.phash, ph)
                if dist > int(settings["phash_near"]):
                    continue
                if best is None or dist < best[1]:
                    best = (fid, dist, cluster_id)
            if best is None:
                for seed in self.store.query("SELECT id, phash FROM seeds"):
                    dist = imaging.hamming(hashes.phash, int(seed["phash"] or 0))
                    if dist <= int(settings["phash_near"]) and (best is None or dist < best[1]):
                        best = (-int(seed["id"]), dist, 0)
            if best is not None:
                fid, dist, _cluster = best
                is_seed = fid < 0
                label = "seed-phash" if is_seed else "phash"
                if dist <= int(settings["phash_dup"]):
                    if is_seed:
                        dup_of_seed, dup_layer = -fid, f"visual:{label}{dist}"
                    else:
                        dup_of, dup_layer = fid, f"visual:{label}{dist}"
                elif vec is not None and not is_seed:
                    other = self.store.finding_vector(fid)
                    if other is not None and self.vision.similarity(vec, other) >= float(
                            settings["semantic_dup"]):
                        dup_of, dup_layer = fid, f"visual+semantic:phash{dist}"
        if (not dup_layer) and hashes is not None and seed_id:
            # An engine often hands back the very crop/variant used as the query. Without this
            # check that echo looked like a brand-new find — and the whole point of the crop
            # module is to compare crops, not to be fooled by them.
            best_variant = None
            for variant_id, vhash, kind in self.store.variant_printers(seed_id):
                dist = imaging.hamming(hashes.phash, vhash)
                if dist <= int(settings["phash_near"]) and (best_variant is None
                                                            or dist < best_variant[1]):
                    best_variant = (variant_id, dist, kind)
            if best_variant is not None and best_variant[1] <= int(settings["phash_dup"]):
                dup_of_seed = seed_id
                dup_layer = f"visual:own-{best_variant[2]}"
        if (not dup_layer) and vec is not None:
            ids, matrix = self.store.embeddings()
            if matrix is not None and len(ids):
                sims = matrix @ vec
                best_idx = int(np.argmax(sims))
                if float(sims[best_idx]) >= float(settings["semantic_dup"]) and \
                        similarity_from_cosine(float(sims[best_idx])) >= 0.55:
                    dup_of = int(ids[best_idx])
                    dup_layer = f"semantic:{float(sims[best_idx]):.3f}"
            if dup_of == 0:
                best_seed, best_sim = 0, 0.0
                for seed in self.store.query("SELECT id FROM seeds"):
                    seed_vec = self.store.seed_vector(int(seed["id"]))
                    if seed_vec is None:
                        continue
                    sim = float(np.dot(seed_vec, vec))
                    if sim > best_sim:
                        best_seed, best_sim = int(seed["id"]), sim
                if best_seed and similarity_from_cosine(best_sim) >= float(settings["semantic_dup"]):
                    dup_of_seed = best_seed
                    dup_layer = f"semantic:seed{best_sim:.3f}"

        # ---------------- persist the artefact --------------------------------
        rel_path = ""
        thumb_path = ""
        width = height = size = 0
        palette: List[Dict[str, Any]] = []
        tags: List[Dict[str, Any]] = []
        if img is not None and payload is not None:
            stem = f"{'dup' if dup_of else 'new'}-{digest[:10]}-{_safe_name(title or platform or 'result')}"
            out = os.path.join(self.result_dir, f"{stem}{self._guess_ext(payload, img)}")
            with open(out, "wb") as fh:
                fh.write(payload)
            rel_path = self._rel(out)
            thumb = os.path.join(self.thumb_dir, f"res-{digest[:16]}.jpg")
            with open(thumb, "wb") as fh:
                fh.write(imaging.thumb_bytes(img))
            thumb_path = self._rel(thumb)
            width, height = img.size
            size = len(payload)
            palette = imaging.dominant_palette(img)
            if vec is not None:
                try:
                    tags = self.vision.tags(vec, top_k=6)
                except Exception:  # noqa: BLE001
                    tags = []

        # ---------------- carrier-of-a-seed record ----------------------------
        # Scored up front because both the duplicate branch and the new-object branch need them.
        potential = self._potential(platform=platform, trust=trust, title=title,
                                    extra=extra, has_image=img is not None)
        metadata_score = self._metadata(width, height, size, tags, title, extra.get("date_hint", ""))

        # ---------------- duplicate: attach as another carrier ----------------
        source_id, source_created = self.store.upsert_source(
            url=source_url, finding_id=dup_of, host=host, platform=platform,
            title=title, snippet=str(extra.get("snippet") or "")[:400], image_url=image_url,
            engine=engine, date_hint=extra.get("date_hint") or engines_mod.date_hint_of(
                f"{title} {source_url}"), trust=trust)

        if dup_of or dup_of_seed:
            self._dupbranch = getattr(self, "_dupbranch", 0) + 1
            if image_url:
                self.store.upsert_source(url=image_url, finding_id=dup_of,
                                         host=engines_mod.host_of(image_url),
                                         platform=engines_mod.platform_of(image_url), engine=engine,
                                         image_url=image_url, trust=trust, title="原始图片文件")
            if dup_of:
                self.store.refresh_cluster(int((self.store.finding(dup_of) or {}).get("cluster_id") or 0))
            else:
                # Carrier of a seed we already hold: record it as a promotion candidate so the
                # extra source is still visible in the UI, without inventing a new visual object.
                existing = self.store.one(
                    "SELECT id FROM findings WHERE sha256=? AND dup_of=0 LIMIT 1",
                    (f"seed:{dup_of_seed}:{digest[:16]}",))
                if existing is None:
                    cur = self.store.execute(
                        """INSERT INTO findings(sha256,phash,dhash,ahash,rel_path,thumb_path,
                               source_url,image_url,title,engine,job_id,variant_id,seed_id,depth,
                               cluster_id,similarity,novelty,potential,bridging,metadata_score,
                               explore_value,triage,status,dup_of,dup_layer,width,height,bytes,
                               palette,tags,caption,embedding,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f"seed:{dup_of_seed}:{digest[:16]}", str(hashes.phash) if hashes else "0",
                         str(hashes.dhash) if hashes else "0", str(hashes.ahash) if hashes else "0",
                         rel_path, thumb_path, source_url, image_url, title, engine,
                         job_id, variant_id, seed_id, 1, 0, 0.0, 0.0,
                         potential, 0.0, metadata_score, 0.0, "tracking", "dup-of-seed",
                         -dup_of_seed, dup_layer, width, height, size,
                         json.dumps(palette, ensure_ascii=False),
                         json.dumps(tags, ensure_ascii=False), "",
                         vector_to_blob(vec) if vec is not None else None, time.time()))
                    self.store.execute("UPDATE sources SET finding_id=? WHERE id=?",
                                       (int(cur.lastrowid), source_id))
            return {"duplicate": True, "dup_of": dup_of, "dup_of_seed": dup_of_seed,
                    "dup_layer": dup_layer, "source_id": source_id,
                    "source_created": source_created, "platform": platform,
                    "image_url": image_url, "source_url": source_url,
                    "similarity": self.vision.similarity(vec, self.store.finding_vector(dup_of))
                    if (vec is not None and dup_of) else 0.0}

        # ---------------- cluster assignment ---------------------------------
        cluster_id = 0
        if vec is not None:
            best_cluster, best_sim = 0, 0.0
            for cluster in self.store.clusters():
                centroid = self.store.cluster_centroid(int(cluster["id"]))
                if centroid is None:
                    continue
                sim = float(np.dot(centroid, vec))
                if sim > best_sim:
                    best_cluster, best_sim = int(cluster["id"]), sim
            if best_cluster and best_sim >= float(settings["cluster_join"]):
                cluster_id = best_cluster
            else:
                cluster_id = self.store._new_cluster(
                    label=(tags[0]["name"] if tags else title[:20] or "未命名簇"), seed_finding_id=0,
                    centroid=vec, platforms=[platform] if platform else [])

        # Similarity is computed before the row exists: pHash equality against a held image is
        # the file/perceptual layer's business (it folds instead of inserting), so reaching
        # this point means "not the same picture". With vectors we keep the honest cosine;
        # without the semantic layer the strongest available evidence is perceptual distance.
        if vec is not None and seed_vec is not None:
            similarity = self.vision.similarity(vec, seed_vec)
        elif hashes is not None:
            reference = self.store.seed(seed_id) if seed_id else None
            if reference and reference.get("phash"):
                dist = imaging.hamming(hashes.phash, int(reference["phash"]))
                similarity = max(0.0, min(0.8, 0.8 * (1.0 - min(1.0, dist / 100.0))))
            else:
                similarity = 0.0
        else:
            similarity = 0.0
        novelty = self._novelty(vec, similarity, hashes)
        # Platform novelty must be read *before* the finding is inserted, otherwise the row
        # about to be written already counts as "a platform we have seen".
        bridging = self._bridging(vec, platform=platform, host=host, cluster_id=cluster_id,
                                  seed_id=seed_id)
        explore_value = (float(settings["w_novelty"]) * novelty
                         + float(settings["w_potential"]) * potential
                         + float(settings["w_bridging"]) * bridging
                         + float(settings["w_metadata"]) * metadata_score)
        triage = self._triage(similarity, explore_value, bridging, platform, settings)

        cur = self.store.execute(
            """INSERT INTO findings(sha256,phash,dhash,ahash,rel_path,thumb_path,source_url,image_url,
                     title,engine,job_id,variant_id,seed_id,parent_finding_id,depth,cluster_id,
                     similarity,novelty,potential,bridging,metadata_score,explore_value,triage,status,
                     dup_of,dup_layer,width,height,bytes,palette,tags,caption,embedding,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (digest, str(hashes.phash) if hashes else "0", str(hashes.dhash) if hashes else "0",
             str(hashes.ahash) if hashes else "0", rel_path, thumb_path, source_url, image_url, title, engine,
             job_id, variant_id, seed_id, 0, 1, cluster_id, round(similarity, 4), round(novelty, 4),
             round(potential, 4), round(bridging, 4), round(metadata_score, 4),
             round(explore_value, 4), triage, "new", 0, "", width, height, size,
             json.dumps(palette, ensure_ascii=False), json.dumps(tags, ensure_ascii=False), "",
             vector_to_blob(vec) if vec is not None else None, time.time()))
        finding_id = int(cur.lastrowid)
        self.store.execute("UPDATE sources SET finding_id=? WHERE id=?", (finding_id, source_id))
        if image_url and image_url != source_url:
            self.store.upsert_source(url=image_url, finding_id=finding_id,
                                     host=engines_mod.host_of(image_url),
                                     platform=engines_mod.platform_of(image_url), engine=engine,
                                     image_url=image_url, trust=trust, title="原始图片文件")
        if cluster_id:
            self.store.refresh_cluster(cluster_id)
        return {"duplicate": False, "finding_id": finding_id, "cluster_id": cluster_id,
                "dup_of_seed": 0,
                "similarity": round(similarity, 4), "explore_value": round(explore_value, 4),
                "triage": triage, "platform": platform, "source_id": source_id,
                "source_created": source_created, "image_url": image_url, "source_url": source_url,
                "title": title, "tags": [t["name"] for t in tags], "dup_layer": ""}

    # ------------------------------------------------------------------ scoring
    def _novelty(self, vec: Optional[np.ndarray], similarity: float,
                 hashes: Optional[imaging.ImageHashes] = None) -> float:
        """How different this is from everything already collected (0..1).

        Primary signal is the semantic distance to the nearest stored vector; when the
        semantic layer is off, the perceptual-hash distance to the nearest held image is
        used instead, so novelty stays meaningful in the degraded mode.
        """
        if vec is not None:
            _ids, matrix = self.store.embeddings()
            if matrix is not None and len(matrix):
                closest = similarity_from_cosine(float(np.max(matrix @ vec)))
                return max(0.0, min(1.0, 0.55 * (1.0 - closest) + 0.45 * (1.0 - similarity)))
            return 0.85
        if hashes is not None:
            distances = [imaging.hamming(hashes.phash, row[1])
                         for row in self.store.dupe_candidates()]
            distances += [imaging.hamming(hashes.phash, int(row["phash"] or 0))
                          for row in self.store.query("SELECT phash FROM seeds")]
            if not distances:
                return 0.85
            nearest = min(distances)
            return max(0.0, min(1.0, 0.35 + 0.65 * min(1.0, nearest / 48.0)))
        return 0.35

    def _potential(self, *, platform: str, trust: str, title: str,
                   extra: Dict[str, Any], has_image: bool) -> float:
        """How likely this result leads back to a traceable original page."""
        trust_weight = {"archive": 1.0, "reference": 0.9, "ugc": 0.8, "curation": 0.6,
                        "commerce": 0.5, "engine": 0.15, "unknown": 0.4}.get(trust, 0.4)
        score = 0.45 * trust_weight
        score += 0.2 if title.strip() else 0.0
        score += 0.15 if (extra.get("source_url") and extra.get("image_url")
                          and extra["source_url"] != extra["image_url"]) else 0.0
        score += 0.1 if extra.get("date_hint") else 0.0
        score += 0.1 if has_image else 0.0
        return max(0.0, min(1.0, score))

    def _bridging(self, vec: Optional[np.ndarray], *, platform: str, host: str, cluster_id: int,
                  seed_id: int) -> float:
        """Value of using this result as the next hop into an unexplored area."""
        score = 0.0
        if platform:
            known = {p["platform"] for p in self.store.platform_counts()}
            score += 0.55 if platform not in known else 0.10
        if vec is not None:
            sims: List[float] = []
            for cluster in self.store.clusters():
                if int(cluster["id"]) == cluster_id:
                    continue
                centroid = self.store.cluster_centroid(int(cluster["id"]))
                if centroid is not None:
                    sims.append(float(np.dot(centroid, vec)))
            if sims:
                top = sorted(sims, reverse=True)[:3]
                score += 0.35 * (sum(top) / len(top))
        else:
            score += 0.15
        if seed_id:
            sibling = self.store.one(
                """SELECT COUNT(*) AS n FROM sources s JOIN findings f ON f.id=s.finding_id
                   WHERE f.seed_id=? AND s.platform=?""", (seed_id, platform))
            if sibling and int(sibling["n"]) <= 1:
                score += 0.1
        return max(0.0, min(1.0, score))

    def _metadata(self, width: int, height: int, size: int, tags: Sequence[Dict[str, Any]],
                  title: str, date_hint: str) -> float:
        score = 0.0
        if width and height:
            megapixels = (width * height) / 1_000_000
            score += min(0.35, 0.1 + 0.25 * min(1.0, megapixels / 2.0))
        if size:
            score += 0.2 if size >= 60_000 else (0.1 if size >= 15_000 else 0.0)
        score += min(0.2, 0.04 * len(tags))
        score += 0.15 if title.strip() else 0.0
        score += 0.15 if date_hint else 0.0
        return max(0.0, min(1.0, score))

    def _triage(self, similarity: float, explore_value: float, bridging: float,
                platform: str, settings: Dict[str, Any]) -> str:
        known = {p["platform"] for p in self.store.platform_counts()}
        if similarity >= float(settings["triage_same_image"]):
            return "tracking"
        if explore_value >= float(settings["triage_bridge_value"]) and (
                bridging >= 0.5 or (platform and platform not in known)):
            return "bridge"
        return "diffusion"

    # ----------------------------------------------------------------- ranking
    def rescore_all(self) -> Dict[str, Any]:
        """Recompute novelty/bridging/explore_value for every finding (weights changed).

        Works in both modes: with vectors it uses semantic distance, without them it falls
        back to perceptual-hash distance, so the formula stays tunable even on a machine
        with no local vision model.
        """
        settings = self.store.settings()
        ids, matrix = self.store.embeddings()
        updated = 0
        platform_rows: Dict[int, str] = {}
        for row in self.store.query(
                """SELECT f.id AS id, MAX(s.platform) AS platform FROM findings f
                   LEFT JOIN sources s ON s.finding_id=f.id GROUP BY f.id"""):
            platform_rows[int(row["id"])] = row["platform"] or ""
        position = {fid: pos for pos, fid in enumerate(ids)}
        for row in self.store.query("SELECT * FROM findings WHERE dup_of=0 "
                                    "AND COALESCE(trashed,0)=0 AND COALESCE(archived_at,0)=0"):
            finding_id = int(row["id"])
            vec = norm_vec(matrix[position[finding_id]]) if finding_id in position else None
            others = None
            if vec is not None and matrix is not None and len(matrix) > 1:
                others = np.delete(matrix, position[finding_id], axis=0)
            novelty = 0.35
            if vec is not None:
                if others is not None and len(others):
                    max_sim = float(np.max(others @ vec))
                    novelty = max(0.0, min(1.0, 0.55 * (1.0 - similarity_from_cosine(max_sim))
                                           + 0.45 * (1.0 - float(row["similarity"] or 0))))
                else:
                    novelty = 0.85
            elif row["phash"]:
                hashes = imaging.ImageHashes("", int(row["phash"] or 0), int(row["dhash"] or 0),
                                             int(row["ahash"] or 0), 0, 0)
                novelty = self._novelty(None, float(row["similarity"] or 0), hashes)
            platform = platform_rows.get(finding_id, "")
            bridging = self._bridging(vec, platform=platform, host="",
                                      cluster_id=int(row["cluster_id"] or 0),
                                      seed_id=int(row["seed_id"] or 0))
            value = (float(settings["w_novelty"]) * novelty
                     + float(settings["w_potential"]) * float(row["potential"] or 0)
                     + float(settings["w_bridging"]) * bridging
                     + float(settings["w_metadata"]) * float(row["metadata_score"] or 0))
            triage = self._triage(float(row["similarity"] or 0), value, bridging, platform, settings)
            self.store.update_finding(finding_id, novelty=round(novelty, 4),
                                      bridging=round(bridging, 4), explore_value=round(value, 4),
                                      triage=triage)
            updated += 1
        return {"updated": updated}

    # ------------------------------------------------------------------ promote
    def promote_findings(self, finding_ids: Sequence[int], *, engines: Optional[Sequence[str]] = None,
                         kinds: Optional[Sequence[str]] = None, depth_from: Optional[int] = None,
                         note: str = "") -> Dict[str, Any]:
        """新种子回流: turn selected results into seeds of the next round (+ queue them)."""
        engine_keys = list(engines or ["baidu"])
        promoted: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for finding_id in finding_ids:
            finding = self.store.finding(int(finding_id))
            if finding is None:
                continue
            data: Optional[bytes] = None
            rel = finding.get("rel_path") or ""
            if rel:
                path = self.abs_media(rel)
                if os.path.isfile(path):
                    with open(path, "rb") as fh:
                        data = fh.read()
            if data is None and finding.get("image_url"):
                try:
                    data, _ctype = self.fetch_bytes(finding["image_url"])
                except Exception as exc:  # noqa: BLE001
                    failed.append({"finding_id": int(finding_id), "reason": str(exc)[:160]})
                    self.log(f"回流失败（取图）：{finding['image_url']} {exc}")
                    continue
            if data is None:
                failed.append({"finding_id": int(finding_id), "reason": "本地文件缺失且没有可用图片地址"})
                continue
            try:
                out = self.import_bytes(
                    data, title=finding.get("title") or f"finding-{finding_id}",
                    kind="promoted", origin_url=finding.get("source_url") or "",
                    note=note or f"由结果 #{finding_id} 回流（{finding.get('triage')}）",
                    variants=kinds, auto_enqueue=True, engines=engine_keys)
            except Exception as exc:  # noqa: BLE001
                self.log(f"提升失败：{exc}")
                continue
            self.store.update_finding(int(finding_id), status="promoted")
            if depth_from is not None:
                self.store.execute("UPDATE findings SET depth=? WHERE id=?", (int(depth_from) + 1,
                                                                            int(finding_id)))
            for job_id in out.get("job_ids", []):
                self.store.execute("UPDATE jobs SET note=? WHERE id=?",
                                   (f"回流自 finding #{finding_id}", int(job_id)))
            promoted.append(out)
        return {"promoted": len(promoted), "seeds": promoted, "failed": failed}

    # -------------------------------------------------------------------- graph
    def search_chain(self, finding_id: int, max_depth: int = 8) -> Dict[str, Any]:
        """Replay the discovery path: seed -> variant -> engine -> result -> source -> new seed."""
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, str]] = []
        finding = self.store.finding(finding_id)
        if finding is None:
            raise ServiceError("结果不存在")
        cursor: Optional[Dict[str, Any]] = finding
        guard = 0
        while cursor is not None and guard < max_depth:
            guard += 1
            fid = int(cursor["id"])
            nodes.append({"type": "finding", "id": fid, "title": cursor.get("title") or f"结果 #{fid}",
                          "thumb": cursor.get("thumb_path") or "", "triage": cursor.get("triage"),
                          "explore_value": cursor.get("explore_value"),
                          "similarity": cursor.get("similarity"),
                          "status": cursor.get("status")})
            for source in self.store.sources(finding_id=fid, limit=20):
                nodes.append({"type": "source", "id": int(source["id"]), "title": source.get("title")
                              or source.get("url"), "url": source.get("url"),
                              "platform": source.get("platform"), "trust": source.get("trust")})
                edges.append({"from": f"finding:{fid}", "to": f"source:{int(source['id'])}",
                              "label": "发现于"})
            variant_id = int(cursor.get("variant_id") or 0)
            if variant_id:
                variant = self.store.variant(variant_id)
                if variant:
                    nodes.append({"type": "variant", "id": variant_id,
                                  "title": f"{variant.get('label')}（{variant.get('kind')}）",
                                  "thumb": variant.get("rel_path") or "",
                                  "note": variant.get("note")})
                    edges.append({"from": f"variant:{variant_id}", "to": f"finding:{fid}",
                                  "label": f"{cursor.get('engine')} 检索"})
                    seed_id = int(variant.get("seed_id") or 0)
                    seed = self.store.seed(seed_id) if seed_id else None
                    if seed:
                        nodes.append({"type": "seed", "id": seed_id, "title": seed.get("title"),
                                      "thumb": seed.get("thumb_path") or "",
                                      "kind": seed.get("kind")})
                        edges.append({"from": f"seed:{seed_id}", "to": f"variant:{variant_id}",
                                      "label": "生成变体"})
                        if seed.get("kind") == "promoted":
                            job = self.store.one(
                                "SELECT note FROM jobs WHERE seed_id=? AND note LIKE '%finding%' LIMIT 1",
                                (seed_id,))
                            if job and job["note"]:
                                match = re.search(r"#(\d+)", job["note"])
                                if match:
                                    parent = self.store.finding(int(match.group(1)))
                                    if parent:
                                        edges.append({"from": f"seed:{seed_id}",
                                                      "to": f"finding:{int(match.group(1))}",
                                                      "label": "回流自"})
                    cursor = None
                    break
            if cursor is None:
                break
            parent_id = int(cursor.get("parent_finding_id") or 0)
            cursor = self.store.finding(parent_id) if parent_id else None
        return {"nodes": nodes, "edges": edges, "root_finding": finding_id}

    def graph(self, limit: int = 200) -> Dict[str, Any]:
        """Whole workbench graph: seeds -> variants -> findings -> sources.

        Labels are kept short and *meaningful*: a source node is named by its host (or the
        platform bucket the aggregator already computed), never by a hard-cut URL — slicing a
        URL at N characters produced unreadable fragments like "com.cn" or "org" in the
        rendered graph. The full URL travels in ``url`` for the tooltip and click-through.
        """
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, str]] = []

        def clip(text: str, fallback: str = "", width: int = 22) -> str:
            cleaned = (text or "").strip() or fallback
            return cleaned if len(cleaned) <= width else cleaned[:width - 1] + "…"

        for seed in self.store.seeds(limit=limit):
            sid = int(seed["id"])
            nodes.append({"id": f"seed:{sid}", "type": "seed",
                          "label": clip(seed["title"], f"种子 {sid}"),
                          "full": seed["title"] or "", "thumb": seed.get("thumb_path") or "",
                          "kind": seed.get("kind")})
        for variant in self.store.variants(limit=limit * 8):
            vid = int(variant["id"])
            nodes.append({"id": f"variant:{vid}", "type": "variant",
                          "label": clip(variant.get("label") or variant.get("kind") or "",
                                        "变体", 13),
                          "full": variant.get("label") or variant.get("kind") or "",
                          "thumb": variant.get("rel_path") or "", "seed_id": variant["seed_id"],
                          "kind": variant.get("kind")})
            edges.append({"from": f"seed:{variant['seed_id']}", "to": f"variant:{vid}"})
        for finding in self.store.query(
                "SELECT id,title,thumb_path,seed_id,variant_id,triage,dup_of FROM findings "
                "WHERE dup_of=0 AND COALESCE(trashed,0)=0 AND COALESCE(archived_at,0)=0 "
                "ORDER BY explore_value DESC LIMIT ?", (limit * 2,)):
            fid = int(finding["id"])
            nodes.append({"id": f"finding:{fid}", "type": "finding",
                          "label": clip(finding["title"], f"结果 {fid}"),
                          "full": finding["title"] or "", "thumb": finding["thumb_path"] or "",
                          "triage": finding["triage"]})
            parent = int(finding["variant_id"] or 0)
            # Link to the variant that actually found it when we know it — the chain is the point.
            if parent:
                edges.append({"from": f"variant:{parent}", "to": f"finding:{fid}"})
            elif finding["seed_id"]:
                edges.append({"from": f"seed:{int(finding['seed_id'])}", "to": f"finding:{fid}"})
        for source in self.store.query(
                """SELECT s.id AS id, s.url AS url, s.host AS host, s.platform AS platform,
                          s.finding_id AS finding_id
                   FROM sources s JOIN findings f ON f.id=s.finding_id
                   WHERE COALESCE(f.trashed,0)=0 AND COALESCE(f.archived_at,0)=0 LIMIT ?""",
                (limit * 6,)):
            if not source["finding_id"]:
                continue
            host = source["host"] or engines_mod.host_of(source["url"])
            nodes.append({"id": f"source:{int(source['id'])}", "type": "source",
                          "label": clip(source["platform"] or host, "来源", 24),
                          "full": f"{source['platform'] or ''} {source['url']}".strip(),
                          "host": host, "url": Store._slim_url(source["url"])})
            edges.append({"from": f"finding:{int(source['finding_id'])}",
                          "to": f"source:{int(source['id'])}"})
        return {"nodes": nodes, "edges": edges}

    # ------------------------------------------------------------------ reindex
    def reindex_missing(self, *, limit: int = 500) -> Dict[str, Any]:
        """Compute SigLIP vectors (and optionally captions) for rows that lack them.

        Runs in a thread so the UI stays usable while the model chews through the
        backlog; progress is reported by :meth:`reindex_state`.
        """
        if self._reindex_state.get("running"):
            return dict(self._reindex_state)
        rows = self.store.query(
            """SELECT id, rel_path, thumb_path FROM findings WHERE embedding IS NULL AND rel_path<>''
               LIMIT ?""", (limit,))
        seed_rows = self.store.query("SELECT id, rel_path FROM seeds WHERE embedding IS NULL AND rel_path<>''")
        total = len(rows) + len(seed_rows)

        def worker() -> None:
            self._reindex_state.update({"running": True, "done": 0, "total": total, "note": "开始"})
            try:
                ok, reason = self.vision.available()
                if not ok:
                    self._reindex_state.update({"running": False, "note": f"语义模型不可用：{reason}"})
                    return
                for index, row in enumerate(rows, start=1):
                    path = self.abs_media(row["rel_path"])
                    if os.path.isfile(path):
                        try:
                            with open(path, "rb") as fh:
                                img = imaging.load_image(fh.read())
                            vec = self.vision.embed_image(img)
                            tags = self.vision.tags(vec, top_k=6)
                            self.store.execute("UPDATE findings SET embedding=?, tags=? WHERE id=?",
                                               (vector_to_blob(vec),
                                                json.dumps(tags, ensure_ascii=False), int(row["id"])))
                        except Exception as exc:  # noqa: BLE001
                            self.log(f"重算失败 #{row['id']}：{exc}")
                    self._reindex_state.update({"done": index, "note": f"结果 #{row['id']}"})
                    if index % 5 == 0:
                        self._rebuild_tag_pool()
                for index, row in enumerate(seed_rows, start=len(rows) + 1):
                    path = self.abs_media(row["rel_path"])
                    if os.path.isfile(path):
                        try:
                            with open(path, "rb") as fh:
                                img = imaging.load_image(fh.read())
                            vec = self.vision.embed_image(img)
                            tags = self.vision.tags(vec, top_k=6)
                            self.store.execute("UPDATE seeds SET embedding=?, tags=? WHERE id=?",
                                               (vector_to_blob(vec),
                                                json.dumps(tags, ensure_ascii=False), int(row["id"])))
                        except Exception as exc:  # noqa: BLE001
                            self.log(f"重算失败 seed#{row['id']}：{exc}")
                    self._reindex_state.update({"done": index, "note": f"种子 #{row['id']}"})
                self.rescore_all()
                self._reindex_state.update({"running": False, "note": "完成"})
            except Exception as exc:  # noqa: BLE001
                self._reindex_state.update({"running": False, "note": f"失败：{exc}"})

        thread = threading.Thread(target=worker, name="vsw-reindex", daemon=True)
        thread.start()
        return dict(self._reindex_state)

    def reindex_state(self) -> Dict[str, Any]:
        return dict(self._reindex_state)

    # -------------------------------------------------------------- text search
    def _rebuild_tag_pool(self) -> None:
        rows = self.store.query("SELECT id,embedding FROM findings "
                                "WHERE embedding IS NOT NULL AND dup_of=0 "
                                "AND COALESCE(trashed,0)=0 AND COALESCE(archived_at,0)=0")
        ids, vecs = [], []
        for row in rows:
            from .engine import blob_to_vector
            ids.append(int(row["id"]))
            vecs.append(norm_vec(blob_to_vector(row["embedding"])))
        self._tag_pool = list(zip(ids, vecs))
        self._tag_matrix = np.vstack(vecs) if vecs else None

    def semantic_search(self, query: str, limit: int = 60) -> Dict[str, Any]:
        ok, reason = self.vision.available()
        if not ok:
            raise ServiceError(f"语义模型不可用：{reason}")
        if self._tag_matrix is None:
            self._rebuild_tag_pool()
        if self._tag_matrix is None or not len(self._tag_pool):
            return {"query": query, "results": [], "note": "还没有任何语义向量，先跑一次「重算语义索引」"}
        query_vec = self.vision.embed_text(query)
        sims = self._tag_matrix @ query_vec
        order = np.argsort(-sims)[:max(1, limit)]
        results = []
        for idx in order:
            finding_id, _vec = self._tag_pool[int(idx)]
            finding = self.store.finding(finding_id)
            if finding:
                finding["query_similarity"] = round(similarity_from_cosine(float(sims[int(idx)])), 4)
                results.append(finding)
        return {"query": query, "results": results}

    def similar_findings(self, finding_id: int, limit: int = 24) -> Dict[str, Any]:
        target = self.store.finding_vector(finding_id)
        if target is None:
            return {"results": [], "note": "该结果还没有语义向量"}
        ids, matrix = self.store.embeddings()
        if matrix is None or not len(ids):
            return {"results": [], "note": "索引为空"}
        sims = matrix @ target
        order = np.argsort(-sims)
        results = []
        for idx in order:
            fid = int(ids[int(idx)])
            if fid == finding_id:
                continue
            finding = self.store.finding(fid)
            if finding and not finding["dup_of"]:
                finding["query_similarity"] = round(similarity_from_cosine(float(sims[int(idx)])), 4)
                results.append(finding)
            if len(results) >= limit:
                break
        return {"results": results}

    # ------------------------------------------------------------------ 回收站
    def trash_findings(self, finding_ids: Sequence[int], *, reason: str = "manual",
                       only_unpromoted: bool = False) -> Dict[str, Any]:
        """把结果移进回收站（软删除，行/来源/文件都留着，随时可恢复）。

        ``only_unpromoted`` 是给「清理结果板」按钮用的安全阀：只扫那些**还没被选为下一轮种子**
        的条目 —— 已经回流的成果（status='promoted'）默认不动，免得把辛苦扩出来的搜索网络拆了。
        """
        ids = [int(i) for i in finding_ids]
        guard: List[Dict[str, Any]] = []
        if only_unpromoted:
            kept: List[int] = []
            for finding_id in ids:
                row = self.store.finding(finding_id)
                if row is None:
                    guard.append({"finding_id": finding_id, "why": "不存在"})
                elif str(row.get("status") or "") == "promoted":
                    guard.append({"finding_id": finding_id, "why": "已选为种子，已保护"})
                else:
                    kept.append(finding_id)
            ids = kept
        result = self.store.trash_findings(ids, reason=reason)
        result["skipped"] = guard + list(result.get("skipped") or [])
        for finding_id in result.get("moved") or []:
            row = self.store.finding(finding_id) or {}
            cluster_id = int(row.get("cluster_id") or 0)
            if cluster_id:
                self.store.refresh_cluster(cluster_id)
        return result

    def trash_unpromoted(self, *, reason: str = "sweep") -> Dict[str, Any]:
        """一键清理：把所有「没被选为种子」的视觉对象放进回收站。"""
        result = self.store.trash_unpromoted(reason=reason)
        for finding_id in result.get("moved") or []:
            row = self.store.finding(finding_id) or {}
            cluster_id = int(row.get("cluster_id") or 0)
            if cluster_id:
                self.store.refresh_cluster(cluster_id)
        return result

    def restore_findings(self, finding_ids: Sequence[int]) -> Dict[str, Any]:
        result = self.store.restore_findings([int(i) for i in finding_ids])
        for finding_id in result.get("restored") or []:
            row = self.store.finding(finding_id) or {}
            cluster_id = int(row.get("cluster_id") or 0)
            if cluster_id:
                self.store.refresh_cluster(cluster_id)
        return result

    def restore_all(self) -> Dict[str, Any]:
        return self.restore_findings([int(r["id"]) for r in
                                      self.store.query("SELECT id FROM findings WHERE trashed=1")])

    def purge_findings(self, finding_ids: Sequence[int]) -> Dict[str, Any]:
        return self.store.purge_findings([int(i) for i in finding_ids])

    def purge_all(self) -> Dict[str, Any]:
        return self.store.purge_all()

    def trash_view(self, limit: int = 200, offset: int = 0) -> Dict[str, Any]:
        items = self.store.findings(limit=limit, offset=offset, order="trashed",
                                    trashed=True, include_dups=True)
        total = self.store.count_findings(trashed=True)
        return {"findings": items, "count": len(items), "total": total,
                "stats": {"trashed": total}}

    # --------------------------------------------------------------- 手机快照
    def _slim_for_mobile(self, item: Dict[str, Any], mode: str,
                         *, with_sources: bool) -> Dict[str, Any]:
        """把一条记录压到手机端"够用"的程度。

        实测（真实库 66 个对象 / 200 条回收站）：完整快照 761 KB，其中回收站占 508 KB ——
        因为每一行都内联了 2.9 KB 的来源数组，而手机上列表只需知道"有几个来源"。
        所以：列表页只给 ``source_count``，点开详情时再按需取那一条的来源。
        ``url`` 已经能从 ``source_url``/``image_url`` 得到，``url_hash`` 是纯冗余，直接丢。
        """
        item.pop("embedding", None)
        item.pop("url_hash", None)
        item.pop("dup_layer", None)
        if mode == "none":
            item["thumb_path"] = ""
            item["rel_path"] = ""
        elif mode == "thumbs":
            item["rel_path"] = ""
        if with_sources:
            sources = item.get("sources") or []
            for source in sources:
                source.pop("url_hash", None)
                # data: URI 能长到十几万字符（图片被内联进来源记录），手机端存下来也打不开
                if "url" in source:
                    source["url"] = Store._slim_url(source["url"])
                if "image_url" in source:
                    source["image_url"] = Store._slim_url(source["image_url"])
            item["sources"] = sources
            item["source_count"] = len(sources)
        else:
            sources = item.pop("sources", None)
            if sources is None:
                row = self.store.one("SELECT COUNT(*) AS n FROM sources WHERE finding_id=?",
                                     (int(item["id"]),))
                item["source_count"] = int(row["n"]) if row else 0
            else:
                item["source_count"] = len(sources)
        return item

    def mobile_snapshot(self, *, thumb_side: int = 240, findings_limit: int = 800,
                        include_trash: bool = True, include_archives: bool = True,
                        image_mode: str = "thumbs", with_sources: bool = False,
                        include_archived: bool = True) -> Dict[str, Any]:
        """给手机端打一份可直接离线使用的快照。

        设计要点（都是被手机的限制逼出来的）：

        * **不含 embedding**：768 维浮点一条就 3KB，几百条就是几 MB，手机上毫无用处
          （离线时跑不了 SigLIP 文本检索）。
        * ``image_mode``：``thumbs`` 只带缩略图（几十 KB 级），``full`` 连原图一起带
          （可以彻底断网使用，但体积按原图算），``none`` 只带元数据。
        * 图片通过**独立的静态路由**按需取，而不是塞进 JSON：把 base64 内联进 IndexedDB
          会让数据库膨胀 ~1.4 倍且无法流式/缓存。
        * ``with_sources=False``（默认）只带来源**数量**，把快照压到列表页够用的大小；
          详情按需取。
        * 队列、归档、回收站一并带过去，手机端才能完整浏览与清理。
        """
        store = self.store
        mode = image_mode if image_mode in ("thumbs", "full", "none") else "thumbs"
        findings: List[Dict[str, Any]] = []
        for row in store.findings(limit=findings_limit, include_dups=True, order="value"):
            item = dict(row)
            if with_sources:
                item["sources"] = store.sources(finding_id=int(row["id"]), limit=50)
            findings.append(self._slim_for_mobile(item, mode, with_sources=with_sources))

        variants = []
        for variant in store.variants(limit=findings_limit * 2):
            item = self._slim_for_mobile(dict(variant), mode, with_sources=False)
            # 变体表没有缩略图列；给手机一个按需生成小图的地址，队列页才能显示
            # "这一条派发的是哪个变体"（下原图太重：几百条 × 几十上百 KB）。
            item["thumb"] = f"api/variant/{int(variant['id'])}/thumb"
            variants.append(item)

        # 队列的条数往往比结果多得多（一次导入就会生成十几个变体 × 引擎），
        # 所以队列单独用更大的上限，而且**同状态下新任务排在前面** —— 手机上刚导入的
        # 图，它的任务应该立刻出现在队列顶部，而不是被几百条老任务挤到截断之外。
        job_cap = max(1200, findings_limit * 4)
        jobs = []
        for job in store.jobs(status="all", limit=job_cap):
            item = dict(job)
            if mode == "none":
                item["variant_path"] = ""
                item["seed_thumb"] = ""
            jobs.append(item)
        priority = {"running": 0, "pending": 1, "review": 2, "done": 3}
        jobs.sort(key=lambda row: (priority.get(row.get("status") or "", 4),
                                   -(row.get("id") or 0)))

        payload: Dict[str, Any] = {
            "version": 1,
            "generated_at": time.time(),
            "host": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", ""),
            "image_mode": mode,
            "thumb_side": thumb_side,
            "stats": store.stats(),
            "seeds": [dict(s) for s in store.seeds(limit=findings_limit)],
            "variants": variants,
            "findings": findings,
            "jobs": jobs,
            "archives": self.archives()["archives"] if include_archives else [],
            "archive_stats": store.archive_stats() if include_archives else {},
            "platforms": store.platform_counts(),
            "settings": {
                "w_novelty": store.get_setting("w_novelty"),
                "w_potential": store.get_setting("w_potential"),
                "w_bridging": store.get_setting("w_bridging"),
                "w_metadata": store.get_setting("w_metadata"),
            },
        }
        if include_trash:
            trash = store.findings(limit=findings_limit, order="trashed", trashed=True,
                                  include_dups=True)
            for item in trash:
                self._slim_for_mobile(item, mode, with_sources=with_sources)
            payload["trash"] = trash
        if include_archived:
            # 归档对象单独带一份：手机端要能点开某个批次看里面有什么，
            # 而且最好离线也能看（归档本来就是"这一轮结束了，留着回看"）。
            archived_rows = store.findings(limit=min(findings_limit, 800), archived=True,
                                           include_dups=False, order="value")
            for item in archived_rows:
                self._slim_for_mobile(item, mode, with_sources=with_sources)
            payload["archived"] = archived_rows
        payload["counts"] = {
            "findings": len(findings),
            "seeds": len(payload["seeds"]),
            "variants": len(variants),
            "jobs": len(jobs),
            "trash": len(payload.get("trash") or []),
            "archived": len(payload.get("archived") or []),
            "archives": len(payload["archives"]),
        }
        return payload

    def apply_mobile_sync(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """把手机端离线期间的操作合并回工作台。

        手机能做的离线操作只有"收集"和"清理"两类 —— 搜索本身（无头浏览器）必须在电脑上跑，
        所以这里不处理队列派发，只处理：
          * ``imports``：拍照/相册/手机上裁剪过的图 → 建成种子（可顺带入队，供电脑稍后自动识图）
          * ``trash``：手机上移进回收站的对象 id → 在工作台里同样软删除
        """
        import base64

        result: Dict[str, Any] = {"imported": [], "trashed": [], "errors": []}
        for entry in body.get("imports") or []:
            name = str(entry.get("name") or "手机导入")
            data_url = str(entry.get("data_url") or "")
            if "," not in data_url:
                result["errors"].append({"name": name, "error": "缺少图片数据"})
                continue
            try:
                raw = base64.b64decode(data_url.split(",", 1)[1])
                out = self.import_bytes(
                    raw, title=name, kind="mobile-import",
                    note=str(entry.get("note") or "由手机端同步导入"),
                    variants=entry.get("variants") or None,
                    auto_enqueue=bool(entry.get("auto_enqueue")),
                    engines=entry.get("engines") or None)
                out["client_id"] = entry.get("client_id") or ""
                result["imported"].append(out)
            except Exception as exc:  # noqa: BLE001
                result["errors"].append({"name": name, "error": str(exc)[:200]})
        trash_ids = [int(i) for i in (body.get("trash") or []) if str(i).isdigit()]
        if trash_ids:
            moved = self.trash_findings(trash_ids, reason="mobile", only_unpromoted=True)
            result["trashed"] = moved.get("moved") or []
            result["trash_skipped"] = moved.get("skipped") or []
        result["ok"] = not result["errors"]
        return result
    # -------------------------------------------------------------------- 归档
    def repair_source_labels(self) -> Dict[str, Any]:
        """修掉历史数据里被错贴的「引擎结果页」标题。

        早期版本给**所有**百度候选都写了「百度识图结果页（该引擎不提供原网页归属…）」当标题，
        后来才发现 pcsimi 一直带着 fromUrl。这些旧标题会一直显示在结果板和图谱上，所以这里做一次性清理：

        * 只有当这个对象**确实有外部来源页**时才清理（有来源就没必要自称"引擎页"）；
        * 只删自己写的那段固定文案，不动识别结论等其它内容。
        """
        marker = "百度识图结果页"          # 匹配旧长文案与新短标识两种历史写法
        fixed: List[int] = []
        for row in self.store.query(
                "SELECT id,title FROM findings WHERE title LIKE ?", (f"%{marker}%",)):
            finding_id = int(row["id"])
            external = self.store.one(
                """SELECT COUNT(*) AS n FROM sources WHERE finding_id=?
                   AND url LIKE 'http%' AND url NOT LIKE '%baidu.com%'
                   AND url NOT LIKE '%so.com%' AND url NOT LIKE '%360.cn%'""", (finding_id,))
            if not external or int(external["n"]) == 0:
                continue
            title = str(row["title"] or "")
            # 只删掉「<分隔> 百度识图结果页…」这一段，保留识别结论
            cleaned = re.sub(r"\s*[·|]\s*百度识图结果页[^·|]*$", "", title)
            if cleaned == title:
                cleaned = re.sub(r"百度识图结果页[^·|]*", "", title)
            cleaned = cleaned.strip(" ·|")
            self.store.execute("UPDATE findings SET title=? WHERE id=?", (cleaned, finding_id))
            fixed.append(finding_id)
        return {"fixed": len(fixed), "ids": fixed[:200]}

    def archive_scope_preview(self, finding_ids: Optional[Sequence[int]] = None) -> Dict[str, Any]:
        """入库归档前先看清"会归档什么"：数量、平台、时间跨度、体积。

        给了 ``finding_ids`` 就只统计这批（对应"只归档选中的图片"）。
        """
        # 一定要走 findings()：它会把每条对象的 sources 一起带上。以前这条路径用的是
        # store.finding()（单条、不带来源），于是归档批次一直显示「来源 0 · 平台 0」。
        all_rows = self.store.findings(limit=100000, include_dups=True, archived=False)
        if finding_ids is not None:
            wanted = {int(i) for i in finding_ids}
            rows = [r for r in all_rows if int(r.get("id") or 0) in wanted]
            live = [f for f in rows if not f.get("dup_of")]
            carriers = len(rows) - len(live)
        else:
            rows = all_rows
            live = [f for f in rows if not f.get("dup_of")]
            carriers = len(rows) - len(live)
        platforms: Dict[str, int] = {}
        sources = 0
        total_bytes = 0
        dates: List[str] = []
        for finding in live:
            total_bytes += int(finding.get("bytes") or 0)
            for source in finding.get("sources") or []:
                sources += 1
                key = source.get("platform") or "(未识别)"
                platforms[key] = platforms.get(key, 0) + 1
            if finding.get("created_at"):
                dates.append(time.strftime("%Y-%m-%d", time.localtime(finding["created_at"])))
        return {
            "findings": len(live),
            "carriers": carriers,
            "duplicates": carriers,
            "sources": sources,
            "platforms": sorted([{"platform": k, "n": v} for k, v in platforms.items()],
                                key=lambda x: -x["n"]),
            "platform_count": len([k for k in platforms if k != "(未识别)"]),
            "bytes": total_bytes,
            "date_from": min(dates) if dates else "",
            "date_to": max(dates) if dates else "",
            "promoted": len([f for f in live if f.get("status") == "promoted"]),
            "scope": "selection" if finding_ids is not None else "all",
        }

    def archive_now(self, *, name: str = "", note: str = "",
                    finding_ids: Optional[Sequence[int]] = None) -> Dict[str, Any]:
        """入库归档：给这批结果建一份快照记录，然后把它们从活动结果板上收起来。

        ``finding_ids`` 为空 = 归档**当前全部活动结果**（整批结案）；
        给了 id = 只归档**选中的那些**（结果板上勾选后点归档就是这条路径）。
        两种口径共用同一套快照逻辑，所以预览数、实际归档数、快照里的数永远一致。

        和回收站不同，归档**不是删除**：对象仍可通过归档视图查看、来源仍可追、随时能整体恢复。
        """
        preview = self.archive_scope_preview(finding_ids)
        if not preview["findings"]:
            raise ServiceError("选中的对象里没有可归档的内容（可能已归档或在回收站）")
        if finding_ids is not None:
            wanted = {int(i) for i in finding_ids}
            targets = [f for f in (self.store.finding(i) for i in wanted)
                       if f and not f.get("trashed") and not f.get("archived_at")
                       and not f.get("dup_of")]
        else:
            targets = self.store.findings(limit=100000, archived=False)
        triage: Dict[str, int] = {}
        for finding in targets:
            key = finding.get("triage") or "unknown"
            triage[key] = triage.get(key, 0) + 1
        stats = self.store.stats()
        label = "选中" if finding_ids is not None else "全部"
        archive_id = self.store.create_archive_run(
            name=name or f"{time.strftime('%Y-%m-%d %H:%M')} {label}批次",
            note=note or f"归档 {preview['findings']} 个视觉对象 / {preview['sources']} 条来源记录",
            stats={"findings": preview["findings"], "sources": preview["sources"],
                   "platforms": preview["platform_count"], "clusters": stats.get("clusters", 0),
                   "seeds": stats.get("seeds", 0)},
            triage=triage, platform_breakdown=preview["platforms"],
            date_from=preview["date_from"], date_to=preview["date_to"],
            total_bytes=preview["bytes"])
        archived = self.store.archive_findings([int(f["id"]) for f in targets], archive_id)
        # 重复载体跟着它们的对象一起走，否则活动视图里会剩下一堆"孤儿转载"。
        # 必须用子查询比对**父对象**的归档状态：直接拿当前批次的 id 去匹配 dup_of 是徒劳的，
        # 因为载体自己的 archived_at 还是 0（这正是它没被收走的原因）。
        self.store.execute(
            """UPDATE findings SET archived_at=?, archive_id=?
               WHERE dup_of>0 AND COALESCE(archived_at,0)=0
                 AND dup_of IN (SELECT id FROM findings WHERE COALESCE(archived_at,0)>0)""",
            (time.time(), int(archive_id)))
        for finding_id in archived:
            row = self.store.finding(finding_id) or {}
            cluster_id = int(row.get("cluster_id") or 0)
            if cluster_id:
                self.store.refresh_cluster(cluster_id)
        record = self.store.archive_run(archive_id) or {}
        return {"archive": record, "archived": len(archived), "preview": preview}

    def archives(self) -> Dict[str, Any]:
        runs = self.store.archive_runs()
        # 批次记录里的 对象/来源/平台 是归档那一刻的快照（老批次甚至是 0）。
        # 归档表要显示的是"这批东西现在是什么样"，所以按成员对象重算后覆盖。
        live = self.store.archive_live_counts()
        auto_note = re.compile(r"^归档 \d+ 个视觉对象 / \d+ 条来源记录$")
        for run in runs:
            counts = live.get(int(run.get("id") or 0))
            run["findings_snapshot"] = run.get("findings")
            run["sources_snapshot"] = run.get("sources")
            run["platforms_snapshot"] = run.get("platforms")
            if not counts:
                # 批次记录还在、但成员对象已经不在了（被恢复归档或删掉了）。
                # 显示"存档那一刻的 20 个对象"是骗人的，如实显示 0 并标为空批次。
                run["findings"] = 0
                run["findings_all"] = 0
                run["trashed"] = 0
                run["sources"] = 0
                run["platforms"] = 0
                run["empty"] = True
                if auto_note.match(str(run.get("note") or "")):
                    run["note"] = "这个批次里的对象已经不在了（被恢复归档或删除）"
                continue
            run["findings"] = counts["findings"]
            run["findings_all"] = counts["findings_all"]
            run["trashed"] = max(0, counts["findings_all"] - counts["findings"])
            run["sources"] = counts["sources"]
            run["platforms"] = counts["platforms"]
            run["empty"] = False
            # 早期版本自动生成的备注里写死了当时的（错误的）数字，显示时按现算重写；
            # 用户自己敲的备注一律不动。
            if auto_note.match(str(run.get("note") or "")):
                run["note"] = (f"归档 {counts['findings_all']} 个视觉对象 / "
                               f"{counts['sources']} 条来源记录")
        return {"archives": runs, "current": self.store.archive_stats(),
                "total": len(runs)}

    def archive_detail(self, archive_id: int, limit: int = 300) -> Dict[str, Any]:
        """打开一个归档批次。

        关键点：**已进回收站的对象也要返回**（`trashed=None` 不再过滤）。
        归档与回收站是正交状态 —— 否则一个批次只要有条目被移进回收站，整批就"看起来是空的"
        （真机+桌面都踩过：225 条里 209 条在回收站，于是每个批次打开都是 0 个对象）。
        返回里分别给出 live / trashed / carriers 三个计数，界面可以如实显示。
        """
        record = self.store.archive_run(archive_id)
        if record is None:
            raise ServiceError("归档记录不存在")
        items = self.store.findings(limit=limit, archive_id=archive_id, include_dups=True,
                                    order="value", archived=None, trashed=None)
        objects = [f for f in items if not f.get("dup_of")]
        live = [f for f in objects if not f.get("trashed")]
        trashed = [f for f in objects if f.get("trashed")]
        return {"archive": record, "findings": items, "count": len(live),
                "count_all": len(objects), "trashed": len(trashed),
                "carriers": len(items) - len(objects)}

    # --------------------------------------------------- 归档：落到磁盘的文件夹
    def archive_dir(self, archive_id: int, record: Optional[Dict[str, Any]] = None) -> str:
        """一个批次对应的磁盘目录：`data/archives/<id>-<名字>/`。

        归档原来只打状态标记，文件散在 media/results 里 —— 想拿这批图只能自己去翻。
        现在每个批次有一个独立目录（图片用**硬链接**放进 imgs/，同一卷上不占额外空间），
        电脑上可以直接打开，手机上可以把路径复制出来或者让电脑打开它。
        """
        record = record or self.store.archive_run(archive_id) or {}
        name = _safe_name(str(record.get("name") or f"batch-{archive_id}"))[:60]
        return os.path.join(self.root, "archives", f"{int(archive_id)}-{name}")

    def merge_items(self, archive_ids: Sequence[int]) -> List[Dict[str, Any]]:
        """把多个批次的对象合成一个列表（按 id 去重，按发散度排序）。

        这就是"合并归档相册"的数据来源：几个批次的图放在一起看/一起导出，
        不需要真的改数据库 —— 批次归属保持不变。
        """
        seen: Dict[int, Dict[str, Any]] = {}
        for archive_id in archive_ids:
            rows = self.store.findings(limit=100000, archive_id=int(archive_id),
                                       include_dups=True, order="value", archived=None,
                                       trashed=None)
            for row in rows:
                if row.get("dup_of"):
                    continue
                seen[int(row["id"])] = row
        out = list(seen.values())
        out.sort(key=lambda f: (-(f.get("explore_value") or 0), -(f.get("similarity") or 0)))
        return out

    def merge_info(self, archive_ids: Sequence[int]) -> Dict[str, Any]:
        ids = [int(i) for i in archive_ids]
        runs = [self.store.archive_run(i) for i in ids]
        runs = [r for r in runs if r]
        if not runs:
            raise ServiceError("没有可合并的归档批次")
        items = self.merge_items(ids)
        live = [f for f in items if not f.get("trashed")]
        platforms: Dict[str, int] = {}
        sources = 0
        total_bytes = 0
        dates: List[str] = []
        for finding in items:
            total_bytes += int(finding.get("bytes") or 0)
            for source in finding.get("sources") or []:
                sources += 1
                key = source.get("platform") or "(未识别)"
                platforms[key] = platforms.get(key, 0) + 1
            if finding.get("created_at"):
                dates.append(time.strftime("%Y-%m-%d", time.localtime(finding["created_at"])))
        return {"batches": [{"id": r["id"], "name": r.get("name") or ""} for r in runs],
                "items": items, "count": len(live), "count_all": len(items),
                "trashed": len(items) - len(live), "sources": sources,
                "platforms": sorted([{"platform": k, "n": v} for k, v in platforms.items()],
                                    key=lambda x: -x["n"]),
                "bytes": total_bytes, "date_from": min(dates) if dates else "",
                "date_to": max(dates) if dates else ""}

    def export_archive(self, archive_id: int) -> Dict[str, Any]:
        """把一个批次落盘成文件夹（见 export_collection）。"""
        record = self.store.archive_run(archive_id)
        if record is None:
            raise ServiceError("归档记录不存在")
        return self.export_collection([int(archive_id)], key=str(int(archive_id)),
                                      label=str(record.get("name") or f"批次{archive_id}"),
                                      target=self.archive_dir(archive_id, record))

    def export_merge(self, archive_ids: Sequence[int]) -> Dict[str, Any]:
        """把多个批次合并导出成一个文件夹（合并归档相册）。"""
        info = self.merge_info(archive_ids)
        key = "merge-" + "-".join(str(b["id"]) for b in info["batches"])[:40]
        label = "合并相册 " + " + ".join(f"#{b['id']}" for b in info["batches"])
        return self.export_collection([int(b["id"]) for b in info["batches"]], key=key,
                                      label=label)

    def export_collection(self, archive_ids: Sequence[int], *, key: str,
                          label: str, target: str = "") -> Dict[str, Any]:
        """把一批（可以是多个）归档批次落盘成一个文件夹。

        imgs/ 里是图片（硬链接，同一卷不占额外空间），manifest.json 是可读清单。
        幂等：已经导出过的不会重复复制。

        `target` 只给单批次用：目录名必须与 `archive_dir()` 一致，否则「先导出、后查路径」
        会指到两个不同的文件夹（曾经出现过：导出写 archives/24，查路径读 archives/24-名字）。
        """
        if not target:
            target = os.path.join(self.root, "archives", f"{_safe_name(key)[:60]}")
        img_dir = os.path.join(target, "imgs")
        os.makedirs(img_dir, exist_ok=True)

        if len(archive_ids) == 1:
            items = self.store.findings(limit=100000, archive_id=int(archive_ids[0]),
                                        include_dups=True, order="value", archived=None,
                                        trashed=None)
        else:
            items = self.merge_items(archive_ids)
        objects = [f for f in items if not f.get("dup_of")]
        linked = copied = reused = skipped = 0
        manifest_items: List[Dict[str, Any]] = []
        for finding in objects:
            rel = finding.get("rel_path") or ""
            source = self.abs_media(rel) if rel else ""
            entry = {
                "id": finding.get("id"),
                "title": finding.get("title") or "",
                "triage": finding.get("triage") or "",
                "explore_value": finding.get("explore_value"),
                "similarity": finding.get("similarity"),
                "width": finding.get("width"), "height": finding.get("height"),
                "bytes": finding.get("bytes"),
                "engine": finding.get("engine") or "",
                "image_url": Store._slim_url(finding.get("image_url") or ""),
                "sources": [{"platform": s.get("platform") or "", "url": Store._slim_url(s.get("url") or "")}
                            for s in (finding.get("sources") or [])],
                "file": "",
            }
            if source and os.path.isfile(source):
                ext = os.path.splitext(source)[1] or ".jpg"
                # id 前缀保证唯一（合并多个批次时标题可能重复）
                name = f"{finding.get('id')}-{_safe_name(str(finding.get('title') or 'result'))[:48]}{ext}"
                dest = os.path.join(img_dir, name)
                entry["file"] = f"imgs/{name}"
                if os.path.exists(dest):
                    reused += 1                    # 上次已经导出过，直接复用
                else:
                    try:
                        os.link(source, dest)          # 硬链接：同一卷上不占额外空间
                        linked += 1
                    except OSError:
                        try:
                            shutil.copy2(source, dest)
                            copied += 1
                        except OSError:
                            skipped += 1
                            entry["file"] = ""
            else:
                skipped += 1
                entry["file"] = ""
            manifest_items.append(entry)

        runs = [self.store.archive_run(i) for i in archive_ids]
        runs = [r for r in runs if r]
        first = runs[0] if runs else {}
        # 清单里的数字按"实际导出的对象"重新算，不抄批次记录 —— 记录是归档那一刻的，
        # 之后又进回收站/又补来源的话会跟文件夹里真实内容对不上（合并多个批次时尤其如此）。
        man_sources = sum(len(f.get("sources") or []) for f in objects)
        man_platforms = {s.get("platform") or "(未识别)"
                         for f in objects for s in (f.get("sources") or [])}
        man_dates = [time.strftime("%Y-%m-%d", time.localtime(f["created_at"]))
                     for f in objects if f.get("created_at")]
        manifest = {
            "archive_id": int(first.get("id") or 0),
            "archive_ids": [int(r["id"]) for r in runs],
            "name": label,
            "note": first.get("note") or "",
            "created_at": first.get("created_at"),
            "findings": len(objects),
            "trashed": len([f for f in objects if f.get("trashed")]),
            "carriers": len(items) - len(objects),
            "sources": man_sources,
            "platforms": len(man_platforms),
            "date_from": min(man_dates) if man_dates else "",
            "date_to": max(man_dates) if man_dates else "",
            "exported_at": time.time(),
            "items": manifest_items,
        }
        with open(os.path.join(target, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        return {"dir": target, "key": key, "name": label,
                "images": {"linked": linked, "copied": copied,
                           "reused": reused, "skipped": skipped},
                "findings": len(objects), "files": len(os.listdir(img_dir))}

    def archive_zip(self, archive_id: int) -> Dict[str, Any]:
        """把一个批次打包成 zip，给手机端「保存到手机」用。"""
        record = self.store.archive_run(archive_id)
        if record is None:
            raise ServiceError("归档记录不存在")
        return self.zip_collection(self.export_archive(archive_id))

    def merge_zip(self, archive_ids: Sequence[int]) -> Dict[str, Any]:
        """把多个批次合并打包成 zip（合并归档相册——一次拿到所有图）。"""
        return self.zip_collection(self.export_merge(archive_ids))

    def zip_collection(self, info: Dict[str, Any]) -> Dict[str, Any]:
        """把已落盘的文件夹打成 zip。

        手机本地打不开电脑上的目录，所以"看到归档文件夹"必须靠一份能落到手机存储里的东西 ——
        一个 zip（图片 + manifest）就够了，相册/文件管理器都能直接打开。
        结果缓存在 `<文件夹>.zip`，内容比缓存新就重建。
        """
        src_dir = info["dir"]
        zip_path = src_dir.rstrip("\\/") + ".zip"
        newest = 0.0
        for root, _dirs, files in os.walk(src_dir):
            for name in files:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, name)))
                except OSError:
                    pass
        if not (os.path.isfile(zip_path) and os.path.getmtime(zip_path) >= newest):
            tmp = zip_path + ".part"
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as bundle:
                for root, dirs, files in os.walk(src_dir):
                    dirs.sort()
                    for name in sorted(files):
                        full = os.path.join(root, name)
                        rel = os.path.relpath(full, src_dir).replace(os.sep, "/")
                        bundle.write(full, rel)
            os.replace(tmp, zip_path)
        return {"path": zip_path, "name": os.path.basename(zip_path),
                "bytes": os.path.getsize(zip_path), "files": info["files"]}

    def archive_paths(self, archive_id: int, export: bool = False) -> Dict[str, Any]:
        """批次的磁盘路径（export=True 时先落盘）。"""
        record = self.store.archive_run(archive_id)
        if record is None:
            raise ServiceError("归档记录不存在")
        info = self.export_archive(archive_id) if export else None
        target = (info or {}).get("dir") or self.archive_dir(archive_id, record)
        files: List[str] = []
        img_dir = os.path.join(target, "imgs")
        if os.path.isdir(img_dir):
            files = sorted(os.path.join(img_dir, n) for n in os.listdir(img_dir))
        return {"dir": target, "exists": os.path.isdir(target), "files": files[:500],
                "count": len(files), "exported": info}

    def unarchive(self, archive_id: Optional[int] = None) -> Dict[str, Any]:
        restored = self.store.unarchive(archive_id=archive_id)
        for finding_id in restored:
            row = self.store.finding(finding_id) or {}
            cluster_id = int(row.get("cluster_id") or 0)
            if cluster_id:
                self.store.refresh_cluster(cluster_id)
        return {"restored": len(restored), "ids": restored[:200]}

    # ------------------------------------------------------------------- report
    def report(self, days: int = 30) -> Dict[str, Any]:
        stats = self.store.stats()
        by_day = [dict(r) for r in self.store.query(
            """SELECT day, COUNT(*) AS ops, COALESCE(SUM(findings_new),0) AS findings,
                      COALESCE(SUM(dup_images),0) AS dups, COALESCE(SUM(new_sources),0) AS sources,
                      COALESCE(SUM(new_platforms),0) AS platforms
               FROM ledger GROUP BY day ORDER BY day DESC LIMIT ?""", (days,))]
        by_engine = [dict(r) for r in self.store.query(
            """SELECT engine, COUNT(*) AS ops, COALESCE(SUM(findings_new),0) AS findings,
                      COALESCE(SUM(dup_images),0) AS dups, COALESCE(SUM(new_sources),0) AS sources
               FROM ledger GROUP BY engine ORDER BY findings DESC""")]
        variant_yield = [dict(r) for r in self.store.query(
            """SELECT v.kind AS kind, v.label AS label, COUNT(*) AS ops,
                      COALESCE(SUM(l.findings_new),0) AS findings,
                      COALESCE(SUM(l.dup_images),0) AS dups
               FROM ledger l JOIN variants v ON v.id=l.variant_id
               GROUP BY v.kind ORDER BY findings DESC""")]
        return {"stats": stats, "by_day": by_day, "by_engine": by_engine,
                "variant_yield": variant_yield, "platforms": self.store.platform_counts()}
