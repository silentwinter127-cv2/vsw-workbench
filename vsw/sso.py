"""第三方应用跳过来免登录用的一次性票据。

场景：HYW（或任何别的站点）上已经登录的人，点「打开工作台」时不该再登一次。
流程是两段式的：

1. 绑定：第三方后端拿用户填的工作台账号密码调 ``POST /api/sso/bind``，
   换回一把长期有效的**绑定令牌**（``VSWL-...``）。密码不留在第三方那边，
   令牌可以随时在工作台「共享 / 账号」里撤销。
2. 打开：第三方后端拿这把令牌调 ``POST /api/sso/ticket``，换一张**一次性票据**，
   把浏览器跳到 ``/sso?ticket=...``。票据 60 秒寿命、用一次就作废。

票据只存在内存里：它就是「刚才有人证明过身份」的一次性凭证，没有落盘的价值。
进程重启后没兑换的票据直接失效，第三方那份代码会自己再要一张。
"""

from __future__ import annotations

import secrets
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

# 票据寿命：够浏览器从第三方站点跳过来（含跨洋 RTT），又短到即使被人
# 从历史记录/日志里翻出来也基本过期了。
TICKET_TTL = 60.0

# 一次最多存多少张未兑换的票据。正常用法是「要一张、立刻用掉」，
# 留个上限只是防止有人拿绑定令牌刷接口把内存撑爆。
MAX_TICKETS = 2000


def new_bind_key() -> str:
    """绑定令牌的明文形状：VSWL-XXXX-XXXX-XXXX（字母表去掉 I/O/0/1）。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = []
    for _ in range(3):
        groups.append("".join(secrets.choice(alphabet) for _ in range(4)))
    return "VSWL-" + "-".join(groups)


def normalize_bind_key(key: Any) -> str:
    """用户手抄/粘贴时可能带空格、小写、少了横线 —— 统一成大写带横线的形状。"""
    raw = "".join(ch for ch in str(key or "").upper() if ch.isalnum())
    if raw.startswith("VSWL"):
        raw = raw[4:]
    if len(raw) != 12:
        return ""
    return "VSWL-" + "-".join(raw[i:i + 4] for i in range(0, 12, 4))


class TicketStore:
    """内存里的一次性票据表：``ticket -> (user_id, note, expiry)``。"""

    def __init__(self, ttl: float = TICKET_TTL, limit: int = MAX_TICKETS) -> None:
        self.ttl = float(ttl)
        self.limit = int(limit)
        self._lock = threading.Lock()
        self._items: Dict[str, Tuple[int, str, float]] = {}

    def _prune(self, now: float) -> None:
        dead = [k for k, (_, _, exp) in self._items.items() if exp < now]
        for k in dead:
            self._items.pop(k, None)

    def issue(self, user_id: int, note: str = "") -> str:
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._prune(now)
            if len(self._items) >= self.limit:
                # 满了就扔最早到期的那批，别让第三方刷接口把内存撑大
                for k, _ in sorted(self._items.items(), key=lambda kv: kv[1][2])[:len(self._items) // 4 + 1]:
                    self._items.pop(k, None)
            self._items[token] = (int(user_id), str(note or ""), now + self.ttl)
        return token

    def redeem(self, token: str) -> Optional[int]:
        """兑换一张票据：成功返回 user_id，并且这张票立刻作废。"""
        key = str(token or "")
        if not key:
            return None
        now = time.time()
        with self._lock:
            item = self._items.pop(key, None)
            if not item:
                return None
            user_id, _note, expiry = item
            if expiry < now:
                return None
            return int(user_id)

    def peek(self, token: str) -> Optional[int]:
        """只查不消耗 —— 探针和「票据对不对」的预检用。"""
        key = str(token or "")
        now = time.time()
        with self._lock:
            item = self._items.get(key)
            if not item or item[2] < now:
                return None
            return int(item[0])

    def size(self) -> int:
        now = time.time()
        with self._lock:
            self._prune(now)
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


# 全进程一份。多个工作区共用同一张票表没问题：票据里带的是 user_id，
# 兑换后再按这个人的成员关系去挑工作区。
TICKETS = TicketStore()


def ticket_url(base: str, ticket: str, next_path: str = "/") -> str:
    base = str(base or "").rstrip("/")
    target = "/sso?ticket=" + urllib.parse.quote(ticket, safe="")
    if next_path and next_path not in ("/", ""):
        target += "&next=" + urllib.parse.quote(next_path, safe="")
    return base + target


def describe(ticket_count: int) -> Dict[str, Any]:
    return {"tickets": int(ticket_count), "ttl": TICKET_TTL}
