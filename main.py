from fastapi import FastAPI, Query, Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
import base64
import hashlib
import hmac
import json
import os
import requests
import time
import random
import re
import subprocess
import threading
import uuid
from datetime import datetime, timedelta

from starlette.middleware.gzip import GZipMiddleware

# 配置、数据库、模型分别放在独立模块；这里统一导入，保持对 scripts/*.py 的
# `from main import ...` 兼容（模块级名称会被 re-export）。
from app.core.config import (  # noqa: F401
    ADMIN_COOKIE_SECURE,
    ALERT_COOLDOWN,
    ALERT_ENABLED,
    ALERT_WECHAT_ENABLED,
    ALERT_PUSH_START,
    ALERT_PUSH_END,
    ALERT_REPEAT_LIMIT,
    ALERT_REPEAT_WINDOW,
    ADMIN_PASSWORD,
    ADMIN_SECRET,
    ADMIN_USER,
    API_TOKEN,
    API_VERSION_PREFIX,
    APP_ENV,
    APP_VERSION,
    BACKUP_KEEP_DAYS,
    BATTERY_CHECK_ENABLED,
    BATTERY_LOW_PERCENT,
    BREAKER_COOLDOWN,
    BREAKER_THRESHOLD,
    CACHE_MAX_ITEMS,
    CACHE_TTL,
    DB_HOST,
    DB_MAX_OVERFLOW,
    DB_NAME,
    DB_POOL_SIZE,
    DB_PORT,
    DB_PWD,
    DB_USER,
    DESKTOP_NOTIFY_ENABLED,
    DESKTOP_NOTIFY_MIN_LEVEL,
    ALERT_AT_MOBILES,
    ALERT_HOST_PERCENT,
    ALERT_WEBHOOK_HIGH,
    ALERT_WEBHOOK_LOW,
    DOCS_ENABLED,
    IP_BURST_LIMIT,
    IP_FREEZE_SECONDS,
    IP_GEO_ENABLED,
    IP_GEO_MINUTES,
    IS_PROD,
    LOG_FILE,
    LOG_JSON,
    LOG_ROTATE_DAYS,
    LOKI_URL,
    NET_API,
    NET_HEADERS,
    NETWORK_CHECK_ENABLED,
    NETWORK_FAIL_COUNT,
    NETWORK_PING_TARGETS,
    NETWORK_PING_TIMEOUT,
    PORTAL_API_LOG_KEEP_DAYS,
    PORTAL_API_LOG_SAMPLE,
    PORTAL_COOKIE_SECURE,
    PORTAL_DEFAULT_DAILY_QUOTA,
    PORTAL_SECRET,
    PORTAL_TOKEN_ABUSE_LIMIT,
    PORTAL_TOKEN_ABUSE_WINDOW,
    PORTAL_USER_QPS,
    GLOBAL_QPS_LIMIT,
    SIGN_ENABLED,
    SIGN_PATHS,
    SIGN_SECRET,
    SIGN_WINDOW,
    SQLALCHEMY_DATABASE_URL,
    TIMEOUT,
    TIME_ZONE,
)
from app.core.db import Base, SessionLocal, engine, get_db  # noqa: F401
from app.core.cache import CACHE
from app.core import sensitive
from app.core.models import (  # noqa: F401
    TABLE_MAP,
    AdminSessionDB,
    AdminUserDB,
    AlertEventDB,
    AlertRuleDB,
    AnswerBookDB,
    ApiLogDB,
    ApiSwitchDB,
    ApiTokenDB,
    AuditLogDB,
    BreakerEventDB,
    BreakerStateDB,
    CopywritingDB,
    CUserDB,
    DailyImageDB,
    ExportJobDB,
    HistoryTodayDB,
    HotNewsDB,
    IdiomDB,
    ImportJobDB,
    ImportLogDB,
    IpRuleDB,
    KnowledgeDB,
    LocalJokeDB,
    LoginLogDB,
    LoveTalkDB,
    NameDB,
    PoemDB,
    PoolMetricDB,
    PortalQuotaLogDB,
    PoisonSoupDB,
    RateLimitLogDB,
    RetortDB,
    RiddleDB,
    ScheduleRunDB,
    ScriptRunDB,
    ScriptScheduleDB,
    SensitiveQuarantineDB,
    SensitiveWordDB,
    SettingDB,
    TwisterDB,
    WordWisdomDB,
    XiehouyuDB,
)
from app.core import alert_store
from app.core import mailer
from app.core import metrics
from app.core import sign_utils
from app.core import dist_lock

START_TIME = time.time()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def setup_json_logging():
    """日志配置：可选 JSON 格式 / 按天滚动文件 / Loki 推送

    - LOG_JSON=1：Uvicorn 与应用日志改为单行 JSON，便于接入 ELK / Loki
    - LOG_FILE=路径：同时写入按天滚动的日志文件（保留 LOG_ROTATE_DAYS 份）
    - LOKI_URL=地址：异步推送日志到 Loki（需配合 LOG_JSON=1）
    三项均未配置时保持原有日志格式不变。
    """
    if not (LOG_JSON or LOG_FILE or LOKI_URL):
        return
    import logging
    import queue
    from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler

    class JsonFormatter(logging.Formatter):
        def format(self, record):
            payload = {
                "time": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            return json.dumps(payload, ensure_ascii=False)

    formatter = JsonFormatter() if LOG_JSON else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s")

    handlers = [logging.StreamHandler()]
    if LOG_FILE:
        try:
            path = os.path.abspath(LOG_FILE)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            handlers.append(TimedRotatingFileHandler(
                path, when="midnight", backupCount=max(1, int(LOG_ROTATE_DAYS)),
                encoding="utf-8"))
        except Exception as e:
            print(f"[日志] 文件日志初始化失败：{e}")
    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = handlers
        logger.propagate = False

    # Loki 推送：QueueHandler 异步写入队列，独立线程批量 POST，不阻塞请求
    if LOKI_URL:
        class LokiHandler(logging.Handler):
            def emit(self, record):
                try:
                    payload = {"streams": [{
                        "stream": {"app": "api-platform", "level": record.levelname},
                        "values": [[str(int(record.created * 1e9)), self.format(record)]],
                    }]}
                    requests.post(LOKI_URL, json=payload, timeout=5)
                except Exception:
                    pass

        loki = LokiHandler()
        loki.setFormatter(formatter)
        log_queue = queue.Queue(maxsize=10000)
        multi = logging.getLogger("api-platform")
        multi.setLevel(logging.INFO)
        multi.addHandler(QueueHandler(log_queue))
        listener = QueueListener(log_queue, loki)
        listener.start()
        print(f"[日志] 已开启 Loki 推送：{LOKI_URL}")


setup_json_logging()

# 启动日志摘要：后台可直接查看，不用登录服务器翻日志文件
STARTUP_LOG = []


def log_startup(message):
    """记录一条启动日志（最多保留 200 条）"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}"
    STARTUP_LOG.append(line)
    del STARTUP_LOG[:-200]


def _apply_timezone():
    """统一进程时区：Linux 服务器默认 UTC，会导致日志、告警和备份文件名比北京时间慢 8 小时。
    通过 TZ 环境变量 + time.tzset() 校准；Windows 跟随系统时区，无需处理。
    """
    zone = (TIME_ZONE or "").strip()
    if not zone:
        return
    os.environ["TZ"] = zone
    try:
        time.tzset()          # 仅 Unix 提供；Windows 没有该函数
    except AttributeError:
        return
    log_startup(f"时区已设置为 {zone}（当前时间 {time.strftime('%Y-%m-%d %H:%M:%S')}）")


_apply_timezone()

# 文档路由由 C 端门户统一挂载并强制登录，不保留公开的默认文档入口
app = FastAPI(
    title="我的自用综合API平台",
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# 自动创建所有数据表，不存在则新建；已有表的缺失字段由 _ensure_schema 补齐
def _create_tables():
    Base.metadata.create_all(bind=engine)
    return True


_create_tables()


class VersionPrefixMiddleware:
    """版本化前缀：把 /v1/api/xxx 重写为 /api/xxx，保留旧路径兼容

    以纯 ASGI 中间件实现，注册在最外层，保证 IP 白名单、调用日志、
    Token 权限校验看到的都是规范化后的真实路径。
    """

    def __init__(self, app, prefix: str = "/v1"):
        self.app = app
        self.prefix = (prefix or "").rstrip("/")

    async def __call__(self, scope, receive, send):
        if self.prefix and scope.get("type") == "http":
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                new_path = path[len(self.prefix):] or "/"
                scope["path"] = new_path
                scope["raw_path"] = new_path.encode("utf-8")
        await self.app(scope, receive, send)



class SensitiveFilterMiddleware:
    """对外 JSON 响应敏感词替换（仅 /api/ 路径，后台与静态资源不受影响）

    纯 ASGI 实现：仅对 application/json 响应缓冲后过滤，其他响应原样透传。
    词库为空时直接放行，不产生额外 JSON 解析开销。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return

        start_message = None
        chunks = []

        async def send_wrapper(message):
            nonlocal start_message
            if message["type"] == "http.response.start":
                start_message = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            headers = list((start_message or {}).get("headers", []))
            ctype = ""
            for key, val in headers:
                if key.lower() == b"content-type":
                    ctype = val.decode("latin-1")
                    break
            if "application/json" in ctype and body:
                try:
                    data = json.loads(body)
                    cleaned = sensitive.clean_obj(data)
                    if cleaned is not data:
                        body = json.dumps(cleaned, ensure_ascii=False).encode("utf-8")
                except Exception:
                    pass
            headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode("latin-1")))
            if start_message is not None:
                start_message["headers"] = headers
                await send(start_message)
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, send_wrapper)


