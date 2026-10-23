"""工作区仓库：按工作区把 Workbench 与它的访客密钥库造出来、缓存住。

一个工作区 = 一个目录（自己的 ``workbench.sqlite3`` + ``media/`` + ``archives/``）。
所以"私人工作区"是真的分开的：两张数据库、两套文件，谁也读不到谁。

服务端每个请求只关心"这次该看哪个工作区"，其余交给这个 hub。
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable, Dict, Optional

from .access import AccessStore
from .accounts import AccountStore, slugify
from .service import Workbench

__all__ = ["WorkspaceHub"]


class WorkspaceHub:
    def __init__(self, auth: AccountStore, root: str, *,
                 log: Optional[Callable[[str], None]] = None,
                 allow_private: bool = False) -> None:
        self.auth = auth
        self.root = os.path.abspath(root)
        self.ws_root = os.path.join(self.root, "ws")
        self.allow_private = allow_private
        self._log = log or (lambda msg: None)
        self._services: Dict[int, Workbench] = {}
        self._access: Dict[int, AccessStore] = {}
        self._lock = threading.RLock()
        os.makedirs(self.ws_root, exist_ok=True)

    # ---------------------------------------------------------------- 目录
    def dir_for(self, ws_id: int) -> str:
        """取工作区目录。老数据（第一个工作区）直接用服务根目录，不搬那 58 MB 媒体。"""
        row = self.auth.workspace(int(ws_id))
        if not row:
            raise KeyError(f"工作区 {ws_id} 不存在")
        target = row.get("dir") or ""
        return os.path.abspath(target) if target else self._default_dir(row)

    def _default_dir(self, row: Dict[str, Any]) -> str:
        return os.path.join(self.ws_root, f"{row['id']}-{slugify(row.get('name') or 'ws')}")

    def ensure_dir(self, ws_id: int) -> str:
        path = self.dir_for(ws_id)
        os.makedirs(path, exist_ok=True)
        return path

    # ---------------------------------------------------------------- 服务
    def service(self, ws_id: int) -> Workbench:
        ws_id = int(ws_id)
        with self._lock:
            cached = self._services.get(ws_id)
            if cached is not None:
                return cached
            path = self.ensure_dir(ws_id)
            svc = Workbench(path, log=self._log, allow_private=self.allow_private)
            self._services[ws_id] = svc
            return svc

    def access(self, ws_id: int) -> AccessStore:
        """访客密钥是"某个工作区里的" —— 换一个工作区，同一把码不通用。"""
        ws_id = int(ws_id)
        with self._lock:
            cached = self._access.get(ws_id)
            if cached is not None:
                return cached
            db = os.path.join(self.ensure_dir(ws_id), "workbench.sqlite3")
            store = AccessStore(db)
            self._access[ws_id] = store
            return store

    def drop(self, ws_id: int) -> None:
        with self._lock:
            svc = self._services.pop(int(ws_id), None)
            store = self._access.pop(int(ws_id), None)
        for item in (svc, store):
            if item is None:
                continue
            try:
                item.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        with self._lock:
            services = list(self._services.values())
            stores = list(self._access.values())
            self._services.clear()
            self._access.clear()
        for svc in services:
            try:
                svc.store.close()
            except Exception:  # noqa: BLE001
                pass
        for store in stores:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 建工作区
    def create_workspace(self, name: str, *, owner_id: int, kind: str = "shared") -> Dict[str, Any]:
        """先在库里拿到 id，再按 id 建目录 —— 目录名要带 id 才不会重名打架。"""
        row = self.auth.create_workspace(name, "", owner_id=owner_id, kind=kind)
        path = self._default_dir(row)
        os.makedirs(path, exist_ok=True)
        self.auth.set_workspace_dir(int(row["id"]), path)
        row = self.auth.workspace(int(row["id"])) or row
        return row
