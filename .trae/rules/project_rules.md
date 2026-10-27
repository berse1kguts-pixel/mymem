# mymem 记忆上报规则

## 〇、会话开始（先读记忆）

任务开始前调用 MCP 工具 `memory_bootstrap`（project=mymem）获取项目必读记忆（约定 + 有效决策），避免重复踩坑。

## 一、代码改动上报（自动兜底）

每次完成一个任务、修改了代码之后，必须运行以下命令上报本次修改的摘要记忆：

```
python D:/program/code/mymem/scripts/mymem_autoreport.py --project mymem --agent trae
```

## 二、对话结论上报（重要，不可跳过）

不只是代码改动——本次对话中产生的任何值得长期记住的内容都要上报，包括：

- 技术讨论的结论、选型决策及其理由（category=decision）
- 踩过的坑、排障思路、失败的尝试（category=lesson）
- 口头约定的项目规范、接口约定、协作方式（category=convention）
- 调研/分析的结论（category=context）

上报方式（任选其一，优先用 MCP）：

1. mymem 的 MCP 工具 `memory_add` / `memory_ingest`；
2. `python D:/program/code/mymem/scripts/mymem_hook.py digest.json`。

## 要求

- `--project` 固定传 `mymem`（这是个人项目）。不同项目的记忆相互隔离，绝不能混用公司项目的项目名。
- 发现旧记忆已过时或被推翻时，用 `supersedes_id` 覆盖它。
- 任务结束前自检：这次对话有没有留下值得记住的东西？有就必须上报，宁多勿漏。
