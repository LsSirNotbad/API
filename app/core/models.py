# -*- coding: utf-8 -*-
"""数据表模型定义：业务表、管理表与表名映射。"""
from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func

from .db import Base


class SoftDeleteMixin:
    """业务表通用软删除字段：删除只做标记，查询默认过滤"""
    is_deleted = Column(Boolean, default=False, server_default="0",
                        comment="软删除标记：0正常 1已删除")


class KnowledgeDB(SoftDeleteMixin, Base):
    """知识点表：存放各类小知识、运维知识点、生活常识等"""
    __tablename__ = "knowledge"
    __table_args__ = {"comment": "知识点表：生活常识、IT知识、历史文学等"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    category = Column(String(100), comment="知识分类：IT/生活/历史/文学等")
    title = Column(String(200), comment="知识点标题")
    content = Column(Text, comment="知识点详细内容")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class RetortDB(SoftDeleteMixin, Base):
    """怼人语录表：存放搞笑/温和/犀利回复语录"""
    __tablename__ = "retort"
    __table_args__ = {"comment": "怼人语录表：温和/犀利/搞笑回复"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    level = Column(String(50), comment="等级：mild温和 / sharp犀利 / funny搞笑")
    content = Column(Text, comment="语录内容")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class NameDB(SoftDeleteMixin, Base):
    """起名词库：男孩名、女孩名、网名、店铺名、项目代号"""
    __tablename__ = "name_lib"
    __table_args__ = {"comment": "起名词库：男孩名/女孩名/网名/店铺名"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    type = Column(String(50), comment="类型：boy男孩名/girl女孩名/net网名/shop店铺名")
    text = Column(String(100), comment="名字文本")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class LocalJokeDB(SoftDeleteMixin, Base):
    """本地笑话表：段子、冷笑话，全部本地读取，不依赖外网"""
    __tablename__ = "local_joke"
    __table_args__ = {"comment": "本地笑话表：段子、冷笑话"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    title = Column(String(512), comment="笑话标题")
    category = Column(String(64), comment="笑话分类")
    source = Column(String(32), comment="来源站点")
    content = Column(Text, comment="笑话内容")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class ApiLogDB(Base):
    """接口调用日志表：记录每次接口访问，方便排查"""
    __tablename__ = "api_log"
    __table_args__ = (
        Index("idx_api_log_time", "time"),
        Index("idx_api_log_path", "path"),
        Index("idx_api_log_ip", "ip"),
        Index("idx_api_log_user_time", "user_id", "time"),
        {"comment": "接口调用日志表"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    time = Column(DateTime, server_default=func.now(), comment="请求时间")
    method = Column(String(10), comment="请求方法")
    path = Column(String(200), comment="请求接口路径")
    ip = Column(String(100), comment="客户端IP")
    params = Column(Text, comment="请求参数")
    status_code = Column(Integer, comment="HTTP状态码")
    duration_ms = Column(Integer, comment="响应耗时(毫秒)")
    user_agent = Column(String(500), comment="客户端UA")
    user_id = Column(Integer, comment="C端用户ID，管理端/主Token为空")
    token_id = Column(Integer, comment="使用的Token ID")


class CUserDB(Base):
    """C端用户表：审批准入、等级与累计调用额度"""
    __tablename__ = "c_user"
    __table_args__ = (
        Index("idx_c_user_status", "status"),
        {"comment": "C端用户：审批状态、等级与累计额度"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    email = Column(String(190), unique=True, nullable=False, comment="登录邮箱")
    password_hash = Column(String(512), nullable=False, comment="PBKDF2-SHA256加盐哈希")
    status = Column(String(20), default="pending", server_default="pending",
                    comment="pending待审批/active正常/banned封禁")
    level = Column(String(20), default="free", server_default="free", comment="用户等级")
    daily_quota = Column(Integer, default=500, server_default="500", comment="累计基础额度")
    bonus_quota = Column(Integer, default=0, server_default="0", comment="累计任务奖励额度")
    today_used = Column(Integer, default=0, server_default="0", comment="累计已用次数（保留历史字段名）")
    quota_date = Column(Date, comment="历史额度日期字段，不再用于跨日清零")
    register_ip = Column(String(100), comment="注册IP")
    approved_at = Column(DateTime, comment="审批通过时间")
    last_login_at = Column(DateTime, comment="最近登录时间")
    created_at = Column(DateTime, server_default=func.now(), comment="注册时间")


class PortalEmailCodeDB(Base):
    """C 端注册邮箱验证码：仅保存带服务端密钥的哈希"""
    __tablename__ = "portal_email_code"
    __table_args__ = {"comment": "C端注册邮箱验证码"}
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(190), unique=True, nullable=False, index=True)
    code_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0, server_default="0")
    last_sent_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class PortalRewardDB(Base):
    """C 端任务进度：每日任务按天领取，一次性新手任务仅可领取一次"""
    __tablename__ = "portal_reward"
    __table_args__ = (
        UniqueConstraint("user_id", "task_key", "task_date", name="uq_portal_reward_daily"),
        Index("idx_portal_reward_user_date", "user_id", "task_date"),
        {"comment": "C端任务进度与累计额度奖励"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("c_user.id"), nullable=False)
    task_key = Column(String(40), nullable=False)
    task_date = Column(Date, nullable=False)
    reward = Column(Integer, default=0, server_default="0")
    claimed_at = Column(DateTime, comment="空表示仅完成任务，尚未领取")
    created_at = Column(DateTime, server_default=func.now())


class PortalQuotaLogDB(Base):
    """C 端统一额度流水：初始额度、管理调整、任务奖励与接口扣减"""
    __tablename__ = "portal_quota_log"
    __table_args__ = (
        Index("idx_portal_quota_user_time", "user_id", "created_at"),
        Index("idx_portal_quota_kind", "kind"),
        {"comment": "C端用户额度变动明细"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("c_user.id"), nullable=False)
    change = Column(Integer, nullable=False, comment="正数增加，负数扣减")
    kind = Column(String(30), nullable=False, comment="initial/admin/task/api_call")
    title = Column(String(100), nullable=False)
    detail = Column(String(500), default="", server_default="")
    path = Column(String(200), default="", server_default="")
    balance_after = Column(Integer, nullable=False, comment="变动后的剩余可用额度")
    operator = Column(String(100), default="system", server_default="system")
    created_at = Column(DateTime, server_default=func.now())


class ApiSwitchDB(Base):
    """接口开关表：后台可启用/禁用某个API路径"""
    __tablename__ = "api_switch"
    __table_args__ = {"comment": "接口开关表：控制单个API是否对外可用"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    path = Column(String(200), unique=True, comment="接口路径")
    enabled = Column(Boolean, default=True, comment="是否启用")
    log_enabled = Column(Boolean, default=True, server_default="1",
                         comment="是否记录调用日志：0关闭可减少日志膨胀")
    cache_ttl = Column(Integer, default=0, server_default="0",
                       comment="独立缓存秒数，0 表示使用全局默认 CACHE_TTL")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class ApiTokenDB(Base):
    """接口密钥表：可配置多个Token，并限制可访问的接口范围"""
    __tablename__ = "api_token"
    __table_args__ = (
        Index("idx_api_token_user", "user_id"),
        {"comment": "接口密钥表：多Token与权限控制"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    name = Column(String(100), comment="密钥名称/用途")
    token = Column(String(64), unique=True, comment="Token 的 SHA-256 哈希（不保存明文）")
    token_hint = Column(String(32), comment="Token 掩码，仅用于后台识别")
    permissions = Column(Text, comment="可访问路径，逗号分隔，* 表示全部")
    enabled = Column(Boolean, default=True, comment="是否启用")
    remark = Column(String(255), comment="备注")
    expires_at = Column(DateTime, comment="过期时间，空表示永久")
    grace_until = Column(DateTime, comment="轮换后旧 Token 的宽限截止时间")
    user_id = Column(Integer, ForeignKey("c_user.id"), comment="所属C端用户，管理Token为空")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class IpRuleDB(Base):
    """IP黑白名单表：黑名单拒绝访问，白名单非空时仅允许白名单访问"""
    __tablename__ = "ip_rule"
    __table_args__ = {"comment": "IP黑白名单表"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    ip = Column(String(100), comment="IP地址")
    rule_type = Column(String(20), comment="类型：black黑名单 / white白名单")
    remark = Column(String(255), comment="备注")
    enabled = Column(Boolean, default=True, comment="是否启用")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class SettingDB(Base):
    """系统配置表：限流、日志自动清理等键值配置"""
    __tablename__ = "app_setting"
    __table_args__ = {"comment": "系统配置表：键值对配置"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    key = Column(String(64), unique=True, comment="配置键")
    value = Column(Text, comment="配置值")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class ImportLogDB(Base):
    """批量导入记录表：记录每次导入结果，失败也可查看详情"""
    __tablename__ = "import_log"
    __table_args__ = {"comment": "批量导入记录表"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    table_key = Column(String(64), comment="数据表标识")
    physical_table = Column(String(64), comment="物理表名")
    filename = Column(String(255), comment="上传文件名")
    total = Column(Integer, comment="有效数据行数")
    inserted = Column(Integer, comment="新增条数")
    skipped = Column(Integer, comment="跳过/重复条数")
    truncated = Column(Integer, comment="字段截断次数")
    status = Column(String(20), comment="结果：success成功 / failed失败")
    message = Column(Text, comment="结果说明")
    detail = Column(Text, comment="失败明细/校验信息")
    operator = Column(String(100), comment="操作人")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class AuditLogDB(Base):
    """后台操作审计表：记录登录、Token、脚本、导入等敏感操作"""
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_log_time", "created_at"),
        Index("idx_audit_log_action", "action"),
        {"comment": "后台操作审计日志表"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    operator = Column(String(100), comment="操作人")
    action = Column(String(64), comment="操作类型：login/token/script/import 等")
    target = Column(String(255), comment="操作对象")
    detail = Column(Text, comment="操作内容")
    ip = Column(String(100), comment="操作IP")
    result = Column(String(20), comment="结果：success失败/success成功")
    created_at = Column(DateTime, server_default=func.now(), comment="操作时间")


class HotNewsDB(SoftDeleteMixin, Base):
    """热点新闻表：手动录入热点资讯，本地查询"""
    __tablename__ = "hot_news"
    __table_args__ = {"comment": "热点新闻表：各平台实时热榜同步"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    title = Column(String(255), comment="热点标题")
    url = Column(String(500), comment="详情链接")
    add_time = Column(String(50), comment="录入时间")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class DailyImageDB(SoftDeleteMixin, Base):
    """每日图片表：壁纸、配图链接，本地存储url"""
    __tablename__ = "daily_image"
    __table_args__ = {"comment": "每日图片表：必应壁纸等图片链接"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    desc = Column(String(255), comment="图片描述")
    img_url = Column(String(500), comment="图片网络地址")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class IdiomDB(SoftDeleteMixin, Base):
    """成语库：成语解释、成语接龙使用"""
    __tablename__ = "idiom_lib"
    __table_args__ = {"comment": "成语库：成语及释义"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    word = Column(String(50), comment="成语")
    pinyin = Column(String(100), comment="拼音")
    abbreviation = Column(String(20), comment="拼音首字母缩写")
    explain = Column(Text, comment="成语释义")
    derivation = Column(Text, comment="出处典故")
    example = Column(Text, comment="例句")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class PoemDB(SoftDeleteMixin, Base):
    """古诗词库：唐诗宋词，随机获取诗词"""
    __tablename__ = "poem_lib"
    __table_args__ = {"comment": "古诗词库：唐诗宋词"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    title = Column(String(100), comment="诗词标题")
    author = Column(String(50), comment="作者")
    dynasty = Column(String(64), comment="朝代")
    content = Column(Text, comment="诗词正文")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class CopywritingDB(SoftDeleteMixin, Base):
    """文案库：朋友圈文案、短视频文案、签名"""
    __tablename__ = "copywriting_lib"
    __table_args__ = {"comment": "文案库：朋友圈/签名/短视频文案"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    ctype = Column(String(50), comment="文案类型：朋友圈/签名/短视频")
    content = Column(Text, comment="文案内容")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class WordWisdomDB(SoftDeleteMixin, Base):
    """短句名言库：格言、人生感悟、励志短句"""
    __tablename__ = "word_wisdom"
    __table_args__ = {"comment": "短句名言库：格言、励志、治愈短句"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    tag = Column(String(50), comment="标签：励志/治愈/清醒")
    content = Column(Text, comment="名言短句")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class LoveTalkDB(SoftDeleteMixin, Base):
    """土味情话库"""
    __tablename__ = "love_talk"
    __table_args__ = {"comment": "土味情话库"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    content = Column(Text, comment="土味情话内容")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class PoisonSoupDB(SoftDeleteMixin, Base):
    """毒鸡汤库"""
    __tablename__ = "poison_soup"
    __table_args__ = {"comment": "毒鸡汤库"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    content = Column(Text, comment="毒鸡汤内容")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class RiddleDB(SoftDeleteMixin, Base):
    """脑筋急转弯库"""
    __tablename__ = "riddle"
    __table_args__ = {"comment": "脑筋急转弯库"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    question = Column(Text, comment="题目")
    answer = Column(Text, comment="答案")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class XiehouyuDB(SoftDeleteMixin, Base):
    """歇后语库"""
    __tablename__ = "xiehouyu"
    __table_args__ = {"comment": "歇后语库"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    riddle = Column(String(255), comment="歇后语前半句")
    answer = Column(String(255), comment="歇后语后半句")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class TwisterDB(SoftDeleteMixin, Base):
    """绕口令库"""
    __tablename__ = "tongue_twister"
    __table_args__ = {"comment": "绕口令库"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    title = Column(String(512), comment="绕口令标题")
    content = Column(Text, comment="绕口令内容")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class AnswerBookDB(SoftDeleteMixin, Base):
    """答案之书：心中有疑问，随机翻一个答案"""
    __tablename__ = "answer_book"
    __table_args__ = {"comment": "答案之书：随机答案"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    content = Column(String(255), comment="答案内容")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class HistoryTodayDB(SoftDeleteMixin, Base):
    """历史上的今天"""
    __tablename__ = "history_today"
    __table_args__ = {"comment": "历史上的今天"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    month_day = Column(String(10), comment="月-日，格式如 10-08")
    year = Column(String(10), comment="事件年份")
    event = Column(Text, comment="历史事件描述")
    category = Column(String(20), comment="分类：古代/近代/现代")
    baidu_url = Column(String(500), comment="百度百科链接")
    created_at = Column(DateTime, server_default=func.now(), comment="写入时间")


class ImportJobDB(Base):
    """批量导入任务表：任务进度持久化，服务重启后可继续查看"""
    __tablename__ = "import_job"
    __table_args__ = (
        Index("idx_import_job_state", "state"), Index("idx_import_job_time", "created_at"),
        {"comment": "批量导入任务表"},
    )
    id = Column(String(32), primary_key=True, comment="任务ID")
    table_key = Column(String(50), comment="目标数据表标识")
    filename = Column(String(255), comment="上传文件名")
    state = Column(String(20), default="running", comment="running/done/failed/interrupted")
    stage = Column(String(50), comment="当前阶段")
    total = Column(Integer, default=0, comment="解析出的总行数")
    processed = Column(Integer, default=0, comment="已处理行数")
    inserted = Column(Integer, default=0, comment="成功写入条数")
    skipped = Column(Integer, default=0, comment="重复跳过条数")
    failed = Column(Integer, default=0, comment="失败条数")
    errors = Column(Text, comment="错误明细 JSON")
    message = Column(Text, comment="结果说明")
    operator = Column(String(100), comment="操作人")
    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")
    finished_at = Column(DateTime, comment="结束时间")


class BreakerStateDB(Base):
    """外网接口熔断状态表：失败次数、熔断截止、累计成功/失败"""
    __tablename__ = "breaker_state"
    __table_args__ = {"comment": "外网接口熔断状态表"}
    key = Column(String(100), primary_key=True, comment="外部接口路径")
    failures = Column(Integer, default=0, comment="连续失败次数")
    open_until = Column(Float, default=0, comment="熔断截止时间戳")
    total_ok = Column(Integer, default=0, comment="累计成功次数")
    total_fail = Column(Integer, default=0, comment="累计失败次数")
    last_error = Column(String(255), comment="最近一次失败原因")
    last_success_at = Column(DateTime, comment="最近成功时间")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(),
                        comment="更新时间")


class ScriptRunDB(Base):
    """脚本运行记录表：记录每次脚本执行的耗时、退出码与结果"""
    __tablename__ = "script_run"
    __table_args__ = (
        Index("idx_script_run_key", "script_key"), Index("idx_script_run_time", "started_at"),
        {"comment": "脚本运行记录表"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    script_key = Column(String(50), comment="脚本标识")
    args = Column(String(100), comment="运行参数")
    state = Column(String(20), comment="running/done/failed/stopped")
    pid = Column(Integer, comment="子进程 PID，用于孤儿进程清理")
    host = Column(String(100), comment="执行实例主机名，多实例下只清理本机进程")
    returncode = Column(Integer, comment="进程退出码")
    duration = Column(Integer, comment="运行耗时秒数")
    started_at = Column(DateTime, comment="开始时间")
    finished_at = Column(DateTime, comment="结束时间")
    output = Column(Text, comment="输出尾部")
    operator = Column(String(100), comment="操作人")


class ScriptScheduleDB(Base):
    """脚本定时任务表：后台可视化配置执行周期"""
    __tablename__ = "script_schedule"
    __table_args__ = {"comment": "脚本定时任务表"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    script_key = Column(String(50), comment="脚本标识")
    args = Column(String(100), comment="运行参数")
    task_type = Column(String(20), default="script", server_default="script",
                       comment="任务类型：script 脚本 / backup 数据库备份")
    cron_expr = Column(String(64), comment="cron 表达式（分 时 日 月 周），空则按间隔分钟")
    backup_dir = Column(String(255), comment="备份目录，仅数据库备份任务使用")
    interval_minutes = Column(Integer, default=60, comment="执行间隔（分钟）")
    enabled = Column(Boolean, default=True, server_default="1", comment="是否启用")
    last_run = Column(DateTime, comment="最近执行时间")
    last_state = Column(String(20), comment="最近执行结果")
    last_duration = Column(Integer, comment="最近耗时秒数")
    next_run = Column(DateTime, comment="下次执行时间")
    operator = Column(String(100), comment="创建人")
    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")


class AdminUserDB(Base):
    """后台账号表：多账号与角色（admin 可读写，viewer 只读）"""
    __tablename__ = "admin_user"
    __table_args__ = {"comment": "后台账号表"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    username = Column(String(50), unique=True, comment="登录账号")
    password_hash = Column(String(128), comment="密码哈希（sha256+盐）")
    role = Column(String(20), default="viewer", comment="角色：admin/viewer")
    enabled = Column(Boolean, default=True, server_default="1", comment="是否启用")
    last_login = Column(DateTime, comment="最近登录时间")
    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")


class SensitiveWordDB(Base):
    """敏感词库：导入拦截与对外文案过滤"""
    __tablename__ = "sensitive_word"
    __table_args__ = {"comment": "敏感词表"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    word = Column(String(100), unique=True, comment="敏感词")
    category = Column(String(50), default="默认", server_default="默认", comment="分类（树状展示一级分组）")
    enabled = Column(Boolean, default=True, server_default="1", comment="是否启用")
    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")


class SensitiveQuarantineDB(Base):
    """敏感词隔离区：导入命中敏感词的数据不直接入库，先在此人工审核"""
    __tablename__ = "sensitive_quarantine"
    __table_args__ = (
        Index("idx_quarantine_status", "status"),
        Index("idx_quarantine_time", "created_at"),
        {"comment": "敏感词隔离区表"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    table_key = Column(String(64), comment="目标数据表标识")
    physical_table = Column(String(64), comment="目标物理表名")
    filename = Column(String(255), comment="来源文件名")
    line_no = Column(Integer, comment="原文件行号")
    content = Column(Text, comment="命中的文本摘要")
    hits = Column(String(500), comment="命中的敏感词")
    row_data = Column(Text, comment="整行数据 JSON")
    status = Column(String(20), default="pending", server_default="pending",
                    comment="pending待审核 / approved已放行 / rejected已驳回")
    operator = Column(String(100), comment="提交人")
    reviewer = Column(String(100), comment="审核人")
    result = Column(String(255), comment="审核结果说明")
    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")
    reviewed_at = Column(DateTime, comment="审核时间")


class ExportJobDB(Base):
    """导出任务表：后台异步生成导出文件，完成后提供下载，过期自动清理"""
    __tablename__ = "export_job"
    __table_args__ = (
        Index("idx_export_job_time", "created_at"),
        {"comment": "异步导出任务表"},
    )
    id = Column(String(32), primary_key=True, comment="任务ID")
    kind = Column(String(32), comment="导出类型：logs/audit")
    state = Column(String(20), default="running", comment="running/done/failed")
    filename = Column(String(255), comment="下载文件名")
    file_path = Column(String(500), comment="服务器文件路径")
    rows = Column(Integer, default=0, comment="导出条数")
    size = Column(Integer, default=0, comment="文件大小字节")
    message = Column(Text, comment="结果说明")
    operator = Column(String(100), comment="操作人")
    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")
    finished_at = Column(DateTime, comment="结束时间")
    expires_at = Column(DateTime, comment="过期时间，过期后自动清理")


class AlertRuleDB(Base):
    """接口告警规则：按接口路径配置调用频率与错误率阈值"""
    __tablename__ = "alert_rule"
    __table_args__ = {"comment": "接口告警规则表"}
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    path = Column(String(200), unique=True, comment="接口路径，空字符串表示全局默认")
    minute_limit = Column(Integer, default=0, comment="每分钟调用次数阈值，0 不限制")
    error_ratio = Column(Integer, default=0, comment="5xx 错误占比阈值（百分比），0 不限制")
    enabled = Column(Boolean, default=True, server_default="1", comment="是否启用")
    remark = Column(String(255), comment="备注")
    created_at = Column(DateTime, server_default=func.now(), comment="创建时间")


class AlertEventDB(Base):
    """告警记录表：普通超限（traffic）与异常报错（error）分开记录"""
    __tablename__ = "alert_event"
    __table_args__ = (
        Index("idx_alert_event_time", "created_at"),
        {"comment": "接口告警记录表"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    path = Column(String(200), comment="接口路径")
    kind = Column(String(20), comment="告警类型：traffic 调用超限 / error 异常报错 / limit 限流")
    value = Column(Integer, default=0, comment="实际值")
    threshold = Column(Integer, default=0, comment="阈值")
    message = Column(String(500), comment="告警内容")
    created_at = Column(DateTime, server_default=func.now(), comment="触发时间")


class RateLimitLogDB(Base):
    """限流日志：记录被限流拒绝的请求，便于识别爬虫与异常访问"""
    __tablename__ = "rate_limit_log"
    __table_args__ = (
        Index("idx_rate_limit_time", "created_at"),
        Index("idx_rate_limit_ip", "ip"),
        {"comment": "限流请求日志表"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    ip = Column(String(100), comment="客户端IP")
    path = Column(String(200), comment="接口路径")
    token_hint = Column(String(32), comment="Token 掩码")
    limit_value = Column(Integer, default=0, comment="触发的阈值")
    created_at = Column(DateTime, server_default=func.now(), comment="触发时间")


class AdminSessionDB(Base):
    """后台在线会话表：用于查看在线登录并强制下线"""
    __tablename__ = "admin_session"
    __table_args__ = {"comment": "后台在线会话表"}
    id = Column(String(64), primary_key=True, comment="会话ID")
    username = Column(String(50), comment="登录账号")
    ip = Column(String(100), comment="登录IP")
    user_agent = Column(String(255), comment="浏览器 UA")
    active = Column(Boolean, default=True, server_default="1", comment="是否有效，0 表示已踢下线")
    created_at = Column(DateTime, server_default=func.now(), comment="登录时间")
    last_seen = Column(DateTime, server_default=func.now(), comment="最近活跃时间")


class LoginLogDB(Base):
    """登录日志表：成功/失败/锁定，记录 IP 与 UA"""
    __tablename__ = "login_log"
    __table_args__ = (
        Index("idx_login_log_time", "created_at"),
        {"comment": "后台与 C 端用户登录日志表"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    username = Column(String(190), comment="登录账号或用户邮箱")
    source = Column(String(20), default="admin", server_default="admin", comment="登录来源：admin/portal")
    ip = Column(String(100), comment="登录IP")
    user_agent = Column(String(255), comment="浏览器 UA")
    state = Column(String(20), comment="success/failed/locked")
    message = Column(String(255), comment="结果说明")
    created_at = Column(DateTime, server_default=func.now(), comment="登录时间")


class ScheduleRunDB(Base):
    """定时任务执行记录：每次执行开始/结束/结果/耗时/失败原因"""
    __tablename__ = "schedule_run"
    __table_args__ = (
        Index("idx_schedule_run_task", "schedule_id"),
        Index("idx_schedule_run_time", "started_at"),
        {"comment": "定时任务执行记录表"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    schedule_id = Column(Integer, comment="定时任务ID")
    name = Column(String(100), comment="任务名称")
    task_type = Column(String(20), comment="任务类型")
    state = Column(String(20), comment="running/done/failed")
    duration = Column(Integer, comment="耗时秒数")
    message = Column(Text, comment="失败原因或摘要")
    output = Column(Text, comment="输出尾部")
    started_at = Column(DateTime, comment="开始时间")
    finished_at = Column(DateTime, comment="结束时间")


class BreakerEventDB(Base):
    """熔断事件表：记录熔断打开与恢复时间，便于评估第三方数据源稳定性"""
    __tablename__ = "breaker_event"
    __table_args__ = (
        Index("idx_breaker_event_time", "created_at"),
        {"comment": "熔断事件记录表"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    key = Column(String(100), comment="外部接口路径")
    kind = Column(String(20), comment="open 熔断 / recover 恢复")
    failures = Column(Integer, default=0, comment="失败次数")
    created_at = Column(DateTime, server_default=func.now(), comment="发生时间")


class PoolMetricDB(Base):
    """连接池时序指标：用于绘制连接池使用率历史曲线"""
    __tablename__ = "pool_metric"
    __table_args__ = (
        Index("idx_pool_metric_time", "created_at"),
        {"comment": "连接池监控时序表"},
    )
    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    size = Column(Integer, default=0, comment="连接池大小")
    checked_out = Column(Integer, default=0, comment="已借出连接")
    overflow = Column(Integer, default=0, comment="溢出连接")
    capacity = Column(Integer, default=0, comment="总容量")
    usage_percent = Column(Float, default=0, comment="使用率百分比")
    created_at = Column(DateTime, server_default=func.now(), comment="采样时间")


# 表名 -> 模型映射，用于通用删除/统计接口
TABLE_MAP = {
    "knowledge": KnowledgeDB,
    "retort": RetortDB,
    "name": NameDB,
    "joke": LocalJokeDB,
    "hot": HotNewsDB,
    "image": DailyImageDB,
    "idiom": IdiomDB,
    "poem": PoemDB,
    "copy": CopywritingDB,
    "wisdom": WordWisdomDB,
    "love": LoveTalkDB,
    "poison": PoisonSoupDB,
    "riddle": RiddleDB,
    "xiehouyu": XiehouyuDB,
    "twister": TwisterDB,
    "answer": AnswerBookDB,
    "history": HistoryTodayDB,
}
