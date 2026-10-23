"""SQLite store for the Visual Search Workbench.

Tables
------
``seeds``      imported source images (real scans, crops, re-encodes, "fake seeds")
``variants``   query variants generated from a seed, with their own hashes
``jobs``       the batch queue: one row per (variant, engine) search operation
``sources``    deduplicated web pages (the *source aggregator* layer)
``findings``   one row per distinct *visual object*; duplicates become extra sources
``clusters``   visual clusters used for novelty/bridging maths
``ledger``     one row per engine operation plus its yield -> the metrics panel
``settings``   tunable weights/thresholds (so the formula is inspectable, not magic)

Three dedup layers decide whether an incoming result is a new visual object or just
another carrier of one already known:

1. file layer      ``sha256`` equality           -> exact same file
2. visual-copy     ``phash`` Hamming distance    -> re-encode / rescale / light crop
3. semantic        SigLIP2 cosine                -> same subject or scene, new shot
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .engine import blob_to_vector, similarity_from_cosine, vector_to_blob
from .imaging import hamming

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS seeds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256 TEXT UNIQUE,
  title TEXT DEFAULT '',
  note TEXT DEFAULT '',
  kind TEXT DEFAULT 'import',
  origin_url TEXT DEFAULT '',
  rel_path TEXT DEFAULT '',
  thumb_path TEXT DEFAULT '',
  width INTEGER DEFAULT 0,
  height INTEGER DEFAULT 0,
  bytes INTEGER DEFAULT 0,
  phash TEXT DEFAULT '0',
  dhash TEXT DEFAULT '0',
  ahash TEXT DEFAULT '0',
  embedding BLOB,
  palette TEXT DEFAULT '[]',
  tags TEXT DEFAULT '[]',
  caption TEXT DEFAULT '',
  created_at REAL
);

CREATE TABLE IF NOT EXISTS variants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seed_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  label TEXT DEFAULT '',
  note TEXT DEFAULT '',
  grp TEXT DEFAULT 'geometry',
  rel_path TEXT DEFAULT '',
  width INTEGER DEFAULT 0,
  height INTEGER DEFAULT 0,
  bytes INTEGER DEFAULT 0,
  sha256 TEXT DEFAULT '',
  phash TEXT DEFAULT '0',
  dhash TEXT DEFAULT '0',
  ahash TEXT DEFAULT '0',
  meta TEXT DEFAULT '{}',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_variants_seed ON variants(seed_id);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url_hash TEXT UNIQUE,
  url TEXT NOT NULL,
  host TEXT DEFAULT '',
  platform TEXT DEFAULT '',
  title TEXT DEFAULT '',
  snippet TEXT DEFAULT '',
  image_url TEXT DEFAULT '',
  engine TEXT DEFAULT '',
  date_hint TEXT DEFAULT '',
  trust TEXT DEFAULT 'unknown',
  finding_id INTEGER DEFAULT 0,
  first_seen REAL,
  last_seen REAL
);
CREATE INDEX IF NOT EXISTS idx_sources_finding ON sources(finding_id);

CREATE TABLE IF NOT EXISTS clusters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT DEFAULT '',
  seed_finding_id INTEGER DEFAULT 0,
  size INTEGER DEFAULT 0,
  centroid BLOB,
  platforms TEXT DEFAULT '[]',
  created_at REAL
);

CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256 TEXT DEFAULT '',
  phash TEXT DEFAULT '0',
  dhash TEXT DEFAULT '0',
  ahash TEXT DEFAULT '0',
  rel_path TEXT DEFAULT '',
  thumb_path TEXT DEFAULT '',
  source_url TEXT DEFAULT '',
  image_url TEXT DEFAULT '',
  title TEXT DEFAULT '',
  engine TEXT DEFAULT '',
  job_id INTEGER DEFAULT 0,
  variant_id INTEGER DEFAULT 0,
  seed_id INTEGER DEFAULT 0,
  parent_finding_id INTEGER DEFAULT 0,
  depth INTEGER DEFAULT 1,
  cluster_id INTEGER DEFAULT 0,
  similarity REAL DEFAULT 0,
  novelty REAL DEFAULT 0,
  potential REAL DEFAULT 0,
  bridging REAL DEFAULT 0,
  metadata_score REAL DEFAULT 0,
  explore_value REAL DEFAULT 0,
  triage TEXT DEFAULT 'diffusion',
  status TEXT DEFAULT 'new',
  dup_of INTEGER DEFAULT 0,
  dup_layer TEXT DEFAULT '',
  trashed INTEGER DEFAULT 0,
  trashed_at REAL DEFAULT 0,
  trash_reason TEXT DEFAULT '',
  archived_at REAL DEFAULT 0,
  archive_id INTEGER DEFAULT 0,
  width INTEGER DEFAULT 0,
  height INTEGER DEFAULT 0,
  bytes INTEGER DEFAULT 0,
  palette TEXT DEFAULT '[]',
  tags TEXT DEFAULT '[]',
  caption TEXT DEFAULT '',
  embedding BLOB,
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_findings_cluster ON findings(cluster_id);
CREATE INDEX IF NOT EXISTS idx_findings_seed ON findings(seed_id);
CREATE INDEX IF NOT EXISTS idx_findings_dup ON findings(dup_of);
CREATE INDEX IF NOT EXISTS idx_findings_value ON findings(explore_value);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seed_id INTEGER NOT NULL,
  variant_id INTEGER NOT NULL,
  engine TEXT NOT NULL,
  group_key TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',
  note TEXT DEFAULT '',
  external_url TEXT DEFAULT '',
  opened_at REAL,
  finished_at REAL,
  attempts INTEGER DEFAULT 0,
  op_id INTEGER DEFAULT 0,
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_seed ON jobs(seed_id);

CREATE TABLE IF NOT EXISTS archive_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT DEFAULT '',
  note TEXT DEFAULT '',
  created_at REAL,
  findings INTEGER DEFAULT 0,
  sources INTEGER DEFAULT 0,
  platforms INTEGER DEFAULT 0,
  clusters INTEGER DEFAULT 0,
  seeds INTEGER DEFAULT 0,
  triage TEXT DEFAULT '{}',
  platform_breakdown TEXT DEFAULT '[]',
  date_from TEXT DEFAULT '',
  date_to TEXT DEFAULT '',
  total_bytes INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT,
  engine TEXT DEFAULT '',
  seed_id INTEGER DEFAULT 0,
  variant_id INTEGER DEFAULT 0,
  job_id INTEGER DEFAULT 0,
  op_kind TEXT DEFAULT 'search',
  findings_new INTEGER DEFAULT 0,
  dup_images INTEGER DEFAULT 0,
  new_sources INTEGER DEFAULT 0,
  new_platforms INTEGER DEFAULT 0,
  duration_s REAL DEFAULT 0,
  note TEXT DEFAULT '',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ledger_day ON ledger(day);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

DEFAULT_SETTINGS: Dict[str, Any] = {
    # three-layer dedup thresholds. phash_dup=4 sits above the measured JPEG-roundtrip
    # noise floor (<=2 bits on a fixture photo) and far below the 20+ bits that separate
    # different pictures, so it never merges two genuinely different frames.
    "phash_dup": 4,          # <= this Hamming distance: same file visually
    "phash_near": 14,        # <= this (and semantic confirm): visual clone
    "semantic_dup": 0.93,    # >= this SigLIP similarity: same visual object
    "cluster_join": 0.86,    # >= this: joins an existing cluster
    # triage thresholds
    "triage_same_image": 0.60,   # similarity >= -> 来源追踪队列
    "triage_bridge_value": 0.42,  # explore_value >= and new platform -> 桥接探索
    # exploration value weights (initial hypothesis, tune against your own picks)
    "w_novelty": 0.30,
    "w_potential": 0.25,
    "w_bridging": 0.25,
    "w_metadata": 0.20,
    "caption_on_import": False,
}

DAY_FMT = "%Y-%m-%d"


def today() -> str:
    return time.strftime(DAY_FMT)


def norm_vec(vec: Optional[np.ndarray]) -> Optional[np.ndarray]:
    if vec is None:
        return None
    vec = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(vec)) or 1.0
    return vec / norm


class Store:
    """One SQLite file, one lock, no ORM."""

    def __init__(self, db_path: str, media_root: str):
        self.db_path = db_path
        self.media_root = media_root
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        os.makedirs(media_root, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()
        for key, value in DEFAULT_SETTINGS.items():
            if self.get_setting(key) is None:
                self.set_setting(key, value)

    # ----------------------------------------------------------------- plumbing
    def _migrate(self) -> None:
        """Additive migrations for databases created by an earlier version."""
        def columns(table: str) -> set:
            return {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}

        if "meta" not in columns("variants"):
            # Crop-box provenance landed after v1.0; existing rows simply get an empty object.
            self._conn.execute("ALTER TABLE variants ADD COLUMN meta TEXT DEFAULT '{}'")
        if "trashed" not in columns("findings"):
            # Recycle bin: soft delete so a mis-click is always recoverable.
            self._conn.execute("ALTER TABLE findings ADD COLUMN trashed INTEGER DEFAULT 0")
            self._conn.execute("ALTER TABLE findings ADD COLUMN trashed_at REAL DEFAULT 0")
            self._conn.execute("ALTER TABLE findings ADD COLUMN trash_reason TEXT DEFAULT ''")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_trashed ON findings(trashed)")
        if "archived_at" not in columns("findings"):
            # Archive: 归档 is not deletion — the object stays searchable, it just leaves the
            # live board so a long-running hunt can be closed off and reopened later.
            self._conn.execute("ALTER TABLE findings ADD COLUMN archived_at REAL DEFAULT 0")
            self._conn.execute("ALTER TABLE findings ADD COLUMN archive_id INTEGER DEFAULT 0")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_archive ON findings(archive_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_findings_archived_at "
                               "ON findings(archived_at)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def query(self, sql: str, args: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args).fetchall())

    def one(self, sql: str, args: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    # ----------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return DEFAULT_SETTINGS.get(key, default)
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def set_setting(self, key: str, value: Any) -> Any:
        self.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))
        return value

    def settings(self) -> Dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for row in self.query("SELECT key,value FROM settings"):
            try:
                out[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                out[row["key"]] = row["value"]
        return out

    # --------------------------------------------------------------------- json
    @staticmethod
    def _slim_url(value: Any) -> Any:
        """Inline data: URIs are megabytes long — never ship them to the UI verbatim.

        Engine result pages embed whole base64 pictures in the URL field, and a single
        results-board response grew to 3 MB because five findings carried them around.
        """
        if isinstance(value, str) and value.startswith("data:") and len(value) > 120:
            header = value.split(",", 1)[0]
            return f"{header},...({len(value)} chars inline)"
        return value

    @staticmethod
    def _row_to_dict(row: sqlite3.Row, drop: Iterable[str] = ("embedding",)) -> Dict[str, Any]:
        data = dict(row)
        for key in drop:
            data.pop(key, None)
        for key in ("palette", "tags", "platforms", "meta"):
            if key in data and isinstance(data[key], str):
                try:
                    data[key] = json.loads(data[key])
                except (TypeError, ValueError):
                    data[key] = [] if key != "meta" else {}
        for key in ("url", "image_url", "source_url"):
            if key in data:
                data[key] = Store._slim_url(data[key])
        return data

    def _slim_source(self, source: Dict[str, Any]) -> Dict[str, Any]:
        for key in ("url", "image_url"):
            if key in source:
                source[key] = self._slim_url(source[key])
        return source

    # -------------------------------------------------------------------- seeds
    def create_seed(self, *, sha256: str, title: str = "", note: str = "", kind: str = "import",
                    origin_url: str = "", rel_path: str = "", thumb_path: str = "",
                    width: int = 0, height: int = 0, size: int = 0,
                    phash: int = 0, dhash: int = 0, ahash: int = 0,
                    embedding: Optional[np.ndarray] = None, palette: Optional[list] = None,
                    tags: Optional[list] = None, caption: str = "") -> Tuple[int, bool]:
        existing = self.one("SELECT id FROM seeds WHERE sha256=?", (sha256,))
        if existing is not None:
            return int(existing["id"]), False
        cur = self.execute(
            """INSERT INTO seeds(sha256,title,note,kind,origin_url,rel_path,thumb_path,width,height,
                                 bytes,phash,dhash,ahash,embedding,palette,tags,caption,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sha256, title, note, kind, origin_url, rel_path, thumb_path, width, height, size,
             phash, dhash, ahash, vector_to_blob(embedding) if embedding is not None else None,
             json.dumps(palette or [], ensure_ascii=False), json.dumps(tags or [], ensure_ascii=False),
             caption, time.time()))
        return int(cur.lastrowid), True

    def seeds(self, limit: int = 500, offset: int = 0) -> List[Dict[str, Any]]:
        rows = self.query(
            """SELECT s.*, (SELECT COUNT(*) FROM variants v WHERE v.seed_id=s.id) AS variant_count,
                      (SELECT COUNT(*) FROM jobs j WHERE j.seed_id=s.id) AS job_count,
                      (SELECT COUNT(*) FROM jobs j WHERE j.seed_id=s.id AND j.status='done') AS jobs_done
               FROM seeds s ORDER BY s.id DESC LIMIT ? OFFSET ?""", (limit, offset))
        return [self._row_to_dict(r) for r in rows]

    def seed(self, seed_id: int) -> Optional[Dict[str, Any]]:
        row = self.one("SELECT * FROM seeds WHERE id=?", (seed_id,))
        return self._row_to_dict(row) if row else None

    def seed_vector(self, seed_id: int) -> Optional[np.ndarray]:
        row = self.one("SELECT embedding FROM seeds WHERE id=?", (seed_id,))
        if row is None or row["embedding"] is None:
            return None
        return norm_vec(blob_to_vector(row["embedding"]))

    def set_seed_embedding(self, seed_id: int, embedding: np.ndarray) -> None:
        self.execute("UPDATE seeds SET embedding=? WHERE id=?",
                     (vector_to_blob(norm_vec(embedding)), seed_id))

    def seed_vectors(self, exclude: Optional[int] = None) -> List[Tuple[int, np.ndarray]]:
        rows = self.query("SELECT id,embedding FROM seeds WHERE embedding IS NOT NULL")
        out = []
        for row in rows:
            if exclude is not None and int(row["id"]) == exclude:
                continue
            out.append((int(row["id"]), norm_vec(blob_to_vector(row["embedding"]))))
        return out

    def delete_seed(self, seed_id: int) -> Dict[str, int]:
        counts = {
            "variants": len(self.query("SELECT id FROM variants WHERE seed_id=?", (seed_id,))),
            "jobs": len(self.query("SELECT id FROM jobs WHERE seed_id=?", (seed_id,))),
            "findings": len(self.query("SELECT id FROM findings WHERE seed_id=?", (seed_id,))),
        }
        self.execute("DELETE FROM variants WHERE seed_id=?", (seed_id,))
        self.execute("DELETE FROM jobs WHERE seed_id=?", (seed_id,))
        self.execute("DELETE FROM findings WHERE seed_id=?", (seed_id,))
        self.execute("DELETE FROM seeds WHERE id=?", (seed_id,))
        return counts

    # ----------------------------------------------------------------- variants
    def add_variant(self, seed_id: int, **fields: Any) -> int:
        cur = self.execute(
            """INSERT INTO variants(seed_id,kind,label,note,grp,rel_path,width,height,bytes,
                                    sha256,phash,dhash,ahash,meta,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (seed_id, fields.get("kind", ""), fields.get("label", ""), fields.get("note", ""),
             fields.get("group", "geometry"), fields.get("rel_path", ""), fields.get("width", 0),
             fields.get("height", 0), fields.get("bytes", 0), fields.get("sha256", ""),
             fields.get("phash", "0"), fields.get("dhash", "0"), fields.get("ahash", "0"),
             json.dumps(fields.get("meta") or {}, ensure_ascii=False), time.time()))
        return int(cur.lastrowid)

    def variants(self, seed_id: Optional[int] = None, limit: int = 2000) -> List[Dict[str, Any]]:
        if seed_id is None:
            rows = self.query("SELECT * FROM variants ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.query("SELECT * FROM variants WHERE seed_id=? ORDER BY id", (seed_id,))
        return [self._row_to_dict(r) for r in rows]

    def variant(self, variant_id: int) -> Optional[Dict[str, Any]]:
        row = self.one("SELECT * FROM variants WHERE id=?", (variant_id,))
        return self._row_to_dict(row) if row else None

    def variant_printers(self, seed_id: Optional[int] = None) -> List[Tuple[int, int, str]]:
        """(variant_id, phash, kind) — used to recognise an engine returning our own query variant."""
        if seed_id:
            rows = self.query("SELECT id,phash,kind FROM variants WHERE seed_id=? AND phash<>'0'",
                              (seed_id,))
        else:
            rows = self.query("SELECT id,phash,kind FROM variants WHERE phash<>'0'")
        out: List[Tuple[int, int, str]] = []
        for row in rows:
            try:
                out.append((int(row["id"]), int(row["phash"] or 0), row["kind"] or ""))
            except (TypeError, ValueError):
                continue
        return out

    def delete_variant(self, variant_id: int) -> None:
        self.execute("DELETE FROM jobs WHERE variant_id=?", (variant_id,))
        self.execute("DELETE FROM variants WHERE id=?", (variant_id,))

    # --------------------------------------------------------------------- jobs
    def enqueue(self, seed_id: int, variant_ids: Sequence[int], engines: Sequence[str],
                group_key: str = "") -> List[int]:
        ids: List[int] = []
        for variant_id in variant_ids:
            for engine in engines:
                existing = self.one(
                    "SELECT id,status FROM jobs WHERE variant_id=? AND engine=?", (variant_id, engine))
                if existing is not None:
                    if existing["status"] in ("failed",):
                        self.execute("UPDATE jobs SET status='pending', attempts=attempts+1 WHERE id=?",
                                     (existing["id"],))
                    ids.append(int(existing["id"]))
                    continue
                cur = self.execute(
                    """INSERT INTO jobs(seed_id,variant_id,engine,group_key,status,created_at)
                       VALUES(?,?,?,?, 'pending', ?)""",
                    (seed_id, variant_id, engine, group_key, time.time()))
                ids.append(int(cur.lastrowid))
        return ids

    def jobs(self, status: Optional[str] = None, seed_id: Optional[int] = None,
             limit: int = 800, engines: Optional[Sequence[str]] = None,
             kinds: Optional[Sequence[str]] = None, text: str = "",
             results: str = "") -> List[Dict[str, Any]]:
        """队列项查询。

        ``results`` 让人只看"有产出的"或"还没产出的"队列项 —— 面对几百条待搜索时，
        这比按状态分栏更有用（真正该看的是"这一轮哪些变体真的捞到东西了"）。
        """
        sql = """SELECT j.*, v.kind AS variant_kind, v.label AS variant_label, v.rel_path AS variant_path,
                        s.title AS seed_title, s.thumb_path AS seed_thumb,
                        (SELECT COUNT(*) FROM findings f WHERE f.job_id=j.id) AS findings
                 FROM jobs j
                 LEFT JOIN variants v ON v.id=j.variant_id
                 LEFT JOIN seeds s ON s.id=j.seed_id WHERE 1=1"""
        args: List[Any] = []
        if status and status != "all":
            if status == "todo":
                sql += " AND j.status IN ('pending','running','review')"
            else:
                sql += " AND j.status=?"
                args.append(status)
        if seed_id:
            sql += " AND j.seed_id=?"
            args.append(seed_id)
        if engines:
            marks = ",".join("?" for _ in engines)
            sql += f" AND j.engine IN ({marks})"
            args.extend(list(engines))
        if kinds:
            marks = ",".join("?" for _ in kinds)
            sql += f" AND v.kind IN ({marks})"
            args.extend(list(kinds))
        if text:
            like = f"%{text.strip()}%"
            sql += """ AND (s.title LIKE ? OR v.label LIKE ? OR v.kind LIKE ? OR j.engine LIKE ?
                           OR j.note LIKE ?)"""
            args.extend([like] * 5)
        if results == "with":
            sql += " AND (SELECT COUNT(*) FROM findings f WHERE f.job_id=j.id) > 0"
        elif results == "without":
            sql += " AND (SELECT COUNT(*) FROM findings f WHERE f.job_id=j.id) = 0"
        sql += (" ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 WHEN 'review' THEN 2"
                " WHEN 'done' THEN 3 ELSE 4 END, j.id LIMIT ?")
        args.append(limit)
        return [self._row_to_dict(r) for r in self.query(sql, args)]

    def job_facets(self) -> Dict[str, Any]:
        """给筛选器用的分布：每条队列项按引擎/变体/产出归类。

        一次查全量再在 Python 里聚合 —— 队列规模是几百到几千条，比按状态分别查便宜得多，
        也保证各维度计数口径一致。
        """
        rows = self.query(
            """SELECT j.engine AS engine, j.seed_id AS seed_id, j.status AS status,
                      v.kind AS kind, s.title AS seed_title,
                      (SELECT COUNT(*) FROM findings f WHERE f.job_id=j.id) AS findings
               FROM jobs j LEFT JOIN variants v ON v.id=j.variant_id
               LEFT JOIN seeds s ON s.id=j.seed_id""")
        engines: Dict[str, Dict[str, int]] = {}
        kinds: Dict[str, Dict[str, int]] = {}
        seeds: Dict[int, Dict[str, Any]] = {}
        results = {"with": 0, "without": 0}
        for row in rows:
            produced = int(row["findings"] or 0)
            results["with" if produced else "without"] += 1
            engine = row["engine"] or "(未知)"
            bucket = engines.setdefault(engine, {"total": 0, "with": 0, "pending": 0})
            bucket["total"] += 1
            if produced:
                bucket["with"] += 1
            if row["status"] in ("pending", "running", "review"):
                bucket["pending"] += 1
            kind = row["kind"] or "(未知)"
            bucket = kinds.setdefault(kind, {"total": 0, "with": 0, "pending": 0})
            bucket["total"] += 1
            if produced:
                bucket["with"] += 1
            if row["status"] in ("pending", "running", "review"):
                bucket["pending"] += 1
            seed_id = int(row["seed_id"] or 0)
            seed = seeds.setdefault(seed_id, {"seed_id": seed_id, "title": row["seed_title"] or "",
                                              "total": 0, "with": 0, "pending": 0})
            seed["total"] += 1
            if produced:
                seed["with"] += 1
            if row["status"] in ("pending", "running", "review"):
                seed["pending"] += 1
        return {
            "engines": [{"key": k, **v} for k, v in sorted(engines.items(), key=lambda x: -x[1]["total"])],
            "kinds": [{"kind": k, **v} for k, v in sorted(kinds.items(), key=lambda x: -x[1]["total"])],
            "seeds": sorted(seeds.values(), key=lambda s: -s["total"])[:120],
            "results": results,
            "total": len(rows),
        }

    def job(self, job_id: int) -> Optional[Dict[str, Any]]:
        row = self.one("""SELECT j.*, v.kind AS variant_kind, v.label AS variant_label,
                                 v.rel_path AS variant_path, s.title AS seed_title
                          FROM jobs j LEFT JOIN variants v ON v.id=j.variant_id
                          LEFT JOIN seeds s ON s.id=j.seed_id WHERE j.id=?""", (job_id,))
        return self._row_to_dict(row) if row else None

    def set_job_status(self, job_id: int, status: str, *, note: Optional[str] = None,
                       external_url: Optional[str] = None) -> Dict[str, Any]:
        fields = ["status=?"]
        args: List[Any] = [status]
        if note is not None:
            fields.append("note=?")
            args.append(note)
        if external_url is not None:
            fields.append("external_url=?")
            args.append(external_url)
        if status == "running":
            fields.append("opened_at=?")
            args.append(time.time())
        if status in ("done", "failed"):
            fields.append("finished_at=?")
            args.append(time.time())
        args.append(job_id)
        self.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", args)
        return self.job(job_id) or {}

    def delete_job(self, job_id: int) -> None:
        self.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def delete_jobs(self, job_ids: Sequence[int]) -> int:
        """批量删除队列项，返回实际删掉的条数。

        手机端的「删除选中 / 删除当前筛选结果」走这里：几百条逐条发请求太慢，
        而且中途失败会留下半删状态。
        """
        ids = [int(i) for i in job_ids if str(i).strip().isdigit()]
        if not ids:
            return 0
        removed = 0
        # 分批拼占位符，避开 SQLite 的变量数上限（旧版本默认 999）
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            cur = self.execute(f"DELETE FROM jobs WHERE id IN ({marks})", chunk)
            removed += cur.rowcount or 0
        return removed

    def clear_jobs(self, statuses: Sequence[str]) -> int:
        if not statuses:
            return 0
        marks = ",".join("?" for _ in statuses)
        cur = self.execute(f"DELETE FROM jobs WHERE status IN ({marks})", list(statuses))
        return cur.rowcount or 0

    # ------------------------------------------------------------------ sources
    @staticmethod
    def url_key(url: str) -> str:
        return hashlib.sha1(url.strip().lower().encode("utf-8", "ignore")).hexdigest()

    def upsert_source(self, *, url: str, finding_id: int = 0, host: str = "", platform: str = "",
                      title: str = "", snippet: str = "", image_url: str = "", engine: str = "",
                      date_hint: str = "", trust: str = "unknown") -> Tuple[int, bool]:
        key = self.url_key(url)
        row = self.one("SELECT id FROM sources WHERE url_hash=?", (key,))
        now = time.time()
        if row is not None:
            self.execute(
                """UPDATE sources SET last_seen=?, finding_id=CASE WHEN ?=0 THEN finding_id ELSE ? END,
                          title=CASE WHEN ?='' THEN title ELSE ? END,
                          image_url=CASE WHEN ?='' THEN image_url ELSE ? END,
                          platform=CASE WHEN ?='' THEN platform ELSE ? END
                   WHERE id=?""",
                (now, finding_id, finding_id, title, title, image_url, image_url,
                 platform, platform, row["id"]))
            return int(row["id"]), False
        cur = self.execute(
            """INSERT INTO sources(url_hash,url,host,platform,title,snippet,image_url,engine,
                                   date_hint,trust,finding_id,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, url, host, platform, title, snippet, image_url, engine, date_hint, trust,
             finding_id, now, now))
        return int(cur.lastrowid), True

    def sources(self, finding_id: Optional[int] = None, limit: int = 1000) -> List[Dict[str, Any]]:
        if finding_id is None:
            rows = self.query("SELECT * FROM sources ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self.query("SELECT * FROM sources WHERE finding_id=? ORDER BY id", (finding_id,))
        return [self._row_to_dict(r) for r in rows]

    # ----------------------------------------------------------------- clusters
    def clusters(self) -> List[Dict[str, Any]]:
        return [self._row_to_dict(r) for r in
                self.query("SELECT * FROM clusters ORDER BY size DESC, id")]

    def cluster(self, cluster_id: int) -> Optional[Dict[str, Any]]:
        row = self.one("SELECT * FROM clusters WHERE id=?", (cluster_id,))
        return self._row_to_dict(row) if row else None

    def cluster_centroid(self, cluster_id: int) -> Optional[np.ndarray]:
        row = self.one("SELECT centroid FROM clusters WHERE id=?", (cluster_id,))
        if row is None or row["centroid"] is None:
            return None
        return norm_vec(blob_to_vector(row["centroid"]))

    def _new_cluster(self, label: str, seed_finding_id: int, centroid: Optional[np.ndarray],
                     platforms: Sequence[str] = ()) -> int:
        cur = self.execute(
            """INSERT INTO clusters(label,seed_finding_id,size,centroid,platforms,created_at)
               VALUES(?,?,?,?,?,?)""",
            (label, seed_finding_id, 0,
             vector_to_blob(centroid) if centroid is not None else None,
             json.dumps(sorted(set(platforms)), ensure_ascii=False), time.time()))
        return int(cur.lastrowid)

    def refresh_cluster(self, cluster_id: int) -> None:
        # Clusters describe the live corpus: trashed and archived findings stop counting.
        rows = self.query("""SELECT embedding FROM findings WHERE cluster_id=? AND embedding IS NOT NULL
                             AND COALESCE(trashed,0)=0 AND COALESCE(archived_at,0)=0""",
                          (cluster_id,))
        vecs = [norm_vec(blob_to_vector(r["embedding"])) for r in rows]
        platforms = [r["platform"] for r in self.query(
            """SELECT DISTINCT s.platform AS platform FROM sources s
               JOIN findings f ON f.id=s.finding_id
               WHERE f.cluster_id=? AND COALESCE(f.trashed,0)=0
                 AND COALESCE(f.archived_at,0)=0""", (cluster_id,))
            if r["platform"]]
        size = len(self.query("""SELECT id FROM findings WHERE cluster_id=?
                                 AND COALESCE(trashed,0)=0 AND COALESCE(archived_at,0)=0""",
                              (cluster_id,)))
        centroid = None
        if vecs:
            centroid = norm_vec(np.mean(np.vstack(vecs), axis=0))
        self.execute("UPDATE clusters SET size=?, centroid=?, platforms=? WHERE id=?",
                     (size, vector_to_blob(centroid) if centroid is not None else None,
                      json.dumps(sorted(set(platforms)), ensure_ascii=False), cluster_id))

    # --------------------------------------------------------------- embeddings
    def embeddings(self) -> Tuple[List[int], Optional[np.ndarray]]:
        rows = self.query("SELECT id,embedding FROM findings "
                          "WHERE embedding IS NOT NULL AND COALESCE(trashed,0)=0 "
                          "AND COALESCE(archived_at,0)=0")
        ids: List[int] = []
        vecs: List[np.ndarray] = []
        for row in rows:
            ids.append(int(row["id"]))
            vecs.append(norm_vec(blob_to_vector(row["embedding"])))
        if not vecs:
            return [], None
        return ids, np.vstack(vecs)

    def dupe_candidates(self) -> List[Tuple[int, int, int, int, int]]:
        """(id, phash, dhash, ahash, cluster_id) for the perceptual dedup layers.

        Archived findings are deliberately excluded: 归档 means "done with this hunt", so a
        fresh search must not be silently folded into something already put away.
        """
        rows = self.query("SELECT id,phash,dhash,ahash,cluster_id FROM findings "
                          "WHERE dup_of=0 AND COALESCE(trashed,0)=0 "
                          "AND COALESCE(archived_at,0)=0")
        return [(int(r["id"]), int(r["phash"] or 0), int(r["dhash"] or 0), int(r["ahash"] or 0),
                 int(r["cluster_id"] or 0)) for r in rows]

    # ----------------------------------------------------------------- findings
    def finding(self, finding_id: int) -> Optional[Dict[str, Any]]:
        row = self.one("SELECT * FROM findings WHERE id=?", (finding_id,))
        return self._row_to_dict(row) if row else None

    def finding_vector(self, finding_id: int) -> Optional[np.ndarray]:
        row = self.one("SELECT embedding FROM findings WHERE id=?", (finding_id,))
        if row is None or row["embedding"] is None:
            return None
        return norm_vec(blob_to_vector(row["embedding"]))

    def findings(self, *, triage: Optional[str] = None, status: Optional[str] = None,
                 cluster_id: Optional[int] = None, seed_id: Optional[int] = None,
                 include_dups: bool = False, limit: int = 400, offset: int = 0,
                 order: str = "value", trashed: Optional[bool] = False,
                 not_promoted: bool = False, archived: Optional[bool] = False,
                 archive_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM findings WHERE 1=1"
        args: List[Any] = []
        # Only the trashed=1 branch needs a placeholder; the normal branch is a literal,
        # otherwise the binding count and the SQL stop matching up.
        # trashed=None 表示**不限制**回收站状态：归档批次要连已进回收站的对象一起看，
        # 否则批次里只要对象被移进回收站，整批就会"看起来是空的"（真实踩过）。
        if trashed is True:
            sql += " AND trashed=1"
        elif trashed is False:
            sql += " AND COALESCE(trashed,0)=0"
        # 归档的条目默认从"活动结果板"里消失，但仍然可以被检索/查看/恢复；
        # archived=None 表示"不限制归档状态"（归档视图用）。
        if archive_id:
            sql += " AND archive_id=?"
            args.append(int(archive_id))
        elif archived is True:
            sql += " AND COALESCE(archived_at,0)>0"
        elif archived is False and not trashed:
            # 归档与回收站正交：已归档对象被移进回收站后，回收站视图必须看得见它，
            # 否则它既不在归档列表、也不在回收站里 —— 变成看不见也清理不掉的幽灵行。
            sql += " AND COALESCE(archived_at,0)=0"
        if not_promoted:
            # "还没被选为下一轮种子" —— 回收站默认就扫这一批，别把已经回流的成果删掉
            sql += " AND status<>'promoted'"
        if triage and triage != "all":
            sql += " AND triage=?"
            args.append(triage)
        if status and status != "all":
            sql += " AND status=?"
            args.append(status)
        if cluster_id:
            sql += " AND cluster_id=?"
            args.append(cluster_id)
        if seed_id:
            sql += " AND seed_id=?"
            args.append(seed_id)
        if not include_dups:
            sql += " AND dup_of=0"
        order_sql = {
            "value": "explore_value DESC, similarity DESC",
            "similarity": "similarity DESC",
            "novelty": "novelty DESC",
            "newest": "id DESC",
            "trashed": "trashed_at DESC, id DESC",
            "archived": "archived_at DESC, id DESC",
        }.get(order, "explore_value DESC")
        sql += f" ORDER BY {order_sql} LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        out = []
        for row in self.query(sql, args):
            # Never serialise the embedding BLOB into list responses: it turned the results
            # board into a 3 MB JSON document for five cards.
            data = self._row_to_dict(row)
            data["sources"] = [self._slim_source(s)
                               for s in self.sources(finding_id=int(row["id"]), limit=50)]
            out.append(data)
        return out

    def count_findings(self, include_dups: bool = False, trashed: bool = False,
                       archived: Optional[bool] = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM findings WHERE " + \
              ("trashed=1" if trashed else "COALESCE(trashed,0)=0")
        if not include_dups:
            sql += " AND dup_of=0"
        # 归档与回收站是两个正交的状态：已归档的对象被移进回收站后，
        # 回收站视图也必须能看见它，否则它就"既不在归档、也不在回收站"，
        # 变成既看不见也清理不掉的幽灵行。所以 trashed 查询不再叠加归档过滤。
        if not trashed:
            if archived is True:
                sql += " AND COALESCE(archived_at,0)>0"
            elif archived is False:
                sql += " AND COALESCE(archived_at,0)=0"
        row = self.one(sql)
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------- 归档
    def create_archive_run(self, name: str, note: str = "", stats: Optional[Dict[str, Any]] = None,
                           triage: Optional[Dict[str, int]] = None,
                           platform_breakdown: Optional[list] = None,
                           date_from: str = "", date_to: str = "",
                           total_bytes: int = 0) -> int:
        stats = stats or {}
        cur = self.execute(
            """INSERT INTO archive_runs(name,note,created_at,findings,sources,platforms,clusters,seeds,
                                        triage,platform_breakdown,date_from,date_to,total_bytes)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, note, time.time(), int(stats.get("findings", 0)), int(stats.get("sources", 0)),
             int(stats.get("platforms", 0)), int(stats.get("clusters", 0)),
             int(stats.get("seeds", 0)), json.dumps(triage or {}, ensure_ascii=False),
             json.dumps(platform_breakdown or [], ensure_ascii=False),
             date_from, date_to, int(total_bytes)))
        return int(cur.lastrowid)

    def archive_findings(self, finding_ids: Sequence[int], archive_id: int) -> List[int]:
        archived: List[int] = []
        for finding_id in finding_ids:
            row = self.one("SELECT id,trashed FROM findings WHERE id=?", (int(finding_id),))
            if row is None or int(row["trashed"] or 0):
                continue
            self.execute("UPDATE findings SET archived_at=?, archive_id=? WHERE id=?",
                         (time.time(), int(archive_id), int(finding_id)))
            archived.append(int(finding_id))
        if archived:
            self.execute("UPDATE archive_runs SET findings=? WHERE id=?",
                         (len(archived), int(archive_id)))
        return archived

    def archive_runs(self) -> List[Dict[str, Any]]:
        return [self._row_to_dict(r) for r in
                self.query("SELECT * FROM archive_runs ORDER BY id DESC")]

    def archive_run(self, archive_id: int) -> Optional[Dict[str, Any]]:
        row = self.one("SELECT * FROM archive_runs WHERE id=?", (int(archive_id),))
        return self._row_to_dict(row) if row else None

    def unarchive(self, *, archive_id: Optional[int] = None) -> List[int]:
        if archive_id:
            rows = self.query("SELECT id FROM findings WHERE archive_id=?", (int(archive_id),))
        else:
            rows = self.query("SELECT id FROM findings WHERE COALESCE(archived_at,0)>0")
        restored = [int(r["id"]) for r in rows]
        for finding_id in restored:
            self.execute("UPDATE findings SET archived_at=0, archive_id=0 WHERE id=?",
                         (finding_id,))
        if archive_id:
            self.execute("DELETE FROM archive_runs WHERE id=?", (int(archive_id),))
        else:
            self.execute("DELETE FROM archive_runs")
        return restored

    def archive_stats(self) -> Dict[str, Any]:
        row = self.one("""SELECT COUNT(*) AS n,
                                 COALESCE(SUM(COALESCE(bytes,0)),0) AS bytes,
                                 MIN(created_at) AS first_at, MAX(created_at) AS last_at
                          FROM findings WHERE dup_of=0 AND COALESCE(archived_at,0)>0
                            AND COALESCE(trashed,0)=0""")
        platforms = self.one("""SELECT COUNT(DISTINCT COALESCE(NULLIF(s.platform,''),'(未识别)')) AS n
                                FROM sources s JOIN findings f ON f.id=s.finding_id
                                WHERE COALESCE(f.archived_at,0)>0 AND COALESCE(f.trashed,0)=0""")
        return {"findings": int(row["n"]) if row else 0,
                "bytes": int(row["bytes"]) if row else 0,
                "first_at": row["first_at"] if row else None,
                "last_at": row["last_at"] if row else None,
                "platforms": int(platforms["n"]) if platforms else 0}

    # ------------------------------------------------------------------ recycle bin
    def trash_findings(self, finding_ids: Sequence[int], reason: str = "manual") -> Dict[str, Any]:
        """Soft-delete: keeps every row, source and file so it can be restored."""
        moved: List[int] = []
        skipped: List[Dict[str, Any]] = []
        for finding_id in finding_ids:
            row = self.one("SELECT id,status,trashed FROM findings WHERE id=?", (int(finding_id),))
            if row is None:
                skipped.append({"finding_id": int(finding_id), "why": "不存在"})
                continue
            if int(row["trashed"] or 0):
                skipped.append({"finding_id": int(finding_id), "why": "已在回收站"})
                continue
            self.execute("UPDATE findings SET trashed=1, trashed_at=?, trash_reason=? WHERE id=?",
                         (time.time(), reason[:200], int(finding_id)))
            moved.append(int(finding_id))
        return {"moved": moved, "skipped": skipped}

    def trash_unpromoted(self, reason: str = "sweep") -> Dict[str, Any]:
        """Sweep everything that was never promoted into a seed of the next round."""
        rows = self.query(
            """SELECT id FROM findings WHERE COALESCE(trashed,0)=0 AND dup_of=0
               AND status<>'promoted' AND status<>'pinned'""")
        return self.trash_findings([int(r["id"]) for r in rows], reason=reason)

    def restore_findings(self, finding_ids: Sequence[int]) -> Dict[str, Any]:
        restored: List[int] = []
        for finding_id in finding_ids:
            row = self.one("SELECT id,trashed FROM findings WHERE id=?", (int(finding_id),))
            if row is None or not int(row["trashed"] or 0):
                continue
            self.execute("UPDATE findings SET trashed=0, trashed_at=0, trash_reason='' WHERE id=?",
                         (int(finding_id),))
            restored.append(int(finding_id))
        return {"restored": restored}

    def restore_all(self) -> Dict[str, Any]:
        rows = self.query("SELECT id FROM findings WHERE trashed=1")
        return self.restore_findings([int(r["id"]) for r in rows])

    def purge_findings(self, finding_ids: Sequence[int]) -> Dict[str, Any]:
        """Permanent: removes rows AND the stored image files (only called from the bin)."""
        purged: List[int] = []
        files_removed = 0
        for finding_id in finding_ids:
            row = self.one("SELECT id,rel_path,thumb_path,trashed FROM findings WHERE id=?",
                           (int(finding_id),))
            if row is None:
                continue
            if not int(row["trashed"] or 0):
                # Safety: permanent deletion is only reachable through the recycle bin.
                continue
            for path in (row["rel_path"], row["thumb_path"]):
                full = self._media_path(path)
                if full and os.path.isfile(full):
                    try:
                        os.remove(full)
                        files_removed += 1
                    except OSError:
                        pass
            self.execute("DELETE FROM sources WHERE finding_id=?", (int(finding_id),))
            self.execute("DELETE FROM findings WHERE id=?", (int(finding_id),))
            purged.append(int(finding_id))
        return {"purged": purged, "files_removed": files_removed}

    def purge_all(self) -> Dict[str, Any]:
        rows = self.query("SELECT id FROM findings WHERE trashed=1")
        return self.purge_findings([int(r["id"]) for r in rows])

    def _media_path(self, rel_path: Optional[str]) -> str:
        """Resolve a stored path to an absolute file path, safely inside the media root.

        Stored values are *data-root* relative ("media/thumbs/x.jpg"), while ``media_root``
        already points at ``<root>/media`` — naively joining them produced
        ``…/media/media/thumbs/x.jpg`` and made permanent deletion a no-op on files.
        """
        if not rel_path:
            return ""
        base = os.path.abspath(self.media_root)
        raw = str(rel_path).replace("\\", "/").lstrip("/")
        candidates = []
        if raw.lower().startswith("media/"):
            candidates.append(os.path.join(base, raw[len("media/"):].replace("/", os.sep)))
        candidates.append(os.path.join(base, raw.replace("/", os.sep)))
        for candidate in candidates:
            resolved = os.path.abspath(candidate)
            if resolved.startswith(base):
                return resolved
        return ""

    def update_finding(self, finding_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = {"status", "triage", "note", "title", "cluster_id", "parent_finding_id",
                   "explore_value", "similarity", "novelty", "potential", "bridging", "metadata_score"}
        pairs = [(k, v) for k, v in fields.items() if k in allowed]
        if not pairs:
            return self.finding(finding_id)
        sql = "UPDATE findings SET " + ", ".join(f"{k}=?" for k, _ in pairs) + " WHERE id=?"
        self.execute(sql, [v for _, v in pairs] + [finding_id])
        return self.finding(finding_id)

    def delete_finding(self, finding_id: int) -> None:
        self.execute("DELETE FROM sources WHERE finding_id=?", (finding_id,))
        self.execute("DELETE FROM findings WHERE id=?", (finding_id,))

    # ------------------------------------------------------------------- ledger
    def log_op(self, *, engine: str, seed_id: int = 0, variant_id: int = 0, job_id: int = 0,
               op_kind: str = "search", findings_new: int = 0, dup_images: int = 0,
               new_sources: int = 0, new_platforms: int = 0, duration_s: float = 0.0,
               note: str = "") -> int:
        cur = self.execute(
            """INSERT INTO ledger(day,engine,seed_id,variant_id,job_id,op_kind,findings_new,
                                  dup_images,new_sources,new_platforms,duration_s,note,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (today(), engine, seed_id, variant_id, job_id, op_kind, findings_new, dup_images,
             new_sources, new_platforms, duration_s, note, time.time()))
        return int(cur.lastrowid)

    def ledger(self, limit: int = 300, day: Optional[str] = None) -> List[Dict[str, Any]]:
        if day:
            rows = self.query("SELECT * FROM ledger WHERE day=? ORDER BY id DESC LIMIT ?", (day, limit))
        else:
            rows = self.query("SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def platform_counts(self, *, live_only: bool = True) -> List[Dict[str, Any]]:
        """来源平台分布。``live_only`` 只统计"活动"对象（未删未归档），因为平台数是指标之一。"""
        if live_only:
            return [dict(r) for r in self.query(
                """SELECT COALESCE(NULLIF(s.platform,''),'(未识别)') AS platform, COUNT(*) AS n
                   FROM sources s JOIN findings f ON f.id=s.finding_id
                   WHERE COALESCE(f.trashed,0)=0 AND COALESCE(f.archived_at,0)=0
                   GROUP BY 1 ORDER BY n DESC""")]
        return [dict(r) for r in self.query(
            """SELECT COALESCE(NULLIF(platform,''),'(未识别)') AS platform, COUNT(*) AS n
               FROM sources GROUP BY 1 ORDER BY n DESC""")]

    def stats(self) -> Dict[str, Any]:
        def scalar(sql: str, args: Sequence[Any] = ()) -> int:
            row = self.one(sql, args)
            return int(list(row)[0]) if row else 0

        findings_total = self.count_findings()
        trashed_total = self.count_findings(trashed=True)
        archived_total = self.count_findings(archived=True)
        dups = scalar("SELECT COUNT(*) FROM findings WHERE dup_of>0")
        sources_total = scalar("""SELECT COUNT(*) FROM sources s JOIN findings f ON f.id=s.finding_id
                                  WHERE COALESCE(f.trashed,0)=0
                                    AND COALESCE(f.archived_at,0)=0""")
        platforms = len([r for r in self.platform_counts() if r["platform"] != "(未识别)"])
        operations = scalar("SELECT COUNT(*) FROM ledger WHERE op_kind='search'")
        discovered = scalar("SELECT COALESCE(SUM(findings_new),0) FROM ledger")
        new_sources = scalar("SELECT COALESCE(SUM(new_sources),0) FROM ledger")
        new_platforms = scalar("SELECT COALESCE(SUM(new_platforms),0) FROM ledger")
        per_100 = (lambda n: round(n * 100.0 / operations, 2) if operations else 0.0)
        return {
            "seeds": scalar("SELECT COUNT(*) FROM seeds"),
            "variants": scalar("SELECT COUNT(*) FROM variants"),
            "jobs": scalar("SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running')"),
            "jobs_done": scalar("SELECT COUNT(*) FROM jobs WHERE status='done'"),
            "jobs_review": scalar("SELECT COUNT(*) FROM jobs WHERE status='review'"),
            "findings": findings_total,
            "trashed": trashed_total,
            "archived": archived_total,
            "duplicates": dups,
            "sources": sources_total,
            "platforms": platforms,
            "clusters": scalar("SELECT COUNT(*) FROM clusters"),
            "operations": operations,
            "new_per_100": per_100(discovered),
            "sources_per_100": per_100(new_sources),
            "platforms_per_100": per_100(new_platforms),
            "ops_per_new_source": round(operations / new_sources, 2) if new_sources else 0.0,
        }
