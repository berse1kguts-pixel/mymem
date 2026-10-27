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


def git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True,
                             timeout=30, encoding="utf-8", errors="replace")
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def collect_changes(since: str | None) -> tuple[str, str] | None:
    """返回 (标题, 内容)，无改动或非 git 仓库返回 None。"""
    if git("rev-parse", "--is-inside-work-tree") != "true":
        print("当前目录不是 git 仓库，跳过上报")
        return None
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    parts: list[str] = ["分支：%s" % branch]

    if since:
        log = git("log", "--oneline", "%s..HEAD" % since)
        stat = git("diff", "--stat", since)
    else:
        log = git("log", "-3", "--oneline")
        stat = git("diff", "--stat", "HEAD")
        status = git("status", "-s")
        if status:
            parts.append("未提交文件：\n%s" % status)

    if log:
        parts.append("提交记录：\n%s" % log)
    if stat:
        parts.append("改动统计：\n%s" % stat)

    if len(parts) <= 1:
        return None
    content = "\n".join(parts)[:MAX_CONTENT]
    title = "改动摘要 %s %s" % (datetime.now().strftime("%H:%M"), branch)
    return title, content


def main() -> int:
    parser = argparse.ArgumentParser(description="mymem 自动上报")
    parser.add_argument("--project",
                        default=os.environ.get("MYMEM_PROJECT", ""),
                        help="项目名（必填；不同项目记忆相互隔离）")
    parser.add_argument("--agent",
                        default=os.environ.get("MYMEM_AGENT", "unknown"))
    parser.add_argument("--since", default=None,
                        help="从此 git 引用开始统计（如 HEAD~1）；缺省统计未提交改动")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将上报的内容，不发送")
    args = parser.parse_args()

    if not args.project:
        print("缺少 --project（或环境变量 MYMEM_PROJECT），项目名用于记忆隔离",
              file=sys.stderr)
        return 1

    collected = collect_changes(args.since)
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