class RequestIDMiddleware:
    """为每个请求生成唯一 request_id，写入 scope 并回写响应头 X-Request-ID

    用户反馈错误时提供该 ID，即可在日志中精确定位本次请求。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        rid = ""
        for key, val in scope.get("headers") or []:
            if key.lower() == b"x-request-id":
                rid = val.decode("latin-1").strip()
                break
        if not rid or len(rid) > 64:
            rid = uuid.uuid4().hex[:16]
        state = scope.setdefault("state", {})
        state["request_id"] = rid

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", rid.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class ResponseMetaMiddleware:
    """为 /api/ 与 /admin/api/ 的 JSON 响应补充 timestamp、request_id

    非 JSON（Excel 导出、SSE 流、静态资源）直接透传，不缓冲、不影响流式输出。
    """

    MAX_BODY = 1024 * 1024

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") != "http" or not (
                path.startswith("/api/") or path.startswith("/admin/api/")):
            await self.app(scope, receive, send)
            return

        rid = (scope.get("state") or {}).get("request_id", "")
        start_message = None
        passthrough = False
        chunks = []

        async def send_wrapper(message):
            nonlocal start_message, passthrough
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                ctype = ""
                for key, val in headers:
                    if key.lower() == b"content-type":
                        ctype = val.decode("latin-1")
                        break
                start_message = message
                # 非 JSON 响应不缓冲，避免 Excel/SSE 大响应占内存
                passthrough = "application/json" not in ctype
                if passthrough:
                    await send(message)
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            if passthrough:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            headers = list((start_message or {}).get("headers", []))
            if body and len(body) <= self.MAX_BODY:
                try:
                    data = json.loads(body)
                except Exception:
                    data = None
                if isinstance(data, dict):
                    data.setdefault("timestamp", int(time.time()))
                    data.setdefault("request_id", rid)
                    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode("latin-1")))
            if start_message is not None:
                start_message["headers"] = headers
                await send(start_message)
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, send_wrapper)


class SignatureMiddleware:
    """重要接口请求签名校验（防截获重放），默认关闭

    开启方式：SIGN_ENABLED=1 且 SIGN_PATHS 配置需要保护的路径前缀。
    请求头：X-Timestamp（秒级时间戳）、X-Nonce（一次性随机串）、X-Sign。
    签名算法与客户端工具统一放在 sign_utils.py。
    """

    def __init__(self, app, secret: str, paths, window: int = 300):
        self.app = app
        self.secret = secret or ""
        self.paths = [p.strip() for p in (paths or []) if p.strip()]
        self.window = max(30, int(window or 300))

    def _matched(self, path: str) -> bool:
        return any(path == p or path.startswith(p) for p in self.paths)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.paths:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not self._matched(path):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in (scope.get("headers") or [])}
        timestamp = (headers.get("x-timestamp") or "").strip()
        nonce = (headers.get("x-nonce") or "").strip()
        sign = (headers.get("x-sign") or "").strip()

        # 先读取请求体，校验后再原样回放给后续处理
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        replayed = {"done": False}

        async def receive_replay():
            if not replayed["done"]:
                replayed["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def deny(msg):
            data = json.dumps({"code": 401, "msg": msg}, ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(data)).encode("latin-1"))]})
            await send({"type": "http.response.body", "body": data, "more_body": False})

        if not timestamp or not nonce or not sign:
            await deny("缺少签名请求头 X-Timestamp / X-Nonce / X-Sign")
            return
        ok, reason = sign_utils.verify(self.secret, scope.get("method", "GET"), path,
                                       timestamp, nonce, sign, body, self.window)
        if not ok:
            await deny(reason)
            return
        if not sign_utils.check_nonce(nonce, self.window):
            await deny("nonce 已使用，疑似重放请求")
            return
        await self.app(scope, receive_replay, send)


# ---------------- Token 哈希工具（数据库只保存哈希，不保存明文） ----------------
def hash_token(raw: str) -> str:
    """Token 统一 SHA-256 哈希，长度固定 64 位"""
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def token_hint(raw: str) -> str:
    """生成 Token 掩码，仅用于后台辨认：前 4 位 + **** + 后 4 位"""
    raw = raw or ""
    if len(raw) <= 8:
        return "****"
    return f"{raw[:4]}****{raw[-4:]}"


MASTER_TOKEN_HASH = hash_token(API_TOKEN)


def _ensure_schema():
    """老库轻量迁移：补 is_deleted / token_hint 字段，并把历史明文 Token 转为哈希"""
    from sqlalchemy import inspect
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    def add_column(table, column, ddl):
        if table not in tables or column in {c["name"] for c in insp.get_columns(table)}:
            return
        if engine.dialect.name == "sqlite":
            ddl = re.sub(r"\s+COMMENT\s+'[^']*'", "", ddl)
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE `{table}` ADD COLUMN {ddl}"))
        print(f"[迁移] {table} 新增字段 {column}")

    add_column("api_token", "token_hint",
               "token_hint VARCHAR(32) NULL COMMENT 'Token 掩码，仅用于后台识别'")
    add_column("api_switch", "log_enabled",
               "log_enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否记录调用日志'")
    add_column("api_switch", "cache_ttl",
               "cache_ttl INT NOT NULL DEFAULT 0 COMMENT '独立缓存秒数，0 表示全局默认'")
    add_column("script_run", "pid",
               "pid INT NULL COMMENT '子进程 PID，用于孤儿进程清理'")
    add_column("script_run", "host",
               "host VARCHAR(100) NULL COMMENT '执行实例主机名，多实例下只清理本机进程'")
    add_column("api_token", "grace_until",
               "grace_until DATETIME NULL COMMENT '轮换后旧 Token 的宽限截止时间'")
    add_column("api_token", "user_id",
               "user_id INT NULL COMMENT '所属C端用户ID'")
    add_column("api_log", "user_id",
               "user_id INT NULL COMMENT 'C端用户ID'")
    add_column("api_log", "token_id",
               "token_id INT NULL COMMENT 'Token ID'")
    add_column("c_user", "bonus_quota",
               "bonus_quota INT NOT NULL DEFAULT 0 COMMENT '今日任务奖励额度'")
    add_column("login_log", "source",
               "source VARCHAR(20) NOT NULL DEFAULT 'admin' COMMENT '登录来源：admin/portal'")
    add_column("script_schedule", "task_type",
               "task_type VARCHAR(20) NOT NULL DEFAULT 'script' COMMENT '任务类型：script/backup'")
    add_column("script_schedule", "cron_expr",
               "cron_expr VARCHAR(64) NULL COMMENT 'cron 表达式（分 时 日 月 周）'")
    add_column("script_schedule", "backup_dir",
               "backup_dir VARCHAR(255) NULL COMMENT '备份目录，仅备份任务使用'")
    add_column("sensitive_word", "category",
               "category VARCHAR(50) NOT NULL DEFAULT '默认' COMMENT '分类（树状展示一级分组）'")
    # 数据分类标签：文案 / 笑话 / 名言支持自定义标签，接口可按标签筛选
    add_column("copywriting_lib", "tag",
               "tag VARCHAR(64) NULL COMMENT '分类标签'")
    add_column("local_joke", "tag",
               "tag VARCHAR(64) NULL COMMENT '分类标签'")
    add_column("word_wisdom", "tag",
               "tag VARCHAR(64) NULL COMMENT '分类标签'")
    for model in TABLE_MAP.values():
        add_column(model.__tablename__, "is_deleted",
                   "is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删除标记：0正常 1已删除'")

    for statement in (
            "CREATE INDEX idx_api_token_user ON api_token(user_id)",
            "CREATE INDEX idx_api_log_user_time ON api_log(user_id, time)"):
        try:
            with engine.begin() as conn:
                conn.execute(text(statement))
        except Exception:
            pass

    hex64 = re.compile(r"^[0-9a-f]{64}$")
    try:
        with engine.begin() as conn:
            rows = conn.execute(text("SELECT id, token FROM api_token")).all()
            for rid, raw in rows:
                if not raw or hex64.fullmatch(raw):
                    continue
                conn.execute(text("UPDATE api_token SET token = :h, token_hint = :n WHERE id = :i"),
                             {"h": hash_token(raw), "n": token_hint(raw), "i": rid})
    except Exception as e:
        print(f"[迁移] 明文 Token 转换失败：{e}")


_ensure_schema()
try:
    _seeded = sensitive.ensure_seeded()
    if _seeded:
        print(f"[敏感词] 已写入内置词库 {_seeded} 条")
except Exception as _e:
    print(f"[敏感词] 内置词库初始化失败：{_e}")

# 接口开关缓存：启动时从数据库加载 {path: enabled}
API_SWITCHES = {}
API_LOG_SWITCHES = {}   # {path: 是否记录调用日志}
API_CACHE_TTLS = {}     # {path: 独立缓存秒数，0 表示用全局默认}
try:
    _s = SessionLocal()
    for _row in _s.query(ApiSwitchDB).all():
        API_SWITCHES[_row.path] = bool(_row.enabled)
        API_LOG_SWITCHES[_row.path] = True if _row.log_enabled is None else bool(_row.log_enabled)
        API_CACHE_TTLS[_row.path] = int(_row.cache_ttl or 0)
    _s.close()
except Exception:
    pass

# ===================== Pydantic 请求模型 =====================
class KnowledgeAdd(BaseModel):
    category: str
    title: str
    content: str

class RetortAdd(BaseModel):
    level: str
    content: str

class NameAdd(BaseModel):
    type: str
    text: str

class JokeAdd(BaseModel):
    content: str

class HotAdd(BaseModel):
    title: str
    url: str

class ImageAdd(BaseModel):
    desc: str
    img_url: str

class IdiomAdd(BaseModel):
    word: str
    explain: str

class PoemAdd(BaseModel):
    title: str
    author: str
    content: str

class CopyAdd(BaseModel):
    ctype: str
    content: str

class WisdomAdd(BaseModel):
    tag: str
    content: str

class LoveTalkAdd(BaseModel):
    content: str

class PoisonAdd(BaseModel):
    content: str

class RiddleAdd(BaseModel):
    question: str
    answer: str

class XiehouyuAdd(BaseModel):
    riddle: str
    answer: str

class TwisterAdd(BaseModel):
    content: str

class AnswerBookAdd(BaseModel):
    content: str

class HistoryAdd(BaseModel):
    month_day: str
    year: str
    event: str

class KnowledgeUpdate(BaseModel):
    category: str = None
    title: str = None
    content: str = None

# ===================== 数据库会话依赖 =====================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ===================== Token鉴权与安全配置 =====================
# Token、IP黑白名单、系统配置统一缓存，后台修改后调用 refresh_security(force=True) 立即生效
SECURITY_CACHE_TTL = 30
SECURITY = {"ts": 0.0, "tokens": {}, "black": set(), "white": set(), "settings": {}}
RATE_STATE = {}  # {(scope, key): [窗口开始时间, 次数]}


def refresh_security(force: bool = False):
    """从数据库加载 Token / IP 规则 / 系统配置（默认 30 秒缓存）"""
    now = time.time()
    if not force and now - SECURITY["ts"] < SECURITY_CACHE_TTL:
        return
    db = SessionLocal()
    try:
        # 数据库 token 列存的是哈希，直接以哈希作为键
        tokens = {
            r.token: {"id": r.id, "user_id": r.user_id,
                      "enabled": bool(r.enabled), "permissions": r.permissions or "*",
                      "expires_at": r.expires_at, "grace_until": r.grace_until}
            for r in db.query(ApiTokenDB).all()
        }
        black, white = set(), set()
        for r in db.query(IpRuleDB).filter(IpRuleDB.enabled.is_(True)).all():
            ip = (r.ip or "").strip()
            if ip:
                (black if r.rule_type == "black" else white).add(ip)
        settings = {r.key: r.value for r in db.query(SettingDB).all()}
    except Exception:
        return
    finally:
        db.close()
    SECURITY.update(ts=now, tokens=tokens, black=black, white=white, settings=settings)


def get_setting(key: str, default: str = "") -> str:
    refresh_security()
    return SECURITY["settings"].get(key, default)


def save_setting(key: str, value) -> None:
    """写入系统配置并立即刷新缓存"""
    db = SessionLocal()
    try:
        row = db.query(SettingDB).filter(SettingDB.key == key).first()
        if row:
            row.value = str(value)
        else:
            db.add(SettingDB(key=key, value=str(value)))
        db.commit()
    finally:
        db.close()
    refresh_security(force=True)


def permission_allowed(permissions, path: str) -> bool:
    """判断 Token 的权限范围是否覆盖目标路径"""
    perms = (permissions or "*").strip()
    if perms in ("*", "all", ""):
        return True
    for p in re.split(r"[,，\s]+", perms):
        if p and (path == p or path.startswith(p)):
            return True
    return False


def _rate_overrides():
    """读取单接口独立限流配置：{路径: 每分钟上限}"""
    raw = get_setting("rate_limit_overrides", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    result = {}
    if isinstance(data, dict):
        for path, limit in data.items():
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                continue
            path = str(path).strip()
            if path.startswith("/api/") and limit > 0:
                result[path] = limit
    return result


def _rate_limit_exceeded(scope_key, limit=None):
    """固定 60 秒窗口限流；limit 为空时使用全局配置"""
    if limit is None:
        if get_setting("rate_limit_enabled", "0") != "1":
            return False
        try:
            limit = max(1, int(get_setting("rate_limit_per_minute", "60")))
        except (TypeError, ValueError):
            limit = 60
    # 计数器放在缓存后端：配置 REDIS_URL 后所有实例共享同一窗口，实现全局 QPS 控制
    count = CACHE.incr(f"rl:{scope_key}", ttl=60)
    if count <= 0:
        return False
    return count > limit


# ===================== 接口告警 / 限流日志 =====================
# 60 秒滚动窗口内按接口统计调用次数与 5xx 数量，超过阈值推送企业微信
ALERT_STATE = {}          # {path: {"ts": 窗口开始, "count": 总调用, "err": 5xx 数量}}
ALERT_LAST = {}           # {(path, kind, level): 上次推送时间戳}，用于冷却
ALERT_WINDOWS = {}        # {(path, kind, level): {start, count}}，限制窗口内推送次数
ALERT_THROTTLE_LOCK = threading.Lock()
ALERT_RULES = {"ts": 0.0, "map": {}}   # 单接口规则缓存
ACTIVE_INTERFACE_ERRORS = {}  # {path: {ts, status}}，供主动巡检进行连续异常判断
ACTIVE_INTERFACE_LOCK = threading.Lock()
ALERT_PUSH_STATE = {"last_at": "", "ok": True, "error": ""}   # 最近一次 Webhook 推送结果
ALERT_MAIL_STATE = {"last_at": "", "ok": True, "error": ""}   # 最近一次告警邮件推送结果
LIMIT_STATE = {}          # {ip: [窗口开始, 命中次数]}


def _send_alert(text, webhook=None, mobiles=None):
    """推送告警并记录结果；失败时写入启动日志，便于在「服务监控 → 启动日志」排查"""
    ok, err = mailer.send_wechat(text, webhook=webhook or None, at_mobiles=mobiles)
    ALERT_PUSH_STATE.update(last_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                            ok=bool(ok), error="" if ok else str(err or "未知错误")[:200])
    if not ok:
        log_startup(f"[告警推送失败] {err}")
    return ok, err


def _send_alert_mail(subject, body):
    """发送告警邮件并记录最近一次结果，供智能自愈与告警中心展示"""
    ok, err = mailer.send_mail(subject, body)
    ALERT_MAIL_STATE.update(last_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                            ok=bool(ok), error="" if ok else str(err or "未知错误")[:200])
    if not ok:
        log_startup(f"[告警邮件发送失败] {err}")
    return ok, err


def _alert_rules():
    """读取单接口告警规则（30 秒缓存）：{path: (minute_limit, error_ratio, enabled)}"""
    now = time.time()
    if now - ALERT_RULES["ts"] < 30:
        return ALERT_RULES["map"]
    mapping = {}
    try:
        db = SessionLocal()
        try:
            for row in db.query(AlertRuleDB).all():
                mapping[(row.path or "").strip()] = (
                    int(row.minute_limit or 0), int(row.error_ratio or 0), bool(row.enabled))
        finally:
            db.close()
    except Exception:
        mapping = {}
    ALERT_RULES.update(ts=now, map=mapping)
    return mapping


def _alert_thresholds(path):
    """接口阈值：单接口规则优先，其次全局规则，最后后台全局设置"""
    rules = _alert_rules()
    for key in (path, ""):
        rule = rules.get(key)
        if rule and rule[2]:
            return rule[0], rule[1]
        if rule is not None and not rule[2]:
            return 0, 0
    try:
        minute = int(get_setting("alert_minute_limit", "100") or 100)
        ratio = int(get_setting("alert_error_ratio", "5") or 5)
    except (TypeError, ValueError):
        minute, ratio = 100, 5
    return max(0, minute), max(0, ratio)


def _alert_enabled():
    value = get_setting("alert_enabled", "1" if ALERT_ENABLED else "0")
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _alert_push_time_allowed(now=None):
    """新全局静默时段优先；未配置时兼容旧的允许推送时段设置。"""
    time_pattern = r"(?:[01]\d|2[0-3]):[0-5]\d"
    current = now or datetime.now()
    minute = current.hour * 60 + current.minute
    silent_start = str(get_setting("alert_silent_start", "") or "").strip()
    silent_end = str(get_setting("alert_silent_end", "") or "").strip()
    if silent_start or silent_end:
        if (not re.fullmatch(time_pattern, silent_start)
                or not re.fullmatch(time_pattern, silent_end) or silent_start == silent_end):
            return True
        start_minute = int(silent_start[:2]) * 60 + int(silent_start[3:])
        end_minute = int(silent_end[:2]) * 60 + int(silent_end[3:])
        if start_minute < end_minute:
            in_silent = start_minute <= minute < end_minute
        else:
            in_silent = minute >= start_minute or minute < end_minute
        return not in_silent
    start = str(get_setting("alert_push_start", ALERT_PUSH_START) or ALERT_PUSH_START).strip()
    end = str(get_setting("alert_push_end", ALERT_PUSH_END) or ALERT_PUSH_END).strip()
    if not re.fullmatch(time_pattern, start) or not re.fullmatch(time_pattern, end):
        return True
    if start == end:
        return True
    start_minute = int(start[:2]) * 60 + int(start[3:])
    end_minute = int(end[:2]) * 60 + int(end[3:])
    if start_minute < end_minute:
        return start_minute <= minute <= end_minute
    return minute >= start_minute or minute <= end_minute


def _deliver_alert(event_id, text, subject, body, webhook, mobiles,
                   wechat_enabled, mail_enabled):
    """后台发送通知，并把每个渠道的真实结果回写到本地事件。"""
    if wechat_enabled:
        ok, err = _send_alert(text, webhook, mobiles)
        alert_store.update_notification(event_id, "wechat", ok, err)
    if mail_enabled:
        ok, err = _send_alert_mail(subject, body)
        alert_store.update_notification(event_id, "mail", ok, err)


def _desktop_notify_enabled():
    """桌面通知开关：仅在 Windows 且配置开启时生效，数据库配置优先于环境变量"""
    if os.name != "nt" or not DESKTOP_NOTIFY_ENABLED:
        return False
    return str(get_setting("desktop_notify_enabled", "1")).strip().lower() in ("1", "true", "yes", "on")


def _desktop_notify(title, message, level="warning"):
    """在 Windows 电脑右下角弹出系统通知（经通知中心展示），失败只记日志不影响主流程。
    非 Windows 环境没有桌面弹窗，改为写入启动日志，方便在「系统诊断」里核对通知时机。
    """
    if os.name != "nt":
        if DESKTOP_NOTIFY_ENABLED:
            log_startup(f"[通知] {level} {title}｜{message}")
        return False
    if not _desktop_notify_enabled():
        return False
    if DESKTOP_NOTIFY_MIN_LEVEL == "error" and level not in ("error", "critical"):
        return False
    title = str(title or "API 接口平台")[:64].replace("'", "''")
    message = str(message or "")[:240].replace("\r", " ").replace("\n", " ").replace("'", "''")
    system_icon = {"error": "Error", "critical": "Error", "warning": "Warning"}.get(level, "Information")
    tip_icon = {"error": "Error", "critical": "Error", "warning": "Warning"}.get(level, "Info")
    script = (
        "$ErrorActionPreference='SilentlyContinue'\n"
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "Add-Type -AssemblyName System.Drawing\n"
        "$n=New-Object System.Windows.Forms.NotifyIcon\n"
        f"$n.Icon=[System.Drawing.SystemIcons]::{system_icon}\n"
        "$n.Visible=$true\n"
        f"$n.ShowBalloonTip(8000,'{title}','{message}',"
        f"[System.Windows.Forms.ToolTipIcon]::{tip_icon})\n"
        "Start-Sleep -Milliseconds 9000\n"
        "$n.Dispose()\n"
    )
    try:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-EncodedCommand", encoded],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception as err:
        log_startup(f"[桌面通知发送失败] {str(err)[:160]}")
        return False


def _alert_write(path, kind, value, threshold, message, level=None, event_type=""):
    """事件始终先写本地 JSONL，再按静默、冷却和频控策略发送通知。"""
    if not _alert_enabled():
        return None
    kind_key = {"traffic": "alert_kind_rate", "error": "alert_kind_rate",
                "limit": "alert_kind_rate", "breaker": "alert_kind_breaker",
                "script": "alert_kind_script", "host": "alert_kind_host",
                "battery": "alert_kind_battery", "network": "alert_kind_network"}.get(kind)
    if kind_key and str(get_setting(kind_key, "1")).strip().lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        event_id = alert_store.append_event(path, kind, value, threshold, message, event_type)
    except OSError as err:
        event_id = None
        log_startup(f"[告警本地事件写入失败] {str(err)[:160]}")
    try:
        cooldown = max(30, int(get_setting("alert_cooldown", str(ALERT_COOLDOWN)) or ALERT_COOLDOWN))
        if kind == "battery" and "恢复" not in str(path):
            cooldown = max(60, int(get_setting("battery_notify_minutes", "10") or 10) * 60)
        repeat_window = max(60, int(get_setting(
            "alert_repeat_window", str(ALERT_REPEAT_WINDOW)) or ALERT_REPEAT_WINDOW))
        if kind == "battery" and "恢复" not in str(path):
            repeat_window = cooldown
        repeat_limit = min(20, max(1, int(get_setting(
            "alert_repeat_limit", str(ALERT_REPEAT_LIMIT)) or ALERT_REPEAT_LIMIT)))
    except (TypeError, ValueError):
        cooldown, repeat_window, repeat_limit = 300, 600, 1
    level = level or {"breaker": "high", "error": "high", "host": "high",
                      "network": "high", "ip": "high", "script": "low",
                      "battery": "low", "traffic": "low", "limit": "low",
                      "token": "low"}.get(kind, "normal")
    if not _alert_push_time_allowed():
        alert_store.set_notification(event_id, "静默未发")
        return event_id
    key = (path, kind, level)
    now = time.time()
    with ALERT_THROTTLE_LOCK:
        if now - ALERT_LAST.get(key, 0) < cooldown:
            alert_store.set_notification(event_id, "冷却未发")
            return event_id
        if not alert_store.reserve_notification(key, cooldown):
            alert_store.set_notification(event_id, "跨进程冷却未发")
            return event_id
        window = ALERT_WINDOWS.get(key)
        if window is None or now - window["start"] >= repeat_window:
            window = {"start": now, "count": 0}
            ALERT_WINDOWS[key] = window
        if window["count"] >= repeat_limit:
            alert_store.set_notification(event_id, "频控未发")
            return event_id
        ALERT_LAST[key] = now
        window["count"] += 1
    common_webhook = get_setting("alert_webhook", "")
    if level in ("high", "critical"):
        webhook = common_webhook or get_setting("alert_webhook_high", ALERT_WEBHOOK_HIGH)
        mobiles = get_setting("alert_at_mobiles", ALERT_AT_MOBILES)
        prefix = "【重大异常】" if level == "critical" else "【高优告警】"
    else:
        webhook = common_webhook or get_setting("alert_webhook_low", ALERT_WEBHOOK_LOW)
        mobiles = ""
        prefix = "【API 平台告警】"
    text = f"{prefix}{message}"
    wechat_enabled = str(get_setting(
        "alert_wechat_enabled", "1" if ALERT_WECHAT_ENABLED else "0")).strip().lower() in (
            "1", "true", "yes", "on")
    try:
        mail_enabled = bool(mailer.mail_config().get("enabled"))
    except Exception:
        mail_enabled = False
    if not wechat_enabled and not mail_enabled:
        alert_store.set_notification(event_id, "未通知")
        return event_id
    subject = f"{prefix}{path} {kind} 告警"
    body = (f"{text}\n\n告警对象：{path}\n告警类型：{kind}\n"
            f"实测值：{value}\n阈值：{threshold}\n"
            f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    alert_store.set_notification(event_id, "通知发送中")
    threading.Thread(target=_deliver_alert,
                     args=(event_id, text, subject, body, webhook, mobiles,
                           wechat_enabled, mail_enabled), daemon=True).start()
    return event_id


def _alert_track(path, status_code):
    """统计调用频率，并把 5xx 交给主动巡检做连续异常确认"""
    now = time.time()
    with ACTIVE_INTERFACE_LOCK:
        if status_code >= 500:
            ACTIVE_INTERFACE_ERRORS[path] = {"ts": now, "status": int(status_code)}
        else:
            ACTIVE_INTERFACE_ERRORS.pop(path, None)
    if not _alert_enabled():
        return
    state = ALERT_STATE.get(path)
    if not state or now - state["ts"] >= 60:
        state = {"ts": now, "count": 0, "err": 0, "traffic": False, "error": False}
        ALERT_STATE[path] = state
    state["count"] += 1
    if status_code >= 500:
        state["err"] += 1
    minute_limit, error_ratio = _alert_thresholds(path)
    if minute_limit and state["count"] > minute_limit and not state["traffic"]:
        state["traffic"] = True
        _alert_write(path, "traffic", state["count"], minute_limit,
                     f"{path} 一分钟内调用 {state['count']} 次，超过阈值 {minute_limit} 次")
    if error_ratio and state["count"] >= 5:
        ratio = state["err"] * 100.0 / state["count"]
        if ratio >= error_ratio:
            # 只保留统计状态；接口 5xx 由独立巡检连续确认后再自愈和通知
            state["error"] = True


def _record_rate_limit(ip, path, token_hint_value=""):
    """记录被限流的请求；同一 IP 10 分钟内频繁触发时推送告警"""
    now = time.time()
    try:
        db = SessionLocal()
        try:
            db.add(RateLimitLogDB(ip=ip[:100], path=path[:200],
                                  token_hint=(token_hint_value or "")[:32]))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass
    window, hits = LIMIT_STATE.get(ip, [now, 0])
    if now - window >= 600:
        window, hits = now, 0
    hits += 1
    LIMIT_STATE[ip] = [window, hits]
    if hits in (20, 60, 120) and _alert_enabled():
        _alert_write(path, "limit", hits, 20,
                     f"IP {ip} 频繁触发限流（10 分钟 {hits} 次），疑似爬虫或异常访问")


def _ip_geo(ip):
    """查询 IP 归属国家（缓存 24 小时）；失败返回空字符串，不阻塞请求"""
    code = CACHE.get(f"geo:{ip}")
    if code:
        return code
    try:
        resp = requests.get(f"http://ip-api.com/json/{ip}?fields=status,countryCode",
                            timeout=3, headers=NET_HEADERS)
        data = resp.json()
        code = data.get("countryCode") if data.get("status") == "success" else ""
    except Exception:
        code = ""
    if code:
        CACHE.set(f"geo:{ip}", code, ttl=86400)
    return code


def _ip_guard(ip, path):
    """IP 风控：突发高频冻结 + 可选地理位置漂移检测；返回 False 表示拒绝本次请求"""
    if CACHE.get(f"ipf:{ip}"):
        return False
    if IP_BURST_LIMIT > 0:
        count = CACHE.incr(f"ipm:{ip}", ttl=60)
        if count > IP_BURST_LIMIT:
            CACHE.set(f"ipf:{ip}", 1, ttl=IP_FREEZE_SECONDS)
            _alert_write(f"ip:{ip}", "ip", count, IP_BURST_LIMIT,
                         f"IP {ip} 一分钟内请求 {count} 次，超过阈值 {IP_BURST_LIMIT}，"
                         f"已冻结 {IP_FREEZE_SECONDS} 秒", level="high")
            return False
    if IP_GEO_ENABLED:
        code = _ip_geo(ip)
        if code:
            last = CACHE.get(f"gel:{ip}")
            if last and last != code:
                CACHE.set(f"ipf:{ip}", 1, ttl=IP_FREEZE_SECONDS)
                _alert_write(f"ip:{ip}", "ip", 1, 1,
                             f"IP {ip} 地理位置漂移：{last} → {code}，已冻结 {IP_FREEZE_SECONDS} 秒",
                             level="high")
                return False
            CACHE.set(f"gel:{ip}", code, ttl=max(1, IP_GEO_MINUTES) * 60)
    return True


def _ban_c_token(token_id: int, reason: str):
    """异常调用立即封禁 Token，并同步当前安全缓存"""
    try:
        db = SessionLocal()
        try:
            row = db.query(ApiTokenDB).filter(ApiTokenDB.id == token_id).first()
            if row is not None:
                row.enabled = False
                row.remark = ((row.remark or "") + f" [自动封禁：{reason}]")[:255]
                db.commit()
        finally:
            db.close()
    except Exception:
        pass
    for info in SECURITY["tokens"].values():
        if info.get("id") == token_id:
            info["enabled"] = False
    _alert_write(f"token-id:{token_id}", "token", 1, 1,
                 f"C 端 Token ID {token_id} 因{reason}已自动封禁", level="critical")


def _consume_daily_quota(user_id: int, path: str = ""):
    """按用户原子扣减累计额度并记录流水；跨日不清零，并发请求不能突破上限"""
    db = SessionLocal()
    try:
        updated = db.query(CUserDB).filter(
            CUserDB.id == user_id,
            CUserDB.status == "active",
            CUserDB.today_used < CUserDB.daily_quota + CUserDB.bonus_quota,
        ).update({CUserDB.today_used: CUserDB.today_used + 1}, synchronize_session=False)
        if updated:
            row = db.query(CUserDB).filter(CUserDB.id == user_id).first()
            remaining = max(0, int(row.daily_quota or 0) + int(row.bonus_quota or 0)
                            - int(row.today_used or 0))
            record_quota_change(
                db, user_id, -1, "api_call", "接口调用扣减", f"调用接口 {path or '未知路径'}",
                path=path, balance_after=remaining)
            db.commit()
            return
        db.rollback()
        row = db.query(CUserDB).filter(CUserDB.id == user_id).first()
        if row is None or row.status != "active":
            raise HTTPException(status_code=403, detail="账号不可用或已被封禁")
        raise HTTPException(status_code=429, detail="额度已用完")
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(status_code=503, detail="额度服务暂不可用，请稍后重试")
    finally:
        db.close()


def _c_token_guard(request: Request, token_id: int, user_id: int):
    """C 端业务 Token 的异常封禁、单用户 QPS 与累计额度控制"""
    request.state.api_token_id = token_id
    request.state.api_user_id = user_id
    if PORTAL_TOKEN_ABUSE_LIMIT > 0:
        hits = CACHE.incr(f"token-abuse:{token_id}", ttl=max(1, PORTAL_TOKEN_ABUSE_WINDOW))
        if hits > PORTAL_TOKEN_ABUSE_LIMIT:
            _ban_c_token(token_id, "短时间异常高频调用")
            raise HTTPException(status_code=403, detail="Token 因异常调用已被封禁")
    if PORTAL_USER_QPS > 0:
        hits = CACHE.incr(f"user-qps:{user_id}", ttl=1)
        if hits > PORTAL_USER_QPS:
            raise HTTPException(status_code=429, detail="请求过于频繁，单用户每秒最多 2 次")
    _consume_daily_quota(user_id, request.url.path)


def _portal_test_identity(request: Request, path: str):
    """校验新手文档内部测试签名，外部请求无法冒充其他 C 端用户"""
    user_id = request.headers.get("x-portal-test-user", "")
    timestamp = request.headers.get("x-portal-test-timestamp", "")
    signature = request.headers.get("x-portal-test-signature", "")
    if not user_id.isdigit() or not timestamp.isdigit() or not signature:
        return None
    try:
        if abs(time.time() - int(timestamp)) > 60:
            return None
    except ValueError:
        return None
    expected = hmac.new(
        str(PORTAL_SECRET).encode("utf-8"), f"{int(user_id)}:{path}:{int(timestamp)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return int(user_id) if hmac.compare_digest(expected, signature) else None


def verify_token(request: Request, token: str = Header(None, max_length=128)):
    """Token鉴权：权限、有效期、C端额度/QPS、按分钟限流与IP风控"""
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="缺少 Token，拒绝访问")
    refresh_security()
    path = request.url.path
    token_hash = hash_token(token)
    if hmac.compare_digest(token_hash, MASTER_TOKEN_HASH):
        portal_user_id = _portal_test_identity(request, path)
        if portal_user_id is None:
            permissions = "*"
        else:
            db = SessionLocal()
            try:
                token_row = db.query(ApiTokenDB).filter(
                    ApiTokenDB.user_id == portal_user_id, ApiTokenDB.enabled.is_(True),
                ).order_by(ApiTokenDB.id).first()
                user_row = db.query(CUserDB).filter(
                    CUserDB.id == portal_user_id, CUserDB.status == "active").first()
            finally:
                db.close()
            if token_row is None or user_row is None:
                raise HTTPException(status_code=401, detail="当前账号 Token 不可用，请在控制台重置")
            if token_row.expires_at and token_row.expires_at < datetime.now():
                raise HTTPException(status_code=401, detail="Token已过期，请在控制台重置")
            permissions = token_row.permissions or "*"
            _c_token_guard(request, token_row.id, portal_user_id)
    else:
        info = SECURITY["tokens"].get(token_hash)
        if not info or not info["enabled"]:
            raise HTTPException(status_code=401, detail="Token无效，拒绝访问")
        expires_at = info["expires_at"]
        if expires_at is not None and expires_at < datetime.now():
            grace_until = info.get("grace_until")
            if grace_until is None or grace_until < datetime.now():
                raise HTTPException(status_code=401, detail="Token已过期，拒绝访问")
            # 宽限期内旧 Token 仍可用，但每次使用都告警，提醒尽快替换
            _alert_write(f"token:{token_hint(token)}", "token", 1, 1,
                         f"旧 Token {token_hint(token)} 仍在宽限期内使用，"
                         f"{grace_until.strftime('%Y-%m-%d %H:%M')} 后失效", level="low")
        permissions = info["permissions"]
        if info.get("user_id") is not None:
            _c_token_guard(request, info["id"], info["user_id"])
    if not permission_allowed(permissions, path):
        raise HTTPException(status_code=403, detail="该 Token 无权访问此接口")
    if not _ip_guard(_client_ip(request), path):
        raise HTTPException(status_code=403, detail="请求频率异常，该 IP 已被临时冻结")

    scope = get_setting("rate_limit_scope", "token")
    identity = token_hash if scope == "token" else _client_ip(request)
    path_limit = _rate_overrides().get(path)
    # 单接口独立阈值优先于全局限流，避免外网接口被高频调用
    scope_key = ("path", path, identity) if path_limit else (scope, identity)
    if _rate_limit_exceeded(scope_key, limit=path_limit):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    return True


def clean_old_logs(days: int) -> int:
    """按天数清理调用日志，返回删除条数"""
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
    db = SessionLocal()
    try:
        deleted = db.query(ApiLogDB).filter(ApiLogDB.time < cutoff).delete(synchronize_session=False)
        db.commit()
        return deleted
    except Exception:
        db.rollback()
        return 0
    finally:
        db.close()


def clean_logs_by_capacity(max_rows: int) -> int:
    """容量兜底：日志超过上限时优先删除最早的记录，返回删除条数"""
    if max_rows <= 0:
        return 0
    db = SessionLocal()
    try:
        total = db.query(func.count(ApiLogDB.id)).scalar() or 0
        if total <= max_rows:
            return 0
        cutoff_id = (db.query(ApiLogDB.id).order_by(ApiLogDB.id.asc())
                     .offset(total - max_rows - 1).limit(1).scalar())
        if not cutoff_id:
            return 0
        deleted = db.query(ApiLogDB).filter(ApiLogDB.id <= cutoff_id).delete(
            synchronize_session=False)
        db.commit()
        return deleted
    except Exception:
        db.rollback()
        return 0
    finally:
        db.close()


def _auto_clean_loop():
    """每 6 小时按配置清理过期日志；容量超限时优先删除最早日志"""
    time.sleep(60)
    while True:
        try:
            if get_setting("log_clean_enabled", "0") == "1":
                days = max(1, int(get_setting("log_clean_days", "30") or 30))
                deleted = clean_old_logs(days)
                save_setting("log_last_clean", time.strftime("%Y-%m-%d %H:%M:%S"))
                if deleted:
                    save_setting("log_last_deleted", deleted)
            try:
                max_rows = int(get_setting("log_max_rows", "0") or 0)
            except (TypeError, ValueError):
                max_rows = 0
            if max_rows > 0:
                removed = clean_logs_by_capacity(max_rows)
                if removed:
                    save_setting("log_last_capacity_clean",
                                 time.strftime("%Y-%m-%d %H:%M:%S"))
                    save_setting("log_last_capacity_deleted", removed)
            # 备份文件自动清理：按保留天数删除过期备份
            if get_setting("backup_clean_enabled", "0") == "1":
                from app.services.backup import prune_backup_dir
                try:
                    bdays = max(1, int(get_setting(
                        "backup_clean_days", str(BACKUP_KEEP_DAYS)) or BACKUP_KEEP_DAYS))
                except (TypeError, ValueError):
                    bdays = max(1, BACKUP_KEEP_DAYS)
                bdir = get_setting("backup_clean_dir", "") or None
                removed_files = prune_backup_dir(bdir, bdays)
                save_setting("backup_last_clean", time.strftime("%Y-%m-%d %H:%M:%S"))
                save_setting("backup_last_deleted", len(removed_files))
        except Exception:
            pass
        # 过期导出文件 / 限流日志 / 告警记录 / 定时任务执行记录自动清理
        try:
            _clean_monitor_records()
        except Exception:
            pass
        time.sleep(6 * 3600)


def _clean_monitor_records():
    """清理监控类历史数据，避免表无限增长"""
    from app.core.config import EXPORT_JOB_KEEP_DAYS, POOL_METRIC_KEEP_HOURS
    now = datetime.now()
    try:
        db = SessionLocal()
        try:
            # 导出任务：过期删除记录并删除文件
            for row in db.query(ExportJobDB).filter(
                    ExportJobDB.expires_at.isnot(None),
                    ExportJobDB.expires_at < now).all():
                try:
                    if row.file_path and os.path.exists(row.file_path):
                        os.remove(row.file_path)
                except Exception:
                    pass
                db.delete(row)
            # C 端抽样日志只保留近 7 天；管理 Token / 主 Token 日志走原有清理策略
            db.query(ApiLogDB).filter(
                ApiLogDB.user_id.isnot(None),
                ApiLogDB.time < now - timedelta(days=max(1, PORTAL_API_LOG_KEEP_DAYS)),
            ).delete(synchronize_session=False)
            # 限流日志保留 30 天，告警记录保留 90 天
            db.query(RateLimitLogDB).filter(
                RateLimitLogDB.created_at < now - timedelta(days=30)).delete(
                synchronize_session=False)
            db.query(AlertEventDB).filter(
                AlertEventDB.created_at < now - timedelta(days=90)).delete(
                synchronize_session=False)
            db.query(BreakerEventDB).filter(
                BreakerEventDB.created_at < now - timedelta(days=90)).delete(
                synchronize_session=False)
            db.query(PoolMetricDB).filter(
                PoolMetricDB.created_at < now - timedelta(
                    hours=max(1, int(POOL_METRIC_KEEP_HOURS)))).delete(
                synchronize_session=False)
            db.query(ScheduleRunDB).filter(
                ScheduleRunDB.started_at < now - timedelta(days=30)).delete(
                synchronize_session=False)
            # 已失效的后台会话清理
            db.query(AdminSessionDB).filter(
                AdminSessionDB.last_seen < now - timedelta(hours=24)).delete(
                synchronize_session=False)
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def collect_pool_metric():
    """采样一次连接池指标并入库，供概览页绘制历史曲线"""
    try:
        pool = engine.pool
        checked_out = int(pool.checkedout())
        capacity = max(1, int(DB_POOL_SIZE) + int(DB_MAX_OVERFLOW))
        db = SessionLocal()
        try:
            db.add(PoolMetricDB(
                size=int(getattr(pool, "size", lambda: DB_POOL_SIZE)()),
                checked_out=checked_out,
                overflow=int(pool.overflow()),
                capacity=capacity,
                usage_percent=round(checked_out * 100.0 / capacity, 1)))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def _pool_metric_loop():
    from app.core.config import POOL_METRIC_INTERVAL
    while True:
        time.sleep(max(20, int(POOL_METRIC_INTERVAL)))
        try:
            collect_pool_metric()
        except Exception:
            pass


# ===================== 自愈巡检（MySQL / 磁盘 / 连接池） =====================
# 每隔一段时间自检关键资源，超过阈值时推送告警并自动执行修复动作：
#   MySQL 不可用     -> 告警 + 重建连接池
#   磁盘剩余 < 10%   -> 告警 + 清理过期日志与旧备份
#   连接池占用 >= 85% -> 告警 + 释放空闲连接（engine.dispose）
SELF_HEAL_STATE = {"last_run": "", "last_result": "尚未执行", "actions": 0,
                   "failures": {}, "critical": []}
_SELF_HEAL_LAST = {}          # {动作名: 上次执行时间戳}，避免重复触发
_SELF_HEAL_FAILURES = {}      # {问题维度: 连续处理且复检失败的次数}
_SELF_HEAL_CRITICAL = set()   # 已推送重大异常的问题，恢复前不重复推送
_SELF_HEAL_LOCK = threading.Lock()
_MYSQL_PROBE_FAILURES = 0


def _self_heal_cooldown():
    try:
        return max(60, int(get_setting("self_heal_cooldown", "600") or 600))
    except (TypeError, ValueError):
        return 600


def _self_heal_max_attempts():
    try:
        return min(20, max(1, int(get_setting("self_heal_max_attempts", "3") or 3)))
    except (TypeError, ValueError):
        return 3


def _self_heal_auto_enabled():
    value = get_setting("self_heal_enabled", "1")
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ===================== 主动检测 -> 自愈 -> 重试 -> 聚合通知 =====================
HEALING_SCENARIOS = {
    "interface": {"label": "接口异常 / 5xx", "kind": "error", "category": "alert_kind_rate",
                  "action": "重建接口连接并重置外部数据源熔断，恢复备用数据选择"},
    "script": {"label": "脚本失败", "kind": "script", "category": "alert_kind_script",
               "action": "按原参数安全拉起失败脚本（禁止 shell 拼接）"},
    "memory": {"label": "内存过高", "kind": "host", "category": "alert_kind_host",
               "action": "释放数据库空闲连接并执行 Python 垃圾回收"},
}
HEALING_LOCK = threading.RLock()
SCRIPT_SELF_HEAL_RUNNER = None
HEALING_SCRIPT_BASELINE = None


def _healing_new_state():
    return {"consecutive": 0, "active": False, "locked": False, "retries": 0,
            "retry_pending": False, "last_retry": 0.0, "last_notify": 0.0,
            "status": "等待巡检", "detail": "", "last_action": "", "last_recovery": "",
            "last_run": "", "current_count": 0, "current_value": 0, "threshold": 3,
            "targets": []}


HEALING_STATE = {key: _healing_new_state() for key in HEALING_SCENARIOS}


def _persist_alert_runtime():
    """将巡检进度和重试计数原子写入本地 JSON，不依赖数据库或 Redis。"""
    try:
        with HEALING_LOCK:
            healing = {key: dict(value) for key, value in HEALING_STATE.items()}
        with _SELF_HEAL_LOCK:
            self_heal = {
                "last": dict(_SELF_HEAL_LAST),
                "failures": dict(_SELF_HEAL_FAILURES),
                "critical": sorted(_SELF_HEAL_CRITICAL),
            }
        host_state = globals().get("HOST_DEVICE_STATE", {})
        alert_store.save_runtime({
            "version": 1,
            "mysql_probe_failures": int(globals().get("_MYSQL_PROBE_FAILURES", 0) or 0),
            "battery_alert_active": bool(globals().get("_BATTERY_ALERT_ACTIVE", False)),
            "healing": healing,
            "self_heal": self_heal,
            "self_heal_state": dict(SELF_HEAL_STATE),
            "host_device_state": {
                key: dict(value) for key, value in host_state.items()
                if isinstance(value, dict)
            },
        })
    except Exception as err:
        log_startup(f"[告警本地状态保存失败] {str(err)[:160]}")


def _restore_alert_runtime():
    """服务重启后恢复未完成的重试，避免计数清零导致重复自动处理。"""
    global _MYSQL_PROBE_FAILURES, _NETWORK_FAILURES
    global _NETWORK_ALERT_ACTIVE, _BATTERY_ALERT_ACTIVE
    try:
        data = alert_store.load_runtime()
        if not data:
            return
        _MYSQL_PROBE_FAILURES = max(0, int(data.get("mysql_probe_failures") or 0))
        network_state = data.get("network_state") or {}
        if isinstance(network_state, dict):
            _NETWORK_FAILURES = max(0, int(network_state.get("failures") or 0))
            _NETWORK_ALERT_ACTIVE = bool(network_state.get("alert_active"))
        else:
            _NETWORK_FAILURES = max(0, int(data.get("network_failures") or 0))
            _NETWORK_ALERT_ACTIVE = bool(data.get("network_alert_active"))
        _BATTERY_ALERT_ACTIVE = bool(data.get("battery_alert_active"))
        healing = data.get("healing") or {}
        with HEALING_LOCK:
            for scene, saved in healing.items():
                if scene in HEALING_STATE and isinstance(saved, dict):
                    allowed = _healing_new_state()
                    HEALING_STATE[scene].update({
                        key: value for key, value in saved.items() if key in allowed})
        saved_self = data.get("self_heal") or {}
        with _SELF_HEAL_LOCK:
            _SELF_HEAL_LAST.update({
                str(key): float(value) for key, value in (saved_self.get("last") or {}).items()
                if isinstance(value, (int, float))})
            _SELF_HEAL_FAILURES.update({
                str(key): max(0, int(value))
                for key, value in (saved_self.get("failures") or {}).items()
                if isinstance(value, (int, float))})
            _SELF_HEAL_CRITICAL.update(str(key) for key in saved_self.get("critical") or [])
        saved_state = data.get("self_heal_state") or {}
        if isinstance(saved_state, dict):
            SELF_HEAL_STATE.update({
                key: saved_state[key] for key in ("last_run", "last_result", "actions")
                if key in saved_state})
        saved_host = data.get("host_device_state") or {}
        for key in ("battery", "network"):
            if isinstance(saved_host.get(key), dict):
                HOST_DEVICE_STATE[key].update(saved_host[key])
        log_startup("告警中心：已从本地 JSON 恢复巡检与重试状态")
    except Exception as err:
        log_startup(f"[告警本地状态恢复失败] {str(err)[:160]}")


def _self_heal_interval():
    from app.core.config import SELF_HEAL_INTERVAL
    try:
        value = int(get_setting("self_heal_interval", str(SELF_HEAL_INTERVAL)) or SELF_HEAL_INTERVAL)
    except (TypeError, ValueError):
        value = SELF_HEAL_INTERVAL
    return max(10, min(3600, value))


def _healing_config(scene):
    meta = HEALING_SCENARIOS[scene]

    def number(suffix, default, low, high):
        try:
            value = int(get_setting(f"heal_{scene}_{suffix}", str(default)) or default)
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def flag(suffix, default):
        value = get_setting(f"heal_{scene}_{suffix}", default)
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    try:
        mail_default = "1" if mailer.mail_config().get("enabled") else "0"
    except Exception:
        mail_default = "0"
    return {
        "enabled": flag("enabled", "1"),
        "max_retries": number("max_retries", 3, 1, 20),
        "retry_interval": number("retry_interval", 30 if scene == "script" else 60, 10, 86400),
        "wechat": flag("wechat", "1"),
        "mail": flag("mail", mail_default),
        "notify_minutes": number("notify_minutes", 5, 1, 1440),
        "quiet_start": str(get_setting(f"heal_{scene}_quiet_start", "00:00") or "00:00"),
        "quiet_end": str(get_setting(f"heal_{scene}_quiet_end", "00:00") or "00:00"),
        "action": meta["action"], "label": meta["label"], "kind": meta["kind"],
    }


def _healing_settings_snapshot():
    result = {}
    with HEALING_LOCK:
        states = {key: dict(value) for key, value in HEALING_STATE.items()}
    for scene, config in ((key, _healing_config(key)) for key in HEALING_SCENARIOS):
        state = states[scene]
        state["targets"] = [dict(item) for item in state.get("targets", [])]
        state.pop("last_retry", None)
        state.pop("last_notify", None)
        result[scene] = dict(config, state=state)
    return result


def _healing_time_allowed(config, now=None):
    """场景静默期只抑制普通告警；开始与结束相同表示不设置静默期"""
    start = str(config.get("quiet_start") or "00:00").strip()
    end = str(config.get("quiet_end") or "08:00").strip()
    pattern = r"(?:[01]\d|2[0-3]):[0-5]\d"
    if not re.fullmatch(pattern, start) or not re.fullmatch(pattern, end) or start == end:
        return True
    current = now or datetime.now()
    minute = current.hour * 60 + current.minute
    start_minute = int(start[:2]) * 60 + int(start[3:])
    end_minute = int(end[:2]) * 60 + int(end[3:])
    if start_minute < end_minute:
        in_quiet = start_minute <= minute < end_minute
    else:
        in_quiet = minute >= start_minute or minute < end_minute
    return not in_quiet


def _healing_record(scene, kind, message, value=0, threshold=0,
                    event_type="", notify_status="未通知"):
    """主动巡检事件只写本地文件，MySQL 断开不影响记录与重试。"""
    try:
        return alert_store.append_event(
            f"healing:{scene}", kind, value, threshold, message,
            event_type=event_type, notify_status=notify_status)
    except OSError as err:
        log_startup(f"[自愈本地事件写入失败] {str(err)[:160]}")
        return None


def _deliver_healing(channel, event_ids, text, subject, webhook, mobiles):
    """聚合通知只发送一次，但将结果回写到每个关联事件。"""
    if channel == "wechat":
        ok, err = _send_alert(text, webhook, mobiles)
    else:
        ok, err = _send_alert_mail(subject, f"{text}\n\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    for event_id in event_ids:
        alert_store.update_notification(event_id, channel, ok, err)


def _healing_send(items, recovery=False):
    """按场景渠道合并发送；每个事件独立记录静默状态和实际发送结果。"""
    true_values = ("1", "true", "yes", "on")
    wechat_global = str(get_setting(
        "alert_wechat_enabled", "1" if ALERT_WECHAT_ENABLED else "0")).strip().lower() in true_values
    try:
        mail_global = bool(mailer.mail_config().get("enabled"))
    except Exception:
        mail_global = False
    eligible = []
    for scene, message in items:
        config = _healing_config(scene)
        state = HEALING_STATE[scene]
        event_id = _healing_record(
            scene, "heal" if recovery else config["kind"], message,
            state.get("current_value"), state.get("threshold"),
            event_type="recovered" if recovery else "heal_failed")
        if not _alert_enabled():
            alert_store.set_notification(event_id, "未通知")
            continue
        category = HEALING_SCENARIOS[scene]["category"]
        if str(get_setting(category, "1")).strip().lower() not in true_values:
            alert_store.set_notification(event_id, "未通知")
            continue
        if not recovery and (not _healing_time_allowed(config) or not _alert_push_time_allowed()):
            alert_store.set_notification(event_id, "静默未发")
            continue
        wechat = bool(config["wechat"] and wechat_global)
        mail = bool(config["mail"] and mail_global)
        if not wechat and not mail:
            alert_store.set_notification(event_id, "未通知")
            continue
        alert_store.set_notification(event_id, "通知发送中")
        eligible.append((scene, message, config, event_id, wechat, mail))
    if not eligible:
        return False

    def build_message(group):
        if recovery:
            return f"【✅ 已恢复】{group[0][1]}", "【API 平台】已恢复"
        text = (f"【智能自愈告警】{len(group)} 项异常已达到重试上限\n" +
                "\n".join(f"• {item[1]}" for item in group) +
                "\n系统已锁定自动重试，请人工排查；未恢复前将按各场景频率提醒。")
        return text, f"【API 平台】{len(group)} 项智能自愈告警"

    wechat_items = [item for item in eligible if item[4]]
    mail_items = [item for item in eligible if item[5]]
    common_webhook = get_setting("alert_webhook", "")
    if wechat_items:
        text, subject = build_message(wechat_items)
        high = any(item[0] != "script" for item in wechat_items)
        webhook = common_webhook or get_setting(
            "alert_webhook_high" if high else "alert_webhook_low",
            ALERT_WEBHOOK_HIGH if high else ALERT_WEBHOOK_LOW)
        mobiles = get_setting("alert_at_mobiles", ALERT_AT_MOBILES) if high else ""
        threading.Thread(target=_deliver_healing,
                         args=("wechat", [item[3] for item in wechat_items], text,
                               subject, webhook, mobiles), daemon=True).start()
    if mail_items:
        text, subject = build_message(mail_items)
        threading.Thread(target=_deliver_healing,
                         args=("mail", [item[3] for item in mail_items], text,
                               subject, "", ""), daemon=True).start()
    return True


def _probe_interface():
    now = time.time()
    with ACTIVE_INTERFACE_LOCK:
        recent = {path: dict(info) for path, info in ACTIVE_INTERFACE_ERRORS.items()
                  if now - info.get("ts", 0) <= max(90, _self_heal_interval() * 3)}
    if recent:
        paths = "、".join(sorted(recent)[:3])
        return False, f"最近检测到 {len(recent)} 个接口 5xx：{paths}", {
            "count": len(recent), "value": len(recent), "threshold": 1, "pending": False}
    try:
        response = health_check()
        if int(getattr(response, "status_code", 200)) >= 500:
            return False, "/health 探活返回 503，接口依赖异常", {
                "count": 1, "value": 503, "threshold": 500, "pending": False}
    except Exception as err:
        return False, f"/health 探活失败：{str(err)[:120]}", {
            "count": 1, "value": 500, "threshold": 500, "pending": False}
    return True, "/health 探活正常，未发现持续 5xx", {"count": 0, "value": 200, "threshold": 500}


def _probe_scripts():
    global HEALING_SCRIPT_BASELINE
    db = SessionLocal()
    try:
        rows = db.query(ScriptRunDB).order_by(ScriptRunDB.id.desc()).limit(200).all()
    finally:
        db.close()
    latest = {}
    for row in rows:
        key = row.script_key or ""
        if key and key not in latest:
            latest[key] = row
    if HEALING_SCRIPT_BASELINE is None:
        # 启动时只建立基线，不自动重跑历史失败任务，避免服务重启产生副作用
        HEALING_SCRIPT_BASELINE = {key: int(row.id) for key, row in latest.items()}
        return True, "已建立脚本运行基线，仅监测后续新失败", {"count": 0, "value": 0, "threshold": 1}
    failures = []
    running = []
    for key, row in latest.items():
        if row.state == "failed" and int(row.id) > int(HEALING_SCRIPT_BASELINE.get(key, 0)):
            failures.append({"key": key, "args": row.args or "", "run_id": int(row.id),
                             "output": (row.output or "")[-200:]})
        elif row.state == "running":
            running.append(key)
    active = bool(HEALING_STATE["script"].get("active"))
    if failures or (active and running):
        details = []
        if failures:
            details.append("失败：" + "、".join(item["key"] for item in failures[:4]))
        if active and running:
            details.append("重试运行中：" + "、".join(running[:4]))
        return False, "；".join(details), {
            "count": len(failures), "value": len(failures), "threshold": 1,
            "pending": bool(active and running), "targets": failures}
    return True, "未发现新的脚本失败", {"count": 0, "value": 0, "threshold": 1, "targets": []}


def _probe_memory():
    try:
        import psutil
        percent = float(psutil.virtual_memory().percent)
    except Exception as err:
        return False, f"内存采集失败：{str(err)[:120]}", {
            "count": 1, "value": 100, "threshold": ALERT_HOST_PERCENT, "pending": False}
    try:
        limit = float(get_setting("alert_memory_percent", get_setting("alert_host_percent", str(ALERT_HOST_PERCENT))) or ALERT_HOST_PERCENT)
    except (TypeError, ValueError):
        limit = float(ALERT_HOST_PERCENT)
    limit = max(1.0, min(100.0, limit))
    unhealthy = percent >= limit
    return not unhealthy, f"内存使用率 {percent:.1f}%（阈值 {limit:.0f}%）", {
        "count": 1 if unhealthy else 0, "value": int(round(percent)),
        "threshold": int(round(limit)), "pending": False}


def _healing_action(scene, state):
    if scene == "interface":
        reset_breaker()
        engine.dispose(close=False)
        return "已重建接口连接并重置外部数据源熔断", False
    if scene == "memory":
        import gc
        collected = gc.collect()
        engine.dispose(close=False)
        return f"已释放数据库空闲连接并执行垃圾回收（回收 {collected} 个对象）", False
    runner = SCRIPT_SELF_HEAL_RUNNER
    if runner is None:
        raise RuntimeError("脚本拉起器尚未就绪")
    results = []
    started = 0
    for target in state.get("targets", []):
        ok, detail = runner(target["key"], target.get("args") or "")
        results.append(detail)
        started += 1 if ok else 0
    if not started:
        raise RuntimeError("；".join(results)[:240] or "没有可拉起的失败脚本")
    return f"已安全拉起 {started} 个失败脚本：" + "；".join(results)[:240], True


def _active_healing_once():
    """连续 3 次异常才确认；重试耗尽后锁定并按场景频率聚合告警"""
    probes = {"interface": _probe_interface, "script": _probe_scripts, "memory": _probe_memory}
    now = time.time()
    issues = []
    notifications = []
    for scene, probe in probes.items():
        config = _healing_config(scene)
        label = config["label"]
        try:
            healthy, detail, metrics = probe()
        except Exception as err:
            healthy, detail, metrics = False, f"巡检执行失败：{str(err)[:160]}", {
                "count": 1, "value": 1, "threshold": 1, "pending": False}
        with HEALING_LOCK:
            state = HEALING_STATE[scene]
            state.update(last_run=time.strftime("%Y-%m-%d %H:%M:%S"), detail=detail,
                         current_count=int(metrics.get("count", 0) or 0),
                         current_value=int(metrics.get("value", 0) or 0),
                         threshold=int(metrics.get("threshold", 1) or 1),
                         retry_pending=bool(metrics.get("pending", False)),
                         targets=[dict(item) for item in metrics.get("targets", [])])
            if healthy:
                was_active = bool(state["active"])
                transient = state["consecutive"] > 0 and not was_active
                previous_retries = int(state["retries"])
                state.update(consecutive=0, active=False, locked=False, retries=0,
                             retry_pending=False, last_retry=0.0, last_notify=0.0,
                             current_count=0, targets=[])
                if was_active:
                    state["status"] = "正常，已发送恢复通知"
                    state["last_recovery"] = time.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    state["status"] = "正常（瞬时异常已自动恢复）" if transient else "正常"
                recovered = was_active
            else:
                recovered = False
                transient = False
                previous_retries = 0
                state["consecutive"] += 1
                if not state["active"] and state["consecutive"] >= 3:
                    state.update(active=True, retries=0, locked=False, last_retry=0.0,
                                 last_notify=0.0)
                    message = f"{label}：连续 3 次巡检异常，确认进入自愈流程。{detail}"
                    issues.append(message)
                    _healing_record(scene, config["kind"], message,
                                    state.get("current_value"), state.get("threshold"),
                                    event_type="fault")
                if not state["active"]:
                    state["status"] = f"异常观察中（连续 {state['consecutive']}/3 次）"
                    continue
                if state["retry_pending"] and not state["locked"]:
                    state["status"] = f"异常未恢复，重试运行中（已发起 {state['retries']} 次）"
                    continue
                if not state["locked"]:
                    if not _self_heal_auto_enabled() or not config["enabled"]:
                        state["locked"] = True
                        state["status"] = "自愈已关闭，转入告警发送"
                    elif state["retries"] >= config["max_retries"]:
                        state["locked"] = True
                        state["status"] = f"已重试 {state['retries']} 次仍未恢复，锁定并转入告警"
                    elif now - state["last_retry"] >= config["retry_interval"]:
                        state["retries"] += 1
                        state["last_retry"] = now
                        retry_no = state["retries"]
                        state["status"] = f"正在执行第 {retry_no}/{config['max_retries']} 次自愈"
                        _persist_alert_runtime()
                        try:
                            action_detail, pending = _healing_action(scene, state)
                            state["retry_pending"] = bool(pending)
                            state["last_action"] = action_detail
                            state["status"] = (f"第 {retry_no} 次自愈已执行，等待复检" if not pending
                                               else f"第 {retry_no} 次自愈已拉起，等待执行结果")
                            message = f"{label}：{action_detail}（第 {retry_no}/{config['max_retries']} 次）"
                            issues.append(message)
                            _healing_record(scene, "heal", message, retry_no,
                                            config["max_retries"], event_type="heal_attempt")
                        except Exception as err:
                            state["retry_pending"] = False
                            state["last_action"] = f"自愈执行失败：{str(err)[:180]}"
                            state["status"] = f"第 {retry_no} 次自愈失败，等待复检"
                            message = f"{label}：第 {retry_no}/{config['max_retries']} 次自愈失败：{str(err)[:120]}"
                            issues.append(message)
                            _healing_record(scene, "heal", message, retry_no,
                                            config["max_retries"], event_type="heal_attempt")
                        continue
                    else:
                        state["status"] = (f"异常已确认，等待第 {state['retries'] + 1} 次重试"
                                           f"（已重试 {state['retries']} 次）")
                if state["locked"]:
                    interval = config["notify_minutes"] * 60
                    if now - state["last_notify"] >= interval:
                        state["last_notify"] = now
                        notifications.append((scene, f"{label}：{detail}；已重试 "
                                                     f"{state['retries']} 次，当前锁定自动重试"))
            # 健康分支在锁外发送单独恢复通知
        if healthy and recovered:
            message = f"{label}已恢复正常。{detail}；恢复前已重试 {previous_retries} 次"
            _healing_send([(scene, message)], recovery=True)
            issues.append(message)
        elif healthy and transient:
            message = f"{label}瞬时异常已自动恢复，未达到连续 3 次确认条件，无需推送"
            _healing_record(scene, "heal", message, event_type="recovered",
                            notify_status="无需通知")
            issues.append(message)
    if notifications:
        _healing_send(notifications)
    _persist_alert_runtime()
    return issues


def _self_heal_action(name):
    """冷却窗口内只执行一次自愈动作；MySQL 支持独立重试间隔"""
    now = time.time()
    cooldown = _self_heal_cooldown()
    if name == "mysql":
        try:
            cooldown = max(10, min(86400, int(get_setting("mysql_retry_interval", "60") or 60)))
        except (TypeError, ValueError):
            cooldown = 60
    elif name in ("disk", "pool"):
        try:
            cooldown = max(10, min(86400, int(get_setting("heal_memory_retry_interval", "60") or 60)))
        except (TypeError, ValueError):
            cooldown = 60
    with _SELF_HEAL_LOCK:
        last = _SELF_HEAL_LAST.get(name, 0)
        if now - last < cooldown:
            return False
        _SELF_HEAL_LAST[name] = now
    _persist_alert_runtime()
    return True


def _self_heal_reset(name):
    """问题恢复后清零连续失败次数，并允许再次立即处理"""
    with _SELF_HEAL_LOCK:
        changed = (name in _SELF_HEAL_FAILURES or name in _SELF_HEAL_CRITICAL
                   or name in _SELF_HEAL_LAST)
        _SELF_HEAL_FAILURES.pop(name, None)
        _SELF_HEAL_CRITICAL.discard(name)
        _SELF_HEAL_LAST.pop(name, None)
    if changed:
        _persist_alert_runtime()


def _self_heal_recover(name, label, action, recheck, issues):
    """执行恢复动作并立即复检，所有尝试和重试计数均写入本地文件。"""
    if not _self_heal_auto_enabled():
        issues.append(f"{label}：自动处理已关闭")
        return
    with _SELF_HEAL_LOCK:
        locked = name in _SELF_HEAL_CRITICAL
        attempt_no = _SELF_HEAL_FAILURES.get(name, 0) + 1
    if locked:
        issues.append(f"{label}：重试上限已到，自动处理已锁定")
        return
    if not _self_heal_action(name):
        issues.append(f"{label}：自动处理冷却中，等待下次巡检")
        return
    limit = _self_heal_max_attempts()
    if name == "mysql":
        try:
            limit = max(1, min(20, int(get_setting("mysql_max_retries", str(limit)) or limit)))
        except (TypeError, ValueError):
            pass
    elif name in ("disk", "pool"):
        try:
            limit = max(1, min(20, int(get_setting("heal_memory_max_retries", str(limit)) or limit)))
        except (TypeError, ValueError):
            pass
    try:
        action_detail = action()
        if action_detail:
            issues.append(f"{label}：{action_detail}")
    except Exception as err:
        action_detail = f"自动处理失败：{str(err)[:120]}"
        issues.append(f"{label}：{action_detail}")
    try:
        ok, detail = recheck()
    except Exception as err:
        ok, detail = False, str(err)[:120]
    if ok:
        _self_heal_reset(name)
        message = f"{label}：第 {attempt_no}/{limit} 次处理后复检通过。{detail}"
        _healing_record(name, "host", message, event_type="recovered")
        issues.append(f"{label}：处理后复检通过")
        return True
    with _SELF_HEAL_LOCK:
        attempts = _SELF_HEAL_FAILURES.get(name, 0) + 1
        _SELF_HEAL_FAILURES[name] = attempts
        critical = attempts >= limit and name not in _SELF_HEAL_CRITICAL
        if critical:
            _SELF_HEAL_CRITICAL.add(name)
    _persist_alert_runtime()
    detail = str(detail or "复检未通过")[:180]
    message = f"{label}：第 {attempts}/{limit} 次处理仍未恢复：{detail}"
    issues.append(message)
    if critical:
        _alert_write(f"host:{label}", "host", attempts, limit,
                     f"重大异常：{label}连续自动处理 {attempts} 次仍未恢复，请立即人工排查。最近复检：{detail}",
                     level="critical", event_type="heal_failed")
    else:
        _healing_record(name, "host", message, value=attempts, threshold=limit,
                        event_type="heal_attempt")


def _mysql_health():
    """返回 MySQL 连通状态与错误详情，供巡检和处理后复检共用"""
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        return True, ""
    except Exception as err:
        return False, str(err)[:200]


def _self_heal_clean_disk():
    """清理过期日志与旧备份，释放磁盘空间（返回清理条数）"""
    freed = 0
    try:
        freed += clean_old_logs(7)
    except Exception:
        pass
    try:
        from app.services.backup import prune_backup_dir
        before = 0
        bdir = get_setting("backup_clean_dir", "") or None
        before = len(prune_backup_dir(bdir, 7))
        freed += before
    except Exception:
        pass
    return freed


def _self_heal_once():
    """执行一次主动场景巡检及原有资源自愈巡检"""
    global _MYSQL_PROBE_FAILURES
    issues = _active_healing_once()
    # 1) MySQL 连通性：连续失败达到独立阈值后才重建连接池
    mysql_ok, mysql_error = _mysql_health()
    try:
        mysql_fail_limit = max(1, min(20, int(get_setting("mysql_fail_count", "3") or 3)))
    except (TypeError, ValueError):
        mysql_fail_limit = 3
    if not mysql_ok:
        _MYSQL_PROBE_FAILURES += 1
        _persist_alert_runtime()
        issues.append(f"MySQL 探活失败 {_MYSQL_PROBE_FAILURES}/{mysql_fail_limit} 次：{mysql_error[:120]}")
        if _MYSQL_PROBE_FAILURES >= mysql_fail_limit:
            _alert_write("host:MySQL", "host", _MYSQL_PROBE_FAILURES, mysql_fail_limit,
                         f"MySQL 连续 {_MYSQL_PROBE_FAILURES} 次连接失败：{mysql_error}",
                         event_type="fault")

            def rebuild_mysql():
                engine.dispose(close=False)
                return "已重建数据库连接池"

            if _self_heal_recover("mysql", "MySQL", rebuild_mysql, _mysql_health, issues):
                _MYSQL_PROBE_FAILURES = 0
    else:
        if _MYSQL_PROBE_FAILURES:
            _healing_record("mysql", "host", "MySQL 已恢复连接，SELECT 1 探活成功",
                            event_type="recovered")
        _MYSQL_PROBE_FAILURES = 0
        _self_heal_reset("mysql")

    # 2) 磁盘剩余空间：独立于 MySQL，断库时仍继续巡检和清理
    try:
        import shutil
        usage = shutil.disk_usage(BASE_DIR)
        free_pct = usage.free * 100.0 / max(1, usage.total)
        try:
            disk_used_limit = get_setting("alert_disk_percent", "")
            limit = (100.0 - float(disk_used_limit)) if str(disk_used_limit).strip() else float(get_setting("self_heal_disk_min", "10") or 10)
            limit = max(0.0, min(99.0, limit))
        except (TypeError, ValueError):
            limit = 10.0
        if free_pct < limit:
            issues.append(f"磁盘剩余 {free_pct:.1f}%，低于 {limit:.0f}% 阈值")
            _alert_write("host:磁盘", "host", int(round(free_pct)), int(limit),
                         f"磁盘剩余空间仅 {free_pct:.1f}%（低于 {limit:.0f}%），已进入自动恢复流程")

            def clean_disk():
                freed = _self_heal_clean_disk()
                save_setting("self_heal_last_disk_clean",
                             time.strftime("%Y-%m-%d %H:%M:%S"))
                return f"已自动清理旧日志/旧备份 {freed} 项"

            def recheck_disk():
                current = shutil.disk_usage(BASE_DIR)
                current_free = current.free * 100.0 / max(1, current.total)
                return current_free >= limit, f"当前剩余 {current_free:.1f}%"

            _self_heal_recover("disk", "磁盘", clean_disk, recheck_disk, issues)
        else:
            _self_heal_reset("disk")
    except Exception as e:
        issues.append(f"磁盘检查失败：{str(e)[:120]}")
    # 3) 连接池占用：只读取池内计数，不依赖 MySQL 探活成功
    try:
        pool = engine.pool
        checked_out = int(pool.checkedout())
        capacity = max(1, int(DB_POOL_SIZE) + int(DB_MAX_OVERFLOW))
        usage_pct = checked_out * 100.0 / capacity
        try:
            limit = float(get_setting("self_heal_pool_percent", "85") or 85)
        except (TypeError, ValueError):
            limit = 85.0
        if usage_pct >= limit:
            issues.append(f"连接池占用 {usage_pct:.0f}%，达到 {limit:.0f}% 阈值")
            _alert_write("host:连接池", "host", int(round(usage_pct)), int(limit),
                         f"数据库连接池占用 {usage_pct:.0f}%，已进入自动恢复流程")

            def release_pool():
                engine.dispose(close=False)
                save_setting("self_heal_last_pool_release",
                             time.strftime("%Y-%m-%d %H:%M:%S"))
                return "已释放连接池空闲连接"

            def recheck_pool():
                current = int(engine.pool.checkedout()) * 100.0 / capacity
                return current < limit, f"当前占用 {current:.0f}%"

            _self_heal_recover("pool", "连接池", release_pool, recheck_pool, issues)
        else:
            _self_heal_reset("pool")
    except Exception as e:
        issues.append(f"连接池检查失败：{str(e)[:120]}")

    with _SELF_HEAL_LOCK:
        failures = dict(_SELF_HEAL_FAILURES)
        critical = sorted(_SELF_HEAL_CRITICAL)
    if issues:
        SELF_HEAL_STATE["actions"] = SELF_HEAL_STATE.get("actions", 0) + 1
    SELF_HEAL_STATE.update(
        last_run=time.strftime("%Y-%m-%d %H:%M:%S"),
        last_result="; ".join(issues) if issues else "巡检正常",
        retry_limit=_self_heal_max_attempts(), failures=failures, critical=critical)
    _persist_alert_runtime()
    if issues:
        log_startup("自愈巡检：" + "; ".join(issues))
        try:
            detail = "; ".join(issues)
            unresolved = ("复检仍未恢复", "自动处理失败", "检查失败", "自动处理已关闭", "冷却中")
            result = "failed" if failures or any(x in detail for x in unresolved) else "success"
            db = SessionLocal()
            db.add(AuditLogDB(operator="system", action="self_heal", target="定时巡检",
                              detail=detail[:1000], ip="", result=result))
            db.commit()
            db.close()
        except Exception:
            pass
    return issues


def _self_heal_loop():
    """独立巡检线程：默认每 30 秒一次，后台修改间隔后下一轮生效"""
    from app.core.config import SELF_HEAL_ENABLED
    if not SELF_HEAL_ENABLED:
        log_startup("自愈巡检：已通过 SELF_HEAL_ENABLED=0 关闭")
        return
    time.sleep(30)
    while True:
        interval = _self_heal_interval()
        try:
            # 分布式锁（按实例维度）：同一 HOST_ID 同时只允许一个巡检线程，
            # 各实例仍会检查本机磁盘/连接池，避免只由一台机器巡检导致其它机器无人管
            handle = dist_lock.acquire(f"self-heal:{dist_lock.host_id()}",
                                       ttl=max(120, interval * 2))
            if handle:
                try:
                    _self_heal_once()
                finally:
                    handle.release()
        except Exception as e:
            log_startup(f"自愈巡检异常：{e}")
        time.sleep(interval)


HOST_DEVICE_STATE = {
    "battery": {"enabled": BATTERY_CHECK_ENABLED, "available": None,
                "percent": None, "charging": None, "checked_at": "", "error": ""},
    "network": {"enabled": NETWORK_CHECK_ENABLED, "online": None, "failures": 0,
                "targets": [], "checked_at": "", "error": ""},
}
_NETWORK_FAILURES = 0
_NETWORK_ALERT_ACTIVE = False
_BATTERY_ALERT_ACTIVE = False


def _collect_battery():
    """读取笔记本电池；台式机或服务器无电池时只展示状态，不推送"""
    enabled = str(get_setting("battery_check_enabled", "1" if BATTERY_CHECK_ENABLED else "0")).lower() in ("1", "true", "yes", "on")
    if not enabled:
        return {"enabled": False, "available": None, "percent": None,
                "charging": None, "checked_at": "", "error": ""}
    result = {"enabled": True, "available": False, "percent": None,
              "charging": None, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "error": ""}
    try:
        import psutil
        battery = psutil.sensors_battery()
        if battery is not None:
            result.update(available=True, percent=round(float(battery.percent), 1),
                          charging=battery.power_plugged)
    except Exception as e:
        result["error"] = str(e)[:120]
    return result


def _check_battery_alert(status):
    """仅在使用电池且低电量时提醒；充电或电量恢复后发送一次恢复通知"""
    global _BATTERY_ALERT_ACTIVE
    if not status.get("enabled") or not status.get("available"):
        _BATTERY_ALERT_ACTIVE = False
        return
    try:
        limit = max(1, min(100, int(get_setting(
            "battery_low_percent", str(BATTERY_LOW_PERCENT)) or BATTERY_LOW_PERCENT)))
    except (TypeError, ValueError):
        limit = BATTERY_LOW_PERCENT
    status["threshold"] = limit
    percent = float(status.get("percent") or 0)
    low = status.get("charging") is False and percent <= limit
    if low:
        _BATTERY_ALERT_ACTIVE = True
        _alert_write("host:电池", "battery", int(round(percent)), limit,
                     f"笔记本电池电量仅 {percent:.0f}%，当前未充电，低于 {limit}% 阈值，请及时接通电源")
    elif _BATTERY_ALERT_ACTIVE:
        _BATTERY_ALERT_ACTIVE = False
        state = "已接通电源" if status.get("charging") else f"电量已恢复至 {percent:.0f}%"
        _alert_write("host:电池恢复", "battery", int(round(percent)), limit,
                     f"笔记本电池{state}，低电量告警解除", level="low")


def _network_targets():
    raw = get_setting("network_ping_targets", NETWORK_PING_TARGETS) or ""
    targets = []
    for value in raw.replace("，", ",").split(","):
        target = value.strip()
        if (target and not target.startswith("-") and len(target) <= 253
                and re.fullmatch(r"[A-Za-z0-9_.:-]+", target)
                and target not in targets):
            targets.append(target)
    return targets[:5]


def _ping_target(target, timeout):
    """执行一次系统 Ping，返回可达性、错误与实际往返耗时（毫秒）"""
    if os.name == "nt":
        command = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), target]
    else:
        command = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), target]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=max(2, int(timeout) + 1), check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        latency = round((time.perf_counter() - started) * 1000, 1)
        return completed.returncode == 0, "" if completed.returncode == 0 else "目标不可达", latency
    except FileNotFoundError:
        return None, "当前系统未安装 ping 命令", None
    except subprocess.TimeoutExpired:
        return False, "Ping 执行超时", None
    except Exception as e:
        return None, str(e)[:120], None


def _run_network_check():
    """全部 Ping 目标连续失败才告警；任一目标恢复即判定网络恢复"""
    global _NETWORK_FAILURES, _NETWORK_ALERT_ACTIVE
    enabled = str(get_setting("network_check_enabled", "1" if NETWORK_CHECK_ENABLED else "0")).lower() in ("1", "true", "yes", "on")
    if not enabled:
        _NETWORK_FAILURES = 0
        _NETWORK_ALERT_ACTIVE = False
        status = {"enabled": False, "online": None, "failures": 0,
                  "targets": [], "checked_at": "", "error": ""}
        HOST_DEVICE_STATE["network"] = status
        return status
    targets = _network_targets()
    try:
        fail_limit = max(1, min(10, int(get_setting(
            "network_fail_count", str(NETWORK_FAIL_COUNT)) or NETWORK_FAIL_COUNT)))
        timeout = max(1, min(10, int(get_setting(
            "network_ping_timeout", str(NETWORK_PING_TIMEOUT)) or NETWORK_PING_TIMEOUT)))
    except (TypeError, ValueError):
        fail_limit, timeout = NETWORK_FAIL_COUNT, NETWORK_PING_TIMEOUT
    status = {"enabled": True, "online": None, "failures": _NETWORK_FAILURES,
              "targets": [], "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "error": "" if targets else "未配置有效 Ping 目标", "fail_limit": fail_limit}
    if not targets:
        HOST_DEVICE_STATE["network"] = status
        return status
    for target in targets:
        ok, error, latency = _ping_target(target, timeout)
        status["targets"].append({"target": target, "ok": ok, "error": error, "latency_ms": latency})
    reachable_latencies = [item["latency_ms"] for item in status["targets"]
                           if item["ok"] is True and item["latency_ms"] is not None]
    status["latency_ms"] = round(sum(reachable_latencies) / len(reachable_latencies), 1) if reachable_latencies else None
    online = any(item["ok"] is True for item in status["targets"])
    executable = any(item["ok"] is not None for item in status["targets"])
    if not executable:
        status["error"] = "；".join(sorted({item["error"] for item in status["targets"] if item["error"]}))
        HOST_DEVICE_STATE["network"] = status
        return status
    if online:
        def _apply_online(state):
            recovered = bool(state.get("alert_active"))
            state["failures"] = 0
            state["alert_active"] = False
            return recovered

        recovered = bool(alert_store.mutate_network_state(_apply_online))
        _NETWORK_FAILURES = 0
        _NETWORK_ALERT_ACTIVE = False
        status.update(online=True, failures=0)
        if recovered:
            reachable = "、".join(item["target"] for item in status["targets"] if item["ok"] is True)
            _alert_write("host:网络恢复", "network", 1, fail_limit,
                         f"服务端网络已恢复，Ping 目标可达：{reachable}", level="low")
            _desktop_notify("网络已恢复", f"服务端网络已恢复，Ping 目标可达：{reachable}", "info")
    else:
        def _apply_offline(state):
            failures = max(0, int(state.get("failures") or 0)) + 1
            state["failures"] = failures
            if failures < fail_limit:
                return failures, False
            was_active = bool(state.get("alert_active"))
            state["alert_active"] = True
            return failures, not was_active

        result = alert_store.mutate_network_state(_apply_offline)
        if isinstance(result, tuple) and len(result) == 2:
            failures, triggered = int(result[0]), bool(result[1])
        else:
            # 极端情况下本地状态写入失败，退化为进程内计数，保证告警不丢
            _NETWORK_FAILURES += 1
            failures = _NETWORK_FAILURES
            triggered = failures >= fail_limit and not _NETWORK_ALERT_ACTIVE
        _NETWORK_FAILURES = failures
        _NETWORK_ALERT_ACTIVE = failures >= fail_limit
        status.update(online=False, failures=failures,
                      error="所有 Ping 目标均不可达")
        if triggered:
            message = (f"服务端网络异常：{len(targets)} 个 Ping 目标均不可达，"
                       f"已连续失败 {failures} 次，请检查服务器网络")
            _alert_write("host:网络", "network", failures, fail_limit, message)
            _desktop_notify("网络已断开", message, "warning")
    HOST_DEVICE_STATE["network"] = status
    return status


def _refresh_device_monitors():
    battery = _collect_battery()
    HOST_DEVICE_STATE["battery"] = battery
    _check_battery_alert(battery)
    _run_network_check()
    _persist_alert_runtime()


def _check_host_alert(result):
    """CPU、内存与磁盘分别使用独立阈值；内存自愈复用内存阈值"""
    default = str(ALERT_HOST_PERCENT)
    for label, key, value in (
            ("CPU", "alert_cpu_percent", result.get("cpu")),
            ("磁盘", "alert_disk_percent", result.get("disk"))):
        try:
            limit = float(get_setting(key, get_setting("alert_host_percent", default)) or ALERT_HOST_PERCENT)
        except (TypeError, ValueError):
            limit = float(ALERT_HOST_PERCENT)
        limit = max(1.0, min(100.0, limit))
        if value is None or float(value) < limit:
            continue
        _alert_write(f"host:{label}", "host", int(round(float(value))), int(round(limit)),
                     f"本机 {label} 使用率 {float(value):.1f}%，超过 {limit:.0f}% 阈值")


def notify_script_failed(key, returncode=None, tail=""):
    """脚本失败后由主动巡检连续确认，避免在运行结束处立即轰炸通知"""
    log_startup(f"脚本「{key}」执行失败（退出码 {returncode}），等待主动巡检确认")


def host_metrics(notify=True):
    """本机 CPU / 内存 / 磁盘使用率

    优先用 psutil；未安装时降级为系统自带接口，CPU 无法获取时返回 None。
    notify=False 时只采集不触发告警（供接口读取缓存/刷新使用）。
    """
    result = {"cpu": None, "memory": None, "memory_total": 0, "memory_used": 0,
              "disk": None, "disk_total": 0, "disk_used": 0, "error": ""}
    try:
        import psutil
        result["cpu"] = round(psutil.cpu_percent(interval=0.2), 1)
        mem = psutil.virtual_memory()
        result.update(memory=round(mem.percent, 1), memory_total=mem.total,
                      memory_used=mem.used)
    except Exception as e:
        result["error"] = str(e)[:120]
    try:
        import shutil as _shutil
        usage = _shutil.disk_usage(os.path.abspath(os.sep))
        result.update(disk=round(usage.used * 100.0 / max(1, usage.total), 1),
                      disk_total=usage.total, disk_used=usage.used)
    except Exception:
        pass
    if notify:
        _check_host_alert(result)
        _refresh_device_monitors()
    result["battery"] = dict(HOST_DEVICE_STATE["battery"])
    network = dict(HOST_DEVICE_STATE["network"])
    network["targets"] = [dict(item) for item in network.get("targets", [])]
    result["network"] = network
    return result


def _host_check_loop():
    """主机、电池与网络后台巡检；间隔可在智能自愈与告警中心动态调整"""
    from app.core.config import HOST_CHECK_ENABLED, HOST_CHECK_INTERVAL
    if not HOST_CHECK_ENABLED:
        log_startup("主机巡检：已通过 HOST_CHECK_ENABLED=0 关闭")
        return
    time.sleep(10)
    while True:
        try:
            host_metrics(notify=True)
        except Exception as e:
            log_startup(f"主机巡检异常：{e}")
        try:
            interval = int(get_setting(
                "alert_check_interval", str(HOST_CHECK_INTERVAL)) or HOST_CHECK_INTERVAL)
        except (TypeError, ValueError):
            interval = HOST_CHECK_INTERVAL
        time.sleep(max(30, min(3600, interval)))

# ===================== 日志记录（中间件，所有请求自动记录） =====================
# 不记录的路径（文档、静态资源），避免日志噪音
LOG_SKIP_PATHS = {"/docs", "/redoc", "/openapi.json", "/favicon.ico", "/docs/oauth2-redirect", "/health"}
LOG_PARAMS_MAX = 2000      # 参数最大记录长度
LOG_UA_MAX = 500           # UA最大记录长度


def _client_ip(request: Request) -> str:
    """获取真实客户端IP，兼容反向代理"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else ""


