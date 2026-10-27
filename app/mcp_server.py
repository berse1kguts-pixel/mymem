# -*- coding: utf-8 -*-
"""MCP 接口：各 code agent 通过 MCP 协议直接读写记忆。

挂载在同进程 /mcp 路径（streamable HTTP），配置示例：
    {"mymem": {"url": "http://1.92.76.106:8700/mcp",
               "headers": {"Authorization": "Bearer <token>"}}}
"""
import json
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import backup as backup_mod
from . import db

# 自部署服务，访问控制由 MYMEM_TOKEN 负责，关闭 DNS-rebinding 防护
mcp = FastMCP("mymem", stateless_http=True,
              transport_security=TransportSecuritySettings(
                  enable_dns_rebinding_protection=False))


def _out(row: dict) -> dict:
    return {k: row[k] for k in (
        "id", "project", "category", "title", "content", "tags",
        "source_agent", "source_system", "status", "archived",
        "supersedes_id", "created_at", "updated_at")}


@mcp.tool()
def memory_search(project: str = "", query: str = "", category: str = "",
                  date: str = "", include_archived: bool = False,
                  limit: int = 20) -> list[dict]:
    """检索记忆。query 为空则按 project/category/date 列出最近记忆。

    date 格式 YYYY-MM-DD。
    """
    conn = db.get_conn()
    rows = db.search_memories(conn, project or None, category or None,
                              query or None, date or None,
                              include_archived, False, limit)
    return [_out(r) for r in rows]


@mcp.tool()
def memory_get(memory_id: int) -> Optional[dict]:
    """按 id 获取单条记忆。"""
    conn = db.get_conn()
    row = db.get_memory(conn, memory_id)
    return _out(row) if row else None


@mcp.tool()
def memory_add(project: str, category: str, title: str, content: str,
               tags: list[str] = [], supersedes_id: int = 0,
               agent: str = "mcp", system: str = "") -> dict:
    """新增一条记忆。category: decision|lesson|convention|context。

    同 project+category+标题 已存在时自动合并更新；
    supersedes_id>0 表示用本条覆盖旧记忆；
    system 传系统标识（主机名|操作系统），用于界面区分来源。
    """
    conn = db.get_conn()
    row = db.insert_memory(conn, project, {
        "category": category, "title": title, "content": content,
        "tags": tags, "supersedes_id": supersedes_id or None,
    }, agent, system)
    return _out(row)


@mcp.tool()
def memory_ingest(project: str, agent: str,
                  memories: list[dict[str, Any]], system: str = "") -> dict:
    """hook 批量摄入：会话结束后提交本次修改产生的多条记忆。

    memories 每项格式：
    {"category": "decision|lesson|convention|context",
     "title": "...", "content": "...", "tags": ["..."],
     "supersedes_id": 可选}
    system 传系统标识（主机名|操作系统）。
    """
    conn = db.get_conn()
    created, merged = [], []
    for item in memories:
        row = db.insert_memory(conn, project, {
            "category": item.get("category", "context"),
            "title": item["title"], "content": item["content"],
            "tags": item.get("tags", []),
            "supersedes_id": item.get("supersedes_id"),
        }, agent, system)
        (merged if row.get("merged") else created).append(_out(row))
    return {"created": created, "merged": merged}


@mcp.tool()
def memory_daily_digest(project: str, date: str) -> dict:
    """每日归档第一步：拉取某天（YYYY-MM-DD）的记忆，按类别分组。

    整理成整体总结后调 memory_archive 提交。
    """
    conn = db.get_conn()
    rows = db.daily_rows(conn, project, date, include_archived=False)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["category"], []).append(_out(r))
    return {"project": project, "date": date, "count": len(rows),
            "groups": groups, "unarchived_ids": [r["id"] for r in rows]}


@mcp.tool()
def memory_archive(project: str, date: str, summary: str,
                   covered_ids: list[int]) -> dict:
    """每日归档第二步：提交整体总结，覆盖的条目自动标记为已归档。"""
    conn = db.get_conn()
    row = db.insert_memory(conn, project, {
        "category": "archive", "title": "每日归档 %s" % date,
        "content": summary, "tags": ["archive", date],
    }, "mcp")
    marked = db.mark_archived(conn, covered_ids)
    result = _out(row)
    result["marked_archived"] = marked
    return result


@mcp.tool()
def memory_backup() -> dict:
    """立即执行一次备份导出 + 邮件推送。"""
    return backup_mod.run_backup()
