# -*- coding: utf-8 -*-
"""mymem 配置：全部通过环境变量覆盖，默认值便于本地开发。"""
import os
from pathlib import Path

# 数据目录（SQLite 与每日导出文件都放在这里，Docker 中挂载为卷）
DATA_DIR = Path(os.environ.get("MYMEM_DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "mymem.sqlite3"
EXPORT_DIR = DATA_DIR / "exports"

# 访问令牌：设置后所有 /api 与 /mcp 请求需带 Authorization: Bearer <token>
API_TOKEN = os.environ.get("MYMEM_TOKEN", "")

# 每日备份邮件（SMTP）
SMTP_HOST = os.environ.get("MYMEM_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("MYMEM_SMTP_PORT", "465"))
SMTP_USER = os.environ.get("MYMEM_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("MYMEM_SMTP_PASSWORD", "")
SMTP_SSL = os.environ.get("MYMEM_SMTP_SSL", "true").lower() != "false"
BACKUP_TO = os.environ.get("MYMEM_BACKUP_TO", "")  # 收件邮箱，逗号分隔
BACKUP_FROM = os.environ.get("MYMEM_BACKUP_FROM", SMTP_USER)

# 每日备份触发时间（HH:MM，容器时区）
BACKUP_TIME = os.environ.get("MYMEM_BACKUP_TIME", "02:00")
TZ = os.environ.get("TZ", "Asia/Shanghai")


def backup_email_enabled() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and BACKUP_TO)