# 敏感参数名：入库前一律用 *** 替换，避免密钥写进日志表
SENSITIVE_PARAM_RE = re.compile(
    r"(?i)(token|access_token|api_key|apikey|secret|password|passwd|authorization|auth|key)=([^&#]*)")


def sanitize_params(raw: str) -> str:
    """清除查询串中的敏感字段值"""
    return SENSITIVE_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", raw or "")


def _request_params(request: Request) -> str:
    """记录请求参数（查询串；敏感字段自动脱敏）"""
    return sanitize_params((request.url.query or "")[:LOG_PARAMS_MAX])


def _write_log(method: str, path: str, ip: str, params: str,
               status_code: int, duration_ms: int, user_agent: str,
               user_id: int = None, token_id: int = None):
    """写入日志（独立会话，失败不影响业务请求）"""
    db = SessionLocal()
    try:
        db.add(ApiLogDB(method=method, path=path, ip=ip, params=sanitize_params(params),
                        status_code=status_code, duration_ms=duration_ms,
                        user_agent=(user_agent or "")[:LOG_UA_MAX],
                        user_id=user_id, token_id=token_id))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


@app.middleware("http")
async def api_log_middleware(request: Request, call_next):
    """记录每一次接口调用：方法、路径、IP、参数、状态码、耗时、UA"""
    path = request.url.path
    business_api = path.startswith("/api/") and not path.startswith("/api/portal")
    # 后台、门户页面、文档与站外扫描（/、/robots.txt、/.env 等）都不写入 API 调用日志
    if (path in LOG_SKIP_PATHS or path.startswith("/admin")
            or path.startswith("/portal") or not path.startswith("/api/")):
        return await call_next(request)

    start = time.perf_counter()
    status_code = 500
    response = None
    try:
        blocked = False
        if path.startswith("/api/"):
            # IP 黑白名单：黑名单直接拒绝；白名单非空时仅放行白名单内 IP
            refresh_security()
            ip = _client_ip(request)
            blocked = ip in SECURITY["black"] or (bool(SECURITY["white"]) and ip not in SECURITY["white"])
        if blocked:
            response = JSONResponse(status_code=403, content={"code": 403, "msg": "当前 IP 已被限制访问"})
            status_code = 403
        # 已禁用的接口直接返回403
        elif path.startswith("/api/") and API_SWITCHES.get(path) is False:
            response = JSONResponse(status_code=403, content={"code": 403, "msg": "该接口已被管理员禁用"})
            status_code = 403
        elif (path.startswith("/api/") and GLOBAL_QPS_LIMIT > 0
              and CACHE.incr("global-qps", ttl=1) > GLOBAL_QPS_LIMIT):
            # 全站总闸：多 IP / 多 Token 同时刷接口时保护整个平台
            response = JSONResponse(status_code=429, content={
                "code": 429, "msg": "平台当前请求过多，请稍后重试"})
            status_code = 429
        else:
            response = await call_next(request)
            status_code = response.status_code
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        if business_api or path == "/health":
            metrics.observe(path, status_code, duration_ms)
        # C 端业务调用按比例抽样；管理 Token 与主 Token 仍完整保留
        user_id = getattr(request.state, "api_user_id", None)
        token_id = getattr(request.state, "api_token_id", None)
        should_log = not path.startswith("/api/portal")
        if business_api and user_id is not None and PORTAL_API_LOG_SAMPLE > 1:
            hits = CACHE.incr(f"user-log-sample:{user_id}", ttl=86400)
            should_log = hits > 0 and hits % PORTAL_API_LOG_SAMPLE == 0
        # 接口可单独关闭日志记录（高频接口），不影响鉴权与限流
        if should_log and API_LOG_SWITCHES.get(path, True) is not False:
            try:
                await run_in_threadpool(
                    _write_log,
                    request.method,
                    path[:200],
                    _client_ip(request)[:100],
                    _request_params(request),
                    status_code,
                    duration_ms,
                    request.headers.get("user-agent", ""),
                    user_id,
                    token_id,
                )
            except Exception:
                pass
        if business_api:
            try:
                _alert_track(path, status_code)
            except Exception:
                pass
            # 429 记录限流日志，后台可查看高频触发限流的 IP / Token
            if status_code == 429:
                try:
                    raw_token = (request.headers.get("token") or "").strip()
                    await run_in_threadpool(_record_rate_limit, _client_ip(request),
                                            path, token_hint(raw_token) if raw_token else "")
                except Exception:
                    pass

    return response

