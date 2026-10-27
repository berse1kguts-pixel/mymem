# mymem 记忆上报规则

每次完成一个任务、修改了代码之后，必须运行以下命令上报本次修改的摘要记忆：

```
python D:/program/code/mymem/scripts/mymem_autoreport.py --project 本项目名 --agent trae
```

要求：

- `--project` 必须传当前项目的名字（个人项目传个人项目名，公司项目传公司项目名）。不同项目的记忆相互隔离，绝不能混用。
- `--agent` 传你自己的标识（如 trae、claude、cursor）。
- 如果改动里有值得长期记住的内容（技术决策、踩坑教训、项目约定），请额外用 mymem 的 MCP 工具 `memory_add` 精确记录一条（category 用 decision/lesson/convention），不要只依赖自动摘要。
- 发现旧记忆已过时或被推翻时，用 `supersedes_id` 覆盖它。
