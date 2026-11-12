# -*- coding: utf-8 -*-
"""集中配置：所有可调参数统一从环境变量读取，避免散落在业务代码里硬编码。"""
import os


def _env(name: str, default: str = "") -> str:
    """读取环境变量，未设置时使用默认值"""
    value = os.getenv(name)
    return default if value is None else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# ---------------- 运行环境 ----------------
# APP_ENV=dev 开发环境（默认，开放接口文档）；APP_ENV=prod 生产环境（关闭文档、Cookie 默认 Secure）
APP_ENV = _env("APP_ENV", "dev").strip().lower()          # 运行环境标识：dev=开发，prod=生产
IS_PROD = APP_ENV in ("prod", "production")               # 是否生产环境（由 APP_ENV 推导，无需单独设置）
DOCS_ENABLED = _env_bool("DOCS_ENABLED", not IS_PROD)     # 是否开放 Swagger 文档；生产环境可用 DOCS_ENABLED=1 强制开启
APP_VERSION = _env("APP_VERSION", "1.1.0")                # 版本号，展示在页面/接口健康检查中


# 项目根目录（config.py 位于 app/core/，向上 3 级）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 项目根目录绝对路径，用于定位 data / backups 等目录


def _build_time() -> str:
    """构建时间：优先环境变量（部署时写入），未设置时取 main.py 最后修改时间"""
    value = _env("APP_BUILD_TIME", "").strip()
    if value:
        return value
    try:
        import datetime
        path = os.path.join(BASE_DIR, "main.py")
        return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


APP_BUILD_TIME = _build_time()                          # 构建/发布时间，展示在页脚与诊断页
API_VERSION_PREFIX = _env("API_VERSION_PREFIX", "/v1")  # 版本化前缀：/v1/api/xxx 等价于 /api/xxx

# ---------------- 静态资源路径 ----------------
STATIC_DIR = os.path.join(BASE_DIR, "static")            # 前端静态文件目录（JS / CSS / favicon），随项目分发
FONT_DIR = _env("FONT_DIR", os.path.join(BASE_DIR, "fonts")).strip()  # 验证码等图像字体目录；默认项目根/fonts，可用 FONT_DIR=/custom/path 覆盖