# ===================== 网络数据源抓取（带熔断降级） =====================
# 连续失败达到阈值后打开熔断，冷却期内直接返回 None，由接口回退本地数据，
# 避免每个请求都卡在外部接口超时上；冷却结束后放行一次探测请求（半开）。
_BREAKER = {}          # {path: {"fails": 连续失败次数, "open_until": 熔断截止时间}}
_BREAKER_LOCK = threading.Lock()


def _breaker_load():
    """启动时从数据库恢复熔断状态，服务重启不再清零"""
    try:
        db = SessionLocal()
        rows = db.query(BreakerStateDB).all()
        db.close()
        with _BREAKER_LOCK:
            for r in rows:
                _BREAKER[r.key] = {"fails": int(r.failures or 0),
                                   "open_until": float(r.open_until or 0)}
    except Exception:
        pass


def _breaker_persist(key: str, state: dict, ok: bool, error: str = ""):
    """把失败次数、熔断截止、累计成功/失败写库（失败不影响主流程）"""
    try:
        db = SessionLocal()
        row = db.query(BreakerStateDB).filter(BreakerStateDB.key == key).first()
        if row is None:
            row = BreakerStateDB(key=key, total_ok=0, total_fail=0)
            db.add(row)
        row.failures = int(state.get("fails", 0))
        row.open_until = float(state.get("open_until", 0))
        if ok:
            row.total_ok = (row.total_ok or 0) + 1
            row.last_success_at = datetime.now()
        else:
            row.total_fail = (row.total_fail or 0) + 1
            row.last_error = (error or "请求失败")[:255]
        db.commit()
        db.close()
    except Exception:
        pass


