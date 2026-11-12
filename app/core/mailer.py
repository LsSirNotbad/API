# -*- coding: utf-8 -*-
"""通知工具：邮件推送与企业微信机器人推送

配置优先读取数据库 app_setting（后台可视化修改），未配置时回退到环境变量。
只做同步发送，调用方放进后台线程，避免阻塞请求。
"""
import json
import smtplib
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from . import config

MAIL_KEYS = ("mail_enabled", "mail_host", "mail_port", "mail_user", "mail_pwd",
             "mail_from", "mail_to", "mail_ssl", "mail_tls")


_LAST_MAIL_CFG = {}   # 数据库不可用时用最近一次成功读取的配置兜底（比如 MySQL 挂了要发告警邮件）


def _db_settings(keys):
    """从 app_setting 读取指定键，返回 (字典, 是否读取成功)"""
    try:
        from .db import SessionLocal
        from .models import SettingDB
        db = SessionLocal()
        try:
            rows = db.query(SettingDB).filter(SettingDB.key.in_(list(keys))).all()
            return {r.key: r.value for r in rows}, True
        finally:
            db.close()
    except Exception:
        return {}, False


def _flag(value, default=False):
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def mail_config():
    """合并环境变量与后台配置，返回邮件发送参数"""
    global _LAST_MAIL_CFG
    saved, ok = _db_settings(MAIL_KEYS)
    if not ok and _LAST_MAIL_CFG:
        # 数据库读不到（如 MySQL 宕机）时沿用上次配置，保证告警邮件还能发出去
        return dict(_LAST_MAIL_CFG)
    cfg = {
        "enabled": _flag(saved.get("mail_enabled"), config.MAIL_ENABLED),
        "host": (saved.get("mail_host") or config.MAIL_HOST or "").strip(),
        "port": int(saved.get("mail_port") or config.MAIL_PORT),
        "user": (saved.get("mail_user") or config.MAIL_USER or "").strip(),
        "pwd": saved.get("mail_pwd") or config.MAIL_PWD or "",
        "from": (saved.get("mail_from") or config.MAIL_FROM or "").strip(),
        "to": (saved.get("mail_to") or config.MAIL_TO or "").strip(),
        "ssl": _flag(saved.get("mail_ssl"), config.MAIL_SSL),
        "tls": _flag(saved.get("mail_tls"), config.MAIL_TLS),
    }
    if ok:
        _LAST_MAIL_CFG = dict(cfg)
    return cfg


def send_mail(subject, body, to=None):
    """发送纯文本邮件，返回 (是否成功, 错误信息)"""
    cfg = mail_config()
    if not cfg["enabled"]:
        return False, "邮件通知未启用"
    if not cfg["host"] or not cfg["user"] or not cfg["pwd"]:
        return False, "SMTP 服务器 / 账号 / 密码未配置"
    receivers = [x.strip() for x in (to or cfg["to"]).replace(";", ",").split(",") if x.strip()]
    if not receivers:
        return False, "收件人未配置"
    sender = cfg["from"] or cfg["user"]
    msg = MIMEText(body or "", "plain", "utf-8")
    msg["Subject"] = Header(subject or "通知", "utf-8")
    msg["From"] = formataddr(("API 平台", sender))
    msg["To"] = ", ".join(receivers)
    try:
        if cfg["ssl"]:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
            if cfg["tls"]:
                server.starttls()
        server.login(cfg["user"], cfg["pwd"])
        server.sendmail(sender, receivers, msg.as_string())
        server.quit()
        return True, ""
    except Exception as e:
        return False, str(e)[:300]


def wechat_webhook():
    """企业微信机器人 Webhook：告警优先，其次复用备份通知的配置"""
    saved, ok = _db_settings(("alert_webhook",))
    if not ok:
        saved = {}
    return (saved.get("alert_webhook") or config.ALERT_WEBHOOK
            or config.BACKUP_NOTIFY_WEBHOOK or "").strip()


def send_wechat(text, webhook=None, at_mobiles=None):
    """推送企业微信文本消息，返回 (是否成功, 错误信息)

    at_mobiles 传手机号（逗号分隔）或 "@all"，高优告警会在企业微信里 @ 对应成员。
    """
    url = (webhook or wechat_webhook()).strip()
    if not url:
        return False, "未配置企业微信机器人 Webhook"
    body = {"content": str(text or "")[:1800]}
    mobiles = [m.strip() for m in str(at_mobiles or "").replace("；", ",")
               .replace(";", ",").split(",") if m.strip()]
    if mobiles:
        body["mentioned_mobile_list"] = mobiles
    payload = json.dumps({"msgtype": "text", "text": body},
                         ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore") or "{}")
            if data.get("errcode", 0) == 0:
                return True, ""
            return False, str(data.get("errmsg") or data)[:200]
    except Exception as e:
        return False, str(e)[:200]