# ---------------- 接口与后台鉴权 ----------------
API_TOKEN = _env("API_TOKEN", "输入自己设置的token")            # 接口访问密钥（入库只存哈希）
ADMIN_USER = _env("ADMIN_USER", "admin")              # 后台登录账号
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "admin123")      # 后台登录密码
ADMIN_SECRET = _env("ADMIN_SECRET", "api-admin-db")      # 会话加密密钥，建议修改
# 部署 HTTPS 后把 ADMIN_COOKIE_SECURE 设为 1（生产环境默认开启），Cookie 将带 Secure 标识
ADMIN_COOKIE_SECURE = _env_bool("ADMIN_COOKIE_SECURE", IS_PROD)      # 后台 Cookie 是否仅 HTTPS 传输
PORTAL_SECRET = _env("PORTAL_SECRET", ADMIN_SECRET + ".portal")      # 用户端会话加密密钥，默认由后台密钥派生
PORTAL_COOKIE_SECURE = _env_bool("PORTAL_COOKIE_SECURE", ADMIN_COOKIE_SECURE)  # 用户端 Cookie 是否仅 HTTPS 传输
PORTAL_DEFAULT_DAILY_QUOTA = _env_int("PORTAL_DEFAULT_DAILY_QUOTA", 500)        # 新用户默认每日调用额度
PORTAL_USER_QPS = _env_int("PORTAL_USER_QPS", 2)                     # 单个用户每秒请求上限（QPS）
PORTAL_TOKEN_ABUSE_LIMIT = _env_int("PORTAL_TOKEN_ABUSE_LIMIT", 30)  # Token 异常使用判定次数
PORTAL_TOKEN_ABUSE_WINDOW = _env_int("PORTAL_TOKEN_ABUSE_WINDOW", 10)  # Token 异常使用统计窗口（秒）
PORTAL_REGISTER_IP_LIMIT = _env_int("PORTAL_REGISTER_IP_LIMIT", 5)   # 同 IP 注册申请次数上限
PORTAL_REGISTER_WINDOW = _env_int("PORTAL_REGISTER_WINDOW", 3600)    # 注册限流窗口（秒），默认 1 小时
PORTAL_EMAIL_CODE_TTL = _env_int("PORTAL_EMAIL_CODE_TTL", 600)       # 邮箱验证码有效期（秒）
PORTAL_EMAIL_RESEND_SECONDS = _env_int("PORTAL_EMAIL_RESEND_SECONDS", 60)  # 同邮箱验证码重发间隔（秒）
PORTAL_EMAIL_DAILY_LIMIT = _env_int("PORTAL_EMAIL_DAILY_LIMIT", 10)  # 单邮箱 24 小时内最多发送验证码次数
PORTAL_CAPTCHA_TTL = _env_int("PORTAL_CAPTCHA_TTL", 300)             # 图形验证码有效期（秒）
PORTAL_API_LOG_SAMPLE = _env_int("PORTAL_API_LOG_SAMPLE", 100)       # C 端调用日志抽样率：100=全量，1=1:100 抽样
PORTAL_API_LOG_KEEP_DAYS = _env_int("PORTAL_API_LOG_KEEP_DAYS", 7)   # C 端调用日志保留天数，到期自动清理
PORTAL_AUTO_APPROVE = _env_bool("PORTAL_AUTO_APPROVE", True)        # 是否自动审核通过用户申请
PORTAL_AUTO_APPROVE_INTERVAL = _env_int("PORTAL_AUTO_APPROVE_INTERVAL", 30)  # 自动审核扫描间隔（秒）
ALLOW_DB_RESET = _env_bool("ALLOW_DB_RESET", False)  # 重置数据库开关：默认禁用，仅首次部署时临时设为 1，用完立即改回
PORTAL_QUOTA_WARN_THRESHOLD = _env_int("PORTAL_QUOTA_WARN_THRESHOLD", 50)  # 额度预警阈值（剩余次数），后台可覆盖，0 表示关闭
PORTAL_QUOTA_WARN_INTERVAL = _env_int("PORTAL_QUOTA_WARN_INTERVAL", 3600)  # 额度预警扫描间隔（秒）
TOKEN_EXPIRY_WARN_DAYS = _env_int("TOKEN_EXPIRY_WARN_DAYS", 7)          # Token 到期前提醒天数，0 关闭
TOKEN_EXPIRY_WARN_INTERVAL = _env_int("TOKEN_EXPIRY_WARN_INTERVAL", 86400)  # Token 到期扫描间隔（秒）
GLOBAL_QPS_LIMIT = _env_int("GLOBAL_QPS_LIMIT", 0)                    # 全站 QPS 总闸，0 表示关闭

# ---------------- MySQL 连接 ----------------
DB_USER = _env("DB_USER", "root")     # 数据库账号
DB_PWD = _env("DB_PWD", "输入数据库密码")   # 数据库密码
DB_HOST = _env("DB_HOST", "127.0.0.1")  # 数据库地址，支持域名/IP
DB_PORT = _env("DB_PORT", "3306")     # 数据库端口
DB_NAME = _env("DB_NAME", "my_api_db")  # 数据库名称
SQLALCHEMY_DATABASE_URL = _env(       # 完整数据库连接串；设置 DATABASE_URL 可覆盖上面各项（如切换 SQLite）
    "DATABASE_URL",
    f"mysql+pymysql://{DB_USER}:{DB_PWD}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4",
)

# ---------------- 连接池 ----------------
DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 10)           # 常驻连接数
DB_MAX_OVERFLOW = _env_int("DB_MAX_OVERFLOW", 20)     # 峰值临时连接数
DB_POOL_TIMEOUT = _env_int("DB_POOL_TIMEOUT", 30)     # 获取连接最长等待秒数
DB_POOL_RECYCLE = _env_int("DB_POOL_RECYCLE", 3600)   # 连接回收秒数，规避 MySQL wait_timeout