def _breaker_history(key: str, failures: int, kind: str = "open"):
    """熔断触发/恢复写入审计日志与熔断事件表，供后台查看历史报表"""
    try:
        db = SessionLocal()
        if kind == "open":
            db.add(AuditLogDB(
                operator="system", action="breaker_open", target=key[:255],
                detail=f"连续失败 {failures} 次，熔断 {BREAKER_COOLDOWN} 秒", ip="",
                result="failed"))
        db.add(BreakerEventDB(key=key[:100], kind=kind, failures=int(failures or 0)))
        db.commit()
        db.close()
    except Exception:
        pass


def _breaker_allow(key: str) -> bool:
    with _BREAKER_LOCK:
        state = _BREAKER.get(key)
        return not state or state["open_until"] <= time.time()


def _breaker_record(key: str, ok: bool, error: str = ""):
    now = time.time()
    opened = 0
    recovered = 0
    with _BREAKER_LOCK:
        state = _BREAKER.setdefault(key, {"fails": 0, "open_until": 0.0})
        if ok:
            if state["open_until"] > 0:
                recovered = state["fails"] or BREAKER_THRESHOLD
            state["fails"] = 0
            state["open_until"] = 0.0
        else:
            state["fails"] += 1
            if state["fails"] >= BREAKER_THRESHOLD:
                state["open_until"] = now + BREAKER_COOLDOWN
                opened = state["fails"]
                state["fails"] = 0
        snapshot = dict(state)
    _breaker_persist(key, snapshot, ok, error)
    if opened:
        _breaker_history(key, opened)
        _alert_write(key, "breaker", opened, BREAKER_THRESHOLD,
                     f"外部数据源 {key} 连续失败 {opened} 次，已熔断 {BREAKER_COOLDOWN} 秒")
    elif recovered:
        _breaker_history(key, recovered, "recover")


