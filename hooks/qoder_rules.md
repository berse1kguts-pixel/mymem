# Schierke 记忆中心 × Qoder 对接规范

## 〇、会话开始（必读约定与有效决策）

任务开始前，调用 MCP 工具 `memory_bootstrap`（参数 `project`=当前项目名），拉取项目专属的必读约定（convention）和有效决策（decision），严格遵循既有规范。

---

## 一、双轨上报机制（核心原则）

### 轨 1：代码改动自动上报（由硬钩子静默兜底）
* 当你在工作区进行了代码修改、或者产生了新 commit，当单轮响应结束（`Stop`）或会话结束（`SessionEnd`）时，Qoder 配置的系统硬钩子会自动调用脚本：
  ```bash
  d:\program\code\mymem\.venv\Scripts\python.exe D:/program/code/mymem/scripts/mymem_qoder_hook.py --agent qoder
  ```
  自动整理 git diff / commit 摘要并推送至 mymem（`category="context"`）。
* **此流程完全静默自动执行，无需模型手动执行命令行。**

### 轨 2：对话结论主动上报（必须主动调用 MCP，不可遗漏！）
仅靠代码 diff 无法沉淀思考过程与技术精髓。**凡在对话中产出以下四类高价值结论，任务结束前必须主动调用 MCP 工具 `memory_add` 沉淀到 mymem**：

| 类别 (`category`) | 上报时机与典型内容 |
|---|---|
| **`decision`** (架构决策) | 技术选型、重构方案、接口设计决策及其权衡理由 |
| **`lesson`** (教训/避坑) | 遇到的报错、调试排障思路、隐蔽陷阱、踩坑后的正确解法 |
| **`convention`** (团队规范) | 命名规约、工时填报铁律（如禅道不填 estimate）、提交格式等约定 |
| **`context`** (分析结论) | 复杂的业务逻辑梳理、现场环境调研、第三方协议格式分析 |

#### MCP 调用标准入参：
```json
{
  "project": "当前项目名",       // 必填：与当前工作区一致（如 IOT-backend、mymem），不同项目物理隔离
  "category": "lesson",        // 必填：decision | lesson | convention | context
  "title": "简明扼要的标题",     // 必填：如「钩子无日志时的排查方法」
  "content": "具体的经验或决策内容", // 必填：讲清背景、排查原因、解决结论
  "tags": ["hooks", "lesson"], // 可选：便于检索分类的标签
  "agent": "qoder"             // 标记来源为 qoder
}
```

---

## 二、项目隔离与覆盖原则

1. **项目隔离**：`project` 参数必须严格区分（公司项目传公司项目名，个人项目传个人项目名），严禁混淆。
2. **知识迭代**：若发现既有记忆已被新设计推翻或过时，可在 `memory_add` 中传入旧记忆的 `supersedes_id` 实现原地版本迭代。
3. **收尾自检**：每一轮任务完成准备向用户汇报前，问自己一句：“本次排查或设计是否有以后其他 Agent 或自己还会踩坑的地方？”若有，必须在回答前完成 `memory_add` 入库！