# ---------------- 外部数据源 ----------------
TIMEOUT = _env_int("NET_TIMEOUT", 8)                  # 单次外部请求超时（秒）
NET_API = _env("NET_API", "https://60s.viki.moe/v2")  # 60s 开源聚合接口
NET_HEADERS = {"User-Agent": "Mozilla/5.0 (my-api-platform)"}  # 抓取外部数据源时统一使用的请求头

# ---------------- Redis（多实例共享缓存 / 限流 / 分布式锁） ----------------
# 留空使用进程内缓存；配置后缓存、限流计数、nonce、分布式锁都走 Redis
REDIS_URL = _env("REDIS_URL", "")                     # 如 redis://127.0.0.1:6379/3
REDIS_PREFIX = _env("REDIS_PREFIX", "apicache:")      # 键前缀，多项目共用 Redis 时区分

# ---------------- 本地缓存 ----------------
CACHE_TTL = _env_int("CACHE_TTL", 60)                 # 高频只读接口缓存秒数
CACHE_MAX_ITEMS = _env_int("CACHE_MAX_ITEMS", 500)    # 缓存键数量上限（仅进程内后端生效）

# ---------------- 外网熔断 ----------------
BREAKER_THRESHOLD = _env_int("BREAKER_THRESHOLD", 5)  # 连续失败次数阈值
BREAKER_COOLDOWN = _env_int("BREAKER_COOLDOWN", 60)   # 熔断保持秒数，之后半开探测

# ---------------- 导入任务 ----------------
IMPORT_JOB_KEEP_DAYS = _env_int("IMPORT_JOB_KEEP_DAYS", 7)   # 导入任务记录保留天数，到期自动清理
IMPORT_MAX_MB = _env_int("IMPORT_MAX_MB", 20)               # 批量导入单文件大小上限（MB）

# ---------------- 脚本运行监控 ----------------
SCRIPT_LONG_MINUTES = _env_int("SCRIPT_LONG_MINUTES", 30)    # 运行超过该分钟数标记为长时间任务
SCRIPT_TIMEOUT_MINUTES = _env_int("SCRIPT_TIMEOUT_MINUTES", 120)  # 脚本全局超时上限，超时强制结束进程
SCRIPT_ORPHAN_CHECK = _env_int("SCRIPT_ORPHAN_CHECK", 30)    # 孤儿进程守护线程检查间隔（秒）

# ---------------- 数据库自动备份 ----------------
BACKUP_ENABLED = _env_bool("BACKUP_ENABLED", True)    # 是否启用每日自动备份
BACKUP_DIR = _env("BACKUP_DIR", os.path.join(BASE_DIR, "backups"))  # 备份文件存放目录，默认项目根目录下 backups
BACKUP_HOUR = _env_int("BACKUP_HOUR", 18)              # 每天几点执行（0-23，服务器本地时间）
BACKUP_KEEP_DAYS = _env_int("BACKUP_KEEP_DAYS", 7)    # 备份文件保留天数
BACKUP_CHECK_INTERVAL = _env_int("BACKUP_CHECK_INTERVAL", 1800)  # 调度器检查间隔（秒）
# 备份通知：填写企业微信机器人 Webhook 后，备份失败会主动推送；BACKUP_NOTIFY_SUCCESS=1 时成功也推送
BACKUP_NOTIFY_WEBHOOK = _env("BACKUP_NOTIFY_WEBHOOK", "")      # 备份结果推送的企业微信机器人 Webhook，留空不推送
BACKUP_NOTIFY_SUCCESS = _env_bool("BACKUP_NOTIFY_SUCCESS", False)  # 备份成功时是否也推送通知

