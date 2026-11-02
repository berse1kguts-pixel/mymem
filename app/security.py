# -*- coding: utf-8 -*-
"""合规层：敏感信息自动脱敏、项目级 token 隔离、审计留痕。

- 脱敏：入库前检测密钥/密码/私钥等敏感串并打码，防止记忆外泄；
- 隔离：全局 token（MYMEM_TOKEN）可读写全部项目；项目级 token
  （MYMEM_PROJECT_TOKENS="项目A=tokenA;项目B=tokenB"）只能访问对应项目；
- 审计：所有读写操作留痕（谁、何时、读写了哪个项目/哪条记忆）。
"""
import re
from contextvars import ContextVar

from . import config, db

# 当前请求的访问范围："*" = 全局；其它 = 仅限该项目；None = 未鉴权（无 token 配置）
_scope_var: ContextVar[str | None] = ContextVar("mymem_scope", default=None)

_TOKEN_PROJECT = {v: k for k, v in config.PROJECT_TOKENS.items()}

_MASK = "***已脱敏***"
_SECRET_PATTERNS = [
    # PEM 私钥块
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?"
                r"-----END [A-Z ]*PRIVATE KEY-----"), _MASK),
    # password=xxx / secret: xxx / token = xxx 等键值对
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|"
                r"access[_-]?key|private[_-]?key)\b(\s*[:=]\s*)\S{4,}"),
     lambda m: "%s%s%s" % (m.group(1), m.group(2), _MASK)),
    # 云厂商/大模型常见密钥
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), _MASK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
     _MASK),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{10,}"), "Bearer " + _MASK),
]


def mask_secrets(*texts: str) -> tuple[list[str], int]:
    """逐个文本打码敏感串，返回 (打码后的文本列表, 打码次数)。"""
    total = 0
    out: list[str] = []
    for text in texts:
        n = 0
        for pattern, repl in _SECRET_PATTERNS:
            text, cnt = pattern.subn(repl, text)
            n += cnt
        total += n
        out.append(text)
    return out, total


def mask_item(item: dict) -> tuple[dict, int]:
    """打码一条记忆的 title/content/tags。"""
    tags = " ".join(item.get("tags", []))
    (title, content, tags), n = mask_secrets(item["title"], item["content"], tags)
    if n:
        item = dict(item)
        item["title"], item["content"] = title, content
        if tags:
            item["tags"] = tags.split()
    return item, n


def set_scope_from_token(token: str) -> str | None:
    """按 token 解析访问范围；无效 token 返回 None。"""
    if config.API_TOKEN and token == config.API_TOKEN:
        return "*"
    if token in _TOKEN_PROJECT:
        return _TOKEN_PROJECT[token]
    return None


def current_scope() -> str | None:
    """当前请求的访问范围：None=未启用 token，"*"=全局，其它=项目名。"""
    return _scope_var.get()


def check_project(project: str) -> None:
    """项目级 token 越权访问时抛 PermissionError。"""
    scope = _scope_var.get()
    if scope and scope != "*" and project != scope:
        raise PermissionError("项目 token 只能访问项目 %s，不能访问 %s" % (scope, project))


def check_project_quiet(project: str) -> bool:
    try:
        check_project(project)
        return True
    except PermissionError:
        return False


def log(action: str, project: str = "", agent: str = "", system: str = "",
        memory_id: int | None = None, detail: str = "") -> None:
    db.log_audit(db.get_conn(), action, project, agent, system, memory_id, detail)
