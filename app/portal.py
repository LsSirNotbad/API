# -*- coding: utf-8 -*-
"""C 端用户门户：审批注册、独立会话、Token 与额度控制台。"""
import base64
import binascii
import hashlib
import hmac
import ipaddress
import io
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime, timedelta
from http.cookies import SimpleCookie

from fastapi import Body, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, TimestampSigner
import requests
from faker import Faker
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders

from .core.cache import CACHE
from .core.config import (
    API_TOKEN,
    DOCS_ENABLED,
    PORTAL_AUTO_APPROVE,
    PORTAL_AUTO_APPROVE_INTERVAL,
    PORTAL_API_LOG_SAMPLE,
    PORTAL_COOKIE_SECURE,
    PORTAL_DEFAULT_DAILY_QUOTA,
    PORTAL_CAPTCHA_TTL,
    PORTAL_EMAIL_CODE_TTL,
    PORTAL_EMAIL_DAILY_LIMIT,
    PORTAL_EMAIL_RESEND_SECONDS,
    PORTAL_QUOTA_WARN_INTERVAL,
    PORTAL_QUOTA_WARN_THRESHOLD,
    PORTAL_REGISTER_IP_LIMIT,
    PORTAL_REGISTER_WINDOW,
    PORTAL_SECRET,
    TOKEN_EXPIRY_WARN_DAYS,
    TOKEN_EXPIRY_WARN_INTERVAL,
)
from .core.db import SessionLocal, get_db
from .core.mailer import send_mail
from .core.models import (
    ApiLogDB, ApiTokenDB, AuditLogDB, CUserDB, IpRuleDB, LoginLogDB, PortalEmailCodeDB,
    PortalQuotaLogDB, PortalRewardDB, SettingDB,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGIN_HTML = os.path.join(BASE_DIR, "templates", "portal_login.html")
DASHBOARD_HTML = os.path.join(BASE_DIR, "templates", "portal_dashboard.html")
DOCS_HTML = os.path.join(BASE_DIR, "templates", "portal_docs.html")
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PBKDF2_ITERATIONS = 600_000
LOGGER = logging.getLogger(__name__)
REWARD_TASKS = {
    "daily_checkin": {"title": "每日签到", "description": "每天签到一次，领取 30 次累计额度", "reward": 30},
    "daily_login": {"title": "今日登录", "description": "每天首次进入个人控制台，领取 20 次累计额度", "reward": 20},
    "read_guide": {"title": "阅读新手指南", "description": "首次打开新手指南，可领取一次 50 次额度", "reward": 50},
    "sandbox_test": {"title": "体验沙盒测试", "description": "首次完成本地模拟，可领取一次 20 次额度", "reward": 20},
    "code_generate": {"title": "生成接入代码", "description": "首次生成接口代码，可领取一次 20 次额度", "reward": 20},
    "api_test": {"title": "完成接口测试", "description": "首次成功发送真实测试，可领取一次 50 次额度", "reward": 50},
}
ONCE_TASKS = {"read_guide", "sandbox_test", "code_generate", "api_test"}
_AUTO_APPROVAL_STARTED = False


def _hash_token(raw):
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def _token_hint(raw):
    return f"{raw[:4]}****{raw[-4:]}" if raw and len(raw) > 8 else "****"


def _make_password_hash(password):
    """PBKDF2-HMAC-SHA256 + 128 位随机盐，禁止快速单轮哈希"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _check_password(password, stored):
    try:
        algorithm, iterations, salt, expected = (stored or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (TypeError, ValueError):
        return False


PORTAL_REMEMBER_COOKIE = "portal_remember"
PORTAL_REMEMBER_COOKIE_PATH = "/api/portal/login"
PORTAL_REMEMBER_MAX_AGE = 30 * 24 * 3600


def _remember_signer():
    return TimestampSigner(str(PORTAL_SECRET) + ".remember-login")


def _password_stamp(user):
    """密码修改后使旧记住登录凭据失效，凭据中不保存密码或密码哈希原文。"""
    return hmac.new(
        str(PORTAL_SECRET).encode("utf-8"), (user.password_hash or "").encode("utf-8"),
        hashlib.sha256).hexdigest()


def _issue_remember_token(user):
    payload = base64.b64encode(json.dumps(
        {"user_id": user.id, "password_stamp": _password_stamp(user)},
        separators=(",", ":")).encode("utf-8"))
    return _remember_signer().sign(payload).decode("utf-8")


def _user_from_remember_cookie(request, db):
    raw = request.cookies.get(PORTAL_REMEMBER_COOKIE, "")
    if not raw:
        return None
    try:
        payload = _remember_signer().unsign(raw.encode("ascii"), max_age=PORTAL_REMEMBER_MAX_AGE)
        data = json.loads(base64.b64decode(payload))
        user_id = int(data.get("user_id") or 0)
        stamp = str(data.get("password_stamp") or "")
    except (BadSignature, ValueError, TypeError, UnicodeError, binascii.Error,
            json.JSONDecodeError):
        return None
    user = db.query(CUserDB).filter(CUserDB.id == user_id).first() if user_id > 0 else None
    if user is None or not hmac.compare_digest(stamp, _password_stamp(user)):
        return None
    return user


def _hash_email_code(email, code, purpose="register"):
    return hmac.new(
        str(PORTAL_SECRET).encode("utf-8"),
        f"{purpose}:{email}:{code}".encode("utf-8"), hashlib.sha256,
    ).hexdigest()


def _captcha_font(size=28):
    """跨平台验证码字体：优先项目内 fonts/ 目录，其次系统字体，最后内置可缩放字体"""
    from PIL import ImageFont
    # 项目根目录下的 fonts/ 目录（随代码分发，跨平台一致）
    _BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _FONT_DIR = os.path.join(_BASE, "fonts")
    # 优先：用户在 config 里显式配置的 FONT_DIR → 项目内置 fonts/ → 系统常见路径
    try:
        from .core import config
        custom_dir = str(getattr(config, "FONT_DIR", "") or "").strip()
    except Exception:
        custom_dir = ""
    candidates = []
    if custom_dir:
        candidates += [os.path.join(custom_dir, "DejaVuSans-Bold.ttf"),
                       os.path.join(custom_dir, "DejaVuSans.ttf"),
                       os.path.join(custom_dir, "arial.ttf")]
    candidates += [os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf"),
                   os.path.join(_FONT_DIR, "DejaVuSans.ttf"),
                   os.path.join(_FONT_DIR, "arial.ttf")]
    candidates += [
        "arial.ttf", "Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for path in candidates:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _captcha_png(text):
    """生成带干扰线与噪点的算术验证码图片，返回 PNG 字节"""
    from PIL import ImageDraw
    width, height = 160, 50
    image = Image.new("RGB", (width, height), (246, 248, 252))
    draw = ImageDraw.Draw(image)
    for _ in range(6):
        draw.line([
            (secrets.randbelow(width), secrets.randbelow(height)),
            (secrets.randbelow(width), secrets.randbelow(height)),
        ], fill=(198, 206, 222), width=1)
    for _ in range(180):
        draw.point((secrets.randbelow(width), secrets.randbelow(height)), fill=(176, 186, 204))
    font = _captcha_font(28)
    draw.text((14, 9), text, fill=(34, 48, 78), font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _verify_captcha(captcha_id, answer):
    """校验图形验证码，无论成功失败都立即作废，防止暴力枚举"""
    if not captcha_id or not re.fullmatch(r"\d{1,3}", str(answer or "").strip()):
        return False
    key = f"portal-captcha:{captcha_id}"
    expected = CACHE.get(key)
    if expected is None or expected == "__used__":
        return False
    CACHE.set(key, "__used__", ttl=max(60, PORTAL_CAPTCHA_TTL))
    return hmac.compare_digest(str(expected), str(answer).strip())


def _total_quota(user):
    return int(user.daily_quota or 0) + int(user.bonus_quota or 0)


def _remaining_quota(user):
    return max(0, _total_quota(user) - int(user.today_used or 0))


def record_quota_change(db, user_id, amount, kind, title, detail="", path="",
                        operator="system", balance_after=None):
    """在调用方事务内追加额度流水，不在此处提交，确保额度与明细同时生效"""
    if balance_after is None:
        user = db.query(CUserDB).filter(CUserDB.id == user_id).first()
        balance_after = _remaining_quota(user) if user is not None else 0
    row = PortalQuotaLogDB(
        user_id=user_id, change=int(amount), kind=str(kind)[:30], title=str(title)[:100],
        detail=str(detail or "")[:500], path=str(path or "")[:200],
        balance_after=max(0, int(balance_after)), operator=str(operator or "system")[:100])
    db.add(row)
    return row


def ensure_initial_quota_log(db, user):
    """为流水功能上线前的账号补录当前基础额度，新账号在注册时直接记录"""
    exists = db.query(PortalQuotaLogDB.id).filter(
        PortalQuotaLogDB.user_id == user.id, PortalQuotaLogDB.kind == "initial").first()
    if exists:
        return
    record_quota_change(
        db, user.id, int(user.daily_quota or 0), "initial", "初始额度",
        "系统初始化基础额度（历史账号为补录）", balance_after=_remaining_quota(user))


def _complete_task(db, user_id, task_key):
    """每日任务按当天记录，一次性新手任务沿用任意日期的历史进度"""
    definition = REWARD_TASKS.get(task_key)
    if not definition:
        return
    today = datetime.now().date()
    query = db.query(PortalRewardDB).filter(
        PortalRewardDB.user_id == user_id,
        PortalRewardDB.task_key == task_key,
    )
    if task_key not in ONCE_TASKS:
        query = query.filter(PortalRewardDB.task_date == today)
    row = query.first()
    if row is None:
        try:
            db.add(PortalRewardDB(
                user_id=user_id, task_key=task_key, task_date=today,
                reward=definition["reward"]))
            db.commit()
        except Exception:
            db.rollback()


def _reward_list(db, user_id):
    today = datetime.now().date()
    rows = db.query(PortalRewardDB).filter(
        PortalRewardDB.user_id == user_id,
        or_(PortalRewardDB.task_date == today, PortalRewardDB.task_key.in_(ONCE_TASKS)),
    ).all()
    progress = {}
    for row in rows:
        current = progress.get(row.task_key)
        if current is None or (not current.claimed_at and row.claimed_at):
            progress[row.task_key] = row
    return [{
        "key": key, "title": item["title"], "description": item["description"],
        "reward": item["reward"], "completed": key in progress,
        "claimed": bool(progress.get(key) and progress[key].claimed_at),
        "once": key in ONCE_TASKS,
    } for key, item in REWARD_TASKS.items()]


class PortalSessionMiddleware:
    """独立的 portal_session Cookie；使用独立 scope，避免覆盖 admin_session"""

    def __init__(self, app, secret_key, https_only=False, max_age=8 * 3600):
        self.app = app
        self.signer = TimestampSigner(str(secret_key))
        self.https_only = https_only
        self.max_age = max_age

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        session = {}
        had_cookie = False
        cookie_header = ""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"cookie":
                cookie_header = value.decode("latin-1")
                break
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except Exception:
            pass
        morsel = cookies.get("portal_session")
        if morsel is not None:
            had_cookie = True
            try:
                raw = self.signer.unsign(morsel.value.encode("ascii"), max_age=self.max_age)
                data = json.loads(base64.b64decode(raw))
                if isinstance(data, dict):
                    session = data
            except (BadSignature, ValueError, TypeError, json.JSONDecodeError):
                session = {}
        scope["portal_session"] = session

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if session:
                    raw = base64.b64encode(json.dumps(session, separators=(",", ":")).encode("utf-8"))
                    signed = self.signer.sign(raw).decode("utf-8")
                    cookie = SimpleCookie()
                    cookie["portal_session"] = signed
                    cookie["portal_session"]["path"] = "/"
                    cookie["portal_session"]["max-age"] = str(self.max_age)
                    cookie["portal_session"]["httponly"] = True
                    cookie["portal_session"]["samesite"] = "lax"
                    if self.https_only:
                        cookie["portal_session"]["secure"] = True
                    headers.append("set-cookie", cookie.output(header="").strip())
                elif had_cookie:
                    cookie = SimpleCookie()
                    cookie["portal_session"] = ""
                    cookie["portal_session"]["path"] = "/"
                    cookie["portal_session"]["max-age"] = "0"
                    cookie["portal_session"]["expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
                    cookie["portal_session"]["httponly"] = True
                    cookie["portal_session"]["samesite"] = "lax"
                    headers.append("set-cookie", cookie.output(header="").strip())
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _session(request):
    return request.scope.setdefault("portal_session", {})


def _client_ip(request):
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _read_image_upload(upload, limit=20 * 1024 * 1024):
    raw = upload.file.read(limit + 1)
    if not raw:
        raise HTTPException(status_code=400, detail=f"{upload.filename or '图片'} 是空文件")
    if len(raw) > limit:
        raise HTTPException(status_code=400, detail=f"{upload.filename or '图片'} 超过 {limit // 1024 // 1024}MB 限制")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(status_code=400, detail=f"{upload.filename or '文件'} 不是有效图片")
    if image.width * image.height > 40_000_000:
        raise HTTPException(status_code=400, detail=f"{upload.filename or '图片'} 总像素不能超过 4000 万")
    return raw, image


def _attachment_response(content, media_type, filename):
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "download"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(filename)}"
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": disposition})


def _record_operation(request, email, action, target="", detail="", result="success"):
    """记录前台用户主动操作及结果，写审计失败不影响业务请求。"""
    db = SessionLocal()
    try:
        db.add(AuditLogDB(
            operator=str(email or "未知用户")[:100], action=str(action)[:64],
            target=str(target or email)[:255], detail=str(detail or "")[:60000],
            ip=_client_ip(request)[:100],
            result="success" if result == "success" else "failed"))
        db.commit()
    except Exception:
        db.rollback()
        LOGGER.exception("C 端操作审计写入失败")
    finally:
        db.close()


def _record_login(db, request, email, state, message):
    """登录日志与人工操作审计分别保存，失败不影响用户登录。"""
    try:
        db.add(LoginLogDB(
            username=email[:190], source="portal", ip=_client_ip(request)[:100],
            user_agent=(request.headers.get("user-agent") or "")[:255],
            state=state, message=message[:255]))
        db.commit()
    except Exception:
        db.rollback()
        LOGGER.exception("C 端登录日志写入失败")
    _record_operation(request, email, "portal_login", email, message,
                      "success" if state == "success" else "failed")


def _register_ip_allowed(ip, security_refresh):
    """限制单 IP 注册频率，异常时立即写入全局黑名单"""
    if PORTAL_REGISTER_IP_LIMIT <= 0:
        return True
    hits = CACHE.incr(f"portal-register:{ip}", ttl=max(60, PORTAL_REGISTER_WINDOW))
    if hits <= 0 or hits <= PORTAL_REGISTER_IP_LIMIT:
        return True
    db = SessionLocal()
    try:
        row = db.query(IpRuleDB).filter(
            IpRuleDB.ip == ip, IpRuleDB.rule_type == "black").first()
        if row is None:
            db.add(IpRuleDB(ip=ip[:100], rule_type="black", enabled=True,
                            remark="C端批量注册自动封禁"))
        else:
            row.enabled = True
        db.commit()
    finally:
        db.close()
    if security_refresh:
        security_refresh(force=True)
    return False


def _require_user(request, db):
    """所有 C 端数据查询均以 Session 中的用户 ID 为唯一边界"""
    user_id = _session(request).get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="请先登录")
    user = db.query(CUserDB).filter(CUserDB.id == user_id).first()
    if user is None or user.status != "active":
        _session(request).clear()
        raise HTTPException(status_code=403, detail="账号待审批、不可用或已被封禁")
    return user


def _issue_token(db, user):
    """首次登录发放 Token；已有 Token 时绝不恢复或返回明文"""
    row = db.query(ApiTokenDB).filter(ApiTokenDB.user_id == user.id).order_by(ApiTokenDB.id).first()
    if row is not None:
        return None, row
    raw = secrets.token_urlsafe(32)
    row = ApiTokenDB(
        name=f"{user.email} 的专属 Token", token=_hash_token(raw), token_hint=_token_hint(raw),
        permissions="*", enabled=True, remark="C端用户专属", user_id=user.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return raw, row


def process_pending_users(security_refresh=None):
    """自动审批已通过邮箱验证的申请，并通过注册邮箱一次性发放 Token"""
    db = SessionLocal()
    approved = 0
    try:
        candidates = db.query(CUserDB).filter(CUserDB.status == "pending").order_by(
            CUserDB.id).limit(100).all()
        for user in candidates:
            try:
                claimed = db.query(CUserDB).filter(
                    CUserDB.id == user.id, CUserDB.status == "pending",
                ).update({CUserDB.status: "active", CUserDB.approved_at: datetime.now()},
                         synchronize_session=False)
                db.commit()
                if not claimed:
                    continue
                db.refresh(user)
                raw, token = _issue_token(db, user)
                approved += 1
                if raw:
                    ok, error = send_mail(
                        "API 平台账号已通过审核",
                        "你的 API 平台账号已通过审核。\n\n"
                        f"登录邮箱：{user.email}\n专属 Token：{raw}\n\n"
                        "请妥善保管此 Token，平台数据库只保存哈希，无法再次查询明文。"
                        "如果未保存，可登录用户中心重置 Token。\n"
                        "用户中心：/portal",
                        to=user.email,
                    )
                    if not ok:
                        LOGGER.warning("自动审批 Token 邮件发送失败：%s %s", user.email, error)
            except Exception as exc:
                db.rollback()
                LOGGER.exception("自动审批用户失败：%s", exc)
        if approved and security_refresh:
            security_refresh(force=True)
        return approved
    finally:
        db.close()


REVIEW_MODE_KEY = "portal_review_mode"     # 审核模式：auto 自动审批 / manual 人工审批


def auto_approve_mode():
    """返回当前审核模式：优先读后台设置，未设置时回退环境变量 PORTAL_AUTO_APPROVE"""
    try:
        db = SessionLocal()
        try:
            row = db.query(SettingDB).filter(SettingDB.key == REVIEW_MODE_KEY).first()
        finally:
            db.close()
        value = str(row.value or "").strip().lower() if row is not None else ""
        if value in ("auto", "manual"):
            return value
    except Exception:
        pass
    return "auto" if PORTAL_AUTO_APPROVE else "manual"


def set_auto_approve_mode(mode):
    """保存审核模式；非法值抛 ValueError，数据库异常向上抛出由调用方处理"""
    mode = str(mode or "").strip().lower()
    if mode not in ("auto", "manual"):
        raise ValueError("审核模式只能是 auto 或 manual")
    db = SessionLocal()
    try:
        row = db.query(SettingDB).filter(SettingDB.key == REVIEW_MODE_KEY).first()
        if row is None:
            db.add(SettingDB(key=REVIEW_MODE_KEY, value=mode))
        else:
            row.value = mode
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return mode


def start_auto_approval(security_refresh=None):
    """启动审批定时线程：每轮读取「审核模式」，人工模式下只跳过审批，不影响其他巡检"""
    global _AUTO_APPROVAL_STARTED
    if _AUTO_APPROVAL_STARTED:
        return None
    _AUTO_APPROVAL_STARTED = True

    def loop():
        time.sleep(2)
        while True:
            try:
                if auto_approve_mode() == "auto":
                    process_pending_users(security_refresh)
            except Exception as exc:
                LOGGER.exception("C 端自动审批任务异常：%s", exc)
            time.sleep(max(5, int(PORTAL_AUTO_APPROVE_INTERVAL)))

    thread = threading.Thread(target=loop, name="portal-auto-approve", daemon=True)
    thread.start()
    return thread


# ---------------- 额度预警 ----------------
QUOTA_WARN_KEY = "portal_quota_warn_threshold"      # 剩余额度预警阈值，0 表示关闭
QUOTA_WARN_STATE_KEY = "portal_quota_warn_state"    # 已提醒记录：{用户ID: 日期}，用于每用户每日去重
_QUOTA_WARN_STARTED = False


def quota_warn_threshold():
    """读取预警阈值：优先后台设置，未配置时回退环境变量"""
    try:
        db = SessionLocal()
        try:
            row = db.query(SettingDB).filter(SettingDB.key == QUOTA_WARN_KEY).first()
        finally:
            db.close()
        if row is not None and str(row.value or "").strip():
            return max(0, int(str(row.value).strip()))
    except Exception:
        pass
    return max(0, int(PORTAL_QUOTA_WARN_THRESHOLD))


def set_quota_warn_threshold(value):
    """保存预警阈值；非法值抛 ValueError，数据库异常向上抛出由调用方处理"""
    try:
        threshold = int(value)
    except (TypeError, ValueError):
        raise ValueError("预警阈值必须是整数")
    if threshold < 0 or threshold > 10_000_000:
        raise ValueError("预警阈值需在 0 ~ 10000000 之间")
    db = SessionLocal()
    try:
        row = db.query(SettingDB).filter(SettingDB.key == QUOTA_WARN_KEY).first()
        if row is None:
            db.add(SettingDB(key=QUOTA_WARN_KEY, value=str(threshold)))
        else:
            row.value = str(threshold)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return threshold


def _load_warn_state():
    """读取已提醒记录，损坏时按空记录处理，不影响后续发送"""
    try:
        db = SessionLocal()
        try:
            row = db.query(SettingDB).filter(SettingDB.key == QUOTA_WARN_STATE_KEY).first()
        finally:
            db.close()
        data = json.loads(row.value or "{}") if row is not None else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_warn_state(state):
    """保存已提醒记录（只保留当天条目，避免无限增长）"""
    try:
        value = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        db = SessionLocal()
        try:
            row = db.query(SettingDB).filter(SettingDB.key == QUOTA_WARN_STATE_KEY).first()
            if row is None:
                db.add(SettingDB(key=QUOTA_WARN_STATE_KEY, value=value))
            else:
                row.value = value
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        LOGGER.warning("额度预警记录保存失败：%s", exc)


def _quota_warn_once():
    """扫描正常用户，剩余额度低于阈值时发提醒邮件；每用户每天最多一次"""
    threshold = quota_warn_threshold()
    if threshold <= 0:
        return 0
    db = SessionLocal()
    try:
        users = db.query(CUserDB).filter(CUserDB.status == "active").all()
        targets = [(user.id, user.email, _remaining_quota(user),
                    _total_quota(user), int(user.today_used or 0)) for user in users]
    finally:
        db.close()
    today = datetime.now().strftime("%Y-%m-%d")
    state = {k: v for k, v in _load_warn_state().items() if v == today}
    sent = 0
    for user_id, email, remaining, total, used in targets:
        if remaining > threshold or state.get(str(user_id)) == today:
            continue
        ok, error = send_mail(
            "API 平台额度不足提醒",
            f"你的 API 平台账号可用额度仅剩 {remaining} 次（低于预警阈值 {threshold} 次）。\n\n"
            f"累计额度：{total} 次\n已用：{used} 次\n剩余：{remaining} 次\n\n"
            "额度用尽后接口调用会返回 429。可在用户中心完成任务或联系管理员增加额度。\n"
            "用户中心：/portal",
            to=email,
        )
        if ok:
            state[str(user_id)] = today
            sent += 1
        else:
            LOGGER.warning("额度预警邮件发送失败：%s %s", email, error)
    if sent:
        _save_warn_state(state)
    return sent


def start_quota_warning():
    """启动额度预警线程：按间隔扫描正常用户，剩余额度不足时发邮件提醒"""
    global _QUOTA_WARN_STARTED
    if _QUOTA_WARN_STARTED:
        return None
    _QUOTA_WARN_STARTED = True

    def loop():
        time.sleep(10)
        while True:
            try:
                sent = _quota_warn_once()
                if sent:
                    LOGGER.info("额度预警本轮发送 %s 封邮件", sent)
            except Exception as exc:
                LOGGER.exception("额度预警任务异常：%s", exc)
            time.sleep(max(60, int(PORTAL_QUOTA_WARN_INTERVAL)))

    thread = threading.Thread(target=loop, name="portal-quota-warning", daemon=True)
    thread.start()
    return thread


_TOKEN_WARN_STARTED = False


def _token_expiry_warn_once():
    """扫描 7 天内到期的 C 端 Token，按用户每天合并发一封提醒邮件"""
    days = int(TOKEN_EXPIRY_WARN_DAYS)
    if days <= 0:
        return 0
    now = datetime.now()
    deadline = now + timedelta(days=days)
    db = SessionLocal()
    try:
        rows = (db.query(ApiTokenDB, CUserDB)
                .join(CUserDB, ApiTokenDB.user_id == CUserDB.id)
                .filter(ApiTokenDB.enabled.is_(True),
                        ApiTokenDB.user_id.isnot(None),
                        ApiTokenDB.expires_at.isnot(None),
                        ApiTokenDB.expires_at > now,
                        ApiTokenDB.expires_at <= deadline,
                        CUserDB.status == "active").all())
        targets = {}
        for token, user in rows:
            if not user.email:
                continue
            targets.setdefault(user.id, {"email": user.email, "tokens": []})["tokens"].append(
                (token.name or "未命名", token.token_hint or "-", token.expires_at))
    finally:
        db.close()
    today = now.strftime("%Y-%m-%d")
    sent = 0
    for user_id, info in targets.items():
        key = f"token-expiry-warn:{user_id}:{today}"
        if CACHE.get(key):
            continue
        lines = [f"以下 Token 将在 {days} 天内到期："]
        for name, hint, expires in info["tokens"]:
            lines.append(f"- {name}（{hint}）到期时间：{expires.strftime('%Y-%m-%d %H:%M')}")
        lines += ["", "为避免调用突然返回 401，请提前续期或联系管理员。", "用户中心：/portal"]
        ok, error = send_mail("API 平台 Token 到期提醒", "\n".join(lines), to=info["email"])
        if ok:
            CACHE.set(key, 1, ttl=86400)
            sent += 1
        else:
            LOGGER.warning("Token 到期提醒发送失败：%s %s", info["email"], error)
    return sent


def start_token_expiry_warning():
    """启动 Token 到期提醒线程：默认每天扫描一次，提前 7 天发邮件"""
    global _TOKEN_WARN_STARTED
    if _TOKEN_WARN_STARTED:
        return None
    _TOKEN_WARN_STARTED = True

    def loop():
        time.sleep(30)
        while True:
            try:
                if _token_expiry_warn_once():
                    LOGGER.info("Token 到期提醒已发送")
            except Exception as exc:
                LOGGER.exception("Token 到期提醒任务异常：%s", exc)
            time.sleep(max(600, int(TOKEN_EXPIRY_WARN_INTERVAL)))

    thread = threading.Thread(target=loop, name="portal-token-expiry", daemon=True)
    thread.start()
    return thread


def _documented_endpoints(app):
    """给新手文档提供只读 GET 接口、表单参数与 OpenAPI 返回结构"""
    friendly = {
        "/api/joke/local": ("随机笑话", "无需参数，点击即可获取一条本地笑话"),
        "/api/love/get": ("随机土味情话", "无需参数，适合第一次体验接口调用"),
        "/api/poem/get": ("随机古诗词", "随机返回一首古诗词，可按朝代筛选"),
        "/api/name/get": ("随机起名", "选择名字类型后获取一个随机名字"),
        "/api/weather/get": ("实时天气", "输入城市名称即可查询天气"),
        "/api/history/today": ("历史上的今天", "查询今天日期对应的历史事件"),
    }
    document = app.openapi()

    def resolve(schema):
        schema = schema or {}
        ref = schema.get("$ref", "")
        if not ref.startswith("#/"):
            return schema
        node = document
        for part in ref[2:].split("/"):
            node = (node or {}).get(part, {})
        return node

    result = []
    for path, methods in (document.get("paths") or {}).items():
        operation = methods.get("get")
        if (not operation or not path.startswith("/api/") or path.startswith("/api/portal")
                or "{" in path):
            continue
        title, beginner = friendly.get(path, (operation.get("summary") or path, ""))
        params = []
        for item in operation.get("parameters") or []:
            if item.get("in") != "query":
                continue
            schema = item.get("schema") or {}
            params.append({
                "name": item.get("name"), "required": bool(item.get("required")),
                "type": schema.get("type") or "string",
                "default": schema.get("default"),
                "description": item.get("description") or schema.get("description") or "",
                "enum": schema.get("enum") or [],
                "minimum": schema.get("minimum"), "maximum": schema.get("maximum"),
            })
        if not beginner:
            beginner = "填写所需参数后发送请求" if params else "无需填写参数，直接发送请求即可"
        response = (operation.get("responses") or {}).get("200") or {}
        media = ((response.get("content") or {}).get("application/json") or {})
        response_schema = resolve(media.get("schema"))
        outputs = [{
            "name": name, "type": (resolve(prop).get("type") or "any"),
            "description": resolve(prop).get("description") or resolve(prop).get("title") or "",
        } for name, prop in (response_schema.get("properties") or {}).items()]
        response_example = media.get("example")
        if response_example is None:
            examples = media.get("examples") or {}
            first_example = next(iter(examples.values()), {})
            response_example = first_example.get("value")
        result.append({
            "path": path, "method": "GET", "title": title, "description": beginner,
            "params": params, "outputs": outputs, "response_example": response_example,
        })
    return sorted(result, key=lambda item: (item["path"] not in friendly, item["path"]))


async def _invoke_docs_test(app, request, path, params, user_id):
    """通过内部 ASGI 调用执行真实接口；签名身份让主流程仍按当前 C 用户鉴权计费"""
    timestamp = str(int(time.time()))
    message = f"{user_id}:{path}:{timestamp}"
    signature = hmac.new(str(PORTAL_SECRET).encode("utf-8"), message.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    headers = [
        (b"host", request.headers.get("host", "localhost").encode("latin-1")),
        (b"accept", b"application/json"),
        (b"user-agent", b"API-Platform-Beginner-Docs/1.0"),
        (b"token", str(API_TOKEN).encode("utf-8")),
        (b"x-forwarded-for", _client_ip(request).encode("latin-1")),
        (b"x-portal-test-user", str(user_id).encode("ascii")),
        (b"x-portal-test-timestamp", timestamp.encode("ascii")),
        (b"x-portal-test-signature", signature.encode("ascii")),
    ]
    query = urllib.parse.urlencode(params).encode("utf-8")
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "GET", "scheme": request.url.scheme,
        "path": path, "raw_path": path.encode("utf-8"), "root_path": "",
        "query_string": query, "headers": headers, "client": request.client,
        "server": (request.url.hostname or "localhost", request.url.port or 80),
        "state": {},
    }
    response = {"status": 500, "body": bytearray()}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            response["status"] = message["status"]
        elif message["type"] == "http.response.body":
            response["body"].extend(message.get("body", b""))

    started = time.perf_counter()
    await app(scope, receive, send)
    duration = int((time.perf_counter() - started) * 1000)
    raw = bytes(response["body"])
    try:
        data = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        data = raw[:200000].decode("utf-8", "replace")
    return response["status"], data, duration


def setup_portal(app, security_refresh=None):
    """挂载 /portal 页面、/api/portal 接口和受登录保护的接口文档"""
    app.add_middleware(
        PortalSessionMiddleware, secret_key=PORTAL_SECRET, https_only=PORTAL_COOKIE_SECURE)

    @app.get("/portal", include_in_schema=False)
    def portal_home(request: Request):
        if not _session(request).get("user_id"):
            return RedirectResponse("/portal/login", status_code=303)
        return FileResponse(DASHBOARD_HTML, media_type="text/html; charset=utf-8")

    @app.get("/portal/login", include_in_schema=False)
    def portal_login_page(request: Request):
        if _session(request).get("user_id"):
            return RedirectResponse("/portal", status_code=303)
        return FileResponse(LOGIN_HTML, media_type="text/html; charset=utf-8")

    @app.get("/api/portal/captcha", include_in_schema=False)
    def portal_captcha():
        left = secrets.randbelow(9) + 1
        right = secrets.randbelow(9) + 1
        answer = left + right
        captcha_id = secrets.token_urlsafe(16)
        ttl = max(60, PORTAL_CAPTCHA_TTL)
        CACHE.set(f"portal-captcha:{captcha_id}", str(answer), ttl=ttl)
        png = _captcha_png(f"{left} + {right} = ?")
        CACHE.set(f"portal-captcha-img:{captcha_id}",
                  base64.b64encode(png).decode("ascii"), ttl=ttl)
        return {"code": 200, "captcha_id": captcha_id,
                "image_url": f"/api/portal/captcha/image/{captcha_id}",
                "expires_in": ttl}

    @app.get("/api/portal/captcha/image/{captcha_id}", include_in_schema=False)
    def portal_captcha_image(captcha_id: str):
        raw = CACHE.get(f"portal-captcha-img:{captcha_id}")
        if not raw:
            return Response(status_code=404)
        return Response(content=base64.b64decode(raw), media_type="image/png",
                        headers={"Cache-Control": "no-store, max-age=0"})

    @app.post("/api/portal/email-code", include_in_schema=False)
    def portal_email_code(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
        email = str(payload.get("email") or "").strip().lower()
        purpose = str(payload.get("purpose") or "register").strip()
        if not _verify_captcha(payload.get("captcha_id"), payload.get("captcha")):
            return JSONResponse(status_code=400, content={
                "code": 400, "msg": "图形验证码不正确或已失效，请重新填写"})
        if not EMAIL_RE.fullmatch(email) or len(email) > 190:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "请输入有效邮箱"})
        if purpose not in ("register", "reset"):
            return JSONResponse(status_code=400, content={"code": 400, "msg": "不支持的验证码用途"})
        user = db.query(CUserDB).filter(CUserDB.email == email).first()
        if purpose == "register" and user is not None:
            return JSONResponse(status_code=409, content={"code": 409, "msg": "该邮箱已注册，请直接登录"})
        if purpose == "reset" and user is None:
            return JSONResponse(status_code=404, content={"code": 404, "msg": "该邮箱尚未注册"})
        if purpose == "reset" and user.status != "active":
            return JSONResponse(status_code=403, content={"code": 403, "msg": "当前账号不可重置密码，请联系管理员"})
        ip = _client_ip(request)
        hits = CACHE.incr(f"portal-email-code:{ip}", ttl=3600)
        if hits > 20:
            return JSONResponse(status_code=429, content={"code": 429, "msg": "验证码请求过于频繁，请稍后再试"})
        sent_today = int(CACHE.get(f"portal-email-daily:{email}") or 0)
        if sent_today >= max(1, PORTAL_EMAIL_DAILY_LIMIT):
            return JSONResponse(status_code=429, content={
                "code": 429,
                "msg": f"该邮箱 24 小时内最多接收 {max(1, PORTAL_EMAIL_DAILY_LIMIT)} 封验证码，请明天再试",
            })
        now = datetime.now()
        # 顺带清理超过 1 天仍未被使用的历史验证码，避免残留记录堆积
        db.query(PortalEmailCodeDB).filter(
            PortalEmailCodeDB.expires_at < now - timedelta(days=1),
        ).delete(synchronize_session=False)
        row = db.query(PortalEmailCodeDB).filter(PortalEmailCodeDB.email == email).first()
        if row and row.last_sent_at:
            elapsed = (now - row.last_sent_at).total_seconds()
            if elapsed < PORTAL_EMAIL_RESEND_SECONDS:
                wait = max(1, int(PORTAL_EMAIL_RESEND_SECONDS - elapsed))
                return JSONResponse(status_code=429, content={"code": 429, "msg": f"请等待 {wait} 秒后再重新获取"})
        code = f"{secrets.randbelow(1_000_000):06d}"
        digest = _hash_email_code(email, code, purpose)
        if row is None:
            row = PortalEmailCodeDB(email=email)
            db.add(row)
        row.code_hash = digest
        row.expires_at = now + timedelta(seconds=max(60, PORTAL_EMAIL_CODE_TTL))
        row.last_sent_at = now
        row.attempts = 0
        db.commit()
        scene = "注册" if purpose == "register" else "密码重置"
        # 按“发送尝试”计数：SMTP 失败也占用当日额度，避免反复失败刷爆发信配额
        sent = CACHE.incr(f"portal-email-daily:{email}", ttl=86400)
        ok, error = send_mail(
            f"API 平台{scene}验证码",
            f"你的{scene}验证码是：{code}\n\n验证码 {max(1, PORTAL_EMAIL_CODE_TTL // 60)} 分钟内有效，"
            f"仅用于本次{scene}。请勿转发给他人；如非本人操作，请忽略此邮件。",
            to=email,
        )
        if not ok:
            db.query(PortalEmailCodeDB).filter(
                PortalEmailCodeDB.email == email, PortalEmailCodeDB.code_hash == digest,
            ).delete(synchronize_session=False)
            db.commit()
            LOGGER.warning("%s验证码邮件发送失败：%s %s", scene, email, error)
            return JSONResponse(status_code=503, content={"code": 503, "msg": "验证码发送失败，请稍后重试或联系管理员"})
        return {"code": 200, "msg": "验证码已发送，请查收邮件（也请检查垃圾邮件）",
                "resend_seconds": max(10, PORTAL_EMAIL_RESEND_SECONDS),
                "daily_left": max(0, max(1, PORTAL_EMAIL_DAILY_LIMIT) - sent)}

    @app.post("/api/portal/register", include_in_schema=False)
    def portal_register(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
        email = str(payload.get("email") or "").strip().lower()
        password = str(payload.get("password") or "")
        code = str(payload.get("code") or "").strip()
        agreed = payload.get("agreement") is True or str(payload.get("agreement")).lower() in ("1", "true", "yes")
        if not EMAIL_RE.fullmatch(email) or len(email) > 190:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "请输入有效邮箱"})
        if not 8 <= len(password) <= 128:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "密码长度应为 8-128 位"})
        if not re.fullmatch(r"\d{6}", code):
            return JSONResponse(status_code=400, content={"code": 400, "msg": "请输入邮件中的 6 位验证码"})
        if not agreed:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "请先阅读并同意用户协议"})
        ip = _client_ip(request)
        if not _register_ip_allowed(ip, security_refresh):
            return JSONResponse(status_code=429, content={"code": 429, "msg": "注册请求异常，该 IP 已被限制"})
        if db.query(CUserDB).filter(CUserDB.email == email).first() is not None:
            return JSONResponse(status_code=409, content={"code": 409, "msg": "该邮箱已提交申请"})
        verification = db.query(PortalEmailCodeDB).filter(
            PortalEmailCodeDB.email == email).first()
        now = datetime.now()
        if verification is None or not verification.expires_at or verification.expires_at < now:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "验证码已失效，请重新获取"})
        if int(verification.attempts or 0) >= 5:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "验证码错误次数过多，请重新获取"})
        if not hmac.compare_digest(verification.code_hash or "", _hash_email_code(email, code)):
            verification.attempts = int(verification.attempts or 0) + 1
            if verification.attempts >= 5:
                db.delete(verification)
            db.commit()
            return JSONResponse(status_code=400, content={"code": 400, "msg": "验证码不正确，请检查后重试"})
        user = CUserDB(
            email=email, password_hash=_make_password_hash(password), status="pending",
            level="free", daily_quota=PORTAL_DEFAULT_DAILY_QUOTA, bonus_quota=0, today_used=0,
            quota_date=now.date(), register_ip=ip[:100])
        db.add(user)
        db.delete(verification)
        db.flush()
        record_quota_change(
            db, user.id, int(user.daily_quota or 0), "initial", "初始额度",
            "注册成功，系统发放初始基础额度", balance_after=int(user.daily_quota or 0))
        db.commit()
        _record_operation(request, email, "portal_register", email, "提交注册申请，等待审核")
        return {"code": 200, "msg": "申请已提交，系统会自动审核；审核结果与 Token 将发送到你的邮箱"}

    @app.post("/api/portal/reset-password", include_in_schema=False)
    def portal_reset_password(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
        email = str(payload.get("email") or "").strip().lower()
        password = str(payload.get("password") or "")
        code = str(payload.get("code") or "").strip()
        if not EMAIL_RE.fullmatch(email) or len(email) > 190:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "请输入有效邮箱"})
        if not 8 <= len(password) <= 128:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "密码长度应为 8-128 位"})
        if not re.fullmatch(r"\d{6}", code):
            return JSONResponse(status_code=400, content={"code": 400, "msg": "请输入邮件中的 6 位验证码"})
        user = db.query(CUserDB).filter(CUserDB.email == email).first()
        if user is None:
            return JSONResponse(status_code=404, content={"code": 404, "msg": "该邮箱尚未注册"})
        if user.status != "active":
            return JSONResponse(status_code=403, content={"code": 403, "msg": "当前账号不可重置密码，请联系管理员"})
        verification = db.query(PortalEmailCodeDB).filter(
            PortalEmailCodeDB.email == email).first()
        now = datetime.now()
        if verification is None or not verification.expires_at or verification.expires_at < now:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "验证码已失效，请重新获取"})
        if int(verification.attempts or 0) >= 5:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "验证码错误次数过多，请重新获取"})
        if not hmac.compare_digest(verification.code_hash or "", _hash_email_code(email, code, "reset")):
            verification.attempts = int(verification.attempts or 0) + 1
            if verification.attempts >= 5:
                db.delete(verification)
            db.commit()
            return JSONResponse(status_code=400, content={"code": 400, "msg": "验证码不正确，请检查后重试"})
        user.password_hash = _make_password_hash(password)
        db.delete(verification)
        db.commit()
        _record_operation(request, email, "portal_password_reset", email, "通过邮箱验证码重置密码")
        return {"code": 200, "msg": "密码已重置，请使用新密码登录"}

    @app.post("/api/portal/remember/clear", include_in_schema=False)
    def portal_remember_clear():
        response = JSONResponse({"code": 200, "msg": "已清除记住登录凭据"})
        response.delete_cookie(PORTAL_REMEMBER_COOKIE, path=PORTAL_REMEMBER_COOKIE_PATH)
        return response

    @app.post("/api/portal/login", include_in_schema=False)
    def portal_login(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
        email = str(payload.get("email") or "").strip().lower()
        password = str(payload.get("password") or "")
        remember = bool(payload.get("remember"))
        use_remember = bool(payload.get("use_remember")) and not password
        if use_remember:
            user = _user_from_remember_cookie(request, db)
            if user is None:
                _record_login(db, request, email, "failed", "记住登录凭据无效或已过期")
                return JSONResponse(status_code=401, content={
                    "code": 401, "msg": "记住的登录凭据已失效，请重新输入密码"})
            email = user.email
        else:
            user = db.query(CUserDB).filter(CUserDB.email == email).first()
            if user is None or not _check_password(password, user.password_hash):
                _record_login(db, request, email, "failed", "邮箱或密码错误")
                return JSONResponse(status_code=401, content={"code": 401, "msg": "邮箱或密码错误"})
        if user.status == "pending":
            _record_login(db, request, email, "failed", "账号待审核")
            return JSONResponse(status_code=403, content={"code": 403, "msg": "申请正在审核，请耐心等待"})
        if user.status != "active":
            _record_login(db, request, email, "failed", "账号已封禁")
            return JSONResponse(status_code=403, content={"code": 403, "msg": "账号已被封禁，请联系管理员"})
        user.last_login_at = datetime.now()
        db.commit()
        _record_login(db, request, email, "success",
                      "C 端用户通过记住凭据登录" if use_remember else "C 端用户登录成功")
        _session(request).clear()
        _session(request).update(user_id=user.id, email=user.email)
        response = JSONResponse({"code": 200, "msg": "登录成功"})
        if remember:
            response.set_cookie(
                PORTAL_REMEMBER_COOKIE, _issue_remember_token(user),
                max_age=PORTAL_REMEMBER_MAX_AGE, path=PORTAL_REMEMBER_COOKIE_PATH,
                httponly=True, secure=PORTAL_COOKIE_SECURE, samesite="lax")
        else:
            response.delete_cookie(PORTAL_REMEMBER_COOKIE, path=PORTAL_REMEMBER_COOKIE_PATH)
        return response

    @app.post("/api/portal/logout", include_in_schema=False)
    def portal_logout(request: Request):
        email = _session(request).get("email") or ""
        _session(request).clear()
        if email:
            _record_operation(request, email, "portal_logout", email, "退出用户控制台")
        return {"code": 200, "msg": "已退出"}

    @app.post("/api/portal/account/delete", include_in_schema=False)
    def portal_account_delete(request: Request, payload: dict = Body(...),
                              db: Session = Depends(get_db)):
        user = _require_user(request, db)
        email = (user.email or "").lower()
        if str(payload.get("confirm_email") or "").strip().lower() != email:
            return JSONResponse(status_code=400, content={
                "code": 400, "msg": "请输入当前账号邮箱以确认注销"})
        if not _check_password(str(payload.get("password") or ""), user.password_hash):
            return JSONResponse(status_code=403, content={
                "code": 403, "msg": "密码不正确，无法注销账号"})
        user_id = user.id
        # 隐私合规：物理删除用户及其专属 Token、额度流水、任务奖励、验证码与本人调用记录
        db.query(ApiTokenDB).filter(ApiTokenDB.user_id == user_id).delete(synchronize_session=False)
        db.query(PortalQuotaLogDB).filter(
            PortalQuotaLogDB.user_id == user_id).delete(synchronize_session=False)
        db.query(PortalRewardDB).filter(
            PortalRewardDB.user_id == user_id).delete(synchronize_session=False)
        db.query(PortalEmailCodeDB).filter(
            PortalEmailCodeDB.email == email).delete(synchronize_session=False)
        db.query(ApiLogDB).filter(ApiLogDB.user_id == user_id).delete(synchronize_session=False)
        db.query(LoginLogDB).filter(LoginLogDB.username == email).delete(synchronize_session=False)
        db.query(CUserDB).filter(CUserDB.id == user_id).delete(synchronize_session=False)
        db.commit()
        if security_refresh:
            security_refresh(force=True)
        _record_operation(request, email, "portal_account_delete", email,
                          "用户主动注销账号，账号及关联数据已物理删除")
        _session(request).clear()
        response = JSONResponse({"code": 200, "msg": "账号及关联数据已删除"})
        response.delete_cookie(PORTAL_REMEMBER_COOKIE, path=PORTAL_REMEMBER_COOKIE_PATH)
        return response

    @app.get("/api/portal/me", include_in_schema=False)
    def portal_me(request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        _complete_task(db, user.id, "daily_login")
        raw, token = _issue_token(db, user)
        if raw and security_refresh:
            security_refresh(force=True)
        total = _total_quota(user)
        return {
            "code": 200,
            "data": {
                "email": user.email, "level": user.level or "free",
                "daily_quota": int(user.daily_quota or 0), "bonus_quota": int(user.bonus_quota or 0),
                "total_quota": total, "today_used": int(user.today_used or 0),
                "remaining": max(0, total - int(user.today_used or 0)),
                "token_hint": token.token_hint or "****", "new_token": raw,
                "token_created_at": token.created_at.strftime("%Y-%m-%d %H:%M:%S") if token.created_at else "",
                "docs_enabled": DOCS_ENABLED,
            },
        }

    @app.get("/api/portal/tools/ip-query", include_in_schema=False)
    def portal_ip_query(request: Request, ip: str = Query("", max_length=100),
                        db: Session = Depends(get_db)):
        user = _require_user(request, db)
        if CACHE.incr(f"portal-ip-query:{user.id}", ttl=600) > 30:
            return JSONResponse(status_code=429, content={"code": 429, "msg": "查询过于频繁，请稍后再试"})
        raw = (ip or _client_ip(request)).strip()
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "请输入有效的 IPv4 或 IPv6 地址"})
        if address.is_loopback:
            scope = "回环地址"
        elif address.is_link_local:
            scope = "链路本地地址"
        elif address.is_private:
            scope = "私有地址"
        elif address.is_multicast:
            scope = "组播地址"
        elif not address.is_global:
            scope = "保留地址"
        else:
            scope = "公网地址"
        result = {"ip": address.compressed, "version": f"IPv{address.version}", "scope": scope}
        if scope != "公网地址":
            result["message"] = "该地址不是公网地址，无需查询公开归属地"
            return {"code": 200, "data": result}
        try:
            response = requests.get(
                f"https://ipwho.is/{address.compressed}",
                params={"fields": "success,message,ip,type,continent,country,region,city,latitude,longitude,connection,timezone"},
                timeout=5, headers={"User-Agent": "API-Portal-IP-Tool/1.0"})
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            result["message"] = "IP 识别成功，公开归属地服务暂时不可用"
            return {"code": 200, "data": result}
        if not payload.get("success"):
            result["message"] = payload.get("message") or "未查询到公开归属地"
            return {"code": 200, "data": result}
        connection = payload.get("connection") or {}
        timezone = payload.get("timezone") or {}
        result.update({
            "continent": payload.get("continent") or "", "country": payload.get("country") or "",
            "region": payload.get("region") or "", "city": payload.get("city") or "",
            "isp": connection.get("isp") or connection.get("org") or "",
            "asn": str(connection.get("asn") or ""), "timezone": timezone.get("id") or "",
            "latitude": payload.get("latitude"), "longitude": payload.get("longitude"),
        })
        return {"code": 200, "data": result}

    @app.post("/api/portal/tools/url", include_in_schema=False)
    def portal_url_tool(request: Request, payload: dict = Body(...),
                        db: Session = Depends(get_db)):
        _require_user(request, db)
        text = str(payload.get("text") or "")
        mode = str(payload.get("mode") or "encode")
        if len(text) > 200_000:
            raise HTTPException(status_code=400, detail="文本不能超过 200000 个字符")
        try:
            result = (urllib.parse.quote(text, safe="") if mode == "encode" else
                      urllib.parse.unquote(text, encoding="utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="URL 内容不是有效的 UTF-8 编码")
        return {"code": 200, "data": {"result": result}}

    @app.post("/api/portal/tools/base64", include_in_schema=False)
    def portal_base64_tool(request: Request, payload: dict = Body(...),
                           db: Session = Depends(get_db)):
        _require_user(request, db)
        text = str(payload.get("text") or "")
        mode = str(payload.get("mode") or "encode")
        if len(text) > 1_000_000:
            raise HTTPException(status_code=400, detail="内容不能超过 1000000 个字符")
        try:
            if mode == "encode":
                result = base64.b64encode(text.encode("utf-8")).decode("ascii")
            else:
                compact = re.sub(r"\s+", "", text)
                result = base64.b64decode(compact, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="Base64 内容无效，或解码结果不是 UTF-8 文本")
        return {"code": 200, "data": {"result": result}}

    @app.post("/api/portal/tools/image-base64", include_in_schema=False)
    def portal_image_base64_tool(request: Request, file: UploadFile = File(...),
                                 db: Session = Depends(get_db)):
        _require_user(request, db)
        raw, image = _read_image_upload(file, limit=10 * 1024 * 1024)
        mime = Image.MIME.get(image.format) or file.content_type or "image/jpeg"
        encoded = base64.b64encode(raw).decode("ascii")
        return {"code": 200, "data": {
            "result": f"data:{mime};base64,{encoded}", "mime": mime,
            "filename": file.filename or "image", "size": len(raw),
        }}

    @app.post("/api/portal/tools/network", include_in_schema=False)
    def portal_network_tool(request: Request, payload: dict = Body(...),
                            db: Session = Depends(get_db)):
        _require_user(request, db)
        raw = str(payload.get("text") or "").strip()
        mode = str(payload.get("mode") or "network")
        if not raw or len(raw) > 100:
            raise HTTPException(status_code=400, detail="请输入有效的 IP、CIDR 或子网掩码")
        try:
            if mode == "int":
                address = ipaddress.ip_address(raw)
                return {"code": 200, "data": {
                    "ip": address.compressed, "version": address.version,
                    "integer": str(int(address)), "hex": hex(int(address)),
                }}
            input_ip = None
            if "/" not in raw:
                try:
                    network = ipaddress.IPv4Network(f"0.0.0.0/{raw}")
                except ValueError:
                    interface = ipaddress.ip_interface(raw)
                    input_ip, network = interface.ip, interface.network
            else:
                interface = ipaddress.ip_interface(raw)
                input_ip, network = interface.ip, interface.network
        except ValueError:
            raise HTTPException(status_code=400, detail="无法识别，请输入如 192.168.1.10/24 或 255.255.255.0")
        if network.version == 4:
            usable = network.num_addresses if network.prefixlen >= 31 else max(0, network.num_addresses - 2)
            first = network.network_address if network.prefixlen >= 31 else network.network_address + 1
            last = network.broadcast_address if network.prefixlen >= 31 else network.broadcast_address - 1
        else:
            usable = network.num_addresses
            first, last = network.network_address + 1, network.broadcast_address
        return {"code": 200, "data": {
            "input_ip": input_ip.compressed if input_ip else "",
            "network": str(network), "network_address": str(network.network_address),
            "netmask": str(network.netmask), "prefix": network.prefixlen,
            "wildcard": str(network.hostmask), "broadcast": str(network.broadcast_address),
            "first_host": str(first), "last_host": str(last),
            "total": str(network.num_addresses), "usable": str(usable),
        }}

    @app.post("/api/portal/tools/fake-data", include_in_schema=False)
    def portal_fake_data_tool(request: Request, payload: dict = Body(...),
                              db: Session = Depends(get_db)):
        _require_user(request, db)
        kind = str(payload.get("kind") or "all")
        count = payload.get("count", 500)
        if kind not in ("all", "name", "phone", "address") or isinstance(count, bool):
            raise HTTPException(status_code=400, detail="生成类型或数量无效")
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="生成数量必须是整数")
        if not 1 <= count <= 500:
            raise HTTPException(status_code=400, detail="生成数量需为 1-500")
        fake = Faker("zh_CN")
        rows = []
        for _ in range(count):
            item = {}
            if kind in ("all", "name"):
                item["name"] = fake.name()
            if kind in ("all", "phone"):
                item["phone"] = fake.phone_number()
            if kind in ("all", "address"):
                item["address"] = fake.address().replace("\n", " ")
            rows.append(item)
        return {"code": 200, "data": {"rows": rows, "count": count}}

    @app.post("/api/portal/tools/image-process", include_in_schema=False)
    def portal_image_process_tool(
            request: Request, files: list[UploadFile] = File(...), width: int = Form(...),
            height: int = Form(...), output_format: str = Form(...), quality: int = Form(85),
            keep_ratio: bool = Form(True), db: Session = Depends(get_db)):
        _require_user(request, db)
        formats = {
            "png": ("PNG", "image/png", "png"), "jpeg": ("JPEG", "image/jpeg", "jpg"),
            "webp": ("WEBP", "image/webp", "webp"), "gif": ("GIF", "image/gif", "gif"),
            "bmp": ("BMP", "image/bmp", "bmp"), "tiff": ("TIFF", "image/tiff", "tiff"),
            "ico": ("ICO", "image/x-icon", "ico"),
        }
        if output_format not in formats:
            raise HTTPException(status_code=400, detail="请选择有效的输出格式")
        if not files or len(files) > 8:
            raise HTTPException(status_code=400, detail="请选择 1-8 张图片")
        if not 1 <= width <= 10_000 or not 1 <= height <= 10_000 or width * height > 40_000_000:
            raise HTTPException(status_code=400, detail="尺寸需为 1-10000，且总像素不能超过 4000 万")
        if not 10 <= quality <= 100:
            raise HTTPException(status_code=400, detail="压缩质量需为 10-100")
        pil_format, mime, extension = formats[output_format]
        results = []
        total_size = 0
        for index, upload in enumerate(files, 1):
            raw, source = _read_image_upload(upload)
            total_size += len(raw)
            if total_size > 50 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="批量图片原始大小合计不能超过 50MB")
            image = ImageOps.exif_transpose(source)
            if keep_ratio:
                image.thumbnail((width, height), Image.Resampling.LANCZOS)
            else:
                image = image.resize((width, height), Image.Resampling.LANCZOS)
            if output_format == "ico":
                image.thumbnail((256, 256), Image.Resampling.LANCZOS)
            if output_format in ("jpeg", "bmp"):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            elif output_format == "gif":
                image = image.convert("P", palette=Image.Palette.ADAPTIVE)
            elif output_format == "ico":
                image = image.convert("RGBA")
            elif image.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "I", "F"):
                image = image.convert("RGBA")
            save_options = {}
            if output_format in ("jpeg", "webp"):
                save_options["quality"] = quality
            elif output_format == "png":
                save_options["optimize"] = True
            elif output_format == "tiff":
                save_options["compression"] = "tiff_deflate"
            buffer = io.BytesIO()
            try:
                image.save(buffer, format=pil_format, **save_options)
            except (OSError, ValueError):
                raise HTTPException(status_code=400, detail=f"{upload.filename or '图片'} 无法转换为 {pil_format}")
            base = re.sub(r"[\\/:*?\"<>|]", "_", os.path.splitext(upload.filename or "image")[0]) or "image"
            prefix = f"{index:02d}_" if len(files) > 1 else ""
            filename = f"{prefix}{base}_{image.width}x{image.height}.{extension}"
            results.append((filename, buffer.getvalue()))
        if len(results) == 1:
            response = _attachment_response(results[0][1], mime, results[0][0])
        else:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for filename, content in results:
                    bundle.writestr(filename, content)
            response = _attachment_response(archive.getvalue(), "application/zip", "processed_images.zip")
        response.headers["X-Processed-Count"] = str(len(results))
        return response

    @app.get("/api/portal/rewards", include_in_schema=False)
    def portal_rewards(request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        return {"code": 200, "data": _reward_list(db, user.id),
                "bonus_quota": int(user.bonus_quota or 0)}

    @app.post("/api/portal/rewards/{task_key}/claim", include_in_schema=False)
    def portal_reward_claim(task_key: str, request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        definition = REWARD_TASKS.get(task_key)
        if not definition:
            return JSONResponse(status_code=404, content={"code": 404, "msg": "任务不存在"})
        today = datetime.now().date()
        user = db.query(CUserDB).filter(CUserDB.id == user.id).with_for_update().first()
        query = db.query(PortalRewardDB).filter(
            PortalRewardDB.user_id == user.id, PortalRewardDB.task_key == task_key,
        )
        if task_key not in ONCE_TASKS:
            query = query.filter(PortalRewardDB.task_date == today)
        rows = query.all()
        row = rows[0] if rows else None
        if row is None and task_key == "daily_checkin":
            _complete_task(db, user.id, task_key)
            row = db.query(PortalRewardDB).filter(
                PortalRewardDB.user_id == user.id, PortalRewardDB.task_key == task_key,
                PortalRewardDB.task_date == today,
            ).first()
            rows = [row] if row else []
        if row is None:
            return JSONResponse(status_code=400, content={"code": 400, "msg": "请先完成任务，再领取奖励"})
        claimed_message = "该一次性任务奖励已领取，不可重复领取" if task_key in ONCE_TASKS else "该任务奖励今天已领取"
        if any(item.claimed_at for item in rows):
            return JSONResponse(status_code=409, content={"code": 409, "msg": claimed_message})
        claimed = db.query(PortalRewardDB).filter(
            PortalRewardDB.id == row.id, PortalRewardDB.claimed_at.is_(None),
        ).update({PortalRewardDB.claimed_at: datetime.now()}, synchronize_session=False)
        if not claimed:
            db.rollback()
            return JSONResponse(status_code=409, content={"code": 409, "msg": claimed_message})
        db.query(CUserDB).filter(CUserDB.id == user.id).update(
            {CUserDB.bonus_quota: CUserDB.bonus_quota + definition["reward"]},
            synchronize_session=False)
        db.flush()
        db.refresh(user)
        task_type = "一次性新手任务" if task_key in ONCE_TASKS else "每日任务"
        record_quota_change(
            db, user.id, definition["reward"], "task", f"任务奖励：{definition['title']}",
            f"完成并领取{task_type}「{definition['title']}」", balance_after=_remaining_quota(user))
        db.commit()
        _record_operation(request, user.email, "portal_reward_claim", definition["title"],
                          f"领取 {definition['reward']} 次累计额度")
        return {"code": 200, "msg": f"已领取 {definition['reward']} 次累计额度",
                "bonus_quota": int(user.bonus_quota or 0),
                "remaining": max(0, _total_quota(user) - int(user.today_used or 0))}

    @app.post("/api/portal/tasks/{task_key}/complete", include_in_schema=False)
    def portal_task_complete(task_key: str, request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        if task_key not in ("sandbox_test", "code_generate"):
            return JSONResponse(status_code=400, content={"code": 400, "msg": "该任务不支持此完成方式"})
        _complete_task(db, user.id, task_key)
        _record_operation(request, user.email, "portal_task_complete",
                          REWARD_TASKS[task_key]["title"], "更新任务完成进度")
        return {"code": 200, "msg": "任务进度已更新", "data": _reward_list(db, user.id)}

    @app.get("/api/portal/quota-logs", include_in_schema=False)
    def portal_quota_logs(request: Request, limit: int = Query(200, ge=1, le=500),
                          db: Session = Depends(get_db)):
        user = _require_user(request, db)
        ensure_initial_quota_log(db, user)
        db.commit()
        rows = db.query(PortalQuotaLogDB).filter(
            PortalQuotaLogDB.user_id == user.id,
        ).order_by(PortalQuotaLogDB.id.desc()).limit(limit).all()
        return {"code": 200, "data": [{
            "time": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
            "change": int(row.change or 0), "kind": row.kind or "", "title": row.title or "",
            "detail": row.detail or "", "path": row.path or "",
            "balance_after": int(row.balance_after or 0), "operator": row.operator or "system",
        } for row in rows]}

    @app.post("/api/portal/token/reset", include_in_schema=False)
    def portal_token_reset(request: Request, db: Session = Depends(get_db)):
        user = _require_user(request, db)
        rows = db.query(ApiTokenDB).filter(ApiTokenDB.user_id == user.id).order_by(ApiTokenDB.id).all()
        raw = secrets.token_urlsafe(32)
        if rows:
            # 只轮换密钥本身，权限、启停、过期时间等管理策略必须保持不变。
            row = rows[0]
            for extra in rows[1:]:
                db.delete(extra)
            row.token = _hash_token(raw)
            row.token_hint = _token_hint(raw)
            row.grace_until = None
            row.remark = "C端用户重置"
        else:
            row = ApiTokenDB(
                name=f"{user.email} 的专属 Token", token=_hash_token(raw), token_hint=_token_hint(raw),
                permissions="*", enabled=True, remark="C端用户重置", user_id=user.id)
            db.add(row)
        db.commit()
        if security_refresh:
            security_refresh(force=True)
        _record_operation(request, user.email, "portal_token_reset", user.email,
                          "重置专属 Token，旧 Token 已失效")
        return {"code": 200, "msg": "旧 Token 已失效，新 Token 仅显示这一次",
                "token": raw, "token_hint": row.token_hint}

    @app.get("/api/portal/logs", include_in_schema=False)
    def portal_logs(request: Request, days: int = Query(7, ge=1, le=7),
                    page: int = Query(1, ge=1),
                    page_size: int = Query(20, ge=1, le=100),
                    start_time: str = Query("", max_length=40),
                    end_time: str = Query("", max_length=40),
                    path: str = Query("", max_length=200),
                    db: Session = Depends(get_db)):
        user = _require_user(request, db)
        now = datetime.now()
        cutoff = now - timedelta(days=days)

        def parse_time(value, label):
            if not value.strip():
                return None
            try:
                parsed = datetime.fromisoformat(value.strip())
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone().replace(tzinfo=None)
                return parsed
            except ValueError:
                raise HTTPException(status_code=400, detail=f"{label}格式不正确")

        start = parse_time(start_time, "开始时间")
        end = parse_time(end_time, "结束时间")
        if start and end and start > end:
            raise HTTPException(status_code=400, detail="开始时间不能晚于结束时间")

        # 防越权边界：user_id 只取当前 Session，绝不接受请求参数覆盖。
        filters = [ApiLogDB.user_id == user.id, ApiLogDB.time >= cutoff]
        if start:
            filters.append(ApiLogDB.time >= max(start, cutoff))
        if end:
            filters.append(ApiLogDB.time <= min(end, now))
        path = path.strip()
        if path:
            filters.append(ApiLogDB.path == path)

        query = db.query(ApiLogDB).filter(*filters)
        total = query.count()
        pages = max(1, (total + page_size - 1) // page_size)
        rows = query.order_by(ApiLogDB.id.desc()).offset(
            (page - 1) * page_size).limit(page_size).all()
        # 调用总量与每日统计取自额度流水（每次调用必有），明细按抽样比例记录。
        quota_filters = [PortalQuotaLogDB.user_id == user.id,
                         PortalQuotaLogDB.kind == "api_call",
                         PortalQuotaLogDB.created_at >= cutoff]
        if start:
            quota_filters.append(PortalQuotaLogDB.created_at >= max(start, cutoff))
        if end:
            quota_filters.append(PortalQuotaLogDB.created_at <= min(end, now))
        if path:
            quota_filters.append(PortalQuotaLogDB.path == path)
        total_calls = int(db.query(func.count(PortalQuotaLogDB.id)).filter(
            *quota_filters).scalar() or 0)
        day_column = func.date(PortalQuotaLogDB.created_at)
        daily_rows = db.query(
            day_column.label("day"), func.count(PortalQuotaLogDB.id).label("count")
        ).filter(*quota_filters).group_by(day_column).order_by(day_column).all()
        docs = _documented_endpoints(app)
        titles = {item["path"]: item["title"] for item in docs}
        return {
            "code": 200, "sampled": PORTAL_API_LOG_SAMPLE > 1,
            "sample_rate": max(1, PORTAL_API_LOG_SAMPLE), "total_calls": total_calls,
            "page": page, "page_size": page_size, "pages": pages, "total": total,
            "interfaces": [{"path": item["path"], "title": item["title"]} for item in docs],
            "daily": [{
                "date": day.strftime("%Y-%m-%d") if hasattr(day, "strftime") else str(day),
                "count": int(count or 0),
            } for day, count in daily_rows],
            "data": [{
                "time": row.time.strftime("%Y-%m-%d %H:%M:%S") if row.time else "",
                "method": row.method or "", "path": row.path or "",
                "title": titles.get(row.path, row.path or "未知接口"),
                "status_code": row.status_code, "duration_ms": row.duration_ms or 0,
            } for row in rows],
        }

    if DOCS_ENABLED:
        def _docs_allowed(request):
            return bool(_session(request).get("user_id"))

        @app.get("/docs/preview", include_in_schema=False)
        def guest_docs_preview():
            """游客预览：仅展示接口名与参数结构，不提供在线测试和返回示例"""
            from html import escape
            cards = []
            for item in _documented_endpoints(app):
                rows = "".join(
                    "<tr><td class=\"mono\">{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                        escape(str(p.get("name") or "")),
                        escape(str(p.get("type") or "string")),
                        "必填" if p.get("required") else "可选",
                        escape(str(p.get("description") or "-")),
                    ) for p in item.get("params") or [])
                param_block = (
                    "<table><thead><tr><th>参数</th><th>类型</th><th>是否必填</th><th>说明</th></tr></thead>"
                    f"<tbody>{rows}</tbody></table>"
                    if rows else "<p class=\"muted\">无需参数</p>")
                cards.append(
                    f"<section class=\"card\"><h2>{escape(str(item['title']))}</h2>"
                    f"<p class=\"mono muted\">{escape(str(item['path']))}</p>{param_block}</section>")
            page = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>接口预览 - API 平台</title>
<style>body{margin:0;background:#f4f6fb;color:#1f2937;font:14px/1.6 -apple-system,"Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:28px 18px 48px}h1{font-size:22px;margin:0 0 6px}
.muted{color:#6b7280}.mono{font-family:Consolas,monospace}
.card{background:#fff;border:1px solid #e3e8f2;border-radius:12px;padding:16px 18px;margin-top:14px}
.card h2{font-size:16px;margin:0 0 4px}table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #eef1f7}th{color:#6b7280;font-weight:600}
.notice{margin-top:16px;padding:14px 16px;border-radius:12px;background:#eef3ff;border:1px solid #d8e2ff}
.notice a{color:#2563eb;text-decoration:none;font-weight:600}</style></head><body><div class="wrap">
<h1>接口预览（游客版）</h1><p class="muted">这里只展示接口名称与参数结构。在线测试、返回示例和完整文档需登录后使用。</p>
<div class="notice">还没有账号？<a href="/portal/login">登录或提交申请</a>，审核通过后即可获得专属 Token 并调用接口。</div>
""" + "".join(cards) + "</div></body></html>"
            return HTMLResponse(page)

        @app.get("/docs", include_in_schema=False)
        def protected_docs(request: Request, db: Session = Depends(get_db)):
            if not _docs_allowed(request):
                return RedirectResponse("/portal/login?next=/docs", status_code=303)
            user = _require_user(request, db)
            _complete_task(db, user.id, "read_guide")
            return FileResponse(DOCS_HTML, media_type="text/html; charset=utf-8")

        @app.get("/docs/advanced", include_in_schema=False)
        def protected_docs_advanced(request: Request):
            if not _docs_allowed(request):
                return RedirectResponse("/portal/login?next=/docs/advanced", status_code=303)
            return get_swagger_ui_html(
                openapi_url="/openapi.json", title=f"{app.title} - 开发者文档",
                oauth2_redirect_url="/docs/oauth2-redirect")

        @app.get("/redoc", include_in_schema=False)
        def protected_redoc(request: Request):
            if not _docs_allowed(request):
                return RedirectResponse("/portal/login?next=/redoc", status_code=303)
            return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")

        @app.get("/docs/oauth2-redirect", include_in_schema=False)
        def protected_docs_oauth2_redirect(request: Request):
            if not _docs_allowed(request):
                return JSONResponse(status_code=401, content={"code": 401, "msg": "请先登录"})
            return get_swagger_ui_oauth2_redirect_html()

        @app.get("/openapi.json", include_in_schema=False)
        def protected_openapi(request: Request):
            if not _docs_allowed(request):
                return JSONResponse(status_code=401, content={"code": 401, "msg": "请先登录"})
            return JSONResponse(app.openapi())

        @app.get("/api/portal/docs/endpoints", include_in_schema=False)
        def portal_doc_endpoints(request: Request, db: Session = Depends(get_db)):
            user = _require_user(request, db)
            token = db.query(ApiTokenDB).filter(
                ApiTokenDB.user_id == user.id, ApiTokenDB.enabled.is_(True),
            ).order_by(ApiTokenDB.id).first()
            usage_rows = db.query(
                PortalQuotaLogDB.path, func.count(PortalQuotaLogDB.id)
            ).filter(
                PortalQuotaLogDB.user_id == user.id,
                PortalQuotaLogDB.kind == "api_call",
                PortalQuotaLogDB.path != "",
            ).group_by(PortalQuotaLogDB.path).all()
            usage = {path: int(count or 0) for path, count in usage_rows}
            items = _documented_endpoints(app)
            for item in items:
                item["usage_count"] = usage.get(item["path"], 0)
            # 同次数保持原目录顺序，仅按当前用户的真实累计调用次数降序排列。
            items.sort(key=lambda item: -item["usage_count"])
            return {"code": 200, "data": items,
                    "token_hint": token.token_hint if token else "未发放",
                    "token_ready": token is not None}

        @app.post("/api/portal/docs/test", include_in_schema=False)
        async def portal_doc_test(request: Request, payload: dict = Body(...),
                                  db: Session = Depends(get_db)):
            user = _require_user(request, db)
            path = str(payload.get("path") or "")
            definition = next((item for item in _documented_endpoints(app)
                               if item["path"] == path), None)
            if definition is None:
                return JSONResponse(status_code=400, content={"code": 400, "msg": "该接口不支持新手测试"})
            source = payload.get("params") or {}
            if not isinstance(source, dict) or len(source) > 20:
                return JSONResponse(status_code=400, content={"code": 400, "msg": "请求参数格式不正确"})
            allowed = {item["name"] for item in definition["params"]}
            if any(key not in allowed for key in source):
                return JSONResponse(status_code=400, content={"code": 400, "msg": "包含文档未声明的参数"})
            params = {}
            for key, value in source.items():
                if value is None or isinstance(value, (dict, list)):
                    continue
                text = str(value).strip()
                if text:
                    params[key] = text[:500]
            missing = [item["name"] for item in definition["params"]
                       if item["required"] and item["name"] not in params]
            if missing:
                return JSONResponse(status_code=400, content={
                    "code": 400, "msg": "请填写必填参数：" + "、".join(missing)})
            status, data, duration = await _invoke_docs_test(app, request, path, params, user.id)
            if 200 <= status < 400:
                _complete_task(db, user.id, "api_test")
            return {"code": 200, "status_code": status, "data": data,
                    "duration_ms": duration, "path": path}
