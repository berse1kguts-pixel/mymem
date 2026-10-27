# mymem · 跨 Agent 共享记忆中心

> 为所有 Code Agent（Trae、Antigravity、Qoder、Cursor、Claude Code 等）提供**长期记忆、约定同步与经验沉淀**的中枢服务。  
> 局域网/公网访问地址：`http://1.92.76.106`

---

## 核心设计：双轨制记忆上报

1. **底线保底（确定性硬钩子）**：每次对话结束（`Stop` / `SessionEnd`）时，IDE 自动触发外部 Python 调度脚本，抓取 Git 代码变更并上报，**不依赖模型自觉**。
2. **价值核心（MCP 主动沉淀）**：在对话中产生的**决策（decision）、踩坑教训（lesson）、团队约定（convention）**，由 Agent 自主调用 MCP 工具 `memory_add` 写入，确保有深度的知识沉淀。

---

## 极简对接指引（以 Qoder 和 Antigravity 为例）

### 一、 Qoder (Qorder) 接入

#### 第一步：配置确定性硬钩子（改动自动上报）
在 `~/.qoder/settings.json` 中配置 `hooks` 分组（或直接合并 [hooks/qoder_hooks.json](hooks/qoder_hooks.json)）：

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "name": "mymem-bootstrap",
            "command": "d:\\program\\code\\mymem\\.venv\\Scripts\\python.exe D:/program/code/mymem/scripts/mymem_qoder_hook.py --agent qoder",
            "timeout": 20
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "name": "mymem-report",
            "command": "d:\\program\\code\\mymem\\.venv\\Scripts\\python.exe D:/program/code/mymem/scripts/mymem_qoder_hook.py --agent qoder",
            "async": true,
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

#### 第二步：配置 MCP 服务（允许 Qoder 主动沉淀记忆）
在项目根目录 `.mcp.json`（或全局 `~/.qoder/settings.json` 的 `mcpServers`）中添加 [hooks/qoder_mcp.json](hooks/qoder_mcp.json)：

```json
{
  "mcpServers": {
    "mymem": {
      "type": "http",
      "url": "http://1.92.76.106/mcp",
      "description": "mymem · 跨 agent 共享记忆中枢",
      "trust": true,
      "alwaysAllow": [
        "memory_bootstrap",
        "memory_search",
        "memory_get",
        "memory_add",
        "memory_ingest"
      ]
    }
  }
}
```

#### 第三步：引入工作流提示词
将 [hooks/qoder_rules.md](hooks/qoder_rules.md) 放入 Qoder 的全局规则或工作区 `.qoder/rules/`，引导 Qoder 在完成排障或设计时主动调用 `memory_add`。

---

### 二、 Antigravity（反重力）接入

#### 第一步：配置全局 Hooks
在 `~/.gemini/config/hooks.json` 中配置：
```json
{
  "mymem": {
    "enabled": true,
    "SessionStart": [
      {
        "type": "command",
        "command": "d:\\program\\code\\mymem\\.venv\\Scripts\\python.exe D:/program/code/mymem/scripts/mymem_qoder_hook.py --event SessionStart --agent antigravity",
        "timeout": 30
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "d:\\program\\code\\mymem\\.venv\\Scripts\\python.exe D:/program/code/mymem/scripts/mymem_qoder_hook.py --event Stop --agent antigravity",
        "timeout": 60
      }
    ]
  }
}
```

#### 第二步：配置全局 MCP
在 `~/.gemini/config/mcp_config.json` 中加入：
```json
{
  "mcpServers": {
    "mymem": {
      "serverUrl": "http://1.92.76.106/mcp"
    }
  }
}
```
*注：Antigravity 远程 HTTP 服务字段必须使用 `serverUrl`。*

---

### 三、 避坑与常见问题

1. **Python 解释器路径**：Windows 环境下命令行请勿直接写裸 `python`（可能未加入 PATH），统一使用完整虚拟环境路径：`d:\program\code\mymem\.venv\Scripts\python.exe`。
2. **多 Repo 顶层工作区**：若当前打开的工作区根目录不是 Git 仓库（如根目录包含 `backend`、`frontend` 两个独立 Git 仓库），调度脚本会自动探测子目录并分别上报，无需手动切目录。
3. **日志排查**：运行日志统一输出在 `%TEMP%/mymem_qoder_hook.log`，可实时查看触发事件、上报状态及返回码。
