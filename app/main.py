# -*- coding: utf-8 -*-
"""mymem：跨 code agent 的共享记忆服务。

- REST API（/api/v1/...）：hook 摄入、记忆 CRUD、每日归档、备份
- MCP 接口（/mcp）：供各 code agent 直接读写记忆
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from . import api, backup, config, security
from .mcp_server import mcp

mcp_app = mcp.streamable_http_app()
UI_PATH = Path(__file__).parent / "ui" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        backup.start_scheduler()
        yield


app = FastAPI(title="mymem", lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if (config.API_TOKEN and request.url.path not in ("/health", "/ui", "/")):
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        scope = security.set_scope_from_token(token)
        if scope is None:
            return Response("Unauthorized", status_code=401)
        # "*"=全局 token；其它=项目 token，只能访问该项目（API/MCP 内校验）
        security._scope_var.set(scope)
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "service": "mymem"}


@app.get("/ui", response_class=HTMLResponse)
def ui():
    return UI_PATH.read_text(encoding="utf-8")


@app.get("/")
def index():
    return RedirectResponse("/ui")


app.include_router(api.router)
app.mount("/", mcp_app)