def reset_breaker(key: str = None):
    """重置熔断状态：key 为空时重置全部（内存 + 数据库）"""
    with _BREAKER_LOCK:
        if key:
            _BREAKER.pop(key, None)
        else:
            _BREAKER.clear()
    try:
        db = SessionLocal()
        q = db.query(BreakerStateDB)
        if key:
            q = q.filter(BreakerStateDB.key == key)
        q.update({"failures": 0, "open_until": 0.0}, synchronize_session=False)
        db.commit()
        db.close()
    except Exception:
        pass


def net_get(path: str, params: dict = None):
    """请求外部真实数据源，成功返回 data 字段，失败或熔断返回 None"""
    key = path.split("?")[0]
    if not _breaker_allow(key):
        return None
    try:
        resp = requests.get(f"{NET_API}{path}", params=params, headers=NET_HEADERS, timeout=TIMEOUT)
        j = resp.json()
        if j.get("code") == 200:
            _breaker_record(key, True)
            return j.get("data")
        _breaker_record(key, False, f"返回码异常：{j.get('code')}")
    except Exception as e:
        _breaker_record(key, False, str(e))
    return None


_breaker_load()

# ===================== 同步函数：网络数据写入数据库 =====================
def sync_hot(db: Session) -> int:
    """抓取微博/知乎/抖音/头条实时热榜，去重写入hot表"""
    total = 0
    sources = {"weibo": "微博", "zhihu": "知乎", "douyin": "抖音", "toutiao": "头条"}
    for src, cname in sources.items():
        data = net_get(f"/{src}")
        if not data:
            continue
        for it in data:
            title = it.get("title")
            if not title:
                continue
            url = it.get("link") or it.get("cover") or ""
            full_title = f"【{cname}】{title}"
            if db.query(HotNewsDB).filter(HotNewsDB.title == full_title).first():
                continue
            db.add(HotNewsDB(title=full_title, url=url,
                             add_time=time.strftime("%Y-%m-%d %H:%M:%S")))
            total += 1
    db.commit()
    return total

