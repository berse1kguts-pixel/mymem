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
    system: str = ""  # 系统标识：主机名|操作系统
    title: str = ""   # 可选：链路周期总结等自定义标题


class RetroRequest(BaseModel):
    """个人复盘 / 日常总结：与任何项目都无关。

    由 AI 从原始材料（如复盘文档）提炼出结构化内容后提交；
    服务端只负责入库（归入 personal 保留命名空间）+ 自动脱敏 + 审计留痕。
    """
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1)  # AI 生成的结构化总结（事件/根因/教训等）
    category: Literal["lesson", "decision", "context"] = "lesson"
    tags: list[str] = []                # 自动追加 "retro" 与可选日期 tag
    source_doc: str = ""                # 原始材料文件名/来源（可选，便于溯源）
    agent: str = "unknown"
    system: str = ""                    # 系统标识：主机名|操作系统
    supersedes_id: Optional[int] = None # 更新旧复盘时显式覆盖


class RetroOut(BaseModel):
    """retro 提交结果。"""
    id: int
    project: str
    category: str
    title: str
    content: str
    tags: list[str]
    created_at: str
    updated_at: str
    masked: int = 0                    # 入库时被打码的敏感串数量
