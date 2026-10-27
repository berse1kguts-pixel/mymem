# -*- coding: utf-8 -*-
"""mymem × Qoder 硬钩子调度器。

由 Qoder Hooks 的确定性事件触发（settings.json 的 hooks 分组），
事件触发就执行脚本，不依赖模型自觉：

- SessionStart：拉取项目必读记忆（bootstrap）并注入会话上下文
- Stop / SessionEnd / TaskCompleted：把本次代码改动整理上报（复用 mymem_autoreport）

用法（Qoder hooks 里配置）：
    python mymem_qoder_hook.py [--project 项目名] [--agent qoder]

项目名解析顺序：--project > 环境变量 MYMEM_PROJECT > 当前 git 仓库目录名。
stdin 会收到 Qoder 传入的事件 JSON（含 hook_event_name / cwd）。
本脚本永不阻塞（始终退出码 0），上报失败不影响 Agent 流程。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPORT_EVENTS = {"Stop", "SessionEnd", "TaskCompleted"}
BOOTSTRAP_EVENTS = {"SessionStart"}


def read_stdin() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def resolve_project(arg_project: str, cwd: str) -> str:
    if arg_project:
        return arg_project
    env = os.environ.get("MYMEM_PROJECT", "")
    if env:
        return env
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, cwd=cwd or None,
                             timeout=15, encoding="utf-8", errors="replace")
        if out.returncode == 0 and out.stdout.strip():
            return os.path.basename(out.stdout.strip())
    except Exception:
        pass
    return ""


def do_report(project: str, agent: str, cwd: str) -> None:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "mymem_autoreport.py")
    subprocess.run([sys.executable, script, "--project", project,
                    "--agent", agent],
                   cwd=cwd or None, timeout=120,
                   capture_output=True, encoding="utf-8", errors="replace")


def do_bootstrap(project: str) -> None:
    from mymem_hook import TOKEN, URL
    url = "%s/api/v1/bootstrap?project=%s&limit=20" % (
        URL, urllib.parse.quote(project))
    headers = {"Authorization": "Bearer " + TOKEN} if TOKEN else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.load(resp)
    except Exception as e:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "（mymem bootstrap 获取失败：%s）" % e,
        }}, ensure_ascii=False))
        return

    lines = ["# mymem 项目必读记忆（%s）" % project,
             "以下为该项目的约定与有效决策，务必遵守：", ""]
    for it in body.get("must_read", []):
        lines.append("- [%s] %s：%s" % (it.get("category"),
                                        it.get("title"), it.get("content")))
    if not body.get("must_read"):
        lines.append("（暂无）")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "\n".join(lines),
    }}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="mymem Qoder 硬钩子")
    parser.add_argument("--project", default="")
    parser.add_argument("--agent", default="qoder")
    args = parser.parse_args()

    payload = read_stdin()
    event = payload.get("hook_event_name", "")
    cwd = payload.get("cwd", "")
    project = resolve_project(args.project, cwd)
    if not project:
        return 0

    if event in BOOTSTRAP_EVENTS:
        do_bootstrap(project)
    elif event in REPORT_EVENTS:
        do_report(project, args.agent, cwd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
