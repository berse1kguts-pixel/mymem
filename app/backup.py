# -*- coding: utf-8 -*-
"""每日备份：导出 JSON / Markdown 摘要 / SQLite 快照，并邮件推送。

防止服务器到期或损坏导致记忆数据丢失：
  data/exports/YYYY-MM-DD/
    memories.json   全量结构化数据（可重新导入）
    digest.md       人类可读的每日摘要
    mymem.sqlite3   数据库快照
"""
import json
import smtplib
import sqlite3
import threading
import time
import zipfile
from datetime import date, datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from . import config, db


def _export_files(target_date: str) -> dict:
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM memories ORDER BY id").fetchall()
    items = [db._row_to_dict(r) for r in rows]

    out_dir = config.EXPORT_DIR / target_date
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "memories.json"
    json_path.write_text(json.dumps({
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(items), "memories": items,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = out_dir / "digest.md"
    md_path.write_text(_render_markdown(items, target_date), encoding="utf-8")

    snap_path = out_dir / "mymem.sqlite3"
    dest = sqlite3.connect(snap_path)
    conn.backup(dest)
    dest.close()

    return {"dir": str(out_dir), "json": str(json_path),
            "markdown": str(md_path), "snapshot": str(snap_path),
            "count": len(items)}


def build_export_zip() -> tuple[Path, str]:
    """打包一份可直接移植的全量导出 zip。

    内含：
      memories.json   全量结构化记忆
      digest.md       人类可读摘要
      mymem.sqlite3   数据库快照（新实例可直接替换 data/mymem.sqlite3）
      manifest.json   导出时间、条数、版本标识
    """
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM memories ORDER BY id").fetchall()
    items = [db._row_to_dict(r) for r in rows]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = config.EXPORT_DIR / ("mymem-export-%s.zip" % stamp)

    snapshot = config.EXPORT_DIR / (".mymem-%s.sqlite3" % stamp)
    dest = sqlite3.connect(snapshot)
    try:
        conn.backup(dest)
    finally:
        dest.close()

    manifest = {
        "service": "schierke",
        "schema_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(items),
    }
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("memories.json", json.dumps({
                "exported_at": manifest["exported_at"],
                "count": len(items),
                "memories": items,
            }, ensure_ascii=False, indent=2))
            zf.writestr("digest.md",
                        _render_markdown(items, date.today().isoformat()))
            zf.write(snapshot, "mymem.sqlite3")
            zf.writestr("manifest.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2))
    finally:
        snapshot.unlink(missing_ok=True)
    return zip_path, zip_path.name


def _render_markdown(items: list[dict], target_date: str) -> str:
    lines = ["# mymem 每日备份 %s" % target_date, "",
             "共 %d 条记忆" % len(items), ""]
    by_project: dict[str, list] = {}
    for it in items:
        by_project.setdefault(it["project"], []).append(it)
    for project, entries in sorted(by_project.items()):
        lines.append("## 项目：%s（%d 条）" % (project, len(entries)))
        for it in entries:
            flag = " [已归档]" if it["archived"] else ""
            lines.append("### [%s] %s%s" % (it["category"], it["title"], flag))
            lines.append(it["content"])
            lines.append("")
    return "\n".join(lines)


def _send_email(files: dict, target_date: str) -> bool:
    if not config.backup_email_enabled():
        return False
    msg = MIMEMultipart()
    msg["Subject"] = "mymem 每日备份 %s（%d 条记忆）" % (target_date, files["count"])
    msg["From"] = config.BACKUP_FROM
    msg["To"] = config.BACKUP_TO
    msg.attach(MIMEText("备份文件见附件：memories.json（全量数据）、digest.md（摘要）、"
                        "mymem.sqlite3（数据库快照）。", "plain", "utf-8"))
    for path in (files["json"], files["markdown"], files["snapshot"]):
        with open(path, "rb") as f:
            part = MIMEApplication(f.read())
        part.add_header("Content-Disposition", "attachment",
                        filename=Path(path).name)
        msg.attach(part)

    if config.SMTP_SSL:
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
        server.starttls()
    try:
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.sendmail(config.BACKUP_FROM,
                        [a.strip() for a in config.BACKUP_TO.split(",")],
                        msg.as_string())
    finally:
        server.quit()
    return True


def run_backup(target_date: str | None = None) -> dict:
    target_date = target_date or date.today().isoformat()
    files = _export_files(target_date)
    emailed = False
    email_error = None
    try:
        emailed = _send_email(files, target_date)
    except Exception as exc:  # 邮件失败不应阻塞备份本身
        email_error = str(exc)
    return {"date": target_date, "exported": files,
            "emailed": emailed, "email_error": email_error}


def _scheduler_loop():
    """每天在 BACKUP_TIME 触发一次备份（HH:MM 本地时间）。"""
    last_run = ""
    while True:
        now = datetime.now()
        if (now.strftime("%H:%M") >= config.BACKUP_TIME
                and last_run != now.date().isoformat()):
            last_run = now.date().isoformat()
            try:
                run_backup()
            except Exception:
                pass
        time.sleep(30)


def start_scheduler():
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    return t