# ---------------- 邮件通知（环境自检等异常推送） ----------------
MAIL_ENABLED = _env_bool("MAIL_ENABLED", False)      # 是否启用邮件通知
MAIL_HOST = _env("MAIL_HOST", "")                    # SMTP 服务器，如 smtp.qq.com
MAIL_PORT = _env_int("MAIL_PORT", 465)               # 465 走 SSL，587 走 STARTTLS
MAIL_USER = _env("MAIL_USER", "")                    # 登录账号
MAIL_PWD = _env("MAIL_PWD", "")                      # SMTP 授权码
MAIL_FROM = _env("MAIL_FROM", "")                    # 发件人，留空使用 MAIL_USER
MAIL_TO = _env("MAIL_TO", "")                        # 收件人，多个用英文逗号分隔
MAIL_SSL = _env_bool("MAIL_SSL", True)               # 是否使用 SSL（465）
MAIL_TLS = _env_bool("MAIL_TLS", False)              # 是否使用 STARTTLS（587）

# ---------------- 接口告警 ----------------
ALERT_ENABLED = _env_bool("ALERT_ENABLED", True)     # 告警总开关（后台可覆盖）
ALERT_WECHAT_ENABLED = _env_bool("ALERT_WECHAT_ENABLED", True)  # 企业微信推送开关
ALERT_PUSH_START = _env("ALERT_PUSH_START", "00:00")  # 允许推送开始时间（服务器本地时间）
ALERT_PUSH_END = _env("ALERT_PUSH_END", "00:00")      # 允许推送结束时间；与开始相同表示全天
ALERT_WEBHOOK = _env("ALERT_WEBHOOK", "")            # 企业微信机器人 Webhook，留空复用备份通知
ALERT_WEBHOOK_HIGH = _env("ALERT_WEBHOOK_HIGH", "")  # 高优告警渠道（接口不可用/熔断），留空复用 ALERT_WEBHOOK
ALERT_WEBHOOK_LOW = _env("ALERT_WEBHOOK_LOW", "")    # 低优告警渠道（脚本偶发失败等），留空复用 ALERT_WEBHOOK
ALERT_AT_MOBILES = _env("ALERT_AT_MOBILES", "")      # 高优告警 @ 的手机号，逗号分隔；填 @all 提醒所有人
ALERT_COOLDOWN = _env_int("ALERT_COOLDOWN", 300)     # 同一接口同类告警的最小间隔秒数
ALERT_REPEAT_WINDOW = _env_int("ALERT_REPEAT_WINDOW", 600)  # 同类告警统计窗口（秒）
ALERT_REPEAT_LIMIT = _env_int("ALERT_REPEAT_LIMIT", 1)      # 每个窗口内同类告警最多推送次数

# ---------------- 异步导出 ----------------
EXPORT_JOB_KEEP_DAYS = _env_int("EXPORT_JOB_KEEP_DAYS", 7)   # 导出文件保留天数，过期自动清理

# ---------------- 系统监控 ----------------
POOL_METRIC_INTERVAL = _env_int("POOL_METRIC_INTERVAL", 60)     # 连接池指标采样间隔（秒）
POOL_METRIC_KEEP_HOURS = _env_int("POOL_METRIC_KEEP_HOURS", 48)  # 连接池指标保留小时数

# ---------------- 自愈巡检 ----------------
SELF_HEAL_ENABLED = _env_bool("SELF_HEAL_ENABLED", True)        # 是否启用自愈巡检
SELF_HEAL_INTERVAL = _env_int("SELF_HEAL_INTERVAL", 30)         # 主动巡检间隔（秒）
SELF_HEAL_DISK_MIN = _env_int("SELF_HEAL_DISK_MIN", 10)         # 磁盘剩余百分比阈值
SELF_HEAL_POOL_PERCENT = _env_int("SELF_HEAL_POOL_PERCENT", 85)  # 连接池占用百分比阈值
SELF_HEAL_COOLDOWN = _env_int("SELF_HEAL_COOLDOWN", 600)        # 同类修复动作冷却秒数

