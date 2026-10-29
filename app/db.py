# -*- coding: utf-8 -*-
"""存储层：SQLite + FTS5（trigram 分词，兼容中文子串检索）。

记忆条目的生命周期：
  - hook 摄入时若与既有活跃记忆重复（同 project+category+标题），视为更新合并；
  - 摄入时可指定 supersedes_id 覆盖旧记忆，旧记忆标记为 superseded；
  - 每日归档时 AI 提交 archive 总结，被覆盖的条目标记 archived=1。

个人命名空间：
  - PERSONAL_PROJECT（"personal"）是保留命名空间，存放与任何项目都无关的
    日常总结 / 故障复盘（经 /api/v1/retro 与 MCP retro_* 工具读写）。
    所有记忆都按 project 命名空间隔离，"项目无关"即归到这一个专属命名空间，
    不新增表、不放宽既有隔离。
"""
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone

from . import config

# 保留命名空间：项目无关的个人复盘 / 日常总结（retro 模块专用）
PERSONAL_PROJECT = "personal"

_SCHEMA = """
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

_init_lock = threading.Lock()
_initialized = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    global _initialized
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with _init_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            # 重建 FTS 索引，保证历史数据可检索（幂等）
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            conn.commit()
            _initialized = True
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d["tags"])
    return d


def _normalize(text: str) -> str:
    return re.sub(r"[\s　]+", "", text).lower()


def insert_memory(conn: sqlite3.Connection, project: str, item: dict,
                  source_agent: str, source_system: str = "") -> dict:
    """写入一条记忆，处理合并（同标题更新）与覆盖（supersedes）。"""
    now = _now()
    title = item["title"].strip()

    # 合并：同 project+category+归一化标题的活跃记忆 → 视为同一条，更新内容
    dup = conn.execute(
        "SELECT id, title FROM memories WHERE project=? AND category=? "
        "AND status='active' AND archived=0",
        (project, item["category"]),
    ).fetchall()
    for row in dup:
        if _normalize(row["title"]) == _normalize(title):
            conn.execute(
                "UPDATE memories SET content=?, tags=?, source_agent=?, "
                "source_system=?, updated_at=? WHERE id=?",
                (item["content"], json.dumps(item.get("tags", []), ensure_ascii=False),
                 source_agent, source_system, now, row["id"]),
            )
            conn.commit()
            out = conn.execute("SELECT * FROM memories WHERE id=?",
                               (row["id"],)).fetchone()
            data = _row_to_dict(out)
            data["merged"] = True
            return data

    supersedes_id = item.get("supersedes_id")
    if supersedes_id is not None:
        conn.execute(
            "UPDATE memories SET status='superseded', updated_at=? WHERE id=?",
            (now, supersedes_id),
        )

    cur = conn.execute(
        "INSERT INTO memories (project, category, title, content, tags, "
        "source_agent, source_system, status, archived, supersedes_id, "
        "created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?, 'active', 0, ?, ?, ?)",
        (project, item["category"], title, item["content"],
         json.dumps(item.get("tags", []), ensure_ascii=False),
         source_agent, source_system, supersedes_id, now, now),
    )
    conn.commit()
    out = conn.execute("SELECT * FROM memories WHERE id=?",
                       (cur.lastrowid,)).fetchone()
    data = _row_to_dict(out)
    data["merged"] = False
    return data


def search_memories(conn: sqlite3.Connection, project: str | None = None,
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
        # 规则 = tags 含 "rule" 的记忆（约定/决策的置顶形态）
        where.append("m.tags LIKE '%\"rule\"%'")
    if date:
        # localtime：按服务器本地时区（TZ）归日，避免凌晨记录归到前一天
        where.append("date(m.created_at, 'localtime') = ?")
        params.append(date)
    if not include_archived:
        where.append("m.archived = 0")
    if not include_superseded:
        where.append("m.status = 'active'")

    if q:
        # trigram FTS 对 <3 字符查询无效，退化为 LIKE
        if len(q.strip()) >= 3:
            # FTS5 的 MATCH / bm25 左操作数需用 FTS 表名（不能用别名）
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


def get_memory(conn: sqlite3.Connection, memory_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM memories WHERE id=?",
                       (memory_id,)).fetchone()
    return _row_to_dict(row) if row else None


def update_memory(conn: sqlite3.Connection, memory_id: int,
                  fields: dict) -> dict | None:
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


def delete_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    conn.commit()
    return cur.rowcount > 0


def daily_rows(conn: sqlite3.Connection, project: str, date: str,
               include_archived: bool = False) -> list[dict]:
    """取某一天（服务器本地日期）的记忆，供 AI 做每日归档。

    archive 类记忆本身是归档产物，不参与待归档列表。
    """
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


def mark_archived(conn: sqlite3.Connection, ids: list[int]) -> int:
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


def period_rows(conn: sqlite3.Connection, project: str,
                start: str, end: str,
                include_archived: bool = False) -> list[dict]:
    """某段周期（start~end，YYYY-MM-DD）内的记忆，按时间正序。"""
    sql = ("SELECT * FROM memories WHERE project=? AND status='active' "
           "AND date(created_at,'localtime') BETWEEN ? AND ?")
    params: list = [project, start, end]
    if not include_archived:
        sql += " AND archived=0"
    sql += " ORDER BY created_at"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def bootstrap_rows(conn: sqlite3.Connection, project: str,
                   limit: int = 20) -> list[dict]:
    """会话开始时的必读记忆：项目约定与有效决策。

    规则（tags 含 "rule"）置顶排最前，其余按最近更新排序。
    """
    rows = conn.execute(
        "SELECT * FROM memories WHERE project=? AND status='active' "
        "AND archived=0 AND category IN ('convention','decision') "
        "ORDER BY CASE WHEN tags LIKE '%\"rule\"%' THEN 0 ELSE 1 END, "
        "updated_at DESC LIMIT ?",
        (project, limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def log_audit(conn: sqlite3.Connection, action: str, project: str = "",
              agent: str = "", system: str = "",
              memory_id: int | None = None, detail: str = "") -> None:
    """审计留痕：记录谁在何时读写了哪个项目/哪条记忆。"""
    conn.execute(
        "INSERT INTO audit_log (ts, action, project, agent, system, "
        "memory_id, detail) VALUES (?,?,?,?,?,?,?)",
        (_now(), action, project, agent, system, memory_id, detail),
    )
    conn.commit()


def audit_rows(conn: sqlite3.Connection, project: str | None = None,
               limit: int = 50) -> list[dict]:
    if project:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE project=? ORDER BY id DESC LIMIT ?",
            (project, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]
