# -*- coding: utf-8 -*-
"""REST API：hook 摄入、记忆 CRUD、每日归档、备份触发。"""
from fastapi import APIRouter, HTTPException, Query

from . import backup, db, models

router = APIRouter(prefix="/api/v1")


@router.post("/ingest", response_model=models.IngestResult)
def ingest(req: models.IngestRequest):
    """hook 接口：agent 完成修改后提交本次会话的记忆条目。

    相同 project+category+标题 的条目自动合并更新；
    MemoryCreate.supersedes_id 可显式覆盖旧记忆。
    可选 session_summary 会作为 context 类记忆存档。
    """
    conn = db.get_conn()
    created, merged = [], []
    items = list(req.memories)
    if req.session_summary:
        items.insert(0, models.MemoryCreate(
            category="context",
            title="会话摘要 %s" % req.agent,
            content=req.session_summary,
            tags=["session"],
        ))
    for item in items:
        data = db.insert_memory(conn, req.project, item.model_dump(),
                                req.agent, req.system)
        out = models.MemoryOut(**{k: data[k] for k in models.MemoryOut.model_fields})
        (merged if data.get("merged") else created).append(out)
    return models.IngestResult(created=created, merged=merged)


@router.post("/memories", response_model=models.MemoryOut)
def create_memory(project: str = Query(...), agent: str = Query("manual"),
                  system: str = Query(""),
                  item: models.MemoryCreate = ...):
    conn = db.get_conn()
    data = db.insert_memory(conn, project, item.model_dump(), agent, system)
    return models.MemoryOut(**{k: data[k] for k in models.MemoryOut.model_fields})


@router.get("/memories", response_model=list[models.MemoryOut])
def list_memories(project: str | None = None, category: str | None = None,
                  q: str | None = None,
                  date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                  include_archived: bool = False,
                  include_superseded: bool = False,
                  limit: int = Query(50, le=500), offset: int = 0):
    conn = db.get_conn()
    rows = db.search_memories(conn, project, category, q, date,
                              include_archived, include_superseded,
                              limit, offset)
    return [models.MemoryOut(**{k: r[k] for k in models.MemoryOut.model_fields})
            for r in rows]


@router.get("/memories/{memory_id}", response_model=models.MemoryOut)
def get_memory(memory_id: int):
    conn = db.get_conn()
    row = db.get_memory(conn, memory_id)
    if not row:
        raise HTTPException(404, "memory not found")
    return models.MemoryOut(**{k: row[k] for k in models.MemoryOut.model_fields})


@router.patch("/memories/{memory_id}", response_model=models.MemoryOut)
def update_memory(memory_id: int, fields: models.MemoryUpdate):
    conn = db.get_conn()
    if not db.get_memory(conn, memory_id):
        raise HTTPException(404, "memory not found")
    row = db.update_memory(conn, memory_id, fields.model_dump(exclude_none=True))
    return models.MemoryOut(**{k: row[k] for k in models.MemoryOut.model_fields})


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: int):
    conn = db.get_conn()
    if not db.delete_memory(conn, memory_id):
        raise HTTPException(404, "memory not found")
    return {"deleted": memory_id}


@router.get("/digest/daily")
def daily_digest(project: str = Query(...),
                 date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
                 include_archived: bool = False):
    """每日归档用：拉取某一天的记忆，按类别分组返回。

    AI 拉取后整理成整体总结，再调 POST /digest/archive 提交归档。
    """
    conn = db.get_conn()
    rows = db.daily_rows(conn, project, date, include_archived)
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["category"], []).append(
            models.MemoryOut(**{k: r[k] for k in models.MemoryOut.model_fields}))
    return {"project": project, "date": date,
            "count": len(rows), "groups": groups,
            "unarchived_ids": [r["id"] for r in rows if not r["archived"]]}


@router.post("/digest/archive", response_model=models.MemoryOut)
def archive_digest(req: models.DigestRequest):
    """提交每日归档总结：生成 archive 记忆，并把 covered_ids 标记为已归档。"""
    conn = db.get_conn()
    data = db.insert_memory(conn, req.project, {
        "category": "archive",
        "title": "每日归档 %s" % req.date,
        "content": req.summary,
        "tags": ["archive", req.date],
    }, req.agent)
    marked = db.mark_archived(conn, req.covered_ids)
    out = models.MemoryOut(**{k: data[k] for k in models.MemoryOut.model_fields})
    return out.model_copy(update={"tags": out.tags + ["covered:%d" % marked]})


@router.post("/backup/run")
def run_backup():
    """立即执行一次备份导出（+ 邮件推送，若已配置 SMTP）。"""
    result = backup.run_backup()
    return result