def sync_joke(db: Session) -> int:
    """抓取网络真实段子/疯狂星期四文案，去重写入joke表"""
    total = 0
    for _ in range(5):
        d = net_get("/duanzi")
        if d and d.get("duanzi"):
            text = d["duanzi"]
            if not db.query(LocalJokeDB).filter(LocalJokeDB.content == text).first():
                db.add(LocalJokeDB(content=text))
                total += 1
        k = net_get("/kfc")
        if k and k.get("kfc"):
            text = k["kfc"]
            if not db.query(LocalJokeDB).filter(LocalJokeDB.content == text).first():
                db.add(LocalJokeDB(content=text))
                total += 1
    db.commit()
    return total

def sync_wisdom(db: Session) -> int:
    """抓取一言（真实句子），去重写入wisdom表"""
    total = 0
    for _ in range(5):
        d = net_get("/hitokoto")
        if d and d.get("hitokoto"):
            text = d["hitokoto"]
            if not db.query(WordWisdomDB).filter(WordWisdomDB.content == text).first():
                db.add(WordWisdomDB(tag="一言", content=text))
                total += 1
    db.commit()
    return total

def sync_image(db: Session) -> int:
    """抓取必应每日壁纸，去重写入image表"""
    d = net_get("/bing")
    if not d:
        return 0
    url = d.get("cover_4k") or d.get("cover")
    desc = d.get("copyright") or d.get("title") or "必应每日壁纸"
    if not url:
        return 0
    if db.query(DailyImageDB).filter(DailyImageDB.img_url == url).first():
        return 0
    db.add(DailyImageDB(desc=desc, img_url=url))
    db.commit()
    return 1

def sync_history(db: Session) -> int:
    """抓取历史上的今天，去重写入history表"""
    d = net_get("/today-in-history")
    if not d:
        return 0
    date_str = d.get("date", "")  # 格式如 10-8
    parts = date_str.split("-")
    if len(parts) != 2:
        return 0
    month_day = f"{int(parts[0]):02d}-{int(parts[1]):02d}"
    total = 0
    for it in d.get("items", []):
        title = it.get("title")
        year = str(it.get("year", ""))
        if not title:
            continue
        if db.query(HistoryTodayDB).filter(
                HistoryTodayDB.month_day == month_day,
                HistoryTodayDB.event == title).first():
            continue
        db.add(HistoryTodayDB(month_day=month_day, year=year, event=title))
        total += 1
    db.commit()
    return total

# 可同步目标 -> 同步函数
SYNC_FUNCS = {
    "hot": sync_hot,
    "joke": sync_joke,
    "wisdom": sync_wisdom,
    "image": sync_image,
    "history": sync_history,
}


def random_row(db: Session, model, *conds, tries: int = 5):
    """随机取一行（自动过滤软删除数据）

    MySQL 的 ORDER BY RAND() 在大表上很慢，这里先按 id 区间随机定位；
    遇到删除产生的 id 空洞导致落空时重试，多次失败再降级为 RAND() 兜底。
    """
    base = db.query(model).filter(model.is_deleted.is_(False), *conds)
    min_id, max_id = (db.query(func.min(model.id), func.max(model.id))
                      .filter(model.is_deleted.is_(False), *conds).first())
    if min_id is None:
        return None
    for _ in range(tries):
        rid = random.randint(min_id, max_id)
        row = base.filter(model.id >= rid).order_by(model.id).first()
        if row is None:
            row = base.filter(model.id <= rid).order_by(model.id.desc()).first()
        if row is not None:
            return row
    return (base.order_by(func.random()).first()
            or base.order_by(model.id).first())


# ===================== 【本地数据库接口】=====================
def cached(key: str, loader, path: str = None):
    """读缓存；未命中时执行 loader 并回填，用于读多写少的只读接口

    path 传入后按 api_switch.cache_ttl 使用该接口的独立缓存时长。
    """
    value = CACHE.get(key)
    if value is None:
        value = loader()
        ttl = API_CACHE_TTLS.get(path or "", 0)
        CACHE.set(key, value, ttl or None)
    return value


# ===================== 健康检查（无需 Token，供探针/监控使用） =====================
@app.get("/health", summary="健康检查", include_in_schema=False)
def health_check():
    """返回服务、数据库、缓存状态；数据库不可用时返回 503"""
    started = time.time()
    db_ok, db_error = True, ""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        db_ok = False
        db_error = str(e)[:200]

    # 连接池监控：活跃连接与溢出数量，接近上限时给出告警
    pool_metrics, pool_warning = {}, ""
    try:
        pool = engine.pool
        checked_out = int(pool.checkedout())
        overflow = int(pool.overflow())
        capacity = max(1, int(DB_POOL_SIZE) + int(DB_MAX_OVERFLOW))
        usage = checked_out / capacity
        pool_metrics = {
            "size": int(getattr(pool, "size", lambda: DB_POOL_SIZE)()),
            "checked_out": checked_out,
            "checked_in": int(getattr(pool, "checkedin", lambda: 0)()),
            "overflow": overflow,
            "max_overflow": int(DB_MAX_OVERFLOW),
            "capacity": capacity,
            "usage_percent": round(usage * 100, 1),
        }
        if usage >= 0.8:
            pool_warning = (f"数据库连接池使用率 {pool_metrics['usage_percent']}%，"
                            f"已占用 {checked_out}/{capacity}，请排查连接泄漏或调大连接池")
    except Exception as e:
        pool_metrics = {"error": str(e)[:120]}
    payload = {
        "code": 200 if db_ok else 503,
        "status": "ok" if db_ok else "degraded",
        "env": APP_ENV,
        "version": APP_VERSION,
        "version_prefix": API_VERSION_PREFIX,
        "uptime_seconds": int(time.time() - START_TIME),
        "database": {"ok": db_ok, "latency_ms": int((time.time() - started) * 1000),
                     "error": db_error,
                     "pool": pool_metrics},
        "cache": CACHE.stats(),
    }
    if pool_warning:
        payload["warning"] = pool_warning
    return JSONResponse(payload, status_code=200 if db_ok else 503)



@app.get("/metrics", summary="Prometheus 指标", include_in_schema=False)
def prometheus_metrics(token: str = Header(None, max_length=128)):
    """Prometheus 抓取端点：需要与 /api 相同的 Token（放在 query 或 header 均可）"""
    raw = (token or "").strip()
    if not raw:
        raise HTTPException(status_code=401, detail="缺少 Token，拒绝访问")
    refresh_security()
    digest = hash_token(raw)
    if not hmac.compare_digest(digest, MASTER_TOKEN_HASH):
        info = SECURITY["tokens"].get(digest)
        if not info or not info["enabled"]:
            raise HTTPException(status_code=401, detail="Token无效，拒绝访问")
    stats = CACHE.stats()
    total = stats["hits"] + stats["misses"]
    ratio = stats["hits"] * 1.0 / total if total else 0.0
    try:
        pool = engine.pool
        size = int(getattr(pool, "size", lambda: 0)() or 0)
        overflow = int(getattr(pool, "_max_overflow", 0) or 0)
        checked_out = int(getattr(pool, "checkedout", lambda: 0)() or 0)
        usage = checked_out / max(1, size + overflow)
    except Exception:
        usage = 0.0
    return Response(metrics.render(pool_percent=usage, cache_ratio=ratio),
                    media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/knowledge/get", summary="查询知识点", dependencies=[Depends(verify_token)])
def get_knowledge(q: str = Query(None, max_length=100, description="搜索关键词"),
                  category: str = Query(None, max_length=50, description="分类"),
                  db: Session = Depends(get_db)):
    """搜索知识点，支持关键词、分类过滤"""
    def load():
        query = db.query(KnowledgeDB).filter(KnowledgeDB.is_deleted.is_(False))
        if q:
            query = query.filter(KnowledgeDB.content.contains(q) | KnowledgeDB.title.contains(q))
        if category:
            query = query.filter(KnowledgeDB.category == category)
        data = query.limit(10).all()
        return [{"id": i.id, "category": i.category, "title": i.title, "content": i.content} for i in data]
    return {"code": 200, "data": cached(f"knowledge:get:{q or ''}:{category or ''}", load,
                                        "/api/knowledge/get")}

## 2.怼人语录
@app.get("/api/retort/get", summary="随机怼人语录", dependencies=[Depends(verify_token)])
def get_retort(level: str = Query(None, max_length=20, description="等级筛选"),
               db: Session = Depends(get_db)):
    """随机获取一条怼人语录，可以按level分类筛选"""
    conds = [RetortDB.level == level] if level else []
    item = random_row(db, RetortDB, *conds)
    if not item:
        return {"code": 404, "msg": "暂无语录"}
    return {"code": 200, "data": item.content}


## 3.起名接口
@app.get("/api/name/get", summary="随机起名", dependencies=[Depends(verify_token)])
def get_name(type: str = Query(..., pattern="^(boy|girl|net|shop)$", description="boy/girl/net/shop"),
             count: int = Query(3, ge=1, le=20), db: Session = Depends(get_db)):
    """根据类型随机生成N个名字，type:boy/girl/net/shop"""
    rows = (db.query(NameDB)
            .filter(NameDB.type == type, NameDB.is_deleted.is_(False))
            .order_by(func.random()).limit(max(1, min(count, 20))).all())
    names = [r.text for r in rows]
    return {"code": 200, "data": names}

## 4.笑话
@app.get("/api/joke/get", summary="随机笑话(实时优先)", dependencies=[Depends(verify_token)])
def get_joke(mode: str = Query("auto", pattern="^(auto|net|local)$"), db: Session = Depends(get_db)):
    """
    mode=auto 优先实时抓网络段子，失败回退本地库；
    mode=net 强制网络获取；mode=local 只读本地库
    """
    if mode in ("auto", "net"):
        d = net_get("/duanzi")
        if d and d.get("duanzi"):
            return {"code": 200, "mode": "net", "data": d["duanzi"]}
        if mode == "net":
            return {"code": 502, "msg": "网络段子获取失败，请稍后再试"}
    r = random_row(db, LocalJokeDB)
    if not r:
        return {"code": 404, "msg": "数据库暂无笑话，请先添加"}
    return {"code": 200, "mode": "local", "data": r.content}

@app.get("/api/joke/local", summary="本地随机笑话", dependencies=[Depends(verify_token)])
def get_local_joke(db: Session = Depends(get_db)):
    """随机获取一条本地笑话"""
    r = random_row(db, LocalJokeDB)
    if not r:
        return {"code": 404, "msg": "数据库暂无笑话，请先添加"}
    return {"code": 200, "data": r.content}

## 5.热点新闻
@app.get("/api/hot/get", summary="热点查询(实时/本地)", dependencies=[Depends(verify_token)])
def get_hot(source: str = Query("auto", pattern="^(auto|local|weibo|zhihu|douyin|toutiao)$"),
            limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db)):
    """
    source=auto 默认实时抓微博热搜，失败回退本地库；
    source=weibo/zhihu/douyin/toutiao 指定平台实时热榜；
    source=local 查本地库
    """
    if source != "local":
        src = "weibo" if source == "auto" else source
        if src in ("weibo", "zhihu", "douyin", "toutiao"):
            data = net_get(f"/{src}")
            if data:
                res = [{"title": i.get("title"), "hot_value": i.get("hot_value"),
                        "url": i.get("link") or i.get("cover", "")} for i in data[:limit]]
                return {"code": 200, "source": src, "mode": "net", "data": res}
            if source != "auto":
                return {"code": 502, "msg": f"{src} 实时热榜获取失败，请稍后再试"}
    # 本地库回退（缓存 60 秒，写操作后由后台统一失效）
    def load_local():
        data = (db.query(HotNewsDB)
                .filter(HotNewsDB.is_deleted.is_(False))
                .order_by(HotNewsDB.id.desc()).limit(limit).all())
        return [{"id": i.id, "title": i.title, "url": i.url, "add_time": i.add_time} for i in data]
    return {"code": 200, "source": "local", "mode": "local",
            "data": cached(f"hot:local:{limit}", load_local, "/api/hot/get")}

## 5.1 每天60s读懂世界【新增，实时网络数据】
@app.get("/api/news/60s", summary="每天60s新闻(实时)", dependencies=[Depends(verify_token)])
def news_60s():
    """实时获取'每天60s读懂世界'新闻列表"""
    data = net_get("/60s")
    if not data:
        return {"code": 502, "msg": "新闻获取失败，请稍后再试"}
    return {"code": 200, "date": data.get("date"), "data": data.get("news", []),
            "tip": data.get("tip", "")}

## 5.2 实时天气【新增】
@app.get("/api/weather/get", summary="实时天气查询", dependencies=[Depends(verify_token)])
def get_weather(city: str = Query("北京", max_length=30, description="城市名")):
    """实时查询指定城市天气，如：北京、上海、青岛"""
    data = net_get("/weather", {"query": city})
    if not data:
        return {"code": 502, "msg": "天气获取失败，请检查城市名或稍后再试"}
    w = data.get("weather", {})
    loc = data.get("location", {})
    return {"code": 200, "data": {
        "city": loc.get("name", city),
        "condition": w.get("condition"),
        "temperature": w.get("temperature"),
        "humidity": w.get("humidity"),
        "wind_direction": w.get("wind_direction"),
        "wind_power": w.get("wind_power"),
        "updated": w.get("updated"),
    }}

## 5.3 实时汇率【新增】
@app.get("/api/exchange-rate/get", summary="实时汇率查询", dependencies=[Depends(verify_token)])
def get_exchange_rate(currency: str = Query(None, pattern="^[A-Za-z]{3}$", description="三位货币代码")):
    """实时查询人民币汇率，currency如USD/EUR/JPY，不传返回全部"""
    data = net_get("/exchange-rate")
    if not data:
        return {"code": 502, "msg": "汇率获取失败，请稍后再试"}
    rates = data.get("rates", [])
    if currency:
        currency = currency.upper()
        for r in rates:
            if r.get("currency") == currency:
                return {"code": 200, "base": data.get("base_code"), "updated": data.get("updated"), "data": r}
        return {"code": 404, "msg": f"未找到货币：{currency}"}
    return {"code": 200, "base": data.get("base_code"), "updated": data.get("updated"), "data": rates}

## 6.图片壁纸
@app.get("/api/image/daily", summary="每日壁纸(实时优先)", dependencies=[Depends(verify_token)])
def daily_img(mode: str = Query("auto", pattern="^(auto|net|local)$"), db: Session = Depends(get_db)):
    """
    mode=auto 优先实时取必应每日壁纸，失败回退本地库；
    mode=net 强制网络获取；mode=local 只读本地库
    """
    if mode in ("auto", "net"):
        d = net_get("/bing")
        if d:
            return {"code": 200, "mode": "net", "data": {
                "desc": d.get("copyright") or d.get("title"),
                "img_url": d.get("cover_4k") or d.get("cover"),
                "date": d.get("update_date", ""),
            }}
        if mode == "net":
            return {"code": 502, "msg": "必应壁纸获取失败，请稍后再试"}
    r = cached("image:daily:local", lambda: random_row(db, DailyImageDB),
               "/api/image/daily")
    if not r:
        return {"code": 404, "msg": "暂无图片数据，请先添加"}
    return {"code": 200, "mode": "local", "data": {"desc": r.desc, "img_url": r.img_url}}

## 7.成语库【新增】
@app.get("/api/idiom/get", summary="随机查询成语", dependencies=[Depends(verify_token)])
def get_idiom(q: str = Query(None, max_length=20, description="成语精确查询"), db: Session = Depends(get_db)):
    """随机获取成语（含拼音/释义/出处/例句）；传q则精确搜索成语"""
    def to_dict(row):
        return {
            "word": row.word,
            "pinyin": row.pinyin,
            "abbreviation": row.abbreviation,
            "explain": row.explain,
            "derivation": row.derivation,
            "example": row.example,
        }
    if q:
        row = (db.query(IdiomDB)
               .filter(IdiomDB.word == q, IdiomDB.is_deleted.is_(False)).first())
        if not row:
            return {"code": 404, "msg": "未找到该成语"}
        return {"code": 200, "data": to_dict(row)}
    else:
        row = random_row(db, IdiomDB)
        if not row:
            return {"code": 404, "msg": "暂无成语数据，请先添加"}
        return {"code": 200, "data": to_dict(row)}

