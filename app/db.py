# -*- coding: utf-8 -*-
"""存储层：支持 SQLite（本地）与 TiDB / MySQL（云端全托管）。

配置了 TIDB_HOST 时自动切换为 TiDB 云数据库，未配置时使用本地 SQLite + FTS5。
"""
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone

from . import config

try:
    import pymysql
    import pymysql.cursors
    import ssl
    import certifi
    _HAS_PYMYSQL = True
except ImportError:
    _HAS_PYMYSQL = False

# 保留命名空间：项目无关的个人复盘 / 日常总结（retro 模块专用）
PERSONAL_PROJECT = "personal"

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project      TEXT NOT NULL,
    category     TEXT NOT NULL CHECK (category IN
                 ('decision','lesson','convention','context','archive')),
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,
    tags         TEXT NOT NULL DEFAULT '[]',
    source_agent TEXT NOT NULL DEFAULT 'unknown',
    source_system TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','superseded')),
    archived     INTEGER NOT NULL DEFAULT 0,
    supersedes_id INTEGER,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project, status, archived);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    title, content, content='memories', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, title, content)
    VALUES (new.id, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);
END;
CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);
    INSERT INTO memories_fts(rowid, title, content)
    VALUES (new.id, new.title, new.content);
END;
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    action     TEXT NOT NULL,
    project    TEXT NOT NULL DEFAULT '',
    agent      TEXT NOT NULL DEFAULT '',
    system     TEXT NOT NULL DEFAULT '',
    memory_id  INTEGER,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_log(project, ts);
