# -*- coding: utf-8 -*-
"""REST API：hook 摄入、记忆 CRUD、每日归档、bootstrap、审计、备份触发。

合规：入库自动脱敏；项目级 token 只能访问自己的项目；读写全留痕。
"""
from fastapi import APIRouter, HTTPException, Query

from . import backup, db, models, security

router = APIRouter(prefix="/api/v1")


def _out(data: dict) -> models.MemoryOut:
    return models.MemoryOut(**{k: data[k] for k in models.MemoryOut.model_fields})


def _guard(project: str, action: str, agent: str = "", system: str = "",
           memory_id: int | None = None, detail: str = "") -> None:
    if not security.check_project_quiet(project):
        raise HTTPException(403, "项目 token 无权访问项目 %s" % project)
    security.log(action, project, agent, system, memory_id, detail)


@router.post("/ingest", response_model=models.IngestResult)
def ingest(req: models.IngestRequest):
    """hook 接口：agent 完成修改后提交本次会话的记忆条目。

    相同 project+category+标题 的条目自动合并更新；
    MemoryCreate.supersedes_id 可显式覆盖旧记忆。
    可选 session_summary 会作为 context 类记忆存档。
    入库前自动脱敏敏感信息。
    """
    conn = db.get_conn()
    _guard(req.project, "write", req.agent, req.system, detail="ingest")
    created, merged, masked = [], [], 0
    items = list(req.memories)
    if req.session_summary:
        items.insert(0, models.MemoryCreate(
            category="context",
            title="会话摘要 %s" % req.agent,
            content=req.session_summary,
            tags=["session"],
        ))
    for item in items:
        data, n = security.mask_item(item.model_dump())
        masked += n
        data = db.insert_memory(conn, req.project, data, req.agent, req.system)
        security.log("write", req.project, req.agent, req.system,
                     data["id"], data["title"])
        (merged if data.get("merged") else created).append(_out(data))
    return models.IngestResult(created=created, merged=merged, masked=masked)


@router.post("/memories", response_model=models.MemoryOut)
def create_memory(project: str = Query(...), agent: str = Query("manual"),
                  system: str = Query(""),
                  item: models.MemoryCreate = ...):
    conn = db.get_conn()
    _guard(project, "write", agent, system)
    data, _ = security.mask_item(item.model_dump())
    data = db.insert_memory(conn, project, data, agent, system)
    security.log("write", project, agent, system, data["id"], data["title"])
    return _out(data)


@router.get("/memories", response_model=list[models.MemoryOut])
def list_memories(project: str | None = None, category: str | None = None,
                  q: str | None = None,
                  date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                  include_archived: bool = False,
                  include_superseded: bool = False,
                  rule: bool = False,
                  limit: int = Query(50, le=500), offset: int = 0):
    conn = db.get_conn()
    if project:
        _guard(project, "read", detail="search q=%s" % (q or ""))
    rows = db.search_memories(conn, project, category, q, date,
                              include_archived, include_superseded,
                              limit, offset, rule_only=rule)
    return [_out(r) for r in rows]


@router.get("/memories/{memory_id}", response_model=models.MemoryOut)
def get_memory(memory_id: int):
    conn = db.get_conn()
    row = db.get_memory(conn, memory_id)
    if not row:
        raise HTTPException(404, "memory not found")
    _guard(row["project"], "read", memory_id=memory_id)
    return _out(row)


@router.patch("/memories/{memory_id}", response_model=models.MemoryOut)
def update_memory(memory_id: int, fields: models.MemoryUpdate):
    conn = db.get_conn()
    old = db.get_memory(conn, memory_id)
    if not old:
        raise HTTPException(404, "memory not found")
    _guard(old["project"], "update", memory_id=memory_id)
    data = fields.model_dump(exclude_none=True)
    if {"title", "content", "tags"} & set(data):
        merged_item = {"title": data.get("title", old["title"]),
                       "content": data.get("content", old["content"]),
                       "tags": data.get("tags", old["tags"])}
        masked_item, _ = security.mask_item(merged_item)
        data.update({k: masked_item[k] for k in data})
    row = db.update_memory(conn, memory_id, data)
    return _out(row)


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: int):
    conn = db.get_conn()
    old = db.get_memory(conn, memory_id)
    if not old:
        raise HTTPException(404, "memory not found")
    _guard(old["project"], "delete", memory_id=memory_id, detail=old["title"])
    db.delete_memory(conn, memory_id)
    return {"deleted": memory_id}


@router.get("/digest/daily")
def daily_digest(project: str = Query(...),
                 date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
                 include_archived: bool = False):
    """每日归档用：拉取某一天的记忆，按类别分组返回。

    AI 拉取后整理成整体总结，再调 POST /digest/archive 提交归档。
    """
    conn = db.get_conn()
    _guard(project, "read", detail="daily_digest %s" % date)
    rows = db.daily_rows(conn, project, date, include_archived)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["category"], []).append(_out(r))
    return {"project": project, "date": date,
            "count": len(rows), "groups": groups,
            "unarchived_ids": [r["id"] for r in rows if not r["archived"]]}


@router.post("/digest/archive", response_model=models.MemoryOut)
def archive_digest(req: models.DigestRequest):
    """提交每日归档总结：生成 archive 记忆，并把 covered_ids 标记为已归档。"""
    conn = db.get_conn()
    _guard(req.project, "write", req.agent, detail="archive %s" % req.date)
    data = db.insert_memory(conn, req.project, {
        "category": "archive",
        "title": "每日归档 %s" % req.date,
        "content": req.summary,
        "tags": ["archive", req.date],
    }, req.agent)
    marked = db.mark_archived(conn, req.covered_ids)
    out = _out(data)
    return out.model_copy(update={"tags": out.tags + ["covered:%d" % marked]})


@router.get("/bootstrap")
def bootstrap(project: str = Query(...), limit: int = Query(20, le=100)):
    """会话开始时调用：返回该项目的必读记忆（约定 + 有效决策）。

    agent 开局即可获得项目背景，无需重复踩坑。
    """
    conn = db.get_conn()
    _guard(project, "read", detail="bootstrap")
    rows = db.bootstrap_rows(conn, project, limit)
    return {"project": project, "count": len(rows),
            "must_read": [_out(r) for r in rows]}


@router.get("/audit")
def audit_log(project: str | None = None,
              limit: int = Query(50, le=500)):
    """审计日志：查看哪个 agent 何时读写了哪个项目/哪条记忆。"""
    conn = db.get_conn()
    if project:
        _guard(project, "read", detail="audit")
    rows = db.audit_rows(conn, project, limit)
    return {"count": len(rows), "entries": rows}


@router.post("/backup/run")
def run_backup():
    """立即执行一次备份导出（+ 邮件推送，若已配置 SMTP）。"""
    result = backup.run_backup()
    return result