## 8.古诗词【新增】
@app.get("/api/poem/get", summary="随机古诗词", dependencies=[Depends(verify_token)])
def get_poem(db: Session = Depends(get_db)):
    """随机获取一首诗词（随机 id 定位 + 空洞重试）"""
    row = random_row(db, PoemDB)
    if not row:
        return {"code": 404, "msg": "暂无诗词数据"}
    return {"code": 200, "data": {
        "title": row.title,
        "author": row.author,
        "dynasty": row.dynasty,
        "content": row.content,
    }}

## 9.文案库【新增】朋友圈/短视频文案
@app.get("/api/copy/get", summary="随机文案", dependencies=[Depends(verify_token)])
def get_copy(ctype: str = Query(None, max_length=20, description="文案分类"), db: Session = Depends(get_db)):
    """随机获取文案，支持分类：朋友圈/签名/短视频"""
    conds = [CopywritingDB.ctype == ctype] if ctype else []
    row = random_row(db, CopywritingDB, *conds)
    if not row:
        return {"code": 404, "msg": "暂无文案数据"}
    return {"code": 200, "data": row.content}

## 10.名言短句【新增】励志/治愈短句
@app.get("/api/wisdom/get", summary="随机名言短句(实时优先)", dependencies=[Depends(verify_token)])
def get_wisdom(tag: str = Query(None, max_length=20, description="标签筛选"),
               mode: str = Query("auto", pattern="^(auto|net|local)$"),
               db: Session = Depends(get_db)):
    """
    mode=auto 优先实时获取一言，失败回退本地库；
    mode=net 强制网络获取；mode=local 只读本地库，tag可筛选：励志/治愈/清醒
    """
    if mode in ("auto", "net") and not tag:
        d = net_get("/hitokoto")
        if d and d.get("hitokoto"):
            return {"code": 200, "mode": "net", "data": d["hitokoto"]}
        if mode == "net":
            return {"code": 502, "msg": "一言获取失败，请稍后再试"}
    conds = [WordWisdomDB.tag == tag] if tag else []
    row = random_row(db, WordWisdomDB, *conds)
    if not row:
        return {"code": 404, "msg": "暂无名言数据"}
    return {"code": 200, "mode": "local", "data": row.content}

## 11.随机抽签/骰子【新增，纯逻辑，不需要数据库】
@app.get("/api/random/draw", summary="随机抽签、骰子", dependencies=[Depends(verify_token)])
def random_draw(type: str = Query("dice", pattern="^(dice|number)$"),
                count: int = Query(1, ge=1, le=100)):
    """
    type=dice 骰子(1-6)；type=number 随机数；count生成数量
    """
    res = []
    if type == "dice":
        for _ in range(count):
            res.append(random.randint(1,6))
    elif type == "number":
        for _ in range(count):
            res.append(random.randint(1,100))
    return {"code":200,"data":res}

## 12.土味情话【新增】
@app.get("/api/love/get", summary="随机土味情话", dependencies=[Depends(verify_token)])
def get_love(db: Session = Depends(get_db)):
    """随机获取一条土味情话"""
    row = random_row(db, LoveTalkDB)
    if not row:
        return {"code": 404, "msg": "暂无土味情话数据"}
    return {"code": 200, "data": row.content}

## 13.毒鸡汤【新增】
@app.get("/api/poison/get", summary="随机毒鸡汤", dependencies=[Depends(verify_token)])
def get_poison(db: Session = Depends(get_db)):
    """随机获取一条毒鸡汤"""
    row = random_row(db, PoisonSoupDB)
    if not row:
        return {"code": 404, "msg": "暂无毒鸡汤数据"}
    return {"code": 200, "data": row.content}

## 14.脑筋急转弯【新增】
@app.get("/api/riddle/get", summary="随机脑筋急转弯", dependencies=[Depends(verify_token)])
def get_riddle(db: Session = Depends(get_db)):
    """随机获取一道脑筋急转弯，含题目和答案"""
    row = random_row(db, RiddleDB)
    if not row:
        return {"code": 404, "msg": "暂无脑筋急转弯数据"}
    return {"code": 200, "data": {"question": row.question, "answer": row.answer}}

## 15.歇后语【新增】
@app.get("/api/xiehouyu/get", summary="随机歇后语", dependencies=[Depends(verify_token)])
def get_xiehouyu(q: str = Query(None, max_length=100, description="前半句搜索"), db: Session = Depends(get_db)):
    """随机获取一条歇后语；传q则按前半句搜索"""
    conds = [XiehouyuDB.riddle.contains(q)] if q else []
    row = random_row(db, XiehouyuDB, *conds)
    if not row:
        return {"code": 404, "msg": "暂无歇后语数据"}
    return {"code": 200, "data": {"riddle": row.riddle, "answer": row.answer}}

## 16.绕口令【新增】
@app.get("/api/twister/get", summary="随机绕口令", dependencies=[Depends(verify_token)])
def get_twister(
    format: str = Query("text", pattern="^(text|html)$", description="返回格式：text=纯文本(带\\n换行)，html=换行转为<br>"),
    db: Session = Depends(get_db)
):
    """随机获取一条绕口令，format=html 时换行符替换为 <br> 便于网页直接渲染"""
    row = random_row(db, TwisterDB)
    if not row:
        return {"code": 404, "msg": "暂无绕口令数据"}
    content = row.content.replace("\n", "<br>") if format == "html" else row.content
    return {"code": 200, "data": content}

## 17.答案之书【新增】
@app.get("/api/answer/get", summary="答案之书", dependencies=[Depends(verify_token)])
def get_answer(db: Session = Depends(get_db)):
    """心中默念一个问题，随机翻开一个答案"""
    row = random_row(db, AnswerBookDB)
    if not row:
        return {"code": 404, "msg": "暂无答案数据"}
    return {"code": 200, "data": row.content}

## 18.历史上的今天【新增】
def _save_history_events(db: Session, month_day: str, events: list) -> int:
    """把抓取的历史事件写入数据库（逐条查重），返回新增条数"""
    added = 0
    for e in events:
        exists = db.query(HistoryTodayDB).filter(
            HistoryTodayDB.month_day == month_day,
            HistoryTodayDB.year == e["year"],
            HistoryTodayDB.event == e["content"]).first()
        if exists:
            continue
        db.add(HistoryTodayDB(month_day=month_day, year=e["year"], event=e["content"],
                              category=e["category"], baidu_url=e["baidu_url"]))
        added += 1
    db.commit()
    if added:
        CACHE.clear("history:local:")
    return added


@app.get("/api/history/today", summary="历史上的今天", dependencies=[Depends(verify_token)])
def history_today(date: str = Query(None, pattern="^\\d{1,2}-\\d{1,2}$", description="MM-DD"),
                  mode: str = Query("auto", pattern="^(auto|net|local)$"),
                  db: Session = Depends(get_db)):
    """
    mode=auto 优先读本地库，库中无该日期数据时自动抓取(lishi.yyxw.com)入库后返回；
    mode=net 强制抓取最新；mode=local 只读本地库。
    date格式：10-08，不传默认今天
    """
    from scripts.history_today import fetch_events as fetch_history_events

    if not date:
        date = time.strftime("%m-%d")
    # 规范化日期格式（10-8 -> 10-08）
    try:
        m, d = date.split("-")[:2]
        date = f"{int(m):02d}-{int(d):02d}"
    except (ValueError, AttributeError):
        return {"code": 400, "msg": "date格式错误，应为 10-08"}

    def query_local():
        def load():
            rows = (db.query(HistoryTodayDB)
                    .filter(HistoryTodayDB.month_day == date,
                            HistoryTodayDB.is_deleted.is_(False)).all())
            return [{"year": r.year, "event": r.event, "category": r.category,
                     "baidu_url": r.baidu_url} for r in rows]
        return cached(f"history:local:{date}", load, "/api/history/today")

    if mode == "local":
        res = query_local()
        if not res:
            return {"code": 404, "msg": f"{date} 本地库暂无数据，请先运行 python history_today.py {date}"}
        return {"code": 200, "mode": "local", "date": date, "data": res}

    if mode == "auto":
        res = query_local()
        if res:
            return {"code": 200, "mode": "local", "date": date, "data": res}
        # 库中无数据，自动抓取并入库
        try:
            m, d = date.split("-")
            events = fetch_history_events(m, d)
            if events:
                _save_history_events(db, date, events)
                res = query_local()
                if res:
                    return {"code": 200, "mode": "net+local", "date": date, "data": res}
        except Exception:
            pass
        return {"code": 404, "msg": f"{date} 本地无数据且网络抓取失败，请稍后再试"}

    # mode=net 强制抓取最新数据，入库后返回
    try:
        m, d = date.split("-")
        events = fetch_history_events(m, d)
    except Exception:
        events = []
    if not events:
        return {"code": 502, "msg": "历史今天获取失败，请稍后再试"}
    _save_history_events(db, date, events)
    res = query_local()
    return {"code": 200, "mode": "net", "date": date, "data": res}


@app.get("/api/knowledge/list", summary="知识点分页列表", dependencies=[Depends(verify_token)])
def list_knowledge(page: int = Query(1, ge=1), size: int = Query(10, ge=1, le=100),
                   category: str = Query(None, max_length=50), db: Session = Depends(get_db)):
    """分页查询知识点，返回总数和当前页数据"""
    def load():
        query = db.query(KnowledgeDB).filter(KnowledgeDB.is_deleted.is_(False))
        if category:
            query = query.filter(KnowledgeDB.category == category)
        total = query.count()
        rows = query.order_by(KnowledgeDB.id.desc()).offset((page - 1) * size).limit(size).all()
        res = [{"id": i.id, "category": i.category, "title": i.title, "content": i.content} for i in rows]
        return {"total": total, "data": res}
    data = cached(f"knowledge:list:{page}:{size}:{category or ''}", load, "/api/knowledge/list")
    return {"code": 200, "total": data["total"], "page": page, "size": size, "data": data["data"]}

# =====================【保留外网请求接口（仅公网IP）】=====================
@app.get("/api/ip/public", summary="获取公网IP", dependencies=[Depends(verify_token)])
def get_public_ip():
    """获取本地电脑的公网IP"""
    IP_API_LIST = [
        {"url": "http://myip.ipip.net/", "type": "text"},
        {"url": "https://myip.ipip.net/", "type": "text"},
    ]
    for item in IP_API_LIST:
        try:
            r = requests.get(item["url"], timeout=5)
            r.raise_for_status()
            text = r.text.strip()
            pattern = r"当前 IP：([\d\.]+)\s+来自于：中国\s+([^\s]+)\s+([^\s]+)\s+(.+)"
            m = re.search(pattern, text)
            if m:
                return {"code": 200, "ip": m.group(1), 'city': m.group(2) + m.group(3), 'isp': m.group(4)}
        except Exception as e:
            return {"code": 500, "msg": str(e)}
    return {"code": 500, "msg": "获取公网IP失败，请稍后重试"}

# ===================== 挂载管理后台（/admin 登录后管理接口、日志、统计、批量导入） =====================
from app.admin.panel import setup_admin

_restore_alert_runtime()

# 启动日志自动清理后台线程（每 6 小时按后台设置清理一次）
threading.Thread(target=_auto_clean_loop, daemon=True).start()
# 连接池指标采样线程（概览页历史曲线）
threading.Thread(target=_pool_metric_loop, daemon=True).start()
# 自愈巡检线程（MySQL / 磁盘 / 连接池，超阈值自动修复）
threading.Thread(target=_self_heal_loop, daemon=True).start()
threading.Thread(target=_host_check_loop, daemon=True).start()
log_startup(f"服务启动：环境 {APP_ENV}，版本 {APP_VERSION}，版本前缀 {API_VERSION_PREFIX or '无'}")
log_startup(f"文档地址：{'/docs 已开启' if DOCS_ENABLED else '已关闭（生产环境）'}")
log_startup(f"数据库：{DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}，连接池 "
            f"{DB_POOL_SIZE}+{DB_MAX_OVERFLOW}")

setup_admin(
    app,
    session_factory=SessionLocal,
    table_map=TABLE_MAP,
    switches=API_SWITCHES,
    log_model=ApiLogDB,
    switch_model=ApiSwitchDB,
    token_model=ApiTokenDB,
    ip_rule_model=IpRuleDB,
    setting_model=SettingDB,
    import_log_model=ImportLogDB,
    import_job_model=ImportJobDB,
    breaker_model=BreakerStateDB,
    breaker_reset=reset_breaker,
    script_run_model=ScriptRunDB,
    script_schedule_model=ScriptScheduleDB,
    audit_log_model=AuditLogDB,
    admin_user_model=AdminUserDB,
    security_refresh=refresh_security,
    cache_invalidate=lambda: CACHE.clear(),
    log_switches=API_LOG_SWITCHES,
    cache_ttls=API_CACHE_TTLS,
    master_token=API_TOKEN,
    master_token_hash=MASTER_TOKEN_HASH,
    cookie_secure=ADMIN_COOKIE_SECURE,
    admin_user=ADMIN_USER,
    admin_password=ADMIN_PASSWORD,
    secret=ADMIN_SECRET,
    started_at=START_TIME,
    app_version=app.version,
    export_job_model=ExportJobDB,
    alert_rule_model=AlertRuleDB,
    alert_event_model=AlertEventDB,
    rate_log_model=RateLimitLogDB,
    session_model=AdminSessionDB,
    login_log_model=LoginLogDB,
    schedule_run_model=ScheduleRunDB,
    breaker_event_model=BreakerEventDB,
    pool_metric_model=PoolMetricDB,
    quarantine_model=SensitiveQuarantineDB,
    c_user_model=CUserDB,
)

# C 端门户使用独立 portal_session，并接管受登录保护的 /docs
from app.portal import (
    record_quota_change, setup_portal, start_auto_approval, start_quota_warning,
    start_token_expiry_warning,
)
setup_portal(app, security_refresh=refresh_security)
start_auto_approval(security_refresh=refresh_security)
start_quota_warning()
start_token_expiry_warning()

# 启动每日数据库自动备份（时间 / 保留天数见 config.py，BACKUP_ENABLED=0 可关闭）
from app.services.backup import start_scheduler as _start_backup_scheduler

_start_backup_scheduler()

# 请求签名校验：默认关闭，SIGN_ENABLED=1 且 SIGN_PATHS 非空时对指定路径生效
_SIGN_PATH_LIST = [p.strip() for p in (SIGN_PATHS or "").split(",") if p.strip()]
if SIGN_ENABLED and _SIGN_PATH_LIST:
    app.add_middleware(SignatureMiddleware, secret=SIGN_SECRET or API_TOKEN,
                       paths=_SIGN_PATH_LIST, window=SIGN_WINDOW)
    log_startup(f"请求签名校验：已启用，保护路径 {', '.join(_SIGN_PATH_LIST)}")
# 敏感词过滤：仅处理 /api/ 下的 JSON 响应；注册在版本前缀之前，先由前缀中间件规范化路径
app.add_middleware(SensitiveFilterMiddleware)
# 统一响应元信息：JSON 响应补充 timestamp / request_id
app.add_middleware(ResponseMetaMiddleware)
# 版本前缀中间件：/v1/api/xxx 等价于 /api/xxx，旧路径继续可用
app.add_middleware(VersionPrefixMiddleware, prefix=API_VERSION_PREFIX)
# Gzip 压缩：本地 ECharts 等静态资源与 JSON 响应压缩下发（SSE 等流式响应自动跳过）
app.add_middleware(GZipMiddleware, minimum_size=1024)
# 请求编号：最外层注册，保证所有响应（含异常与静态资源）都带 X-Request-ID
app.add_middleware(RequestIDMiddleware)

def runtime_mode_info():
    """返回当前运行模式与监听参数，供启动与后台「系统诊断」统一读取"""
    host = os.getenv("HOST") or ("127.0.0.1" if IS_PROD else "0.0.0.0")
    port = int(os.getenv("PORT") or ("8000" if IS_PROD else "443"))
    cert = os.getenv("SSL_CERTFILE", "" if IS_PROD else "./openSSL/local.crt")
    key = os.getenv("SSL_KEYFILE", "" if IS_PROD else "./openSSL/local.key")
    return {
        "env": "prod" if IS_PROD else "dev",
        "mode": "生产（Nginx 反代）" if IS_PROD else "开发（自签 HTTPS）",
        "host": host, "port": port,
        "https": bool(cert and key),
        "certfile": cert if cert and key else "",
        "keyfile": key if cert and key else "",        "reload": not IS_PROD,
    }


def _uvicorn_options():
    """按运行环境决定监听参数。
    开发环境：0.0.0.0:443 + openSSL 自签证书，开启热重载，浏览器可直接访问。
    生产环境：127.0.0.1:8000、不加载 SSL（HTTPS 与 443 交给 Nginx 反向代理），关闭热重载。
    HOST / PORT / SSL_CERTFILE / SSL_KEYFILE 环境变量可覆盖对应默认值；
    SSL_CERTFILE 与 SSL_KEYFILE 均非空时才启用 HTTPS。
    """
    info = runtime_mode_info()
    options = {"host": info["host"], "port": info["port"], "reload": info["reload"]}
    if info["https"]:
        options["ssl_certfile"] = info["certfile"]
        options["ssl_keyfile"] = info["keyfile"]
    message = (f"启动模式：{info['mode']} "
               f"监听 {info['host']}:{info['port']}，HTTPS "
               f"{'自签证书' if info['https'] else '由前置代理处理'}")
    log_startup(message)
    print(message, flush=True)
    return options


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", **_uvicorn_options())
