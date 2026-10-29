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
from . import security

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
                  rule_only: bool = False,
                  limit: int = 20) -> list[dict]:
    """检索记忆（全文检索，按相关度排序）。query 为空则按 project/category/date 列出最近记忆。

    date 格式 YYYY-MM-DD。rule_only=true 只返回当前生效的规则（tags 含 rule）。
    """
    security.check_project(project)
    conn = db.get_conn()
    rows = db.search_memories(conn, project or None, category or None,
                              query or None, date or None,
                              include_archived, False, limit,
                              rule_only=rule_only)
    security.log("read", project, detail="search query=%s" % (query or ""))
    return [_out(r) for r in rows]


@mcp.tool()
def memory_bootstrap(project: str, limit: int = 20) -> dict:
    """会话开始时调用：返回项目的必读记忆（约定 + 有效决策）。

    开局先读它，获得项目背景与历史结论，避免重复踩坑。
    规则（tags 含 "rule"）排最前，必须优先遵守；上报硬约束时给约定/决策
    加 tag "rule"，所有 agent 下次会话开始就会自动收到。
    """
    security.check_project(project)
    conn = db.get_conn()
    rows = db.bootstrap_rows(conn, project, limit)
    security.log("read", project, detail="bootstrap")
    return {"project": project, "count": len(rows),
            "must_read": [_out(r) for r in rows]}


@mcp.tool()
def memory_get(memory_id: int) -> Optional[dict]:
    """按 id 获取单条记忆。"""
    conn = db.get_conn()
    row = db.get_memory(conn, memory_id)
    if row:
        security.check_project(row["project"])
        security.log("read", row["project"], memory_id=memory_id)
    return _out(row) if row else None


@mcp.tool()
def memory_add(project: str, category: str, title: str, content: str,
               tags: list[str] = [], supersedes_id: int = 0,
               agent: str = "mcp", system: str = "") -> dict:
    """新增一条记忆。category: decision|lesson|convention|context。

    同 project+category+标题 已存在时自动合并更新；
    supersedes_id>0 表示用本条覆盖旧记忆；
    system 传系统标识（主机名|操作系统），用于界面区分来源。
    入库前自动脱敏敏感信息。
    """
    security.check_project(project)
    conn = db.get_conn()
    item, _ = security.mask_item({
        "category": category, "title": title, "content": content,
        "tags": tags, "supersedes_id": supersedes_id or None,
    })
    row = db.insert_memory(conn, project, item, agent, system)
    security.log("write", project, agent, system, row["id"], row["title"])
    return _out(row)


@mcp.tool()
def memory_ingest(project: str, agent: str,
                  memories: list[dict[str, Any]], system: str = "") -> dict:
    """hook 批量摄入：会话结束后提交本次修改产生的多条记忆。

    memories 每项格式：
    {"category": "decision|lesson|convention|context",
     "title": "...", "content": "...", "tags": ["..."],
     "supersedes_id": 可选}
    system 传系统标识（主机名|操作系统）。入库前自动脱敏。
    """
    security.check_project(project)
    conn = db.get_conn()
    created, merged = [], []
    for item in memories:
        data, _ = security.mask_item({
            "category": item.get("category", "context"),
            "title": item["title"], "content": item["content"],
            "tags": item.get("tags", []),
            "supersedes_id": item.get("supersedes_id"),
        })
        row = db.insert_memory(conn, project, data, agent, system)
        security.log("write", project, agent, system, row["id"], row["title"])
        (merged if row.get("merged") else created).append(_out(row))
    return {"created": created, "merged": merged}


@mcp.tool()
def memory_daily_digest(project: str, date: str) -> dict:
    """每日归档第一步：拉取某天（YYYY-MM-DD）的记忆，按类别分组。

    整理成整体总结后调 memory_archive 提交。
    """
    security.check_project(project)
    conn = db.get_conn()
    rows = db.daily_rows(conn, project, date, include_archived=False)
    security.log("read", project, detail="daily_digest %s" % date)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["category"], []).append(_out(r))
    return {"project": project, "date": date, "count": len(rows),
            "groups": groups, "unarchived_ids": [r["id"] for r in rows]}


