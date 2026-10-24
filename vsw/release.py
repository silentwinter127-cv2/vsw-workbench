"""应用自我更新：这个 APK 从哪里拿到「最新版本」，以及怎么把新包发出去。

Android 不允许 App 静默装自己 —— 系统一定会弹一次安装框。所以这里的职责只有两件：

  1. **如实回答「有没有新版本」**：给自己电脑上的工作台发一条 `/api/app/version`，
     拿不到再退到 GitHub Releases（`--gh OWNER/REPO`）。
  2. **把新包交出去**：`/api/app/apk` 流式返回本地 `dist/` 里的 APK，附带大小与 SHA256，
     手机端下载后自己校验一次再交给系统安装。

版本号就是构建标签（`r17` → `17`）。比较用这个整数，不用字符串比 —— 否则 `r9` > `r17`。

发布的包放在数据目录下的 `dist/`：

    vsw/data/dist/vsw-workbench.apk
    vsw/data/dist/version.json     {"build": "r17", "notes": "...", "channel": "stable"}

`scripts/vsw-publish-apk.py` 会读 `MainActivity.java` 里的 `BUILD_TAG` 自动生成这两件东西，
`build_release.bat` 打完包就调它 —— 于是"打一次包"就等于"发布一次"。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
from typing import Any, Dict, Optional

APK_NAME = "vsw-workbench.apk"
META_NAME = "version.json"
APK_MIME = "application/vnd.android.package-archive"

# GitHub 的公共 API 有速率限制，而且手机每次启动都会问一次 —— 缓存一会儿。
_GH_TTL = 300.0
_gh_cache: Dict[str, Any] = {"at": 0.0, "key": "", "value": None}


def build_code(tag: str) -> int:
    """把 `r17` / `R17` / `17` / `1.2.3` 都折成一个可比较的整数。

    规则：取第一段「r/v + 数字」，否则第一段纯数字。`r17-debug` 也算 17，`v3` 也算 3。
    """
    text = str(tag or "").strip()
    matched = re.search(r"[rRvV]\s*(\d+)", text)
    if matched:
        return int(matched.group(1))
    matched = re.match(r"\s*(\d+)", text)
    if matched:
        return int(matched.group(1))
    return 0


def dist_dir(root: str) -> str:
    return os.path.join(root, "dist")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _read_meta(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


_apk_cache: Dict[str, Any] = {"key": "", "value": None}


def local_release(root: str) -> Optional[Dict[str, Any]]:
    """读 `dist/` 里那一个包。没有包就返回 None（不是错误：很多人只用网页版）。"""
    apk_path = os.path.join(dist_dir(root), APK_NAME)
    if not os.path.isfile(apk_path):
        return None
    stat = os.stat(apk_path)
    # SHA256 要读整个文件，按 (路径, 大小, mtime) 缓存，别每次请求都算一遍
    key = f"{apk_path}:{stat.st_size}:{int(stat.st_mtime)}"
    digest = ""
    if _apk_cache.get("key") == key:
        digest = _apk_cache["value"]
    else:
        digest = _sha256(apk_path)
        _apk_cache["key"] = key
        _apk_cache["value"] = digest
    meta = _read_meta(os.path.join(dist_dir(root), META_NAME))
    # 没有 version.json（或它坏了）时**不要编一个版本号**：宁可说"说不清"，
    # 也不能让界面拿一个假标签去比大小（曾经这里退化成 "size1204" 这种鬼东西）。
    tag = str(meta.get("build") or "").strip()
    return {
        "source": "local",
        "build": tag,
        "build_code": build_code(tag),
        "size": stat.st_size,
        "sha256": digest,
        "notes": str(meta.get("notes") or ""),
        "channel": str(meta.get("channel") or "stable"),
        "updated_at": int(stat.st_mtime),
        "url": "/api/app/apk",
    }


def github_release(repo: str, *, timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    """查 GitHub Releases 里最新的那个带 .apk 的版本。失败一律返回 None（不抛）。"""
    repo = (repo or "").strip().strip("/")
    if not repo or "/" not in repo:
        return None
    now = time.time()
    if (_gh_cache["key"] == repo and _gh_cache["value"] is not None
            and now - float(_gh_cache["at"] or 0) < _GH_TTL):
        return _gh_cache["value"]
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "vsw-workbench-update-check",
    })
    value: Optional[Dict[str, Any]] = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assets = data.get("assets") or []
        asset = next((a for a in assets if str(a.get("name", "")).lower().endswith(".apk")), None)
        if asset:
            tag = str(data.get("tag_name") or data.get("name") or "").strip()
            # 新版 GitHub API 会在 asset 上给 digest: "sha256:..."
            digest = str(asset.get("digest") or "")
            if digest.lower().startswith("sha256:"):
                digest = digest.split(":", 1)[1].upper()
            value = {
                "source": "github",
                "build": tag,
                "build_code": build_code(tag),
                "size": int(asset.get("size") or 0),
                "sha256": digest.upper() if digest else "",
                "notes": str(data.get("body") or "")[:2000],
                "channel": "stable",
                "updated_at": int(time.time()),
                "url": str(asset.get("browser_download_url") or ""),
            }
    except Exception:  # noqa: BLE001 - 没网/被墙/限速都只是"没查到"
        value = None
    _gh_cache.update({"at": now, "key": repo, "value": value})
    return value


def latest(root: str, *, gh_repo: str = "", current: str = "") -> Dict[str, Any]:
    """当前能提供的最新版本（服务端优先，GitHub 兜底）。

    返回体总是可用的：查不到任何包时 `available=False`，并带上 `current` 与比较结果，
    这样界面能如实说「没配更新源」而不是假装"已是最新"。
    """
    here = local_release(root)
    cloud = github_release(gh_repo) if gh_repo else None
    picks = [p for p in (here, cloud) if p]
    best: Optional[Dict[str, Any]] = None
    for item in picks:
        if best is None or int(item.get("build_code") or 0) > int(best.get("build_code") or 0):
            best = item
    cur_code = build_code(current)
    out: Dict[str, Any] = {
        "available": bool(best),
        "current": str(current or ""),
        "current_code": cur_code,
        "sources": {"local": bool(here), "github": bool(cloud)},
        "github": gh_repo or "",
    }
    if best:
        out.update(best)
        # 只有两边都认得出版本号时才敢下结论；否则交给界面说"说不清"
        out["comparable"] = bool(cur_code and int(best.get("build_code") or 0))
        out["update_available"] = bool(out["comparable"]
                                       and int(best["build_code"]) > cur_code)
    else:
        out["update_available"] = False
    return out


def apk_path(root: str) -> Optional[str]:
    path = os.path.join(dist_dir(root), APK_NAME)
    return path if os.path.isfile(path) else None
