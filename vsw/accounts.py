"""账号 / 工作区 / 成员 / 会话。

一个「工作区」就是一个数据目录：自己的 SQLite、自己的媒体、自己的归档、自己的访客密钥。
所以私人工作区是**物理隔离**的 —— 不是靠每张表加 owner_id 过滤出来的假隔离。

这张库（``auth.sqlite3``）只放身份关系，不放图片内容。

三个角色沿用 ``vsw.access`` 的那套：``owner`` 管人管密钥、``edit`` 能搜能归档、
``view`` 只读。区别只是从前它挂在密钥上，现在挂"某人在某个工作区里"。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .access import ROLE_EDIT, ROLE_LABEL, ROLE_NOTE, ROLE_OWNER, ROLE_RANK, ROLE_VIEW

__all__ = [
    "AccountError", "AccountStore", "SESSION_COOKIE", "WORKSPACE_COOKIE",
    "default_auth_path", "public_user", "slugify",
]

SESSION_COOKIE = "vsw_sid"
WORKSPACE_COOKIE = "vsw_ws"
SESSION_DAYS = 30

# 手打友好：去掉 I / O / 0 / 1
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_PBKDF2_ROUNDS = 120_000
_MIN_PASSWORD = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    name_key      TEXT    NOT NULL UNIQUE,   -- 小写去空格，用来判重
    salt          TEXT    NOT NULL,
    hash          TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL    NOT NULL,
    last_login_at REAL    NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS workspaces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    slug        TEXT    NOT NULL DEFAULT '',
    dir         TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'shared',   -- personal | shared
    created_by  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    ws_id      INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL DEFAULT 'view',
    created_at REAL    NOT NULL,
    PRIMARY KEY (ws_id, user_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at REAL    NOT NULL,
    last_seen  REAL    NOT NULL,
    expires_at REAL    NOT NULL,
    ip         TEXT    NOT NULL DEFAULT '',
    ua         TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invites (
    code_hash  TEXT PRIMARY KEY,
    ws_id      INTEGER NOT NULL,
    role       TEXT    NOT NULL DEFAULT 'view',
    created_by INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL,
    expires_at REAL    NOT NULL,
    used_at    REAL    NOT NULL DEFAULT 0,
    used_by    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_fails (
    ip         TEXT PRIMARY KEY,
    fails      INTEGER NOT NULL DEFAULT 0,
    first_at   REAL    NOT NULL DEFAULT 0,
    locked_at  REAL    NOT NULL DEFAULT 0
);
"""

MAX_FAILS = 10
FAIL_WINDOW = 300.0
LOCK_SECONDS = 300.0


class AccountError(Exception):
    """可以直接写给用户看的错误（密码太短、名字被占用……）。"""


def default_auth_path(root: str) -> str:
    return os.path.join(os.path.abspath(root), "auth.sqlite3")


def slugify(name: str, fallback: str = "ws") -> str:
    text = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", (name or "").strip()).strip("-")
    # 目录名里不保留中文会让"我的工作台"变成一串拼音乱码，这里只做长度与非法字符处理。
    text = re.sub(r"[\\/:*?\"<>|]+", "", text)[:24]
    return text or fallback


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt.encode("utf-8"), _PBKDF2_ROUNDS).hex()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_invite_code() -> str:
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "INV-" + "-".join(groups)


def normalize_invite(code: str) -> str:
    text = re.sub(r"[^0-9A-Za-z]", "", (code or "")).upper()
    if text.startswith("INV"):
        text = text[3:]
    return text


