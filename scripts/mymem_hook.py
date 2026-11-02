# -*- coding: utf-8 -*-
"""Schierke 记忆上报客户端：code agent 在完成一段修改后调用，把本次会话的记忆
整理并上报到 Schierke。

用法：
    python mymem_hook.py digest.json
    echo '{"project":"demo","memories":[...]}' | python mymem_hook.py -

digest.json 格式：
{
  "project": "my-repo",
  "agent": "trae",
  "session_summary": "本次会话完成了……",
  "memories": [
    {"category": "decision", "title": "选型 FastAPI",
     "content": "因为……", "tags": ["backend"]},
    {"category": "lesson", "title": "SQLite trigram 分词",
     "content": "……", "tags": ["sqlite"], "supersedes_id": 12}
  ]
}

环境变量：
    MYMEM_URL    服务地址，默认 http://1.92.76.106
    MYMEM_TOKEN  访问令牌（服务端设置了 MYMEM_TOKEN 时必填）
"""
import json
import os
import platform
import sys
import urllib.request

URL = os.environ.get("MYMEM_URL", "http://1.92.76.106").rstrip("/")
TOKEN = os.environ.get("MYMEM_TOKEN", "")


def system_identity() -> str:
    """系统标识：主机名|操作系统，随每条记录保存。"""
    return "%s|%s" % (platform.node(), platform.platform())


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    arg = sys.argv[1]
    raw = sys.stdin.read() if arg == "-" else open(arg, encoding="utf-8-sig").read()
    payload = json.loads(raw)
    payload.setdefault("agent", os.environ.get("MYMEM_AGENT", "unknown"))
    payload.setdefault("system", system_identity())

    req = urllib.request.Request(
        URL + "/api/v1/ingest",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + TOKEN} if TOKEN else {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    print("created: %d, merged: %d" % (len(result.get("created", [])),
                                       len(result.get("merged", []))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