"""

_TIDB_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    project      VARCHAR(64) NOT NULL,
    category     VARCHAR(32) NOT NULL,
    title        VARCHAR(255) NOT NULL,
    content      LONGTEXT NOT NULL,
    tags         TEXT NOT NULL,
    source_agent VARCHAR(64) NOT NULL DEFAULT 'unknown',
    source_system VARCHAR(64) NOT NULL DEFAULT '',
    status       VARCHAR(32) NOT NULL DEFAULT 'active',
    archived     TINYINT NOT NULL DEFAULT 0,
    supersedes_id BIGINT NULL,
    created_at   VARCHAR(64) NOT NULL,
    updated_at   VARCHAR(64) NOT NULL,
    INDEX idx_memories_project (project, status, archived)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts         VARCHAR(64) NOT NULL,
    action     VARCHAR(32) NOT NULL,
    project    VARCHAR(64) NOT NULL DEFAULT '',
    agent      VARCHAR(64) NOT NULL DEFAULT '',
    system     VARCHAR(64) NOT NULL DEFAULT '',
    memory_id  BIGINT NULL,
    detail     TEXT NOT NULL,
    INDEX idx_audit_project (project, ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

_init_lock = threading.Lock()
_initialized = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TiDBCursorWrapper:
    def __init__(self, raw_cursor):
        self._cur = raw_cursor

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class TiDBConnWrapper:
    def __init__(self, raw_conn):
        self._raw = raw_conn

    def execute(self, sql: str, params=None):
        sql = sql.replace("?", "%s")
        sql = sql.replace("date(m.created_at, 'localtime')", "DATE(m.created_at)")
        sql = sql.replace("date(created_at, 'localtime')", "DATE(created_at)")
        sql = sql.replace("date(created_at,'localtime')", "DATE(created_at)")
        cur = self._raw.cursor(pymysql.cursors.DictCursor)
        if params is not None:
            if isinstance(params, list):
                params = tuple(params)
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return TiDBCursorWrapper(cur)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def _get_tidb_conn():
    ctx = ssl.create_default_context(cafile=certifi.where())
    raw = pymysql.connect(
        host=config.TIDB_HOST,
        port=config.TIDB_PORT,
        user=config.TIDB_USER,
        password=config.TIDB_PASSWORD,
        database=config.TIDB_DATABASE,
        ssl=ctx,
        autocommit=False,
        charset="utf8mb4"
    )
    return TiDBConnWrapper(raw)


def get_conn():
    global _initialized
    if config.is_tidb_enabled() and _HAS_PYMYSQL:
        conn = _get_tidb_conn()
        with _init_lock:
            if not _initialized:
                for stmt in _TIDB_SCHEMA.strip().split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        conn.execute(stmt)
                conn.commit()
                _initialized = True
        return conn

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with _init_lock:
        if not _initialized:
            conn.executescript(_SQLITE_SCHEMA)
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            conn.commit()
            _initialized = True
    return conn


def _row_to_dict(row) -> dict:
    d = dict(row)
    if isinstance(d.get("tags"), str):
        try:
            d["tags"] = json.loads(d["tags"])
        except Exception:
            d["tags"] = []
    return d


def _normalize(text: str) -> str:
    return re.sub(r"[\s　]+", "", text).lower()


def insert_memory(conn, project: str, item: dict,
                  source_agent: str, source_system: str = "") -> dict:
    now = _now()
    title = item["title"].strip()
    new_tags = item.get("tags", [])

    supersedes_id = item.get("supersedes_id")
    if supersedes_id is None:
        dup = conn.execute(
            "SELECT id, title, content, tags FROM memories WHERE project=? "
            "AND category=? AND status='active' AND archived=0",
            (project, item["category"]),
        ).fetchall()
        for row in dup:
            if _normalize(row["title"]) != _normalize(title):
                continue
            old_tags = row["tags"] if isinstance(row["tags"], list) else []
            if isinstance(row["tags"], str):
                try:
                    old_tags = json.loads(row["tags"])
                except Exception:
                    old_tags = []
            if (row["content"].strip() == item["content"].strip()
                    and old_tags == new_tags):
                conn.execute(
                    "UPDATE memories SET source_agent=?, source_system=?, "
                    "updated_at=? WHERE id=?",
                    (source_agent, source_system, now, row["id"]),
                )
                conn.commit()
                out = conn.execute("SELECT * FROM memories WHERE id=?",
                                   (row["id"],)).fetchone()
                data = _row_to_dict(out)
                data["merged"] = True
                return data
            supersedes_id = row["id"]
            break

    if supersedes_id is not None:
        conn.execute(
            "UPDATE memories SET status='superseded', updated_at=? "
            "WHERE id=? AND status='active'",
            (now, supersedes_id),
        )

    cur = conn.execute(
        "INSERT INTO memories (project, category, title, content, tags, "
        "source_agent, source_system, status, archived, supersedes_id, "
        "created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?, 'active', 0, ?, ?, ?)",
        (project, item["category"], title, item["content"],
         json.dumps(new_tags, ensure_ascii=False),
         source_agent, source_system, supersedes_id, now, now),
    )
    conn.commit()
    out = conn.execute("SELECT * FROM memories WHERE id=?",
                       (cur.lastrowid,)).fetchone()
    data = _row_to_dict(out)
    data["merged"] = False
    return data


def search_memories(conn, project: str | None = None,
                    category: str | None = None, q: str | None = None,
                    date: str | None = None,
                    include_archived: bool = False,
                    include_superseded: bool = False,
                    limit: int = 50, offset: int = 0,
                    rule_only: bool = False) -> list[dict]:
    where = []
    params: list = []
    if project:
        where.append("m.project = ?")
        params.append(project)
    if category:
        where.append("m.category = ?")
        params.append(category)
    if rule_only:
        where.append("m.tags LIKE '%\"rule\"%'")
    if date:
        where.append("date(m.created_at, 'localtime') = ?")
        params.append(date)
    if not include_archived:
        where.append("m.archived = 0")
    if not include_superseded:
        where.append("m.status = 'active'")

    if q:
        if not config.is_tidb_enabled() and len(q.strip()) >= 3:
            sql = ("SELECT m.* FROM memories_fts f JOIN memories m "
                   "ON m.id = f.rowid WHERE memories_fts MATCH ?")
            params = ["\"%s\"" % q.replace('"', '""')] + params
            if where:
                sql += " AND " + " AND ".join(where)
            sql += (" ORDER BY bm25(memories_fts), m.updated_at DESC "
                    "LIMIT ? OFFSET ?")
            params += [limit, offset]
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_dict(r) for r in rows]
        sql = "SELECT m.* FROM memories m WHERE (m.title LIKE ? OR m.content LIKE ?)"
        params = ["%%%s%%" % q, "%%%s%%" % q] + params
    else:
        sql = "SELECT m.* FROM memories m"

    if where:
        sql += (" WHERE " if " WHERE " not in sql else " AND ") + " AND ".join(where)
    sql += " ORDER BY m.updated_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_memory(conn, memory_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM memories WHERE id=?",
                       (memory_id,)).fetchone()
    return _row_to_dict(row) if row else None


def update_memory(conn, memory_id: int, fields: dict) -> dict | None:
    allowed = {k: v for k, v in fields.items() if v is not None}
    if "tags" in allowed:
        allowed["tags"] = json.dumps(allowed["tags"], ensure_ascii=False)
    if not allowed:
        return get_memory(conn, memory_id)
    allowed["updated_at"] = _now()
    sets = ", ".join("%s=?" % k for k in allowed)
    conn.execute("UPDATE memories SET %s WHERE id=?" % sets,
                 list(allowed.values()) + [memory_id])
    conn.commit()
    return get_memory(conn, memory_id)


def delete_memory(conn, memory_id: int) -> bool:
    cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    conn.commit()
    return cur.rowcount > 0


_CATEGORIES = {"decision", "lesson", "convention", "context", "archive"}


def import_memories(conn, items: list[dict],
                    agent: str = "import", system: str = "") -> dict:
    now = _now()
    imported = skipped = 0
    id_map: dict[int, int] = {}
    for it in items:
        project = (it.get("project") or "").strip()
        category = it.get("category") or ""
        title = (it.get("title") or "").strip()
        content = it.get("content") or ""
        if not project or not title or category not in _CATEGORIES:
            skipped += 1
            continue
        tags = it.get("tags") if isinstance(it.get("tags"), list) else []
        dup = conn.execute(
            "SELECT id FROM memories WHERE project=? AND category=? "
            "AND title=? AND content=?",
            (project, category, title, content),
        ).fetchone()
        old_id = it.get("id")
        if dup:
            if isinstance(old_id, int):
                id_map[old_id] = dup["id"]
            skipped += 1
            continue
        old_parent = it.get("supersedes_id")
        parent_id = id_map.get(old_parent) if isinstance(old_parent, int) else None
        cur = conn.execute(
            "INSERT INTO memories (project, category, title, content, tags, "
            "source_agent, source_system, status, archived, supersedes_id, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (project, category, title, content,
             json.dumps(tags, ensure_ascii=False),
             it.get("source_agent") or agent,
             it.get("source_system") or system,
             "superseded" if it.get("status") == "superseded" else "active",
             1 if it.get("archived") else 0,
             parent_id,
             it.get("created_at") or now, it.get("updated_at") or now),
        )
        if isinstance(old_id, int):
            id_map[old_id] = cur.lastrowid
        imported += 1
    conn.commit()
    return {"imported": imported, "skipped": skipped, "total": len(items)}


def daily_rows(conn, project: str, date: str,
               include_archived: bool = False) -> list[dict]:
    where = ["project=?", "date(created_at, 'localtime')=?", "status='active'",
             "category != 'archive'"]
    params: list = [project, date]
    if not include_archived:
        where.append("archived=0")
    rows = conn.execute(
        "SELECT * FROM memories WHERE %s ORDER BY category, id" % " AND ".join(where),
        params,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def mark_archived(conn, ids: list[int]) -> int:
    if not ids:
        return 0
    now = _now()
    marks = ",".join("?" for _ in ids)
    cur = conn.execute(
        "UPDATE memories SET archived=1, updated_at=? WHERE id IN (%s)" % marks,
        [now] + ids,
    )
    conn.commit()
    return cur.rowcount


def period_rows(conn, project: str,
                start: str, end: str,
                include_archived: bool = False) -> list[dict]:
    sql = ("SELECT * FROM memories WHERE project=? AND status='active' "
           "AND date(created_at,'localtime') BETWEEN ? AND ?")
    params: list = [project, start, end]
    if not include_archived:
        sql += " AND archived=0"
    sql += " ORDER BY created_at"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def bootstrap_rows(conn, project: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM memories WHERE project=? AND status='active' "
        "AND archived=0 AND category IN ('convention','decision') "
        "ORDER BY CASE WHEN tags LIKE '%\"rule\"%' THEN 0 ELSE 1 END, "
        "updated_at DESC LIMIT ?",
        (project, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def log_audit(conn, action: str, project: str = "",
              agent: str = "", system: str = "",
              memory_id: int | None = None, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, action, project, agent, system, "
        "memory_id, detail) VALUES (?,?,?,?,?,?,?)",
        (_now(), action, project, agent, system, memory_id, detail),
    )
    conn.commit()


def audit_rows(conn, project: str | None = None, limit: int = 50) -> list[dict]:
    if project:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE project=? ORDER BY id DESC LIMIT ?",
            (project, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]
