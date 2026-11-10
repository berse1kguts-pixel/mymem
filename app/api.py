# -*- coding: utf-8 -*-
"""REST API：hook 摄入、记忆 CRUD、每日归档、bootstrap、审计、备份触发。

合规：入库自动脱敏；项目级 token 只能访问自己的项目；读写全留痕。
"""
from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse

from . import backup, config, db, models, security

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

    同 project+category+标题 的条目：内容无变化则合并刷新；
    内容有变化则新建版本并把旧版本标记为 superseded（历史可追溯）。
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
    _guard(req.project, "write", req.agent, req.system, detail="archive %s" % req.date)
    data = db.insert_memory(conn, req.project, {
        "category": "archive",
        "title": req.title or "每日归档 %s" % req.date,
        "content": req.summary,
        "tags": ["archive", req.date],
    }, req.agent, req.system)
    marked = db.mark_archived(conn, req.covered_ids)
    out = _out(data)
    return out.model_copy(update={"tags": out.tags + ["covered:%d" % marked]})


@router.post("/retro", response_model=models.RetroOut)
def add_retro(req: models.RetroRequest):
    """个人复盘 / 日常总结（与任何项目都无关）。

    AI 从原始材料（复盘文档、长篇排障记录等）提炼出结构化总结后提交；
    服务端只负责入库：固定归入 personal 保留命名空间、自动追加 "retro" 标签、
    脱敏 + 审计留痕。同标题再次复盘视为一次修订：内容有变化时保留旧版本
    （supersedes_id 版本链），内容不变则只刷新时间；显式 supersedes_id 可直接指定父版本。
    """
    conn = db.get_conn()
    _guard(db.PERSONAL_PROJECT, "write", req.agent, req.system,
           detail="retro add %s" % req.title)
    tags = list(req.tags)
    if "retro" not in tags:
        tags.append("retro")
    if req.source_doc:
        src = req.source_doc.strip()[-60:]
        if src not in tags:
            tags.append("src:" + src)
    item, masked = security.mask_item({
        "category": req.category,
        "title": req.title,
        "content": req.summary,
        "tags": tags,
        "supersedes_id": req.supersedes_id,
    })
    data = db.insert_memory(conn, db.PERSONAL_PROJECT, item,
                           req.agent, req.system)
    security.log("write", db.PERSONAL_PROJECT, req.agent, req.system,
                 data["id"], data["title"])
    return models.RetroOut(
        id=data["id"], project=data["project"], category=data["category"],
        title=data["title"], content=data["content"], tags=data["tags"],
        created_at=data["created_at"], updated_at=data["updated_at"],
        masked=masked,
    )


@router.get("/retro", response_model=list[models.RetroOut])
def list_retros(q: str | None = None, limit: int = Query(50, le=500)):
    """列出个人复盘 / 日常总结（personal 命名空间，项目无关）。

    q 非空走全文检索（中文子串可用）。
    """
    conn = db.get_conn()
    _guard(db.PERSONAL_PROJECT, "read", detail="retro list q=%s" % (q or ""))
    rows = db.search_memories(conn, db.PERSONAL_PROJECT, None,
                             q, None, False, False, limit, 0)
    return [models.RetroOut(
        id=r["id"], project=r["project"], category=r["category"],
        title=r["title"], content=r["content"], tags=r["tags"],
        created_at=r["created_at"], updated_at=r["updated_at"],
    ) for r in rows]


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


@router.get("/export")
def export_all():
    """一键导出全部记忆（zip：memories.json + digest.md + sqlite 快照 + manifest）。

    下载后可直接解压查看，或用 mymem.sqlite3 替换新实例的 data 目录完成迁移。
    项目级 token 无权导出（会跨项目泄露）。
    """
    scope = security.current_scope()
    if scope not in (None, "*"):
        raise HTTPException(403, "导出全部数据需要全局 token")
    security.log("read", "", detail="export all")
    path, filename = backup.build_export_zip()
    return FileResponse(path, filename=filename, media_type="application/zip")


@router.post("/import")
def import_all(payload: dict = Body(...)):
    """导入 memories.json（导出包里的同名文件，也接受纯数组）。

    按 project+category+title+content 去重，已存在的条目跳过；
    返回 {imported, skipped, total}。项目级 token 无权导入。
    """
    scope = security.current_scope()
    if scope not in (None, "*"):
        raise HTTPException(403, "导入数据需要全局 token")
    items = payload.get("memories") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise HTTPException(400, "需要包含 memories 数组的 JSON")
    result = db.import_memories(db.get_conn(), items)
    security.log("write", "", detail="import %d skipped %d"
                 % (result["imported"], result["skipped"]))
    return result


@router.get("/status")
def get_status():
    conn = db.get_conn()
    total = 0
    try:
        cur = conn.execute("SELECT COUNT(*) as c FROM memories")
        row = cur.fetchone()
        if isinstance(row, dict):
            total = row.get("c", 0)
        elif row:
            total = row[0]
    except Exception as e:
        total = str(e)
    return {
        "status": "ok",
        "backend": "tidb" if config.is_tidb_enabled() else "sqlite",
        "tidb_configured": config.is_tidb_enabled(),
        "total_memories": total
    }