@mcp.tool()
def memory_period_digest(project: str, start_date: str, end_date: str) -> dict:
    """链路周期总结第一步：拉取一段周期（YYYY-MM-DD ~ YYYY-MM-DD）的记忆。

    整理成链路周期总结后调 memory_archive 提交（title 可自定义，
    如「链路周期总结 2026-10-24 ~ 2026-10-27」）。
    """
    security.check_project(project)
    conn = db.get_conn()
    rows = db.period_rows(conn, project, start_date, end_date)
    security.log("read", project,
                 detail="period_digest %s~%s" % (start_date, end_date))
    groups: dict[str, list] = {}
    for r in rows:
        day = (r["created_at"] or "")[:10]
        groups.setdefault(day, []).append(_out(r))
    return {"project": project, "start_date": start_date, "end_date": end_date,
            "count": len(rows), "days": groups,
            "unarchived_ids": [r["id"] for r in rows]}


@mcp.tool()
def memory_archive(project: str, date: str, summary: str,
                   covered_ids: list[int], title: str = "",
                   system: str = "") -> dict:
    """归档第二步：提交总结，覆盖的条目自动标记为已归档。

    title 可选：每日归档可省略；链路周期总结建议传
    「链路周期总结 YYYY-MM-DD ~ YYYY-MM-DD」便于页面展示。
    system 传系统标识（主机名|操作系统），用于界面区分来源。
    """
    security.check_project(project)
    conn = db.get_conn()
    row = db.insert_memory(conn, project, {
        "category": "archive", "title": title or "每日归档 %s" % date,
        "content": summary, "tags": ["archive", date],
    }, "mcp", system)
    security.log("write", project, "mcp", system, row["id"],
                 detail="archive %s" % date)
    marked = db.mark_archived(conn, covered_ids)
    result = _out(row)
    result["marked_archived"] = marked
    return result


@mcp.tool()
def memory_retro_add(title: str, summary: str,
                     category: str = "lesson",
                     tags: list[str] = [], source_doc: str = "",
                     system: str = "") -> dict:
    """提交一条个人复盘 / 日常总结（与任何项目都无关）。

    适用于"刚解决一个困扰很久的问题"这类跨项目经验沉淀：先由 AI 把原始
    材料（复盘文档等）提炼成结构化总结（事件/根因/修复/可复用教训），再调用
    本工具入库。固定存入 personal 保留命名空间，自动追加 retro 标签、
    脱敏敏感信息并留审计痕。同标题再提交视为刷新更新。

    category: lesson(默认，排障/复盘经验) | decision | context
    source_doc: 原始材料文件名（可选，自动落成 src:xxx 标签便于溯源）
    """
    security.check_project(db.PERSONAL_PROJECT)
    conn = db.get_conn()
    tags = list(tags)
    if "retro" not in tags:
        tags.append("retro")
    if source_doc:
        src = source_doc.strip()[-60:]
        if src not in tags:
            tags.append("src:" + src)
    item, _ = security.mask_item({
        "category": category, "title": title, "content": summary,
        "tags": tags,
    })
    row = db.insert_memory(conn, db.PERSONAL_PROJECT, item, "mcp", system)
    security.log("write", db.PERSONAL_PROJECT, "mcp", system,
                 row["id"], row["title"])
    return _out(row)


@mcp.tool()
def memory_retro_search(query: str = "", limit: int = 20) -> list[dict]:
    """检索个人复盘 / 日常总结（personal 命名空间，项目无关）。

    query 非空走全文检索（中文子串可用）；为空则按最近更新时间列出。
    """
    security.check_project(db.PERSONAL_PROJECT)
    conn = db.get_conn()
    rows = db.search_memories(conn, db.PERSONAL_PROJECT, None,
                             query or None, None, False, False, limit)
    security.log("read", db.PERSONAL_PROJECT, detail="retro_search q=%s" % (query or ""))
    return [_out(r) for r in rows]


@mcp.tool()
def memory_backup() -> dict:
    """立即执行一次备份导出 + 邮件推送。"""
    return backup_mod.run_backup()
