# -*- coding: utf-8 -*-
"""mymem 自动上报：从 git 工作区收集本次修改，整理成记忆并合并到 mymem。

由各 code agent 的 hook / 规则触发（不依赖 git 钩子），保证"改完就上报"。

用法：
    python mymem_autoreport.py --project mymem [--agent trae] [--since HEAD~1] [--dry-run]

项目区分（重要）：
    --project（或环境变量 MYMEM_PROJECT）必须与当前项目一致。
    不同项目名的记忆在 mymem 中相互隔离——个人项目传个人项目名，
    公司项目传公司项目名，检索/界面按项目过滤，互不混淆。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mymem_hook import TOKEN, URL, system_identity  # noqa: E402

MAX_CONTENT = 3000


def git(*args: str, cwd: str | None = None) -> str | None:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True,
                             cwd=cwd,
                             timeout=30, encoding="utf-8", errors="replace")
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def find_git_repos(cwd: str | None = None) -> list[tuple[str, str]]:
    """返回当前目录及其下所有有效的 git 仓库列表 [(repo_name, repo_dir), ...]"""
    target = os.path.abspath(cwd) if cwd else os.getcwd()
    if git("rev-parse", "--is-inside-work-tree", cwd=target) == "true":
        name = os.path.basename(target)
        return [(name, target)]

    repos = []
    try:
        for entry in os.listdir(target):
            sub = os.path.join(target, entry)
            if os.path.isdir(sub) and not entry.startswith("."):
                if git("rev-parse", "--is-inside-work-tree", cwd=sub) == "true":
                    repos.append((entry, sub))
    except Exception:
        pass
    return repos


def collect_repo_changes(repo_name: str, repo_dir: str, since: str | None,
                         is_multi: bool) -> tuple[str, list[str]]:
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_dir) or "unknown"
    header = "[%s] 分支：%s" % (repo_name, branch) if is_multi else "分支：%s" % branch
    parts: list[str] = [header]

    if since:
        log = git("log", "--oneline", "%s..HEAD" % since, cwd=repo_dir)
        stat = git("diff", "--stat", since, cwd=repo_dir)
    else:
        status = git("status", "-s", cwd=repo_dir)
        if status:
            parts.append("未提交文件：\n%s" % status)
        stat = git("diff", "--stat", "HEAD", cwd=repo_dir)
        if stat:
            parts.append("改动统计：\n%s" % stat)
        recent_commit = git("log", "-1", "--since=20 minutes ago", "--oneline", cwd=repo_dir)
        if status or stat or recent_commit:
            log = git("log", "-3", "--oneline", cwd=repo_dir)
            if log:
                parts.append("提交记录：\n%s" % log)

    if len(parts) <= 1:
        return branch, []
    return branch, parts


def collect_changes(since: str | None, cwd: str | None = None) -> tuple[str, str] | None:
    """返回 (标题, 内容)，无改动或非 git 仓库返回 None。"""
    repos = find_git_repos(cwd)
    if not repos:
        print("当前目录及子目录未发现 git 仓库，跳过上报")
        return None

    is_multi = len(repos) > 1
    all_sections: list[str] = []
    branch_labels: list[str] = []

    for name, rdir in repos:
        branch, parts = collect_repo_changes(name, rdir, since, is_multi)
        if parts:
            all_sections.append("\n".join(parts))
            branch_labels.append("%s:%s" % (name, branch) if is_multi else branch)

    if not all_sections:
        return None

    content = "\n\n".join(all_sections)[:MAX_CONTENT]
    title = "改动摘要 %s %s" % (datetime.now().strftime("%H:%M"), " ".join(branch_labels))
    return title, content


def main() -> int:
    parser = argparse.ArgumentParser(description="mymem 自动上报")
    parser.add_argument("--project",
                        default=os.environ.get("MYMEM_PROJECT", ""),
                        help="项目名（必填；不同项目记忆相互隔离）")
    parser.add_argument("--agent",
                        default=os.environ.get("MYMEM_AGENT", "unknown"))
    parser.add_argument("--cwd", default=None,
                        help="工作目录（缺省为当前进程工作目录）")
    parser.add_argument("--since", default=None,
                        help="从此 git 引用开始统计（如 HEAD~1）；缺省统计未提交改动")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将上报的内容，不发送")
    args = parser.parse_args()

    if not args.project:
        print("缺少 --project（或环境变量 MYMEM_PROJECT），项目名用于记忆隔离",
              file=sys.stderr)
        return 1

    collected = collect_changes(args.since, cwd=args.cwd)
    if collected is None:
        print("无改动，未上报")
        return 0
    title, content = collected

    payload = {
        "project": args.project,
        "agent": args.agent,
        "system": system_identity(),
        "memories": [{
            "category": "context", "title": title, "content": content,
            "tags": ["auto-report"],
        }],
    }
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    req = urllib.request.Request(
        URL + "/api/v1/ingest",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + TOKEN} if TOKEN else {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    print("已上报 [%s] created: %d, merged: %d" %
          (args.project, len(result.get("created", [])),
           len(result.get("merged", []))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