def public_user(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """剥掉盐和哈希 —— 这对象会被塞进 JSON 发到浏览器。"""
    if not row:
        return None
    return {"id": row["id"], "name": row["name"], "is_admin": bool(row.get("is_admin")),
            "created_at": row.get("created_at", 0), "last_login_at": row.get("last_login_at", 0)}


class AccountStore:
    def __init__(self, db_path: str) -> None:
        self.path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ 小工具
    def _q(self, sql: str, params: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
        with self._lock:
            cur = self._db.execute(sql, params)
            return cur.fetchall()

    def _x(self, sql: str, params: Tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    # ------------------------------------------------------------------- 状态
    def has_users(self) -> bool:
        return bool(self._q("SELECT 1 FROM users LIMIT 1"))

    def user_count(self) -> int:
        return int(self._q("SELECT COUNT(*) AS n FROM users")[0]["n"])

    def admin_exists(self) -> bool:
        return bool(self._q("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1"))

    def users(self) -> List[Dict[str, Any]]:
        rows = self._q("SELECT * FROM users ORDER BY id")
        out = []
        for row in rows:
            item = public_user(dict(row)) or {}
            item["workspaces"] = len(self.memberships_of(int(row["id"])))
            out.append(item)
        return out

    # -------------------------------------------------------------------- 账号
    def user(self, user_id: int) -> Optional[Dict[str, Any]]:
        rows = self._q("SELECT * FROM users WHERE id=?", (int(user_id),))
        return dict(rows[0]) if rows else None

    def user_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        key = (name or "").strip().lower()
        if not key:
            return None
        rows = self._q("SELECT * FROM users WHERE name_key=?", (key,))
        return dict(rows[0]) if rows else None

    def create_user(self, name: str, password: str, *, is_admin: bool = False) -> Dict[str, Any]:
        name = (name or "").strip()
        if len(name) < 2:
            raise AccountError("名字至少 2 个字符")
        if len(name) > 32:
            raise AccountError("名字太长（最多 32 个字符）")
        if len(password or "") < _MIN_PASSWORD:
            raise AccountError(f"密码至少 {_MIN_PASSWORD} 位")
        key = name.lower()
        if self.user_by_name(name):
            raise AccountError(f"「{name}」这个名字已经有人用了")
        salt = secrets.token_hex(16)
        now = time.time()
        cur = self._x(
            "INSERT INTO users(name, name_key, salt, hash, is_admin, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (name, key, salt, _hash_password(password, salt), 1 if is_admin else 0, now))
        row = self.user(int(cur.lastrowid))
        assert row is not None
        return row

    def set_password(self, user_id: int, password: str) -> bool:
        if len(password or "") < _MIN_PASSWORD:
            raise AccountError(f"密码至少 {_MIN_PASSWORD} 位")
        salt = secrets.token_hex(16)
        cur = self._x("UPDATE users SET salt=?, hash=? WHERE id=?",
                      (salt, _hash_password(password, salt), int(user_id)))
        self.drop_user_sessions(int(user_id))
        return bool(cur.rowcount)

    def rename_user(self, user_id: int, name: str) -> bool:
        name = (name or "").strip()
        if len(name) < 2:
            raise AccountError("名字至少 2 个字符")
        other = self.user_by_name(name)
        if other and int(other["id"]) != int(user_id):
            raise AccountError(f"「{name}」这个名字已经有人用了")
        cur = self._x("UPDATE users SET name=?, name_key=? WHERE id=?",
                      (name, name.lower(), int(user_id)))
        return bool(cur.rowcount)

    def mark_login(self, user_id: int) -> None:
        """登录成功就记一笔时间 —— 建号或用邀请码注册的那一刻也算登录。"""
        self._x("UPDATE users SET last_login_at=? WHERE id=?", (time.time(), int(user_id)))

    def verify_login(self, name: str, password: str) -> Optional[Dict[str, Any]]:
        row = self.user_by_name(name)
        if not row:
            # 名字不存在也走一遍哈希，别让响应时间泄露"这个名字存在"。
            _hash_password(password or "", "0" * 32)
            return None
        expected = row["hash"]
        got = _hash_password(password or "", row["salt"])
        if not hmac.compare_digest(expected, got):
            return None
        self._x("UPDATE users SET last_login_at=? WHERE id=?", (time.time(), row["id"]))
        return self.user(int(row["id"]))

    # ------------------------------------------------------------------ 工作区
    def workspaces(self) -> List[Dict[str, Any]]:
        rows = self._q("SELECT * FROM workspaces ORDER BY id")
        return [dict(r) for r in rows]

    def workspace(self, ws_id: int) -> Optional[Dict[str, Any]]:
        rows = self._q("SELECT * FROM workspaces WHERE id=?", (int(ws_id),))
        return dict(rows[0]) if rows else None

    def create_workspace(self, name: str, dir_path: str, *, owner_id: int,
                         kind: str = "shared") -> Dict[str, Any]:
        name = (name or "").strip() or "新工作区"
        now = time.time()
        cur = self._x("INSERT INTO workspaces(name, slug, dir, kind, created_by, created_at)"
                      " VALUES(?,?,?,?,?,?)",
                      (name, slugify(name), os.path.abspath(dir_path), kind,
                       int(owner_id), now))
        ws_id = int(cur.lastrowid)
        self.add_member(ws_id, owner_id, ROLE_OWNER)
        row = self.workspace(ws_id)
        assert row is not None
        return row

    def set_workspace_dir(self, ws_id: int, dir_path: str) -> bool:
        cur = self._x("UPDATE workspaces SET dir=? WHERE id=?",
                      (os.path.abspath(dir_path), int(ws_id)))
        return bool(cur.rowcount)

    def rename_workspace(self, ws_id: int, name: str) -> bool:
        name = (name or "").strip()
        if not name:
            raise AccountError("名字不能是空的")
        cur = self._x("UPDATE workspaces SET name=?, slug=? WHERE id=?",
                      (name, slugify(name), int(ws_id)))
        return bool(cur.rowcount)

    def delete_workspace(self, ws_id: int) -> bool:
        """只删登记；数据目录留给用户自己处理（删目录是不可逆操作，别替他决定）。"""
        self._x("DELETE FROM memberships WHERE ws_id=?", (int(ws_id),))
        self._x("DELETE FROM invites WHERE ws_id=?", (int(ws_id),))
        cur = self._x("DELETE FROM workspaces WHERE id=?", (int(ws_id),))
        return bool(cur.rowcount)

    # -------------------------------------------------------------------- 成员
    def add_member(self, ws_id: int, user_id: int, role: str = ROLE_VIEW) -> str:
        if role not in ROLE_RANK:
            raise AccountError("角色只有 主人 / 可操作 / 只看 三种")
        now = time.time()
        self._x("INSERT INTO memberships(ws_id, user_id, role, created_at) VALUES(?,?,?,?)"
                " ON CONFLICT(ws_id, user_id) DO UPDATE SET role=excluded.role",
                (int(ws_id), int(user_id), role, now))
        return role

    def set_member_role(self, ws_id: int, user_id: int, role: str) -> str:
        if role not in ROLE_RANK:
            raise AccountError("角色只有 主人 / 可操作 / 只看 三种")
        owners = [m for m in self.members(ws_id) if m["role"] == ROLE_OWNER]
        if role != ROLE_OWNER and len(owners) <= 1 and any(
                int(m["user_id"]) == int(user_id) for m in owners):
            raise AccountError("这是最后一位主人，先给别人升成主人再降它")
        self._x("UPDATE memberships SET role=? WHERE ws_id=? AND user_id=?",
                (role, int(ws_id), int(user_id)))
        return role

    def remove_member(self, ws_id: int, user_id: int) -> bool:
        owners = [m for m in self.members(ws_id) if m["role"] == ROLE_OWNER]
        if len(owners) <= 1 and any(int(m["user_id"]) == int(user_id) for m in owners):
            raise AccountError("这是最后一位主人，不能把自己踢出去")
        cur = self._x("DELETE FROM memberships WHERE ws_id=? AND user_id=?",
                      (int(ws_id), int(user_id)))
        return bool(cur.rowcount)

    def roles(self) -> List[Dict[str, Any]]:
        """成员可用的三种角色（界面上的下拉直接吃这个）。"""
        return [{"role": role, "label": ROLE_LABEL.get(role, role),
                 "note": ROLE_NOTE.get(role, "")}
                for role in sorted(ROLE_RANK, key=lambda item: -ROLE_RANK[item])]

    def members(self, ws_id: int) -> List[Dict[str, Any]]:
        rows = self._q(
            "SELECT m.user_id, m.role, m.created_at, u.name, u.last_login_at"
            " FROM memberships m JOIN users u ON u.id = m.user_id"
            " WHERE m.ws_id=? ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'edit' THEN 1"
            " ELSE 2 END, u.name", (int(ws_id),))
        return [{"user_id": int(r["user_id"]), "name": r["name"], "role": r["role"],
                 "role_label": ROLE_LABEL.get(r["role"], r["role"]),
                 "joined_at": r["created_at"], "last_login_at": r["last_login_at"]}
                for r in rows]

    def role_of(self, ws_id: int, user_id: int) -> Optional[str]:
        rows = self._q("SELECT role FROM memberships WHERE ws_id=? AND user_id=?",
                       (int(ws_id), int(user_id)))
        return str(rows[0]["role"]) if rows else None

    def memberships_of(self, user_id: int) -> List[Dict[str, Any]]:
        rows = self._q(
            "SELECT w.*, m.role FROM memberships m JOIN workspaces w ON w.id = m.ws_id"
            " WHERE m.user_id=? ORDER BY w.id", (int(user_id),))
        out = []
        for row in rows:
            item = dict(row)
            item["members"] = int(self._q("SELECT COUNT(*) AS n FROM memberships WHERE ws_id=?",
                                          (int(row["id"]),))[0]["n"])
            out.append(item)
        return out

    def default_workspace(self, user_id: int) -> Optional[Dict[str, Any]]:
        """优先回自己的私人工作区，其次第一个加入的共享工作区。"""
        mine = self.memberships_of(user_id)
        for item in mine:
            if item.get("kind") == "personal":
                return item
        return mine[0] if mine else None

    # -------------------------------------------------------------------- 会话
    def create_session(self, user_id: int, ip: str = "", ua: str = "",
                       days: int = SESSION_DAYS) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._x("INSERT INTO sessions(token_hash, user_id, created_at, last_seen, expires_at,"
                " ip, ua) VALUES(?,?,?,?,?,?,?)",
                (_hash_token(token), int(user_id), now, now, now + days * 86400,
                 (ip or "")[:45], (ua or "")[:200]))
        return token

    def session_user(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        rows = self._q("SELECT * FROM sessions WHERE token_hash=?", (_hash_token(token),))
        if not rows:
            return None
        row = rows[0]
        if float(row["expires_at"]) < time.time():
            self.drop_session(token)
            return None
        if time.time() - float(row["last_seen"]) > 60:
            self._x("UPDATE sessions SET last_seen=? WHERE token_hash=?",
                    (time.time(), row["token_hash"]))
        return self.user(int(row["user_id"]))

    def drop_session(self, token: str) -> bool:
        cur = self._x("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
        return bool(cur.rowcount)

    def drop_user_sessions(self, user_id: int) -> int:
        cur = self._x("DELETE FROM sessions WHERE user_id=?", (int(user_id),))
        return int(cur.rowcount or 0)

    # -------------------------------------------------------------------- 邀请
    def create_invite(self, ws_id: int, role: str, *, created_by: int,
                      hours: float = 72.0) -> Dict[str, Any]:
        if role not in ROLE_RANK:
            raise AccountError("角色只有 主人 / 可操作 / 只看 三种")
        if role == ROLE_OWNER:
            raise AccountError("邀请码不能直接发「主人」：先让别人以「可操作」进来，再在成员里升级")
        code = new_invite_code()
        now = time.time()
        self._x("INSERT INTO invites(code_hash, ws_id, role, created_by, created_at, expires_at)"
                " VALUES(?,?,?,?,?,?)",
                (_hash_token(normalize_invite(code)), int(ws_id), role, int(created_by),
                 now, now + max(1.0, hours) * 3600))
        return {"code": code, "ws_id": int(ws_id), "role": role,
                "expires_at": now + max(1.0, hours) * 3600}

    def invites(self, ws_id: Optional[int] = None) -> List[Dict[str, Any]]:
        if ws_id is None:
            rows = self._q("SELECT * FROM invites ORDER BY created_at DESC")
        else:
            rows = self._q("SELECT * FROM invites WHERE ws_id=? ORDER BY created_at DESC",
                           (int(ws_id),))
        now = time.time()
        out = []
        for row in rows:
            out.append({
                "ws_id": int(row["ws_id"]), "role": row["role"],
                "created_by": int(row["created_by"]), "created_at": row["created_at"],
                "expires_at": row["expires_at"], "used": float(row["used_at"]) > 0,
                "expired": float(row["expires_at"]) < now,
                "usable": float(row["used_at"]) == 0 and float(row["expires_at"]) >= now,
            })
        return out

    def peek_invite(self, code: str) -> Optional[Dict[str, Any]]:
        key = normalize_invite(code)
        if len(key) != 12:
            return None
        rows = self._q("SELECT * FROM invites WHERE code_hash=?", (_hash_token(key),))
        if not rows:
            return None
        row = rows[0]
        if float(row["used_at"]) > 0:
            return None
        if float(row["expires_at"]) < time.time():
            return None
        return dict(row)

    def use_invite(self, code: str, user_id: int) -> Dict[str, Any]:
        row = self.peek_invite(code)
        if not row:
            raise AccountError("邀请码不对、已经用过，或者过期了")
        self.add_member(int(row["ws_id"]), int(user_id), row["role"])
        self._x("UPDATE invites SET used_at=?, used_by=? WHERE code_hash=?",
                (time.time(), int(user_id), row["code_hash"]))
        ws = self.workspace(int(row["ws_id"])) or {}
        return {"ws_id": int(row["ws_id"]), "workspace": ws, "role": row["role"]}

    # -------------------------------------------------------------------- 限速
    def blocked_for(self, ip: str) -> float:
        rows = self._q("SELECT * FROM login_fails WHERE ip=?", ((ip or "?")[:45],))
        if not rows:
            return 0.0
        row = rows[0]
        locked = float(row["locked_at"] or 0)
        if locked and time.time() - locked < LOCK_SECONDS:
            return LOCK_SECONDS - (time.time() - locked)
        return 0.0

    def note_failure(self, ip: str) -> int:
        """记一次失败，返回还剩几次机会（0 = 已经锁了）。"""
        ip = (ip or "?")[:45]
        now = time.time()
        rows = self._q("SELECT * FROM login_fails WHERE ip=?", (ip,))
        if not rows:
            self._x("INSERT INTO login_fails(ip, fails, first_at, locked_at) VALUES(?,?,?,?)",
                    (ip, 1, now, 0.0))
            return MAX_FAILS - 1
        row = rows[0]
        fails = int(row["fails"]) + 1
        first = float(row["first_at"] or now)
        if now - first > FAIL_WINDOW:
            fails, first = 1, now
        locked = 0.0
        if fails >= MAX_FAILS:
            locked = now
        self._x("UPDATE login_fails SET fails=?, first_at=?, locked_at=? WHERE ip=?",
                (fails, first, locked, ip))
        return max(0, MAX_FAILS - fails)

    def note_success(self, ip: str) -> None:
        self._x("DELETE FROM login_fails WHERE ip=?", ((ip or "?")[:45],))
