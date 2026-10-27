# -*- coding: utf-8 -*-
"""合规测试：脱敏、项目级 token 隔离、bootstrap、审计、中文全文检索。"""
import os
import tempfile

os.environ.setdefault("MYMEM_DATA_DIR", tempfile.mkdtemp(prefix="mymem-test-"))
os.environ.setdefault("MYMEM_TOKEN", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import config, security  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def test_mask_secrets_unit():
    texts, n = security.mask_secrets(
        "password=hunter22222",
        "token: abcdef1234567890",
        "AKIA0123456789ABCDEF",
        "Bearer eyJhbGciOi.abc123.def456",
        "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
    )
    assert n >= 5
    for masked, raw in zip(texts, ["hunter22222", "abcdef1234567890",
                                   "AKIA0123456789ABCDEF"]):
        assert raw not in masked
        assert "已脱敏" in masked
    assert "PRIVATE KEY" not in texts[4]


def test_ingest_masks_secrets():
    r = client.post("/api/v1/ingest", json={
        "project": "sec-demo", "agent": "trae",
        "memories": [{"category": "lesson", "title": "配置数据库",
                      "content": "连接串 password=SuperSecret123，注意保管",
                      "tags": []}],
    })
    body = r.json()
    assert body["masked"] >= 1
    stored = body["created"][0]["content"]
    assert "SuperSecret123" not in stored
    assert "已脱敏" in stored

    # 原始密文不应存在于数据库
    r2 = client.get("/api/v1/memories", params={"project": "sec-demo"})
    assert all("SuperSecret123" not in m["content"] for m in r2.json())


def test_project_token_isolation(monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "root-tok")
    monkeypatch.setattr(config, "PROJECT_TOKENS", {"mymem": "p-tok"})
    monkeypatch.setattr(security, "_TOKEN_PROJECT", {"p-tok": "mymem"})

    # 无 token → 401
    assert client.get("/api/v1/memories").status_code == 401
    # 项目 token 访问自己的项目 → 200
    r = client.get("/api/v1/memories",
                   headers={"Authorization": "Bearer p-tok"},
                   params={"project": "mymem"})
    assert r.status_code == 200
    # 项目 token 访问别的项目 → 403
    r2 = client.get("/api/v1/memories",
                    headers={"Authorization": "Bearer p-tok"},
                    params={"project": "company"})
    assert r2.status_code == 403
    # 项目 token 不能写别的项目
    r3 = client.post("/api/v1/ingest",
                     headers={"Authorization": "Bearer p-tok"},
                     json={"project": "company", "agent": "trae",
                           "memories": [{"category": "context", "title": "x",
                                         "content": "y"}]})
    assert r3.status_code == 403
    # 全局 token 畅通无阻
    r4 = client.get("/api/v1/memories",
                    headers={"Authorization": "Bearer root-tok"},
                    params={"project": "company"})
    assert r4.status_code == 200


def test_bootstrap_and_audit():
    client.post("/api/v1/ingest", json={
        "project": "boot-demo", "agent": "trae",
        "memories": [
            {"category": "convention", "title": "提交前必须跑测试",
             "content": "pytest 全过再提交", "tags": []},
            {"category": "decision", "title": "选型 SQLite",
             "content": "轻量免运维", "tags": []},
            {"category": "context", "title": "普通流水账",
             "content": "不该出现在 bootstrap", "tags": []},
        ],
    })
    r = client.get("/api/v1/bootstrap", params={"project": "boot-demo"})
    titles = [m["title"] for m in r.json()["must_read"]]
    assert "提交前必须跑测试" in titles
    assert "选型 SQLite" in titles
    assert "普通流水账" not in titles

    ra = client.get("/api/v1/audit", params={"project": "boot-demo"})
    entries = ra.json()["entries"]
    assert any(e["action"] == "write" for e in entries)
    assert any(e["action"] == "read" for e in entries)


def test_fts_chinese_search():
    client.post("/api/v1/ingest", json={
        "project": "fts-demo", "agent": "trae",
        "memories": [{"category": "lesson", "title": "端口拦截",
                      "content": "华为云非常规端口对外不通，需要放行安全组规则",
                      "tags": []}],
    })
    r = client.get("/api/v1/memories",
                   params={"project": "fts-demo", "q": "安全组"})
    assert r.status_code == 200
    assert any("安全组" in m["content"] for m in r.json())
