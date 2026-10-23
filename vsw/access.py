"""访问密钥：把工作台开放给别人时，那道门。

设计取舍（都是踩过或想清楚才这么定的）：

1. **密钥分角色，不是一把钥匙管所有事。**
   把工作台开给朋友看，不等于让他能删你的回收站、清空队列、重建库。
   ``owner`` = 你自己（管密钥、彻底删除、在电脑上打开文件夹）；
   ``edit`` = 能搜、能导入、能进回收站（协作的人）；
   ``view`` = 只能看（只读，任何写操作都 403）。

2. **码要能手打。** 手机端第一次连的时候，最省事的路径是"看一眼屏幕，敲进去"。
   所以码是 ``VSW-XXXX-XXXX-XXXX``：16 个字符里 12 个有效位，约 60 bit，
   字母表去掉了 I/O/0/1（手写和口述不会认错）。需要更强的场景可以同时开多条密钥。

3. **库里只存哈希。** 明文码只在创建那一刻返回一次。拿到库也拿不到能用的码。
   每条密钥独立加盐，比较用 :func:`hmac.compare_digest`。

4. **失败要限速。** 12 位码虽然难猜，但它是一个可以无限次的在线口令 ——
   同一个 IP 5 分钟内错 8 次就先锁 5 分钟，避免被当成爆破靶子。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

ROLE_OWNER = "owner"
ROLE_EDIT = "edit"
ROLE_VIEW = "view"

ROLE_RANK = {ROLE_VIEW: 1, ROLE_EDIT: 2, ROLE_OWNER: 3}
ROLE_LABEL = {ROLE_OWNER: "主人（全部权限）", ROLE_EDIT: "可操作（不能管密钥）",
              ROLE_VIEW: "只看（只读）"}
ROLE_NOTE = {
    ROLE_OWNER: "能建/撤密钥、彻底删除、清空回收站、在电脑上打开文件夹",
    ROLE_EDIT: "能搜索、导入、归档、移入回收站；不能删密钥、不能彻底删除",
    ROLE_VIEW: "只能看结果、归档、队列；所有写操作都会被拒绝",
}

# 手打友好的字母表：去掉 I / O / 0 / 1
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_PREFIX = "VSW"
_GROUPS = 3
_GROUP_SIZE = 4

MAX_FAILS = 8
FAIL_WINDOW = 300.0
LOCK_SECONDS = 300.0
TOUCH_INTERVAL = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS access_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT    NOT NULL DEFAULT '',
    role         TEXT    NOT NULL DEFAULT 'view',
    salt         TEXT    NOT NULL,
    hash         TEXT    NOT NULL,
    created_at   REAL    NOT NULL DEFAULT 0,
    revoked_at   REAL    NOT NULL DEFAULT 0,
    last_used_at REAL    NOT NULL DEFAULT 0,
    last_ip      TEXT    NOT NULL DEFAULT '',
    uses         INTEGER NOT NULL DEFAULT 0
);
"""


def new_code() -> str:
    """生成一条新密钥：``VSW-XXXX-XXXX-XXXX``。"""
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_GROUPS * _GROUP_SIZE))
    groups = [body[i:i + _GROUP_SIZE] for i in range(0, len(body), _GROUP_SIZE)]
    return f"{_PREFIX}-" + "-".join(groups)


def normalize(code: str) -> str:
    """把手打的/带空格的/大小写混杂的输入收拾成统一形状。

    接受 ``vsw abcd efgh jklm`` / ``VSW-ABCD-EFGH-JKLM`` / ``abcdefghjklm``，
    一律折成 ``ABCDEFGHJKLM``（不含前缀、不含连字符）。
    """
    cleaned = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    if cleaned.startswith(_PREFIX):
        cleaned = cleaned[len(_PREFIX):]
    return cleaned


def looks_like_code(code: str) -> bool:
    body = normalize(code)
    return len(body) == _GROUPS * _GROUP_SIZE and all(ch in _ALPHABET for ch in body)


def pretty(code: str) -> str:
    """规范形状，剪贴板里给人看的那个。"""
    body = normalize(code)
    groups = [body[i:i + _GROUP_SIZE] for i in range(0, len(body), _GROUP_SIZE)]
    return f"{_PREFIX}-" + "-".join(g for g in groups if g)


