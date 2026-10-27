# -*- coding: utf-8 -*-
import os
import tempfile

os.environ["MYMEM_DATA_DIR"] = tempfile.mkdtemp(prefix="mymem-test-")
os.environ["MYMEM_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health():
    assert client.get("/health").json()["status"] == "ok"


def test_ingest_merge_and_supersede():
    r = client.post("/api/v1/ingest", json={
        "project": "demo", "agent": "trae", "system": "HOST-A|Windows-11",
        "session_summary": "完成了存储层",
        "memories": [
            {"category": "decision", "title": "使用 SQLite",
             "content": "轻量、零运维", "tags": ["db"]},
            {"category": "lesson", "title": "PowerShell 不支持 &&",
             "content": "用分号分隔命令"},
        ],
    })
    assert r.status_code == 200
    body = r.json()
    assert len(body["created"]) == 3  # 含 session_summary
    assert body["created"][0]["source_system"] == "HOST-A|Windows-11"

    # 同标题再提交 → 合并更新，不新增
    r2 = client.post("/api/v1/ingest", json={
        "project": "demo", "agent": "claude",
        "memories": [{"category": "decision", "title": "使用 SQLite",
                      "content": "轻量、零运维、支持 FTS5", "tags": ["db"]}],
    })
    assert len(r2.json()["created"]) == 0
    assert len(r2.json()["merged"]) == 1

    # supersedes 覆盖旧记忆（created[2] 是 PowerShell 教训那条）
    old_id = body["created"][2]["id"]
    r3 = client.post("/api/v1/ingest", json={
        "project": "demo", "agent": "trae",
        "memories": [{"category": "lesson", "title": "PowerShell 命令分隔",
                      "content": "PowerShell 5 用分号，不能用 &&",
                      "supersedes_id": old_id}],
    })
    new_id = r3.json()["created"][0]["id"]
    old = client.get("/api/v1/memories/%d" % old_id).json()
    assert old["status"] == "superseded"
    assert client.get("/api/v1/memories/%d" % new_id).json()["status"] == "active"


def test_search():
    r = client.get("/api/v1/memories", params={"project": "demo", "q": "SQLite"})
    assert r.status_code == 200
    titles = [m["title"] for m in r.json()]
    assert any("SQLite" in t for t in titles)

    from datetime import date
    r2 = client.get("/api/v1/memories",
                    params={"date": date.today().isoformat(), "limit": 500})
    assert all(date.today().isoformat() in m["created_at"] for m in r2.json())


def test_ui_page():
    r = client.get("/ui")
    assert r.status_code == 200
    assert "AI 记忆中心" in r.text
    assert client.get("/").status_code in (200, 307)


def test_daily_digest_and_archive():
    from datetime import date
    today = date.today().isoformat()
    r = client.get("/api/v1/digest/daily",
                   params={"project": "demo", "date": today})
    assert r.status_code == 200
    ids = r.json()["unarchived_ids"]
    assert ids

    r2 = client.post("/api/v1/digest/archive", json={
        "project": "demo", "date": today, "agent": "trae",
        "summary": "今日完成存储层与接口层，踩坑 PowerShell 兼容性。",
        "covered_ids": ids,
    })
    assert r2.status_code == 200
    assert r2.json()["category"] == "archive"

    r3 = client.get("/api/v1/digest/daily",
                    params={"project": "demo", "date": today})
    assert r3.json()["unarchived_ids"] == []


def test_backup_export():
    r = client.post("/api/v1/backup/run")
    assert r.status_code == 200
    body = r.json()
    assert body["exported"]["count"] >= 1
    assert os.path.exists(body["exported"]["json"])
    assert os.path.exists(body["exported"]["markdown"])
    assert os.path.exists(body["exported"]["snapshot"])


def test_mcp_endpoint_reachable():
    with TestClient(app) as client:  # 进入 lifespan，初始化 MCP task group
        r = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26",
                       "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}},
        }, headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 200


def test_rules_pinned_first():
    """规则（tags 含 rule）：bootstrap 置顶 + rule 过滤查询。"""
    client.post("/api/v1/ingest", json={
        "project": "demo", "agent": "trae",
        "memories": [{"category": "convention", "title": "禁止直接改生产库",
                      "content": "必须走 SQL 审核流程", "tags": ["rule"]}],
    })
    b = client.get("/api/v1/bootstrap", params={"project": "demo"}).json()
    assert b["must_read"], "bootstrap 应返回必读记忆"
    first = b["must_read"][0]
    assert first["title"] == "禁止直接改生产库"   # 规则排最前
    assert "rule" in first["tags"]

    rows = client.get("/api/v1/memories",
                      params={"project": "demo", "rule": "true"}).json()
    assert rows and all("rule" in m["tags"] for m in rows)
    assert any(m["title"] == "禁止直接改生产库" for m in rows)


def test_period_digest_and_archive_title():
    """链路周期总结：period_rows 区间查询 + 归档自定义标题。"""
    from datetime import date
    from app import db as _db
    today = date.today().isoformat()
    rows = _db.period_rows(_db.get_conn(), "demo", today, today)
    assert rows

    r = client.post("/api/v1/digest/archive", json={
        "project": "demo", "date": today,
        "title": "链路周期总结 %s ~ %s" % (today, today),
        "summary": "本期完成了规则功能与界面精简。",
        "covered_ids": [],
    })
    assert r.status_code == 200
    assert r.json()["title"].startswith("链路周期总结")
