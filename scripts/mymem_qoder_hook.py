# -*- coding: utf-8 -*-
"""mymem 硬钩子调度器（兼容 Qoder 与 Antigravity）。

由确定性 Hooks 的事件触发，事件触发就执行脚本，不依赖模型自觉：

- SessionStart：拉取项目必读记忆（bootstrap）并注入会话上下文
- Stop / SessionEnd / TaskCompleted：把本次代码改动整理上报（复用 mymem_autoreport）

用法：
    Qoder：      python mymem_qoder_hook.py --agent qoder
    Antigravity：python mymem_qoder_hook.py --event Stop --agent antigravity

两家协议差异：
- Qoder：事件名在 stdin JSON 的 hook_event_name，目录在 cwd；
  SessionStart 上下文用 hookSpecificOutput.additionalContext 返回。
- Antigravity：stdin 不带事件名（用 --event 传入），目录在 workspacePaths；
  SessionStart 用 injectSteps 注入，Stop 需返回 {"decision": "stop"}。

项目名解析顺序：--project > 环境变量 MYMEM_PROJECT > 当前 git 仓库目录名。
本脚本永不阻塞（始终退出码 0），上报失败不影响 Agent 流程。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

LOG_PATH = os.path.join(tempfile.gettempdir(), "mymem_qoder_hook.log")


def log_line(msg: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPORT_EVENTS = {"Stop", "SessionEnd", "TaskCompleted"}
BOOTSTRAP_EVENTS = {"SessionStart"}


def read_stdin() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def git_toplevel(path: str) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, cwd=path or None,
                             timeout=15, encoding="utf-8", errors="replace")
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def discover_repos(cwd: str) -> list[str]:
    """返回要上报的 git 仓库路径列表。

    cwd 是 git 仓库 → [仓库根目录]；
    否则（多仓库工作区，如 IOT/ 下含 IOT-backend、IOT-frontend）
    → 扫描一层子目录，返回所有含 .git 的子项目；都没有则回退 [cwd]。
    """
    if not cwd:
        return []
    top = git_toplevel(cwd)
    if top:
        return [top]
    repos = []
    try:
        for name in sorted(os.listdir(cwd)):
            p = os.path.join(cwd, name)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, ".git")):
                repos.append(p)
    except Exception:
        pass
    return repos or [cwd]


def repo_name(path: str) -> str:
    return os.path.basename(git_toplevel(path) or os.path.normpath(path))


def resolve_project(arg_project: str, cwd: str) -> str:
    if arg_project:
        return arg_project
    env = os.environ.get("MYMEM_PROJECT", "")
    if env:
        return env
    return repo_name(cwd) if cwd else ""


def do_report(project: str, agent: str, cwd: str) -> None:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "mymem_autoreport.py")
    repos = discover_repos(cwd)
    for rpath in repos:
        pname = repo_name(rpath) or project
        cmd = [sys.executable, script, "--project", pname, "--agent", agent, "--cwd", rpath]
        res = subprocess.run(cmd, cwd=rpath, timeout=120,
                             capture_output=True, encoding="utf-8", errors="replace")
        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
        log_line("[%s] autoreport project=%s rc=%d out=%s err=%s" % (
            __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            pname, res.returncode, out or "empty", err or "empty"))


def do_bootstrap(project: str, agy: bool) -> None:
    from mymem_hook import TOKEN, URL
    url = "%s/api/v1/bootstrap?project=%s&limit=20" % (
        URL, urllib.parse.quote(project))
    headers = {"Authorization": "Bearer " + TOKEN} if TOKEN else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.load(resp)
    except Exception as e:
        text = "（mymem bootstrap 获取失败：%s）" % e
    else:
        lines = ["# mymem 项目必读记忆（%s）" % project,
                 "以下为该项目的约定与有效决策，务必遵守：", ""]
        for it in body.get("must_read", []):
            tags = it.get("tags") or []
            # 规则（tags 含 rule）标注为必须遵守，普通约定/决策带类别
            if "rule" in tags:
                lines.append("- 【规则·务必遵守】%s：%s" % (it.get("title"),
                                                        it.get("content")))
            else:
                lines.append("- [%s] %s：%s" % (it.get("category"),
                                                it.get("title"),
                                                it.get("content")))
        if not body.get("must_read"):
            lines.append("（暂无）")
        text = "\n".join(lines)

    if agy:
        print(json.dumps({"injectSteps": [
            {"systemMessage": {"systemMessage": text}}]}, ensure_ascii=False))
    else:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="mymem 硬钩子调度器")
    parser.add_argument("--project", default="")
    parser.add_argument("--agent", default="qoder")
    parser.add_argument("--event", default="",
                        help="事件名（Antigravity 经命令行传入；Qoder 在 stdin）")
    args = parser.parse_args()

    payload = read_stdin()
    event = args.event or payload.get("hook_event_name", "")
    agy = bool(args.event)  # 事件名来自命令行 → Antigravity 协议
    cwd = payload.get("cwd", "")
    if not cwd and payload.get("workspacePaths"):
        cwd = payload["workspacePaths"][0]
    project = resolve_project(args.project, cwd)
    log_line("[%s] event=%s agent=%s project=%s cwd=%s keys=%s" % (
        __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        event or "?", args.agent, project or "?", cwd or "?",
        ",".join(sorted(payload.keys())) or "no-stdin"))
    if not project:
        if agy and event == "Stop":
            print('{"decision": "stop"}')
        return 0

    if event in BOOTSTRAP_EVENTS:
        do_bootstrap(project, agy)
    elif event in REPORT_EVENTS:
        try:
            do_report(project, args.agent, cwd)
        except Exception:
            pass
        if agy and event == "Stop":
            print('{"decision": "stop"}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
