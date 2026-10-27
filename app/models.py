# -*- coding: utf-8 -*-
"""Pydantic 模型：API / hook / 归档的请求与响应结构。"""
from typing import Literal, Optional

from pydantic import BaseModel, Field

Category = Literal["decision", "lesson", "convention", "context", "archive"]


class MemoryCreate(BaseModel):
    category: Category
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    tags: list[str] = []
    supersedes_id: Optional[int] = None


class MemoryUpdate(BaseModel):
    category: Optional[Category] = None
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    status: Optional[Literal["active", "superseded"]] = None
    archived: Optional[bool] = None


class IngestRequest(BaseModel):
    """hook 摄入：agent 在完成一段修改后提交本次会话产生的记忆。"""
    project: str = Field(min_length=1, max_length=100)
    agent: str = "unknown"
    system: str = ""  # 系统标识：主机名|操作系统，由 hook 自动采集
    session_summary: Optional[str] = None
    memories: list[MemoryCreate] = []


class MemoryOut(BaseModel):
    id: int
    project: str
    category: Category
    title: str
    content: str
    tags: list[str]
    source_agent: str
    source_system: str
    status: str
    archived: bool
    supersedes_id: Optional[int]
    created_at: str
    updated_at: str


class IngestResult(BaseModel):
    created: list[MemoryOut] = []
    merged: list[MemoryOut] = []
    masked: int = 0  # 入库时被打码的敏感串数量


class DigestRequest(BaseModel):
    """每日归档：AI 拉取当日记忆后提交整体总结。"""
    project: str
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    summary: str = Field(min_length=1)
    covered_ids: list[int] = []
    agent: str = "unknown"
