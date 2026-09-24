"""邮件 MCP 服务：实时接收通知邮件（IMAP 轮询）+ 发送升级邮件（SMTP）

以 MCP 工具的形式向 LangGraph Agent 提供两个能力：
  - receive_emails(): 拉取未读通知邮件
  - send_escalation(): 将无法自动处理的运维事件升级发送到指定邮箱
"""
import email
import email.header
import imaplib
import logging
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parseaddr

from .config import load_config

log = logging.getLogger("ops-agent.email")

_seen_uids: set[str] = set()


def _decode_header(v) -> str:
    if not v:
        return ""
    parts = email.header.decode_header(v)
    out = []
    for data, charset in parts:
        if isinstance(data, bytes):
            out.append(data.decode(charset or "utf-8", "ignore"))
        else:
            out.append(str(data))
    return "".join(out)


def _get_body(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                body = payload.decode(charset, "ignore")
                break
            if ct == "text/html" and not body and "attachment" not in disp:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                body = payload.decode(charset, "ignore")
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        body = payload.decode(charset, "ignore")
    # 粗略去 HTML 标签
    import re
    body = re.sub(r"<[^>]+>", " ", body)
    return body.strip()[:6000]


def receive_emails(limit: int = 10) -> list[dict]:
    """IMAP 拉取匹配运维关键词的未读邮件。"""
    cfg = load_config()["email"]
    if not cfg.get("enabled") or not cfg.get("username") or not cfg.get("auth_code"):
        return []

    keywords = [k.lower() for k in cfg.get("subject_keywords", []) if k]
    mails: list[dict] = []
    conn = None
    try:
        ctx = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg.get("imap_port", 993)), ssl_context=ctx)
        conn.login(cfg["username"], cfg["auth_code"])
        conn.select("INBOX")
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return []
        ids = data[0].split()[-limit:]
        for mid in ids:
            status, mdata = conn.fetch(mid, "(RFC822)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(mdata[0][1])
            subject = _decode_header(msg.get("Subject", ""))
            sender = parseaddr(msg.get("From", ""))[1]
            date = msg.get("Date", "")
            body = _get_body(msg)
            low = subject.lower()
            if keywords and not any(k in low for k in keywords):
                continue  # 非运维相关，跳过（保持未读不影响）
            uid = f"{sender}|{subject}|{date}"
            if uid in _seen_uids:
                continue
            _seen_uids.add(uid)
            mails.append({
                "subject": subject,
                "from": sender,
                "date": date,
                "body": body,
            })
            try:
                conn.store(mid, "+FLAGS", "\\Seen")
            except Exception:
                pass
        return mails
    except Exception as e:
        log.warning("IMAP 拉取失败: %s", e)
        return []
    finally:
        if conn:
            try:
                conn.logout()
            except Exception:
                pass


def _render_escalation_html(event: dict) -> str:
    rows = []
    parsed = event.get("parsed") or {}
    meta = [
        ("事件编号", event.get("id", "-")),
        ("时间", event.get("created_at", "-")),
        ("来源", event.get("source", "-")),
        ("邮件主题", event.get("subject", "-")),
        ("严重级别", str(parsed.get("severity", "-"))),
        ("事件类型", str(parsed.get("type", "-"))),
        ("涉及服务", str(parsed.get("service", "-"))),
    ]
    for k, v in meta:
        rows.append(f"<tr><td style='padding:6px 12px;border:1px solid #eee;color:#666'>{k}</td>"
                    f"<td style='padding:6px 12px;border:1px solid #eee'>{v}</td></tr>")
    trace = event.get("trace") or []
    trace_html = "<br/>".join(
        f"[{t.get('node', '')}] {t.get('note', '')}" for t in trace
    ) or "（无）"
    return f"""
<div style="font-family:-apple-system,'Segoe UI','PingFang SC',sans-serif;max-width:640px">
  <h2 style="color:#c0392b">⚠️ 运维事件升级通知</h2>
  <p>以下运维事件无法由运维 Agent 自动处理，需要人工介入：</p>
  <table style="border-collapse:collapse;font-size:14px">{''.join(rows)}</table>
  <h3 style="font-size:15px">事件内容</h3>
  <pre style="background:#f7f7f7;padding:12px;border-radius:6px;white-space:pre-wrap;font-size:13px">{event.get('body', '')[:2000]}</pre>
  <h3 style="font-size:15px">Agent 处理轨迹</h3>
  <p style="font-size:13px;color:#555">{trace_html}</p>
  <p style="color:#999;font-size:12px">本邮件由运维 Agent 自动发送，请勿直接回复。</p>
</div>"""


def send_escalation(event: dict) -> dict:
    """将无法处理的事件升级发送到指定邮箱（默认 596826873@qq.com）。"""
    cfg = load_config()["email"]
    parsed = event.get("parsed") or {}
    to = cfg.get("escalate_to") or "596826873@qq.com"
    subject = f"[运维升级] {parsed.get('type', '未知事件')} - {event.get('subject', event.get('id', ''))}"

    if not cfg.get("enabled") or not cfg.get("username") or not cfg.get("auth_code"):
        return {"ok": False, "message": "邮箱服务未启用/未配置授权码，升级邮件未发送（事件已记录为待升级）"}

    m = MIMEMultipart("alternative")
    m["From"] = cfg["username"]
    m["To"] = to
    m["Subject"] = subject
    m.attach(MIMEText(_render_escalation_html(event), "html", "utf-8"))
    try:
        if int(cfg.get("smtp_port", 465)) == 465:
            server = smtplib.SMTP_SSL(cfg["smtp_host"], 465, timeout=20)
        else:
            server = smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587)), timeout=20)
            server.starttls()
        server.login(cfg["username"], cfg["auth_code"])
        server.sendmail(cfg["username"], [to], m.as_string())
        server.quit()
        log.info("升级邮件已发送至 %s (event=%s)", to, event.get("id"))
        return {"ok": True, "message": f"升级邮件已发送至 {to}", "to": to, "subject": subject}
    except Exception as e:
        log.warning("升级邮件发送失败: %s", e)
        return {"ok": False, "message": f"升级邮件发送失败: {e}"}


def email_status() -> dict:
    cfg = load_config()["email"]
    return {
        "enabled": bool(cfg.get("enabled")),
        "configured": bool(cfg.get("username") and cfg.get("auth_code")),
        "username": cfg.get("username", ""),
        "escalate_to": cfg.get("escalate_to", ""),
        "poll_interval": cfg.get("poll_interval", 60),
        "pending_seen": len(_seen_uids),
    }