# ---------------- 主机资源巡检（与页面无关，后台线程定期执行） ----------------
HOST_CHECK_ENABLED = _env_bool("HOST_CHECK_ENABLED", True)      # 是否启用主机资源巡检
HOST_CHECK_INTERVAL = _env_int("HOST_CHECK_INTERVAL", 60)       # 巡检间隔（秒）
ALERT_HOST_PERCENT = _env_int("ALERT_HOST_PERCENT", 90)         # CPU/内存/磁盘使用率告警阈值（%）
BATTERY_CHECK_ENABLED = _env_bool("BATTERY_CHECK_ENABLED", True)  # 是否启用电池电量巡检
BATTERY_LOW_PERCENT = _env_int("BATTERY_LOW_PERCENT", 20)       # 未充电且低于该电量时告警
NETWORK_CHECK_ENABLED = _env_bool("NETWORK_CHECK_ENABLED", True)  # 是否启用外网连通性（Ping）巡检
NETWORK_PING_TARGETS = _env("NETWORK_PING_TARGETS", "223.5.5.5,119.29.29.29")  # Ping 目标，逗号分隔，全部失败才算断网
NETWORK_FAIL_COUNT = _env_int("NETWORK_FAIL_COUNT", 3)          # 全部目标连续失败次数后告警
NETWORK_PING_TIMEOUT = _env_int("NETWORK_PING_TIMEOUT", 2)      # 单次 Ping 超时（秒）
# 桌面通知：网络断开/恢复、主机资源告警时，在 Windows 电脑右下角弹出系统通知（仅 Windows 生效）
DESKTOP_NOTIFY_ENABLED = _env_bool("DESKTOP_NOTIFY_ENABLED", True)  # 是否启用右下角桌面通知
DESKTOP_NOTIFY_MIN_LEVEL = _env("DESKTOP_NOTIFY_MIN_LEVEL", "warning").strip().lower()  # 最低通知级别：warning=告警与恢复，error=仅严重故障

# ---------------- 时区 ----------------
TIME_ZONE = _env("TZ", "Asia/Shanghai").strip()   # 进程时区：服务器默认 UTC 时设为 Asia/Shanghai，让日志、告警与备份时间对齐本地；留空则沿用系统时区

# ---------------- 日志 ----------------
LOG_JSON = _env_bool("LOG_JSON", False)   # 1=输出 JSON 格式日志（便于接入 ELK / Loki）
LOG_FILE = _env("LOG_FILE", "")           # 日志文件路径，留空只输出到控制台
LOG_ROTATE_DAYS = _env_int("LOG_ROTATE_DAYS", 7)   # 日志按天滚动保留份数
LOKI_URL = _env("LOKI_URL", "")           # Loki 推送地址，如 http://127.0.0.1:3100/loki/api/v1/push

# ---------------- IP 风控 ----------------
IP_BURST_LIMIT = _env_int("IP_BURST_LIMIT", 0)          # 单 IP 每分钟请求上限，0=关闭
IP_FREEZE_SECONDS = _env_int("IP_FREEZE_SECONDS", 600)  # 触发上限后冻结时长（秒）
IP_GEO_ENABLED = _env_bool("IP_GEO_ENABLED", False)     # 是否开启地理位置漂移检测
IP_GEO_MINUTES = _env_int("IP_GEO_MINUTES", 5)          # 地理漂移检测窗口（分钟）

# ---------------- Token 轮换 ----------------
TOKEN_ROTATE_GRACE_HOURS = _env_int("TOKEN_ROTATE_GRACE_HOURS", 24)  # 旧 Token 宽限期小时数

# ---------------- 请求签名（防重放） ----------------
# 默认关闭；对重要接口开启后，请求需携带 X-Timestamp / X-Nonce / X-Sign
# 签名算法：HMAC-SHA256(secret, method + "\n" + path + "\n" + timestamp + "\n" + nonce + "\n" + body)
SIGN_ENABLED = _env_bool("SIGN_ENABLED", False)        # 是否启用请求签名校验（默认关闭）
SIGN_SECRET = _env("SIGN_SECRET", API_TOKEN)          # 留空则复用 API_TOKEN
SIGN_PATHS = _env("SIGN_PATHS", "")                   # 逗号分隔的路径前缀，如 /api/pay,/api/message
SIGN_WINDOW = _env_int("SIGN_WINDOW", 300)            # 时间戳允许偏差（秒）