def _hash(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{normalize(code)}".encode("utf-8")).hexdigest()


class AccessStore:
    """密钥表 + 失败限速。线程安全（HTTP 服务是多线程的）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._fails: Dict[str, List[float]] = {}
        self._locked_until: Dict[str, float] = {}

    # ------------------------------------------------------------------ reads
    def keys(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, label, role, created_at, revoked_at, last_used_at, last_ip, uses "
                "FROM access_keys ORDER BY revoked_at > 0, id DESC").fetchall()
        out = []
        now = time.time()
        for row in rows:
            item = dict(row)
            item["revoked"] = bool(item["revoked_at"])
            item["role_label"] = ROLE_LABEL.get(item["role"], item["role"])
            item["active"] = not item["revoked"]
            item["age_days"] = int((now - (item["created_at"] or now)) // 86400)
            item["idle_days"] = (int((now - item["last_used_at"]) // 86400)
                                 if item["last_used_at"] else None)
            out.append(item)
        return out

    def active_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM access_keys WHERE revoked_at = 0").fetchone()
        return int(row["n"] if row else 0)

    def enabled(self) -> bool:
        """有任意一条有效密钥 = 这道门关上了（未登录一律 401）。"""
        return self.active_count() > 0

    def roles(self) -> List[Dict[str, str]]:
        return [{"role": role, "label": ROLE_LABEL[role], "note": ROLE_NOTE[role],
                 "rank": ROLE_RANK[role]}
                for role in (ROLE_OWNER, ROLE_EDIT, ROLE_VIEW)]

    # ------------------------------------------------------------------ writes
    def create(self, label: str = "", role: str = ROLE_VIEW) -> Dict[str, Any]:
        role = role if role in ROLE_RANK else ROLE_VIEW
        code = new_code()
        salt = secrets.token_hex(8)
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO access_keys (label, role, salt, hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(label or "").strip()[:60] or "未命名", role, salt, _hash(salt, code), now))
            self._conn.commit()
            key_id = int(cur.lastrowid)
        return {"id": key_id, "code": pretty(code), "label": label, "role": role,
                "role_label": ROLE_LABEL.get(role, role), "created_at": now}

    def revoke(self, key_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE access_keys SET revoked_at = ? WHERE id = ? AND revoked_at = 0",
                (time.time(), int(key_id)))
            self._conn.commit()
        return cur.rowcount > 0

    def restore(self, key_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE access_keys SET revoked_at = 0 WHERE id = ? AND revoked_at > 0",
                (int(key_id),))
            self._conn.commit()
        return cur.rowcount > 0

    def delete(self, key_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM access_keys WHERE id = ?", (int(key_id),))
            self._conn.commit()
        return cur.rowcount > 0

    def rename(self, key_id: int, label: str) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE access_keys SET label = ? WHERE id = ?",
                                     (str(label or "").strip()[:60], int(key_id)))
            self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------- verification
    def verify(self, code: str, ip: str = "") -> Optional[Dict[str, Any]]:
        """校验一条密钥；返回 ``{id, label, role}``，不对返回 ``None``。

        「不对」包括：形状不对、哈希不匹配、已被撤销。这三种对外都是同一个 401，
        不告诉对方是哪一种（少给爆破一点信息）。
        """
        body = normalize(code)
        if not looks_like_code(body):
            return None
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, label, role, salt, hash, last_used_at FROM access_keys "
                "WHERE revoked_at = 0").fetchall()
            matched = None
            for row in rows:
                if hmac.compare_digest(row["hash"], _hash(row["salt"], body)):
                    matched = row
                    break
            if matched is None:
                return None
            if now - float(matched["last_used_at"] or 0) > TOUCH_INTERVAL:
                self._conn.execute(
                    "UPDATE access_keys SET last_used_at = ?, last_ip = ?, uses = uses + 1 "
                    "WHERE id = ?", (now, str(ip or "")[:64], matched["id"]))
                self._conn.commit()
            return {"id": int(matched["id"]), "label": matched["label"],
                    "role": matched["role"]}

    # ------------------------------------------------------------- rate limiting
    def blocked_for(self, ip: str) -> float:
        """这个 IP 还要等多少秒才能再试（0 = 没被锁）。"""
        now = time.time()
        with self._lock:
            until = self._locked_until.get(ip, 0.0)
            if until > now:
                return until - now
            if until:
                self._locked_until.pop(ip, None)
            stamps = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW]
            self._fails[ip] = stamps
            if len(stamps) >= MAX_FAILS:
                self._locked_until[ip] = now + LOCK_SECONDS
                self._fails[ip] = []
                return LOCK_SECONDS
        return 0.0

    def note_failure(self, ip: str) -> int:
        now = time.time()
        with self._lock:
            stamps = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW]
            stamps.append(now)
            self._fails[ip] = stamps
            return max(0, MAX_FAILS - len(stamps))

    def note_success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)
            self._locked_until.pop(ip, None)

    # --------------------------------------------------------------- lifecycle
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


def ensure_owner_key(store: AccessStore, label: str = "主人",
                    code: str = "") -> Dict[str, Any]:
    """没有密钥时，建一条主人密钥（启动时 ``--protect`` 用）。

    允许指定 ``code``（``--key``），这样重装/重启后手上的码还是同一个。
    """
    if store.active_count() > 0:
        return {"created": False}
    if code and looks_like_code(code):
        salt = secrets.token_hex(8)
        now = time.time()
        with store._lock:  # noqa: SLF001 - 同模块内，故意的
            cur = store._conn.execute(
                "INSERT INTO access_keys (label, role, salt, hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (label, ROLE_OWNER, salt, _hash(salt, code), now))
            store._conn.commit()
            key_id = int(cur.lastrowid)
        return {"created": True, "id": key_id, "code": pretty(code),
                "label": label, "role": ROLE_OWNER}
    made = store.create(label, ROLE_OWNER)
    made["created"] = True
    return made


def default_db_path(root: str) -> str:
    """密钥表跟工作台数据放一起（同一个 SQLite 文件，少一个要备份的东西）。"""
    return os.path.join(root, "workbench.sqlite3")
