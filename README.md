# 我的自用综合API平台

基于 **FastAPI + MySQL** 的个人综合数据接口平台：对外提供 24 个随机/查询类接口（统一 Token 鉴权），
同时提供 C 端用户门户（邮箱注册/找回密码、专属 Token、额度、接口文档与实用工具）和带登录验证码的管理后台（日志、数据统计、接口开关、批量导入、定时任务）。

> **编码约定**：项目内所有文件（`.py`、`.html`、`.md`、`.json`、`.txt` 等）统一使用 **UTF-8（无 BOM）** 编码，
> 不要在 Windows 下用记事本另存为 ANSI/GBK，否则中文会乱码。
>
> **Windows 终端约定**：运行脚本或执行命令时优先使用 **PowerShell** 或 **Git Bash**，避免使用 CMD。
> CMD 默认使用 GBK 代码页，读取 UTF-8 源码和中文文件名时容易出现乱码或 `SyntaxError`。
>
> ```powershell
> # PowerShell：设置环境变量后启动
> $env:APP_ENV = "prod"; $env:DB_PWD = "你的密码"; python main.py
> ```
>
> ```bash
> # Git Bash：同一条命令内临时设置环境变量
> APP_ENV=prod DB_PWD="你的密码" python main.py
> ```

---

## 一、目录结构

> 下列文件均为 UTF-8 编码。

```
API接口/
├─ main.py                    # 服务入口：公开接口、鉴权、日志中间件、/metrics（re-export 基础模块）
├─ README.md                  # 本说明文档
│
├─ app/                       # 应用代码（Python 包）
│  ├─ __init__.py
│  ├─ core/                   # 基础模块
│  │  ├─ config.py            # 集中配置（支持环境变量覆盖）
│  │  ├─ db.py                # 数据库引擎、会话工厂、Base、get_db
│  │  ├─ models.py            # 全部数据模型与 TABLE_MAP
│  │  ├─ cache.py             # 缓存：默认进程内 TTL，配置 REDIS_URL 后切 Redis（缓存/限流/nonce 共享）
│  │  ├─ dist_lock.py         # 分布式锁：Redis SET NX EX → MySQL GET_LOCK → 进程内锁；host_id() 实例标识
│  │  ├─ metrics.py           # Prometheus 指标：耗时 histogram、路径计数、连接池/缓存命中率
│  │  ├─ sign_utils.py        # HMAC-SHA256 请求签名（canonical/sign/verify/check_nonce/SignClient）
│  │  ├─ mailer.py            # 通知：邮件（SMTP）与企业微信机器人（send_wechat，支持 @手机号）
│  │  ├─ sensitive.py         # 敏感词库：导入前拦截、响应替换、后台维护
│  │  └─ alert_store.py       # 告警中心本地存储：事件追加/裁剪、通知结果、冷却占位（数据库不可用时兜底）
│  ├─ services/               # 运维服务
│  │  └─ backup.py            # MySQL 备份（mysqldump + 逻辑导出兜底）、校验、清理、失败通知
│  ├─ portal.py               # C 端注册/登录/找回密码、Token/额度、接口文档与 12 个用户工具
│  └─ admin/                  # 管理后台
│     └─ panel.py             # 登录、验证码、日志/统计/接口/脚本/导入/定时任务/审计/监控
│
├─ backups/                   # 自动备份输出目录（运行后生成；目录与保留天数可在后台修改）
├─ exports/                   # 后台「数据导出」生成的 Excel 文件（按保留天数自动清理）
│
├─ templates/                 # 后台页面（由 app/admin/panel.py 读取）
│  ├─ admin_login.html        # 登录页（账号+密码+验证码）
│  ├─ admin_dashboard.html    # 后台单页应用（概览/日志/统计/接口管理/脚本管理/批量导入/定时任务/监控）
│  ├─ portal_login.html       # C 端登录、邮箱注册与邮箱验证码找回密码（固定高度三模式卡片）
│  ├─ portal_dashboard.html   # C 端控制台、Token/额度/任务/调用记录与 12 个实用工具
│  ├─ portal_docs.html        # 登录后可访问的接口目录、详情、在线测试与接入代码
│  ├─ firework.html           # 烟花 / 息屏页（概览右上角「烟花模式」进入，可返回后台）
│  └─ 3D_Particles.html       # 3D 粒子演示页（静态展示入口）
│
├─ static/                    # 后台静态资源（无需登录即可加载）
│  ├─ echarts.min.js          # ECharts 5，供概览饼图与趋势图使用
│  ├─ qrcode.min.js           # C 端二维码生成工具的本地依赖
│  ├─ favicon.ico             # 站点图标（浏览器标签页图标）
│  └─ firework/               # 烟花页依赖：fscreen.js、stage.js、mymath.js（原 CDN 脚本已本地化）
│
├─ fonts/                     # 跨平台字体目录（图形验证码绘制用；随代码一起分发，保证各环境字形一致）
│  └─ DejaVuSans-Bold.ttf     # Bitstream Vera 开源字体，可自由分发；可用环境变量 FONT_DIR 指向自定义目录
│
├─ scripts/                   # 独立运行的运维/采集脚本（不参与服务启动）
│  ├─ __init__.py             # 包标识，使 from scripts.xxx import ... 可用
│  ├─ check_env.py            # 环境自检：MySQL 连通性、字符集、mysqldump、目录读写权限
│  ├─ 数据初始化.py            # 一键：补表/字段注释 + 灌入基础示例数据（已有数据的表自动跳过）
│  ├─ 数据清理.py              # 清理空白/空格污染与格式异常数据（只处理 17 张内容表）
│  ├─ 重置数据库.py            # 危险操作：默认禁用，仅首次部署（ALLOW_DB_RESET=1）时可双重确认执行
│  ├─ 数据同步.py              # 从 60s API 抓取热榜/笑话/名言/壁纸/历史并入库
│  ├─ 数据导入.py              # 把 data/*.json（歇后语、成语）去重导入数据库
│  ├─ history_today.py        # 历史上的今天采集（单日 / 指定日期 / 全年 366 天）
│  ├─ joke.py                 # 笑话爬虫，写入 local_joke 表
│  ├─ tongue_twister.py       # 绕口令爬虫，写入 tongue_twister 表
│  └─ answer_book.py          # 答案之书采集，写入 answer_book 表
│
├─ tests/                     # 单元测试（标准库 unittest，无需额外依赖）
│  ├─ test_alert_store.py     # 告警事件读写、通知结果合并、冷却占位、网络状态原子更新、并发写入
│  ├─ test_captcha.py         # 验证码图片尺寸、字体来源、一次性校验（成功/失败/非法输入）
│  ├─ test_config.py          # 环境变量解析与关键默认值
│  └─ test_sign_utils.py      # 签名规范化、签名一致性、时间窗口、防重放
│
├─ data/                      # 离线数据文件（仅导入脚本使用，不对外提供）
│  ├─ idiom.json              # 成语原始数据（约 3 万条，含拼音/释义/出处/例句）
│  ├─ xiehouyu.json           # 歇后语原始数据（约 1.4 万条）
│  ├─ envcheck_runs.json      # 环境自检历史记录（后台「环境自检」读写，勿手动编辑）
│  ├─ alert_events.jsonl      # 告警事件流水（告警中心读写，超过 5000 条自动裁剪，勿手动编辑）
│  └─ alert_runtime.json      # 告警巡检状态、通知结果与冷却时间戳（勿手动编辑）
│
├─ openSSL/                   # 本地自签名证书（仅在直接以 443 启动时使用；生产走 Nginx 反代可忽略）
│  ├─ local.crt / local.key   # 本地开发用证书与私钥，可用 SSL_CERTFILE / SSL_KEYFILE 指定其他路径
│
├─ .env / .env.example        # 本地环境变量示例（真实密钥不要提交到仓库）
├─ compose.yaml / Dockerfile  # Docker Compose 部署文件
├─ requirements.txt           # Python 依赖清单（均已限定主版本区间）
│
└─ archive/                   # 历史归档（代码不读取，可整体删除或另存备份）
   └─ my_api_data.db          # 早期 SQLite 数据库文件，仅历史归档，不要在程序中读取
```

---

## 二、各文件详细说明

### 1. `main.py`（核心，必须保留）
服务入口，`python main.py` 即启动。代码按职责拆分到独立模块，`main.py` 只保留应用实例、中间件、鉴权与公开路由；同时把 `models / engine / SessionLocal / TABLE_MAP` 等名称原样 re-export，保证 `scripts/*.py` 里的 `from main import ...` 继续可用。

| 区块 | 内容 |
| --- | --- |
| 基础模块 | `app/core/config.py` 集中配置；`app/core/db.py` 管理引擎与会话；`app/core/models.py` 存放全部模型；`app/core/cache.py` 提供缓存（进程内 TTL 或 Redis）；`app/core/dist_lock.py` 提供跨实例分布式锁；`app/core/metrics.py` 采集 Prometheus 指标；`app/core/sign_utils.py` 提供请求签名；`app/services/backup.py` 负责每日自动备份。`main.py` 顶部统一导入并对外暴露这些名称 |
| 环境区分 | `APP_ENV=dev`（默认）或 `APP_ENV=prod`。生产环境默认关闭 `/docs`、`/redoc`、`/openapi.json`，后台会话 Cookie 默认追加 `Secure`；需要时可用 `DOCS_ENABLED=1` 单独放开文档 |
| 健康检查 | `GET /health`（无需 Token、不记调用日志）返回服务状态、环境、版本、运行时长、数据库连通性与耗时、**连接池指标**（`database.pool`：常驻连接数、已借出 `checked_out`、等待中的 `overflow`、容量、使用率 `usage_percent`）、缓存命中统计；连接池使用率达到 80% 时额外返回 `warning` 提示，数据库不可用返回 HTTP 503 |
| 版本前缀 | 通过最外层中间件把 `/v1/api/xxx` 重写为 `/api/xxx`，旧路径继续可用；前缀可用 `API_VERSION_PREFIX` 调整，健康检查与后台路径同样支持 `/v1` 访问 |
| 配置区 | 位于 `app/core/config.py`，**每个字段均带中文注释**：`API_TOKEN`（接口密钥）、`ADMIN_USER/ADMIN_PASSWORD/ADMIN_SECRET`（后台账号与会话密钥）、`ADMIN_COOKIE_SECURE`（HTTPS 下设为 1）、`DB_*`（MySQL 连接）、`NET_API`（外部数据源）、`NET_TIMEOUT`、`CACHE_TTL`、`BREAKER_THRESHOLD/BREAKER_COOLDOWN`、`PORT`/`SSL_CERTFILE`/`SSL_KEYFILE`（直接以 443 启动时使用）、`STATIC_DIR`/`FONT_DIR`（静态资源与验证码字体目录）；全部可用同名环境变量覆盖，避免密钥写死在代码里 |
| 数据库连接池 | `app/core/db.py` 中 `pool_size=10`、`max_overflow=20`、`pool_timeout=30`、`pool_recycle=3600`、`pool_pre_ping=True`：连接超 1 小时自动回收，取连接前先探活，规避 MySQL `wait_timeout` 断连与连接耗尽 |
| 数据模型 | `app/core/models.py` 中 44 张表的 SQLAlchemy 定义，均带 `__table_args__` 表注释、字段注释和 `created_at` 写入时间；17 张业务表统一继承 `SoftDeleteMixin`，其中 `is_deleted` 字段**仅用于兼容历史模型，当前业务不再读写**（后台已不提供删除/回收站功能） |
| 业务表 | `knowledge` 知识点、`retort` 怼人语录、`name_lib` 起名、`local_joke` 笑话、`hot_news` 热点、`daily_image` 壁纸、`idiom_lib` 成语、`poem_lib` 诗词、`copywriting_lib` 文案、`word_wisdom` 名言、`love_talk` 土味情话、`poison_soup` 毒鸡汤、`riddle` 脑筋急转弯、`xiehouyu` 歇后语、`tongue_twister` 绕口令、`answer_book` 答案之书、`history_today` 历史今天 |
| 管理表 | `api_log` 调用日志、`api_switch` 接口开关与独立日志/缓存配置、`api_token` 多密钥与权限、`ip_rule` 接口 IP 黑白名单、`app_setting` 系统配置、`import_log` 批量导入记录、`audit_log` 后台操作审计 |
| 鉴权 | `verify_token` 依赖：请求头 `token` 校验；数据库只保存 Token 的 SHA-256 哈希（`api_token.token`）与掩码 `token_hint`，不落明文；主 Token 同样按哈希比对（`hmac.compare_digest`）。支持主 Token + 后台新建的多个 Token，按 Token 配置可访问路径、启用状态、过期时间；开启限流后超限返回 429 |
| 缓存 | `cached(key, loader)` 对只读接口做短期缓存：默认 `CACHE_TTL=60` 秒，**可在「接口列表」按接口单独设置 `cache_ttl`**（0 表示使用全局默认）；接口命中后按 `api_switch.cache_ttl` 独立过期，静态内容（成语、诗词）可调大，热点新闻可调小。后台导入完成、脚本结束统一清空缓存 |
| 熔断降级 | `net_get` 按外部路径统计连续失败，连续 `BREAKER_THRESHOLD=5` 次失败后熔断 `BREAKER_COOLDOWN=60` 秒；熔断期间直接返回 `None`，接口立即回退本地库，不再卡在外部超时；冷却结束后放行一次探测（半开），成功即恢复 |
| 参数校验 | 所有公开接口的查询参数改为 `Query(...)` 约束：分页 `page>=1`、`size` 1-100、`limit` 1-50、`count` 1-100，枚举参数（`mode/source/type/format`）用正则限定取值，日期限定 `MM-DD`，货币限定三位字母，字符串限长；不合法直接返回 422，不再静默容错 |
| 自动备份 | `app/services/backup.py` 支持两种入口：①内置调度线程每天 `BACKUP_HOUR` 点自动执行（当天已有备份则跳过）；②在后台「定时任务」中新建 `backup` 类型任务，用 cron 或间隔自由指定时间和**独立备份目录**。存在启用的 backup 定时任务时，内置每日线程自动让位，避免重复备份。优先 `mysqldump --single-transaction`，未安装时回退纯 Python 逻辑导出；输出 gzip 并做完整性校验，超过保留天数自动清理；也可 `python -m app.services.backup` 手动执行 |
| 中间件 | `api_log_middleware`：先校验**接口 IP 黑白名单**，再判断接口开关与**接口独立日志开关**（`api_switch.log_enabled`），最后记录每次 `/api/*` 调用的方法、路径、IP、参数、状态码、耗时、UA；写入前对 `token/secret/password/api_key/authorization` 等参数值统一脱敏为 `***`；黑名单或白名单外 IP 直接返回 403。关闭日志的接口不再写 `api_log`，但鉴权、限流、IP 校验照常生效 |
| 自动清理 | 后台线程每 6 小时按 `app_setting` 中的设置清理历史调用日志（默认保留 30 天）与**过期备份文件**（独立开关 + 保留天数 + 备份目录） |
| 启动 | `python main.py` 按 `APP_ENV` 自动切换：**dev** 监听 `0.0.0.0:443` + `openSSL` 自签证书并开启 reload；**prod** 监听 `127.0.0.1:8000`、不加载 SSL、关闭 reload（443 与 HTTPS 交给 Nginx）。可用 `HOST`/`PORT`/`SSL_CERTFILE`/`SSL_KEYFILE` 覆盖；`SSL_CERTFILE`、`SSL_KEYFILE` 均为空时不启用 HTTPS。启动时会打印当前监听模式并写入「系统诊断」启动日志 |
| 末尾 | 调用 `app.admin.panel.setup_admin(...)` 挂载 `/admin` 后台 |

> 公开接口共 24 个，全部需要 token；管理类接口（日志、平台状态、统计）已从公开 API 移除，只在后台登录后可见。
> 接口文档：开发环境 `https://127.0.0.1/docs`（443 自签证书）、`https://127.0.0.1/openapi.json`；生产环境默认关闭。

### 2. `app/admin/panel.py`（后台逻辑）
通过 `setup_admin()` 一次性注册所有后台路由，不污染公开接口的 OpenAPI 文档（`include_in_schema=False`）。

- 会话：`SessionMiddleware` 签名的 `admin_session` Cookie，有效期 8 小时；Cookie 固定带 `HttpOnly`（脚本无法读取）与 `SameSite=Lax`，部署到 HTTPS 后把环境变量 `ADMIN_COOKIE_SECURE=1` 即可追加 `Secure` 标识。
- 登录：`GET /admin/login` 页面 → `POST /admin/login` 校验账号密码 + 验证码；验证码 300 秒有效且**只能使用一次**（无论校验成功还是失败都立即作废，需重新获取），连续失败 5 次锁定 60 秒；登录成功/失败、锁定、登出均写入审计。
- 审计日志：后台登录、Token 增改删、接口规则与开关、限流/清理设置、脚本运行/停止、批量导入、**数据导出**、定时任务增删改、备份执行等敏感操作都会写入 `audit_log` 表（操作人、动作、目标、详情、IP、结果、时间）；导出操作会记录表名与筛选条件（关键词、时间范围、匹配与导出条数）。后台「日志管理 → 操作审计」页可按动作/操作人/结果/时间段/关键词筛选，`GET /admin/api/audit-logs` 提供查询接口。
- 脚本进程管控：脚本启动时把 PID 写入 `script_run.pid`；守护线程按 `SCRIPT_ORPHAN_CHECK` 秒检查一次，发现「主进程已标记停止但子进程仍在运行」时主动 `taskkill /T`（Windows）或 `SIGKILL`（Linux）结束进程树；所有脚本统一受 `SCRIPT_TIMEOUT_MINUTES` 全局超时约束，超时强制终止，避免爬虫卡死残留进程。
- 脚本输出实时推送：`GET /admin/api/scripts/{key}/stream` 以 SSE 推送脚本运行状态与输出（每 1 秒检测一次变化，快照不变时发 keep-alive，结束或空闲超时发 `end` 事件并关闭）；前端优先用 `EventSource`，连接出错或收到 `end` 后回退到轮询。
- 批量导入异步化：`POST /admin/api/import` 上传后立即返回 `job_id` 并在后台线程处理，`GET /admin/api/import/jobs/{job_id}` 查询进度（阶段 / 已处理条数 / 总数）；前端每 800ms 轮询进度并展示进度条，完成后自动刷新导入记录。任务结果保留 1 小时后自动清理。
- 导入预览（dry-run）：提交时带 `dry_run=1`，只做解析、必填/类型/查重/敏感词校验，返回有效、可导入、重复、错误数量与逐行错误，**不写数据库**，适合大批量数据入库前先校验脏数据。
- 上传安全：仅接受 `.xlsx / .xlsm / .csv`，单文件大小上限 `IMPORT_MAX_MB`（默认 20MB），xlsx 校验 `PK\x03\x04` 文件头、CSV 检查是否含二进制内容，避免仅凭后缀判断类型。
- 概览/脚本轮询自适应：标签页处于隐藏时（`visibilityState === 'hidden'`）暂停轮询，恢复可见后立即刷新一次；并且用 `ovLoading` 标记做 in-flight 去重，避免定时器叠加造成堆积；脚本 SSE 连接存活期间跳过该脚本的轮询。
- 验证码：`GET /admin/captcha` 直接输出 SVG 图片（不依赖 PIL）。
- 静态资源：`GET /admin/static/{filename}` 读取项目 `static/` 目录（仅限该目录内文件），用于加载本地 ECharts 5、`favicon.ico` 与烟花页依赖脚本。
- 烟花 / 息屏页：`GET /admin/firework` 返回 `templates/firework.html`（需登录），从概览右上角「烟花模式」进入，页面右下角「← 返回后台」可回管理后台。
- 概览图表：请求状态分布 + 接口调用次数分布均为 ECharts 环形饼图（`radius 40%~70%`、图例、悬停显示名称与次数），数据每 4 秒刷新，窗口/菜单切换自动 resize；接口饼图取调用次数前 8 的接口，其余合并为「其他接口」。
- 后台 JSON 接口（均需登录）：
  - `GET /admin/api/session` 当前登录状态
  - `GET /admin/api/overview` 平台概览（服务器时间戳、启动时长、调用量、状态码分布、各接口调用次数分布 `path_dist`、热门接口中文名）
  - `GET /admin/api/logs` 调用日志查询（按路径/IP/状态码/时间段筛选 + 分页；`min_status`/`max_status` 支持错误区间）
  - `POST /admin/api/logs/clear` 按天数清理日志
  - `GET /admin/api/endpoints`、`POST /admin/api/endpoints/toggle` 查看接口列表，并支持启用/禁用、**独立日志开关**（`log_enabled`）、**独立缓存时长**（`cache_ttl`，秒，0 表示使用全局默认）
  - `GET /admin/api/tokens`、`POST /admin/api/tokens` 多 Token 列表与新建（留空自动生成）；列表只显示掩码 `token_hint`，新建时明文只在响应中返回一次，入库即转换为 SHA-256 哈希
  - `POST /admin/api/tokens/{id}/update`、`POST /admin/api/tokens/{id}/delete` 修改权限/启停/过期时间、删除
  - `POST /admin/api/tokens/{id}/rotate` Token 轮换：旧 Token 进入 `TOKEN_ROTATE_GRACE_HOURS` 宽限期，新 Token 立即生效
  - `GET /admin/api/master-token` 获取主 Token 掩码（仅后台登录后可见；不返回明文，在线测试需手工填写 Token）
  - `GET /admin/api/ip-rules`、`POST /admin/api/ip-rules`、`POST /admin/api/ip-rules/{id}/update|delete` **接口 IP 黑白名单**
  - `GET|POST /admin/api/admin-ip` **后台访问 IP 白名单**（独立于接口 IP 黑白名单）
  - `GET|POST /admin/api/rate-settings` 限流开关、统计维度（Token/IP）、每分钟上限
  - `GET|POST /admin/api/log-settings` 日志自动清理开关、保留天数、容量上限、**备份文件自动清理**（开关 / 保留天数 / 目录）、上次清理结果
  - `GET /admin/api/import-logs`、`GET /admin/api/import-logs/{id}` 批量导入记录与失败详情
  - `GET /admin/api/tables`、`GET /admin/api/quality` 各表记录量与字段、数据质检（空内容 / 超长 / 重复）
  - `GET /admin/api/export?table_key=xxx` 按表导出 Excel（`keyword`、`date_from`、`date_to` 筛选，单次最多 5 万条），并写入导出审计
  - `GET /admin/api/scripts` 脚本列表与运行状态
  - `POST /admin/api/scripts/run` 后台运行脚本（白名单校验参数，子进程执行，输出实时收集）
  - `POST /admin/api/scripts/stop` 停止正在运行的脚本
  - `GET /admin/api/scripts/{key}/stream` SSE 推送脚本运行状态与输出（前端优先使用）
  - `GET /admin/api/schedules`、`POST /admin/api/schedules` 定时任务列表与新建（`task_type=script|backup`，支持 `cron_expr` 5 段表达式或 `interval_minutes`；backup 任务可指定 `backup_dir`）
  - `POST /admin/api/schedules/{id}/toggle|run|delete` 启用/停用、立即执行、删除定时任务
  - `GET /admin/api/template?table_key=xxx` 下载批量导入 Excel 模板（表头含字段说明 + 「字段说明」工作表）
  - `POST /admin/api/import` 上传 xlsx/csv 创建异步导入任务，立即返回 `job_id`（必填校验 + md5 查重 + 超长自动截断 + 敏感词入库前拦截；重复行跳过、格式错误逐行记录、入库失败逐条定位；成功/失败都会写入 `import_log`；`dry_run=1` 时只预览不写入）
  - `GET /admin/api/import/jobs`、`GET /admin/api/import/jobs/{job_id}` 异步导入任务列表、阶段、进度与结果
  - `GET /admin/api/audit-logs` 操作审计查询（动作/操作人/结果/时间段/关键词 + 分页）
  - `GET /admin/api/backups` 查看自动备份配置与最近的备份文件
  - `POST /admin/api/backups/run` 立即执行一次数据库备份（写入审计；失败时按需推送通知）
  - `POST /admin/api/backups/restore` 从校验通过的备份一键恢复（需传 `confirm=RESTORE`；恢复前自动再做一次回退备份）
  - `GET /admin/api/portal/review-mode`、`POST /admin/api/portal/review-mode` C 端注册审核模式（`auto` 自动 / `manual` 人工）
  - `GET /admin/api/portal/quota-warn`、`POST /admin/api/portal/quota-warn` 剩余额度预警阈值（0 表示关闭）
  - `GET /admin/api/sensitive/hits`、`POST /admin/api/sensitive/hits/reset` 敏感词命中统计与清零
  - `GET /admin/api/metrics/summary` Prometheus 指标摘要（总请求 / 错误 / 平均耗时 / 耗时分桶 / Top 路径）
  - `GET /admin/api/latency` 延迟趋势（`path` 过滤、`compare=1` 返回上一周期 `p95_prev` 用于对比）
  - `GET /admin/api/usage-stats?days=7` 接口用量看板：按接口聚合最近 1~30 天的调用量、错误率与平均耗时

> 脚本只能运行 `app/admin/panel.py` 中 `SCRIPTS` 白名单里的预置脚本，不接受任意命令。子进程用参数列表传入（`shell=False`，不拼接命令行），脚本路径限制在 `scripts/` 目录内，参数按类型校验（选项 / MM-DD 日期 / JSON 文件名）并再过一遍 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` 正则白名单，防止命令注入。
> Token、IP 规则、限流/清理配置统一缓存 30 秒；后台保存时只**标记失效**（信号标记），不会清空全部缓存，避免配置变更瞬间大量请求同时回源数据库，读取方在下次访问时按需重建。

### 3. `templates/admin_login.html`
登录页：渐变卡片样式，用户名/密码/验证码输入，点击验证码图片可刷新，错误信息通过 URL 参数回显。

### 4. `templates/admin_dashboard.html`
后台单页应用，左侧菜单按功能分组；右上角显示当前登录账号、角色、登录时间与「退出登录」入口。

顶部标签页：每点击一个菜单就在页面顶部新增一个标签（点击已打开菜单只切换不重复添加），同时最多保留 10 个；超过 10 个时弹窗提示「将关闭最早的标签」，提示中「本次登录内不再提示」默认勾选，勾选后不再弹窗、直接自动关闭最早打开的非当前标签并打开新页面。标签可点「×」单独关闭，关闭当前标签会自动切到相邻标签；标签支持**拖拽排序**，右键可「关闭左侧 / 右侧 / 全部」；标签栏右侧的「全部关闭」可一键清空并回到平台概览。

左侧菜单折叠改为菜单栏右边缘的圆形箭头按钮（展开态箭头朝左、折叠态朝右），折叠状态记录在浏览器本地；右上角账号信息下方实时显示服务器时间（每秒刷新），不再单独占据工具栏一行；工具栏不再提供「接口文档」内嵌预览入口（需要看文档直接访问 `/docs`）。

- **概览**：顶部 3 张大卡片分别展示服务器时间（每秒刷新）、运行时长（可切换单位）、数据库大小；数字卡片带滚动动画；每 4 秒自动刷新（标签页隐藏时暂停，切回立即刷新一次）；统计卡片 4 列均匀排列（含**磁盘占用**，磁盘剩余低于 5GB 时卡片变红预警）；请求状态环形图（带旋转高光与数值切换动画，悬停中心文字使用小字号避免遮挡环形）；接口延迟分位数折线图（P50 / P95 / P99，按天 14 天 / 按小时 24 小时）；热门接口显示中文名 + 路径 + 柱状条；**接口用量看板**（7 / 14 / 30 天切换，按接口展示调用量、错误率与平均耗时，错误率高于 5% 标红）；底部信息区展示数据库/备份目录占用/磁盘剩余。
- **接口管理**：接口列表（启停开关 + **独立日志开关** + **独立缓存时长**输入框）、在线测试（选接口后自动按接口参数注解生成参数模板，可保存自定义模板，展示状态码/耗时/返回）、数据统计（各表记录量与字段，支持一键**导出 Excel**、数据质检；数据表统计 / 数据质检 / 数据预览与在线修改三个面板均可折叠）、限流 & 黑白名单（**接口 IP 黑白名单**与**后台访问 IP 白名单**分开配置）。
- **Token 密钥管理**：Token 列表（仅显示掩码，新建、启停、删除、过期时间；新建成功时明文只显示一次并尝试写入剪贴板）；每行「编辑权限」按钮弹出接口勾选面板，直接维护该 Token 的可访问路径（全选 = `*`，全部取消 = 无权限），保存后立即刷新权限缓存生效。
- **日志管理**：请求日志（错误请求已合并进来，状态码下拉可选「全部错误（≥400）/4xx/5xx」）、操作审计（按动作/操作人/结果/时间段/关键词筛选，记录登录、Token、脚本、导入、导出等敏感操作）、自动清理（日志默认 30 天，可选 7/30/90/180/365 天，只保存设置；手动清理历史日志与备份文件统一放在「手动清理」区）。
- **系统**：脚本管理（查看状态、参数选择、运行/停止、SSE 实时输出控制台，带**自动滚动开关**：用户向上滚动查看历史时自动暂停，滚回底部恢复；断线自动回退轮询）、批量导入（选择文件后显示文件名/大小/已就绪卡片，可勾选**预览校验（dry-run）**只校验不入库；提交后在后台异步导入并显示阶段与进度条；导入记录与任务详情的「错误记录」支持**一键复制全部**，便于粘到 Excel 清洗）、定时任务（按 cron 或固定间隔自动运行脚本，或执行**数据库备份**并指定备份目录；页面内附备份列表、「立即备份」与「恢复」按钮，恢复需二次确认并输入 `RESTORE`）。
- **运维监控**：环境自检、限流日志、在线会话、登录日志、导出记录、服务监控、**智能自愈与告警中心**（企业微信 Webhook 统一配置：接口高频调用/错误率、熔断、脚本失败、CPU/内存/磁盘超限四类开关，支持发送测试消息与告警事件筛选/清空）。
- 顶部全局搜索支持 `Ctrl+K` 快捷聚焦，↑↓ 选择、回车跳转、Esc 关闭；搜索范围包含接口、页面、脚本、Token、定时任务、数据表与调用日志。

前端 `fetch` 携带同源 Cookie，收到 401 自动跳回登录页；无第三方 CDN 依赖，离线可用。
顶部工具栏提供「暗色模式 / 亮色模式」切换：主题写入 `localStorage`，刷新后保持；首次访问跟随系统 `prefers-color-scheme`。切换时按当前配色重建概览饼图并预加载 ECharts 暗色主题，避免切换时图表闪烁或样式错乱。

### 5. C 端用户门户（`app/portal.py` + `portal_*.html`）

- **账号与邮件验证**：`/portal/login` 在同一固定高度卡片中切换登录、申请账号和忘记密码；注册与找回密码均使用邮箱验证码，两种验证码用途隔离，重置密码不会绕过原密码以外的账号状态校验。邮件发送使用 `MAIL_*` SMTP 配置。
- **验证码与防轰炸**：发送邮箱验证码前必须通过图形验证码（`PORTAL_CAPTCHA_TTL` 默认 300 秒，**无论校验成功与否立即销毁**，一次性使用）；同一邮箱 24 小时内最多发送 `PORTAL_EMAIL_DAILY_LIMIT`（默认 10）次（**按发送尝试计数，SMTP 失败同样计入**，防止失败重试刷爆发信配额），同一 IP 每小时限 `PORTAL_REGISTER_IP_LIMIT`（默认 5）次申请，超限直接拒绝并提示。
- **日志抽样**：C 端调用记录按 `PORTAL_API_LOG_SAMPLE`（默认 1:100）抽样写入，控制台展示抽样口径与真实总调用量，避免全量落库导致表膨胀。
- **注销账号**：控制台提供二次确认注销，物理删除用户、Token、额度流水、任务奖励、验证码及相关调用/登录日志，并清理会话与记住登录 Cookie。
- **Token 重置防误触**：重置前弹窗展示当前 Token 创建时间并要求勾选确认，重置只轮换密钥、不改变管理员授予的接口权限。
- **独立会话与安全**：`portal_session` 与后台 `admin_session` 完全分离；密码使用 PBKDF2-HMAC-SHA256 加盐哈希。专属 Token 仅保存 SHA-256 哈希，明文只在首次发放或重置时展示一次。
- **Token 重置不扩权**：`POST /api/portal/token/reset` 只轮换 Token 哈希和掩码，保留管理员配置的接口权限、启停状态与过期时间；旧 Token 立即失效，用户不能通过重置恢复已取消的接口权限。
- **个人控制台**：展示剩余额度、基础/任务奖励额度、额度明细、近 7 天抽样调用记录；支持每日签到、登录和新手任务领取累计额度。
- **受保护接口文档**：`/docs` 提供接口目录、紧凑详情页、在线测试和接入代码，只有启用文档且登录有效 C 端账号后可访问。
- **12 个实用工具**：JSON 格式化、时间戳、文本对比、二维码、图片处理、随机生成器、IP 查询、正则表达式、URL 编解码、Base64、子网掩码/IP 转 Int、Faker 随机假数据。
- **在线工具实现**：URL 使用 `urllib.parse.quote/unquote`；Base64 文本与图片使用 Python `base64`，图片结果自动添加真实 MIME 的 `data:image/...;base64,` 前缀；网络换算使用 `ipaddress`；假数据使用 `Faker("zh_CN")`，默认一次生成 100 条姓名、手机号和地址。
- **图片批处理**：使用 Pillow，每次处理 1–8 张图片，支持 PNG、JPEG、WebP、GIF、BMP、TIFF、ICO 输出；可等比缩放、设置质量，多图自动打包 ZIP。单张最大 20MB、批量原始文件合计最大 50MB，图片转 Base64 单张最大 10MB。

### 6. `scripts/` 运维脚本（均可在项目任意目录执行）

| 脚本 | 作用 | 用法 |
| --- | --- | --- |
| `数据初始化.py` | **一键完成维护 + 初始化**：先按模型补表/字段注释（可重复运行、不动数据），再给各表灌入基础示例数据，已有数据的表自动跳过 | `python scripts/数据初始化.py` |
| `数据清理.py` | 清理业务表中的空白、空格污染与格式异常数据：批量 TRIM 文本字段、删除关键字段为空的记录、校验图片 URL/日期/分类/级别格式；只处理 17 张内容表 | `python scripts/数据清理.py` |
| `重置数据库.py` | **危险操作 · 默认禁用**：清空全部 44 张表（业务内容 + C 端用户令牌 + 系统配置 + 日志监控）；后台需先设 `ALLOW_DB_RESET=1` 并重启，再通过「确认项 + 输入 RESET + 管理员密码」；命令行需带 `RESET` | `python scripts/重置数据库.py RESET` |
| `数据同步.py` | 联网抓取热榜/笑话/名言/壁纸/历史今天并写入数据库（自动去重） | `python scripts/数据同步.py`、`python scripts/数据同步.py hot` |
| `数据导入.py` | 导入 `data/` 下的 JSON：按字段自动识别成语/歇后语 | `python scripts/数据导入.py`、`python scripts/数据导入.py idiom.json` |
| `check_env.py` | 环境自检：Python/依赖版本、关键环境变量、MySQL 连通性与字符集、`mysqldump` 是否可用、备份/data/templates 目录读写权限，输出结论与退出码 | `python scripts/check_env.py` |
| `history_today.py` | 历史上的今天采集，6 线程并发 | `python scripts/history_today.py`、`... 10-22`、`... all` |
| `joke.py` | 笑话站点爬虫，写入 `local_joke` | `python scripts/joke.py` |
| `tongue_twister.py` | 绕口令爬虫，写入 `tongue_twister` | `python scripts/tongue_twister.py` |
| `answer_book.py` | 答案之书采集，写入 `answer_book` | `python scripts/answer_book.py` |
| `__init__.py` | 让 `scripts` 成为包，支持 `from scripts.history_today import fetch_events` | — |

> `数据初始化.py`、`数据清理.py`、`重置数据库.py`、`数据同步.py` 会自动把项目根目录加入 `sys.path`，
> 因此在 `scripts/` 内或任意工作目录下执行都能正常导入 `main.py`。
> **推荐执行顺序**：重置数据库 → 重启项目（自动重建接口开关与内置敏感词）→ 数据初始化。
> 四个爬虫脚本（history/joke/tongue_twister/answer_book）自带数据库配置，不依赖主程序。
> 以上脚本也可以直接在后台「脚本管理」里运行，无需命令行。

### 7. `data/`
离线原始数据，仅 `数据导入.py` 与人工导入使用。
- `idiom.json`：字段 `word / pinyin / abbreviation / explanation / derivation / example` → 导入 `idiom_lib`，按 `word` 去重。
- `xiehouyu.json`：字段 `riddle / answer` → 导入 `xiehouyu`，按 `riddle` 去重。
- `envcheck_runs.json`：后台「环境自检」写入的历史结果，仅自检模块读写，**不要手动编辑**。

### 8. `archive/`
历史归档目录，代码中无任何引用，可整体删除或另存到别处。
- `my_api_data.db`：项目早期使用 SQLite 时的数据库文件，已全部迁移到 MySQL；排查旧数据时再单独用 SQLite 工具打开。
- `import_templates/`：早期手工下载的导入模板 Excel；后台「批量导入模板」现由接口按表动态生成，这些文件已不再使用。

---

## 三、公开接口一览（全部需要请求头 `token`）

> 下表均为 `/api/...` 形式；加版本前缀访问等价，例如 `/v1/api/poem/get`。

| 接口 | 说明 | 主要参数 |
| --- | --- | --- |
| `/api/knowledge/get` | 知识点搜索 | `q`、`category` |
| `/api/knowledge/list` | 知识点分页列表 | `page`、`size`、`category` |
| `/api/retort/get` | 随机怼人语录 | `level` |
| `/api/name/get` | 随机起名 | `type`(boy/girl/net/shop)、`count` |
| `/api/joke/get` | 随机笑话（实时优先） | `mode`(auto/online/local) |
| `/api/joke/local` | 本地随机笑话 | — |
| `/api/hot/get` | 热点查询 | `source`、`limit` |
| `/api/news/60s` | 每天 60 秒新闻（实时） | — |
| `/api/weather/get` | 实时天气 | `city` |
| `/api/exchange-rate/get` | 实时汇率 | `currency` |
| `/api/image/daily` | 每日壁纸 | `mode` |
| `/api/idiom/get` | 随机成语 / 精确查询 | `q` |
| `/api/poem/get` | 随机古诗词 | — |
| `/api/copy/get` | 随机文案 | `ctype` |
| `/api/wisdom/get` | 随机名言短句 | `tag`、`mode` |
| `/api/random/draw` | 随机抽签 / 骰子 | `type`、`count` |
| `/api/love/get` | 随机土味情话 | — |
| `/api/poison/get` | 随机毒鸡汤 | — |
| `/api/riddle/get` | 随机脑筋急转弯 | — |
| `/api/xiehouyu/get` | 随机歇后语 | `q` |
| `/api/twister/get` | 随机绕口令 | `format`(text/html) |
| `/api/answer/get` | 答案之书 | — |
| `/api/history/today` | 历史上的今天（本地优先） | `date`(MM-DD)、`mode` |
| `/api/ip/public` | 获取公网 IP | — |

统一返回格式：成功 `{"code": 200, "data": ...}`，失败 `{"code": 4xx/500, "msg": "..."}`。

---

## 四、环境与启动

```bash
# 依赖（包含 Faker 与 Pillow）
pip install -r requirements.txt

# 启动（项目根目录）—— 按 APP_ENV 自动选择监听方式
python main.py                  # 开发：0.0.0.0:443 + openSSL 自签证书，开热重载
APP_ENV=prod python main.py     # 生产：127.0.0.1:8000，无 SSL，443/HTTPS 交给 Nginx

# 单元测试（标准库 unittest，无需额外依赖；共 34 项）
python -m unittest discover -s tests -v
```

- 开发访问：`https://127.0.0.1/...`（自签证书需在浏览器点“继续访问”）
- 生产访问：由 Nginx 反代后走 `https://你的域名/...`，本机调试用 `http://127.0.0.1:8000/...`
- 接口文档：`/docs`（仅 `APP_ENV=dev` 或 `DOCS_ENABLED=1` 时开放，且必须登录 C 端账号）
- C 端用户门户：`/portal`（未登录自动跳转 `/portal/login`）
- 健康检查：`/health`（无需 Token）
- 版本前缀：`/v1/api/poem/get` 与 `/api/poem/get` 等价
- 管理后台：`/admin`（账号密码以 `app/core/config.py` 为准）
- 数据库：MySQL 5.7 / 8.0，库名 `my_api_db`，字符集 `utf8mb4`；首次启动会自动 `create_all` 建表

常用环境变量（全部变量汇总，可直接复制到 docker `-e`、`environment:` 或 systemd `Environment=`）：

| 分类 | 变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 运行环境 | `APP_ENV` | `dev` | `prod` 时关闭文档、后台 Cookie 默认 Secure |
| 运行环境 | `APP_VERSION` | `1.1.0` | `/health` 返回的版本号，升级时手工同步 |
| 运行环境 | `DOCS_ENABLED` | 跟随环境 | 生产环境设为 `1` 可强制开放 `/docs` |
| 运行环境 | `API_VERSION_PREFIX` | `/v1` | 版本化路径前缀，设为空可关闭 |
| 后台鉴权 | `API_TOKEN` | 代码内默认值 | 接口主 Token，**生产必须修改** |
| 后台鉴权 | `ADMIN_USER` | `admin` | 后台登录账号 |
| 后台鉴权 | `ADMIN_PASSWORD` | `admin` | 后台登录密码，**生产必须修改** |
| 后台鉴权 | `ADMIN_SECRET` | `api-admin` | 会话签名密钥，**生产必须修改** |
| 后台鉴权 | `ADMIN_COOKIE_SECURE` | 跟随环境 | HTTPS 部署设为 `1`，Cookie 追加 `Secure` |
| C 端门户 | `PORTAL_SECRET` | `ADMIN_SECRET + .portal` | 独立用户会话签名密钥，生产建议显式设置 |
| C 端门户 | `PORTAL_COOKIE_SECURE` | 跟随后台 | HTTPS 部署设为 `1` |
| C 端门户 | `PORTAL_DEFAULT_DAILY_QUOTA` | `500` | 新用户初始基础额度 |
| C 端门户 | `PORTAL_USER_QPS` | `2` | 单用户每秒请求限制 |
| C 端门户 | `PORTAL_REGISTER_IP_LIMIT` / `PORTAL_REGISTER_WINDOW` | `5` / `3600` | 单 IP 注册申请次数与统计窗口 |
| C 端门户 | `PORTAL_API_LOG_SAMPLE` / `PORTAL_API_LOG_KEEP_DAYS` | `1` / `7` | C 端请求日志抽样比例与保留天数 |
| C 端门户 | `PORTAL_EMAIL_CODE_TTL` / `PORTAL_EMAIL_RESEND_SECONDS` | `600` / `60` | 邮箱验证码有效期与重发间隔（秒） |
| C 端门户 | `PORTAL_AUTO_APPROVE` / `PORTAL_AUTO_APPROVE_INTERVAL` | `1` / `30` | 邮箱验证后的自动审批开关与扫描间隔 |
| C 端门户 | `PORTAL_QUOTA_WARN_THRESHOLD` / `PORTAL_QUOTA_WARN_INTERVAL` | `50` / `3600` | 剩余额度预警阈值（后台可覆盖，0 关闭）与扫描间隔（秒） |
| 安全 | `ALLOW_DB_RESET` | `0` | 重置数据库总开关，默认禁用；仅首次部署需清库时临时设为 `1`，用完后立即改回 |
| 邮件 | `MAIL_ENABLED`、`MAIL_HOST`、`MAIL_PORT`、`MAIL_USER`、`MAIL_PWD` | 见 `config.py` | 注册/找回密码验证码及通知邮件的 SMTP 配置 |
| 数据库 | `DB_HOST` / `DB_PORT` | `127.0.0.1` / `3306` | MySQL 地址与端口 |
| 数据库 | `DB_USER` / `DB_PWD` | `root` / 代码内默认值 | MySQL 账号与密码 |
| 数据库 | `DB_NAME` | `my_api_db` | 数据库名（utf8mb4） |
| 连接池 | `DB_POOL_SIZE` | `10` | 常驻连接数 |
| 连接池 | `DB_MAX_OVERFLOW` | `20` | 峰值临时连接数 |
| 连接池 | `DB_POOL_TIMEOUT` | `30` | 获取连接最长等待秒数 |
| 连接池 | `DB_POOL_RECYCLE` | `3600` | 连接回收秒数，规避 `wait_timeout` |
| 外部数据源 | `NET_API` | `https://60s.viki.moe/v2` | 聚合数据源根地址 |
| 外部数据源 | `NET_TIMEOUT` | `8` | 单次外部请求超时（秒） |
| 外部数据源 | `BREAKER_THRESHOLD` | `5` | 连续失败熔断阈值 |
| 外部数据源 | `BREAKER_COOLDOWN` | `60` | 熔断冷却秒数 |
| 缓存 | `CACHE_TTL` | `60` | 只读接口全局缓存秒数 |
| 缓存 | `CACHE_MAX_ITEMS` | `500` | 缓存键数量上限 |
| 导入 | `IMPORT_JOB_KEEP_DAYS` | `7` | 导入任务记录保留天数 |
| 导入 | `IMPORT_MAX_MB` | `20` | 批量导入单文件大小上限（MB） |
| 脚本 | `SCRIPT_LONG_MINUTES` | `30` | 超过该分钟数标记为长时间任务 |
| 脚本 | `SCRIPT_TIMEOUT_MINUTES` | `120` | 脚本全局超时上限，超时强制结束 |
| 脚本 | `SCRIPT_ORPHAN_CHECK` | `30` | 孤儿进程守护线程检查间隔（秒） |
| 备份 | `BACKUP_ENABLED` | `1` | 是否启用内置每日自动备份 |
| 备份 | `BACKUP_HOUR` | `3` | 内置备份执行小时（0-23） |
| 备份 | `BACKUP_DIR` | `./backups` | 备份输出目录 |
| 备份 | `BACKUP_KEEP_DAYS` | `7` | 备份文件保留天数 |
| 备份 | `BACKUP_CHECK_INTERVAL` | `1800` | 内置调度检查间隔（秒） |
| 备份通知 | `BACKUP_NOTIFY_WEBHOOK` | 空 | 企业微信机器人 Webhook，填写后启用通知 |
| 备份通知 | `BACKUP_NOTIFY_SUCCESS` | `0` | 设为 `1` 时成功也推送（默认只推失败） |
| 多实例 | `REDIS_URL` | 空 | 留空用进程内缓存；填写后缓存/限流计数/nonce/分布式锁统一走 Redis |
| 多实例 | `REDIS_PREFIX` | `apicache:` | Redis 键前缀，多个项目共用同一 Redis 时用于隔离 |
| 多实例 | `HOST_ID` | 主机名 | 实例标识；多机部署时建议显式设置（如 `api-1`、`api-2`） |
| 日志 | `LOG_JSON` | `0` | 设为 `1` 输出 JSON 结构化日志，便于接入 ELK / Loki |
| 日志 | `LOG_FILE` | 空 | 日志文件路径，留空只输出控制台；填写后按天滚动 |
| 日志 | `LOG_ROTATE_DAYS` | `7` | 滚动日志保留份数（天） |
| 日志 | `LOKI_URL` | 空 | Loki 推送地址，如 `http://127.0.0.1:3100/loki/api/v1/push` |
| 告警 | `ALERT_ENABLED` | `1` | 告警总开关（后台可覆盖） |
| 告警 | `ALERT_WEBHOOK` | 空 | 默认企业微信机器人 Webhook，留空复用备份通知 |
| 告警 | `ALERT_WEBHOOK_HIGH` | 空 | 高优渠道（接口不可用/熔断/主机资源/IP 风控），留空复用 `ALERT_WEBHOOK` |
| 告警 | `ALERT_WEBHOOK_LOW` | 空 | 低优渠道（脚本失败/限流/Token 宽限），留空复用 `ALERT_WEBHOOK` |
| 告警 | `ALERT_AT_MOBILES` | 空 | 高优告警 @ 的手机号，逗号分隔；填 `@all` 提醒所有人 |
| 告警 | `ALERT_HOST_PERCENT` | `90` | CPU/内存/磁盘使用率告警阈值（%），后台「智能自愈与告警中心」可覆盖 |
| 主机巡检 | `HOST_CHECK_ENABLED` | `1` | 是否启用后台主机资源巡检（与监控页是否打开无关） |
| 主机巡检 | `HOST_CHECK_INTERVAL` | `60` | 主机巡检间隔（秒），最小 30 |
| 告警 | `ALERT_COOLDOWN` | `300` | 同一接口同类告警的最小间隔秒数 |
| 主动自愈 | `SELF_HEAL_ENABLED` | `1` | 是否启动独立主动巡检线程 |
| 主动自愈 | `SELF_HEAL_INTERVAL` | `30` | 主动巡检间隔（秒），后台可改为 10–3600 秒 |
| IP 风控 | `IP_BURST_LIMIT` | `0` | 单 IP 每分钟请求上限，`0` 表示关闭 |
| 全局限流 | `GLOBAL_QPS_LIMIT` | `0` | 全站每秒请求总闸，`0` 关闭；防止多 IP/多 Token 同时刷爆平台 |
| Token 提醒 | `TOKEN_EXPIRY_WARN_DAYS` / `TOKEN_EXPIRY_WARN_INTERVAL` | `7` / `86400` | 到期前提醒天数（0 关闭）与扫描间隔（秒） |
| IP 风控 | `IP_FREEZE_SECONDS` | `600` | 触发上限后自动冻结秒数 |
| IP 风控 | `IP_GEO_ENABLED` | `0` | 是否开启地理位置漂移检测 |
| IP 风控 | `IP_GEO_MINUTES` | `5` | 地理漂移检测时间窗口（分钟） |
| Token | `TOKEN_ROTATE_GRACE_HOURS` | `24` | Token 轮换后旧 Token 的宽限期小时数，期内使用触发低优告警 |
| 请求签名 | `SIGN_ENABLED` | `0` | 是否开启 HMAC 签名校验（开启后请求需带 `X-Timestamp/X-Nonce/X-Sign`） |
| 请求签名 | `SIGN_SECRET` | 复用 `API_TOKEN` | 签名密钥 |
| 请求签名 | `SIGN_PATHS` | 空 | 逗号分隔的路径前缀，只对这些路径强制校验签名 |
| 请求签名 | `SIGN_WINDOW` | `300` | 时间戳允许偏差秒数 |

> 说明：`API_TOKEN`、`ADMIN_PASSWORD`、`ADMIN_SECRET`、`DB_PWD` 在仓库中带有默认值仅供本机开发，
> 生产部署请通过环境变量注入；导出的环境变量字典不要在日志或聊天工具中明文传播。

### 首次部署顺序建议
1. 修改 `app/core/config.py` 中的配置（数据库密码、`API_TOKEN`、后台账号密码、`ADMIN_SECRET`），或改用同名环境变量。
2. 启动一次服务完成建表。
3. 运行 `python scripts/check_env.py` 做环境自检（MySQL、字符集、mysqldump、目录权限）。
4. `python scripts/数据初始化.py` 一键补齐表/字段注释并灌入基础示例数据（后台「脚本管理」里也能直接运行）。
5. 按需导入数据：`python scripts/数据导入.py`、`python scripts/history_today.py all`。
6. 日常更新：后台「批量导入」上传 Excel（可先勾选预览校验），或在「脚本管理」里运行数据同步；需要定时执行时到「定时任务」配置 cron。
7. 新增 `python scripts/数据清理.py`（清理空白/异常数据）与 `python scripts/重置数据库.py RESET`（清空全部数据重来）两个维护入口，按需使用。

---

## 五、后台定时任务与备份

### 1. 定时任务类型

后台「系统 → 定时任务」统一管理两类任务：

| 类型 | 作用 | 关键配置 |
| --- | --- | --- |
| 脚本任务（`script`） | 按周期自动运行预置采集/同步脚本 | 脚本、参数、cron 或间隔分钟 |
| 备份任务（`backup`） | 按周期自动执行数据库备份 | cron 或间隔分钟、**独立备份目录** |

- `cron_expr` 为 5 段标准格式（分 时 日 月 周），例如 `0 3 * * *` 表示每天 03:00；留空则按「每 N 分钟」执行。
- 调度线程每 20 秒检查一次到期任务；「立即执行」只是把 `next_run` 提前，最多 20 秒内生效。
- **支持多实例**：每个任务执行前会抢 `schedule:{id}` 分布式锁（Redis 优先，无 Redis 时回退 MySQL `GET_LOCK`），同一任务同一时刻只由一台机器执行，不会重复跑脚本或重复备份；未抢到锁的实例直接跳过本次周期。
- 存在启用的备份任务时，内置每日备份线程自动让位，避免重复备份。

### 2. 备份目录与自动清理

- 备份任务可单独指定 `backup_dir`；未指定时使用 `BACKUP_DIR`（默认项目下 `backups/`）。新建任务时会自动创建目录并校验可写。
- 「日志管理 → 自动清理」页的「备份文件自动清理」面板可设置：是否启用、保留天数、备份目录（留空用默认）。
- 清理线程每 6 小时执行一次，复用 `backup.prune_backup_dir`，连带删除对应的 `.meta.json` / `.sha256` 与校验失败的 `.invalid` 文件。
- 备份失败通知：设置 `BACKUP_NOTIFY_WEBHOOK` 后，失败（含校验失败）会推送企业微信机器人消息；`BACKUP_NOTIFY_SUCCESS=1` 时成功也推送。

### 3. Docker Compose 部署（Windows / Linux / 其他服务器，推荐）

在新服务器上完整拷贝项目后，可以直接使用 Docker Compose 启动。宿主机**不需要另外安装 Python、项目依赖或 MySQL**；`api` 和 `mysql` 都在容器中运行。Compose 使用相对路径挂载，Windows 和 Linux 均可使用。

#### 首次部署

1. 安装并启动 Docker，同时确认 Compose 插件可用：

```bash
docker version
docker compose version
```

2. 完整拷贝项目目录，特别注意不要漏掉以下文件：

| 文件/目录 | 用途 |
| --- | --- |
| `Dockerfile`、`compose.yaml`、`requirements.txt` | 构建镜像、启动 API 与 MySQL |
| `app/`、`main.py`、`templates/`、`static/`、`scripts/` | 应用代码与后台页面 |
| `openSSL/local.crt`、`openSSL/local.key` | HTTPS 证书；由 Compose 只读挂载到容器 |
| `data/` | 离线数据与环境自检历史；需要保留历史时一起拷贝 |
| `.env` | 数据库、接口 Token、后台账号等私密配置 |

`.env` 和 `openSSL/` 被 `.dockerignore` 排除，不会打入镜像，但 `.env` 会由 Compose 读取，`openSSL/` 会通过目录挂载提供，因此拷项目时仍必须保留。

3. 全新部署时，如没有 `.env`，根据模板创建并修改所有“请替换”配置：

```bash
# Linux / macOS
cp .env.example .env
```

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

必须设置 `MYSQL_ROOT_PASSWORD`、`DB_PWD`、`API_TOKEN`、`ADMIN_PASSWORD`、`ADMIN_SECRET`。生产环境应使用新的强密码和随机会话密钥，不要把真实 `.env` 提交到 Git 或发到聊天工具。

4. 在项目根目录检查配置并启动：

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

首次启动会创建 `mysql_data` 数据卷、自动建立数据表，并启动 HTTPS 服务。查看启动日志：

```bash
docker compose logs -f --tail=100 api
```

默认访问地址为 `https://服务器IP/admin`，HTTPS 端口由 `.env` 的 `HTTPS_PORT` 决定（默认 `443`）。云安全组和服务器防火墙需放行该端口。MySQL 默认仅映射到 `127.0.0.1:3306`，供服务器本机的 Navicat 等工具连接；可用 `MYSQL_HOST_PORT` 修改宿主机端口，不要将 `MYSQL_BIND_ADDR` 改为 `0.0.0.0` 直接暴露公网。

> 如果更换了服务器公网 IP 或域名，需要重新签发包含新 IP/域名 SAN 的证书。自签名证书仍会产生浏览器信任警告，正式公网服务建议换成受信任 CA 证书。

#### 文件更新后的操作

| 更新内容 | 执行命令 | 说明 |
| --- | --- | --- |
| Python、HTML、静态资源或其他项目文件 | `docker compose up -d --build` | 重新构建镜像并更新容器 |
| `requirements.txt` | `docker compose up -d --build` | 安装变化的依赖并更新镜像 |
| 仅 `.env` 或 Compose 环境变量 | `docker compose up -d` | 按新配置重建容器，无需重新构建镜像 |
| 仅替换 `openSSL/` 中证书 | `docker compose restart api` | 证书目录已挂载，重启后加载新证书 |

不要只执行 `docker compose restart` 来更新代码，因为旧镜像中的文件不会变化。正常重建不会清空 MySQL 数据卷、`data/`、`backups/` 或 `exports/`；不要执行带 `-v` 的 `docker compose down -v`，除非明确要删除数据库卷。

#### 迁移已有服务器的数据

直接拷贝项目并启动会在新服务器创建一个**新的空数据库卷**，只自动建表，不会自动搬来旧 MySQL 业务数据。迁移已有环境时还需要：

1. 在旧服务器备份 MySQL，并在新服务器恢复到 `mysql` 容器；不要直接复制 Windows/MySQL 的物理数据目录到 Linux。
2. 后台关键配置可使用「环境自检 → 配置导出/导入」迁移；业务数据仍需数据库备份恢复。
3. 按需拷贝 `data/envcheck_runs.json`、备份和导出文件。
4. 启动后检查 `docker compose ps`、`docker compose logs api` 和后台环境自检。

#### pip 下载与构建加速

`Dockerfile` 默认使用阿里云 PyPI 镜像，并通过 BuildKit 缓存 pip 下载目录。`requirements.txt` 未变化时，普通代码更新会复用依赖安装层，不会重复安装全部依赖。

需要切换为其他镜像源时，可在构建时覆盖参数：

```bash
docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
docker compose up -d
```

首次构建仍需下载基础镜像和依赖；后续构建会复用 Docker 层缓存与 pip 缓存。不要对日常构建使用 `--no-cache`，也不要频繁清理 BuildKit 缓存。

### 4. 非 Docker 部署示例

**Linux（systemd）**

```ini
# /etc/systemd/system/my-api.service
[Unit]
Description=My API Platform
After=network.target mysql.service

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/api-platform
Environment=APP_ENV=prod
Environment=DB_HOST=127.0.0.1
Environment=DB_PORT=3306
Environment=DB_USER=api_user
Environment=DB_PWD=请替换为实际密码
Environment=DB_NAME=my_api_db
Environment=API_TOKEN=请替换为实际Token
Environment=ADMIN_USER=admin
Environment=ADMIN_PASSWORD=请替换为强密码
Environment=ADMIN_SECRET=请替换为随机字符串
Environment=ADMIN_COOKIE_SECURE=1
ExecStart=/usr/bin/python3 /opt/api-platform/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now my-api
sudo systemctl status my-api
journalctl -u my-api -f      # 查看日志
```

**Windows（nssm 后台托管）**

```powershell
# 下载 nssm 并解压后，在 PowerShell 中执行（路径按实际修改）
$nssm = "C:\tools\nssm\nssm.exe"
$python = "C:\Python311\python.exe"
nssm install MyApiPlatform $python "E:\自己写的小程序\API接口\main.py"
nssm set MyApiPlatform AppDirectory "E:\自己写的小程序\API接口"
nssm set MyApiPlatform AppEnvironmentExtra "APP_ENV=prod" "DB_HOST=127.0.0.1" "DB_PWD=请替换为实际密码"
nssm set MyApiPlatform AppStdout "E:\自己写的小程序\API接口\logs\service.log"
nssm set MyApiPlatform AppStderr "E:\自己写的小程序\API接口\logs\error.log"
nssm set MyApiPlatform AppRotateFiles 1
nssm set MyApiPlatform Start SERVICE_AUTO_START
nssm start MyApiPlatform
```

> nssm 会把服务注册为 Windows 服务，开机自启并自动拉起；日志与启动参数都可在 `nssm edit MyApiPlatform` 中查看修改。
> 注意：nssm 的环境变量值包含中文或特殊字符时，建议先写入系统环境变量再引用，避免命令行解析问题。

---

## 六、数据库约定

- 每张业务表都有 `created_at`（写入时间，`DEFAULT CURRENT_TIMESTAMP`）；`is_deleted` 字段**仅用于兼容历史模型**，当前后台删除为物理删除，不再提供回收站功能，业务代码也不再读写该字段。
- 批量导入不再使用 `INSERT IGNORE`：先按各表自然键（见 `app/admin/panel.py` 的 `DEDUP_FIELDS`）拼字符串算 MD5，文件内先自去重，再与数据库已有数据逐条比对；重复行只计入「重复跳过」，不报错；缺必填字段、类型转换失败、查重字段为空、命中敏感词等记为「格式错误」并附行号；批量写入失败会回滚并逐条重试，失败原因逐条记录。
- 需要大批量写入前，建议先勾选「预览校验（dry-run）」：只解析、校验、统计成功/重复/错误数量，不提交入库。
- 随机取数：先在 `id` 区间内随机取点，命中不到就重试最多 5 次（应对 id 空洞），仍失败则降级为 `ORDER BY RAND()`，保证一定有数据返回。
- 表注释、字段注释均写在模型里，可用 `scripts/数据初始化.py` 同步到已存在的表（该脚本先补注释、再灌数据）。
- 管理类表（`api_log`、`api_switch`、`api_token`、`ip_rule`、`app_setting`、`import_log`、`import_job`、`audit_log`、`breaker_state`、`script_run`、`script_schedule`、`admin_user`、`sensitive_word`）随服务启动自动创建，不参与批量导入与模板生成。
- `app_setting` 常用键：`rate_limit_enabled`（0/1）、`rate_limit_scope`（token/ip）、`rate_limit_per_minute`、`rate_limit_overrides`（JSON 单接口阈值）、`log_clean_enabled`（0/1）、`log_clean_days`、`log_clean_max_rows`（容量兜底行数）、`log_last_clean`、`log_last_deleted`、`backup_clean_enabled`（0/1）、`backup_clean_days`、`backup_clean_dir`、`backup_last_clean`、`backup_last_deleted`、`admin_ip_guard`（0/1）、`admin_ip_whitelist`、`sensitive_seeded`（内置词库已初始化标记）。
- `api_switch` 除接口启停外，还保存接口级 `log_enabled`（是否记录调用日志）与 `cache_ttl`（独立缓存秒数，0 表示使用全局 `CACHE_TTL`）。
- `script_run` 记录每次脚本运行的 `pid`、状态、输出与起止时间；`script_schedule` 保存 `task_type`（script/backup）、`cron_expr`、`backup_dir` 与 `next_run`。

---

## 七、注意事项

- 后台与管理接口不在 `/openapi.json` 中暴露；公开 API 里也没有日志/状态/统计入口。
- Token 明文只在新建时返回一次（页面自动复制到剪贴板），数据库 `api_token.token` 只存 64 位 SHA-256 哈希，`token_hint` 存掩码用于后台辨认；启动时会自动把旧版本遗留的明文 Token 转成哈希，无需手工迁移。主 Token 由 `API_TOKEN` 决定，同样按哈希比对，后台只显示掩码。
- 密钥类配置（`API_TOKEN`、`ADMIN_PASSWORD`、`ADMIN_SECRET`、`DB_PWD`）建议用环境变量注入；部署 HTTPS 后设置 `ADMIN_COOKIE_SECURE=1`，后台 Cookie 会追加 `Secure`。
- 请求日志入库前会把 `token/secret/password/api_key/authorization` 等参数值替换为 `***`，避免日志泄露密钥。
- 新增的 `is_deleted`、`token_hint` 字段在启动时按 `information_schema` 检查后自动 `ALTER TABLE` 补齐（可重复执行）。
- 修改 `templates/` 内文件名或目录时，需同步修改 `app/admin/panel.py` 顶部的 `TEMPLATE_DIR / LOGIN_HTML / DASHBOARD_HTML`。
- 批量导入模板按表动态生成，必填字段从 `information_schema` 读取；导入成功与失败都会写入 `import_log`，可在「批量导入 → 导入记录」查看详情。
- **接口 IP 黑白名单**只对 `/api/*` 生效：黑名单命中直接 403；只要存在启用中的白名单，未在名单内的 IP 一律 403（后台 `/admin` 不受影响）。
- 限流为 60 秒固定窗口，按 Token 或 IP 计数，超限返回 429；主 Token 同样受限流约束。
- 日志自动清理后台线程每 6 小时检查一次，按 `log_clean_days` 删除旧日志，删除后不可恢复；同一线程还会按「备份文件自动清理」设置删除过期备份。也可在「日志管理 → 自动清理」立即执行。
- 只读接口缓存为 TTL 缓存（默认 60 秒、最多 500 条，可用 `CACHE_TTL` / `CACHE_MAX_ITEMS` 调整；接口级可用 `cache_ttl` 覆盖）；后台批量导入完成、脚本同步结束后立即清空，因此写入后不会读到旧数据。未配置 `REDIS_URL` 时为进程内缓存，多实例各进程独立，需接受最多 `CACHE_TTL` 秒的读延迟；配置 `REDIS_URL` 后缓存与限流计数在多实例间共享。
- 接口列表中的「日志」开关关闭后，该接口不再写 `api_log`，适合高频接口压缩日志量；鉴权、限流、IP 校验仍然生效，只影响日志记录。
- 脚本统一受 `SCRIPT_TIMEOUT_MINUTES` 全局超时约束；守护线程每 `SCRIPT_ORPHAN_CHECK` 秒清理「已停止但子进程仍存活」的孤儿进程，避免爬虫脚本卡死残留。
- 外网接口连续失败 5 次会熔断 60 秒（`BREAKER_THRESHOLD` / `BREAKER_COOLDOWN` 可调），期间相关接口直接回退本地库；熔断状态与累计成功/失败次数写入 `breaker_state` 表，服务重启不会清零，可在后台「安全设置 → 外网接口熔断监控」查看成功率与熔断历史并手动重置。
- 批量导入为异步任务：任务状态同时写入内存与 `import_job` 表（文件名、总行数、成功/重复/失败条数、错误明细、起止时间），完成后保留 `IMPORT_JOB_KEEP_DAYS` 天（默认 7 天），服务重启后仍可在「批量导入 → 导入任务记录」查看；重启时未完成的任务会标记为「已中断」。
- **后台访问 IP 白名单**（`admin_ip_guard` / `admin_ip_whitelist`）只作用于 `/admin`，与**接口 IP 黑白名单**完全独立；开启后只有白名单 IP 能打开后台登录页，白名单为空时不做限制。
- 单接口独立限流（`rate_limit_overrides`）优先于全局限流，适合天气、汇率、60 秒新闻等会消耗第三方配额的外网接口；未配置的接口沿用全局限流。
- `audit_log` 只增不改，记录后台敏感操作；修改 Token 权限、系统配置、限流参数等敏感变更会额外记录修改前/后快照，普通查看和脚本运行不记录快照，避免日志量过大；**数据导出同样写入审计**（表名 + 筛选条件 + 导出条数）。
- 敏感词库（`sensitive_word`）首次启动会写入一批内置词（已初始化后不再重复写入）。批量导入时命中敏感词的行在**入库前**即按格式错误拒绝并记录行号，避免违规原文落库；对外 `/api/*` 的 JSON 响应会把命中的词替换为等长 `*` 作为兜底。后台「安全设置 → 敏感词库」可增删、启停，修改后立即生效。
- 后台账号（`admin_user`）支持 `admin`（可读写）与 `viewer`（只读）两种角色：只读账号只能查看统计、日志、配置，所有写操作（POST）由后端直接返回 403。配置文件中的内置管理员始终拥有最高权限，不受账号列表影响；密码按「随机盐 + SHA-256」存储，新建/改密时**最少 8 位**（不做复杂度强校验）。
- 批量导入仅接受 `.xlsx / .xlsm / .csv`，单文件上限 `IMPORT_MAX_MB`（默认 20MB），并校验文件头（xlsx 的 `PK\x03\x04`、CSV 是否含二进制内容），不只看后缀。
- 生产部署建议设置 `APP_ENV=prod`：接口文档自动关闭，后台 Cookie 默认带 `Secure`（必须走 HTTPS），如需临时查看文档可加 `DOCS_ENABLED=1`。开发环境保持默认 `dev` 即可。
- 版本前缀由最外层中间件统一重写，`/v1` 可整体关闭或改名；调用日志、**接口 IP 黑白名单**、Token 权限都按规范化后的 `/api/...` 路径处理。
- 参数不合法（超长、越界、枚举值错误、日期格式错误）直接返回 HTTP 422，不再静默截断或返回空结果；客户端应按 OpenAPI 约束传参。
- 内置自动备份默认每天 3 点执行到 `backups/`，运行期间会占用一定磁盘 IO；数据库较大时建议把 `BACKUP_HOUR` 调到低峰时段，并用 `BACKUP_DIR` 指向独立磁盘，或在「定时任务」中改用 cron 精确控制。`mysqldump` 不在 PATH 时会自动回退到逻辑导出（较慢）。
- 新增的 4 张表/新路由需要重启一次服务才会加载（`create_all` 会自动建表）。
- `archive/` 下的历史文件（SQLite 旧库、早期导入模板 Excel）代码不读取，不需要时可直接删除，不影响任何接口；其中 `archive/import_templates/poem_导入模板.xlsx` 体积约 148MB。

---

## 八、备份与恢复

### 1. 备份文件说明

每次备份（内置每日任务、后台「定时任务 → 立即备份」或 `python -m app.services.backup`）会在实际使用的备份目录生成三个文件：

| 文件 | 说明 |
| --- | --- |
| `my_api_db_YYYYMMDD_HHMMSS.sql.gz` | 数据库备份主体，gzip 压缩 |
| `my_api_db_YYYYMMDD_HHMMSS.sql.gz.meta.json` | 备份元信息：生成方式（mysqldump / 逻辑导出）、大小、SHA-256、校验结果、备注 |
| `my_api_db_YYYYMMDD_HHMMSS.sql.gz.sha256` | 校验和文件，用于手动核对文件完整性 |

备份完成后会自动校验：文件是否存在、是否为空、gzip 是否可正常解压、内容是否包含建表语句。校验失败的文件会被重命名为 `.invalid` 后缀，并写入审计日志（`action=backup_run`），可通过「操作审计」按 `backup_run` 筛选查看失败原因；配置了 `BACKUP_NOTIFY_WEBHOOK` 时会主动推送失败通知。过期备份（含 `.invalid`）由内置保留天数或「备份文件自动清理」设置统一删除，并连带删除 `.meta.json`、`.sha256`。

### 2. 恢复前的风险提示

> **恢复备份会覆盖当前数据库中的现有数据。** 恢复操作不可撤销，执行前务必确认：
> - 已停止 API 服务与后台脚本，避免恢复过程中仍有写入；
> - 当前库中若有备份之后新增的数据，会被直接覆盖丢失；
> - 建议先对当前库再做一次手动备份（后台「定时任务 → 立即备份」或 `python -m app.services.backup`），保留回退余地；
> - 确认选择的是正确时间点的备份文件，并已通过 `.sha256` 校验。

### 3. 后台一键恢复

后台「定时任务 → 备份列表」每行提供「恢复」按钮：二次确认后还需输入 `RESTORE` 才会提交。服务端会校验文件名（仅允许 `my_api_db_*.sql.gz`）、gzip 完整性与 `.meta.json` 中的 SHA-256，任一不符直接拒绝；校验通过后**先自动执行一次当前库备份**作为回退点，再用 `mysql` 客户端流式导入（`MYSQL_PWD` 传密码，不拼命令行）。恢复结果与恢复前备份名写入操作审计（`action=backup_restore`）。恢复期间建议暂停业务写入，完成后重启服务并抽查接口。

### 4. 恢复步骤（命令行）

```bash
# 1) 校验备份文件完整性（可选但建议）
sha256sum -c my_api_db_20261026_030000.sql.gz.sha256

# 2) 解压（保留原文件，便于重试）
gzip -dk my_api_db_20261026_030000.sql.gz

# 3) 恢复（会覆盖现有表结构与数据，请确认库名）
mysql -h 127.0.0.1 -P 3306 -u root -p my_api_db < my_api_db_20261026_030000.sql

# 4) 重启服务，确认后台可登录、接口正常返回
python main.py
```

恢复完成后建议检查：
- `/admin` 能正常登录，概览统计正常；
- 抽查几个高频接口（如热榜、诗词、笑话）是否有数据返回；
- 「审计日志」中出现恢复时间段之后的记录属于正常现象（恢复只覆盖数据库快照内的数据）。

### 5. 常见恢复失败原因

- **备份文件为空或损坏**：检查 `.meta.json` 中的 `verified` 字段与 `remark`；优先换用其他时间点的备份。
- **MySQL 版本不一致**：高版本导出的 SQL 在低版本可能语法不兼容，尽量保持与备份时相同的 MySQL 大版本。
- **字符集报错**：确认库和连接均使用 `utf8mb4`，导入时可追加 `--default-character-set=utf8mb4`。
- **权限不足**：恢复需要建表、写入权限，建议使用具备该库全部权限的账号。

---

## 九、故障排查

### 1. MySQL 断连

**现象**：接口大量返回 500，`/health` 返回 503 或提示数据库不可用，日志出现 `Lost connection to MySQL server` / `Can't connect to MySQL server`。

**排查顺序**：
1. 确认 MySQL 进程：`systemctl status mysql`（Windows 为服务管理器）。
2. 命令行连库：`mysql -h 127.0.0.1 -P 3306 -u root -p -e "SELECT 1"`，排除账号、密码、库名错误。
3. 核对环境变量 `DB_HOST / DB_PORT / DB_USER / DB_PWD / DB_NAME` 是否被覆盖。
4. 查看连接数：`SHOW PROCESSLIST;`、`SHOW VARIABLES LIKE 'max_connections';`，连接耗尽时会出现间歇性失败。
5. 确认 `wait_timeout`：连接空闲过久被服务端断开时，`app/core/db.py` 的 `pool_pre_ping` 与 `pool_recycle=3600` 会自动探活与回收；若仍频繁断连，优先排查网络与中间代理的超时设置。

**处理**：重启 MySQL 或服务；适度提高 `max_connections`；拉长 `wait_timeout`；云数据库检查白名单与安全组。

### 2. 接口熔断

**现象**：天气、汇率、热榜等接口持续返回本地兜底数据；后台「安全设置 → 外网接口熔断监控」显示「熔断中」。

**排查**：查看该接口的最近错误、成功率与冷却截止时间；在服务器上直接请求第三方地址，确认是网络不通、超时还是第三方限流/封禁；检查是否被单接口限流挡住。

**处理**：等待冷却结束自动半开探测，或在后台手动「重置」；第三方不可用时先在「接口管理」禁用对应接口，避免持续失败；必要时调大 `TIMEOUT` 或更换数据源。

### 3. 脚本卡死

**现象**：脚本长时间处于 `running`，列表「最近一次」显示运行时长超过 `SCRIPT_LONG_MINUTES`（默认 30 分钟）并带超时提醒。

**排查**：打开脚本控制台看最后一行输出，判断卡在哪个请求；爬虫类脚本常见原因为第三方无响应、未设置超时、被反爬拦截。

**处理**：后台「停止」终止进程（守护线程也会在发现子进程仍存活时主动结束进程树），修正脚本内的 `requests` 超时与重试，再手动运行一次验证；耗时确实较长的任务改用「定时任务」并设置合理周期，避免重叠执行。超过 `SCRIPT_TIMEOUT_MINUTES`（默认 120 分钟）会被强制终止。

### 4. 批量导入失败

**现象**：导入任务状态 `failed`，或成功但大量行被跳过。

**排查**：
1. 「导入任务记录 → 详情」查看逐行错误：缺必填字段、类型转换失败、查重字段为空、包含敏感词；可点「复制全部错误记录」粘到 Excel 批量修正。
2. 对照「下载模板」检查表头名称与列顺序，表头必须与模板一致。
3. CSV 请使用 UTF-8 编码，Excel 请确认没有合并单元格。
4. 超长文本会被截断并计入「截断」次数，属于预期行为。
5. 文件过大时会被大小上限拦截（默认 20MB，`IMPORT_MAX_MB` 可调），请拆分后上传。

**处理**：先用「预览校验（dry-run）」跑一遍确认错误数量，按行号修正后重新上传；重复数据会被自动跳过，不需要手工去重。

### 5. 日志表膨胀或数据库占满

**排查**：后台「概览 → 接口调用趋势」查看访问峰值；「日志清理」检查保留天数与最大行数是否合理；「接口列表」对高频接口关闭日志开关；「自动清理」确认备份自动清理已启用。

**处理**：设置 `log_clean_max_rows` 启用容量兜底（超限优先删除更早日志），或手动执行一次清理；导出归档后可直接清理历史日志；备份目录过大时降低备份保留天数。

---

## 十、版本升级说明

### 1. 升级步骤

1. 先备份：后台「定时任务 → 立即备份」，或执行 `python -m app.services.backup`。
2. 停止服务，替换代码（`app/`、`main.py`、`templates/`、`static/` 一并更新）。
3. 启动服务：自动执行 `Base.metadata.create_all` 建表，再由 `_ensure_schema` 补齐历史缺失字段。
4. 登录后台确认各页面正常，再放量。

### 2. 数据表创建与自动补字段

- 启动时 `_create_tables()` 调用 `Base.metadata.create_all`：表不存在就新建，已存在则跳过，不会覆盖数据。
- `_ensure_schema` 只做「新增列」兜底：字段不存在时执行 `ALTER TABLE ... ADD COLUMN`，可重复执行，不会重复添加。
- 不修改已有列类型、不删除列、不自动迁移数据；类型调整需要手工执行 SQL。
- 新增列均带默认值或允许为空，旧数据不会因补字段报错。
- 旧版本遗留的明文 Token 会在启动时自动转换为 SHA-256 哈希并写入 `token_hint`，此过程不可逆。

### 3. 升级风险

- 大表执行 `ALTER TABLE` 可能短暂锁表，建议在低峰期升级并预留足够磁盘空间。
- 跨大版本恢复备份存在 MySQL 语法与字符集兼容风险，参见「七、备份与恢复」。
- 新版本新增的后台菜单、定时任务线程需要重启后才会生效。
- 若升级后异常，回滚方式为：停止服务 → 恢复旧代码 → 用升级前的备份恢复数据库（会覆盖升级后写入的数据）。

### 4. 升级上传清单

每次升级把这几个路径整体覆盖上传（其余不要动）：

| 上传 | 说明 |
| --- | --- |
| `main.py` | 入口与启动参数 |
| `app/` | 全部后端代码（含 `app/core/config.py`、`app/core/models.py`、`app/admin/panel.py`） |
| `templates/` | 后台与 C 端页面模板 |
| `static/` | 静态资源与 `favicon.ico` |
| `fonts/` | 验证码字体（**Linux 必需，漏传会导致验证码字形异常**） |
| `scripts/` | 运维脚本（新增/改名时同步，如已删除 `数据库维护.py`） |
| `requirements.txt` | 依赖变化时执行 `pip install -r` |

**不要上传**：`data/`（服务器运行数据与告警流水）、`backups/`、`archive/`、`openSSL/`（生产证书由 Nginx 提供）、`.venv/`、`__pycache__/`、`.env`（含真实密钥）。

重启后按顺序自检：`/health` → `/portal/login`（验证码能刷新）→ `/admin`（favicon 显示）→ 后台「脚本管理」是否列出新脚本。

---

## 十一、公网部署安全注意事项

### 1. 启用 HTTPS

- 用 Nginx/Caddy 反向代理到 `127.0.0.1:8000`，只对外暴露 443（80 仅用于跳转）。
- 证书可用 Let's Encrypt 自动续期；禁止使用自签名证书对外提供服务。
- 设置 `APP_ENV=prod`、`ADMIN_COOKIE_SECURE=1`，后台会话 Cookie 只在 HTTPS 下传输。
- Uvicorn 启动时加 `--proxy-headers --forwarded-allow-ips="127.0.0.1"`，确保后台访问 IP 白名单与调用日志拿到真实客户端 IP。
- 生产环境保持文档关闭（`APP_ENV=prod` 默认关闭 `/docs`），确有需要再临时设置 `DOCS_ENABLED=1`。

### 2. 后台访问防护

- 先把自己的固定 IP 加入「后台访问 IP 白名单」，确认能登录后再开启 `admin_ip_guard`，避免把自己锁在门外。
- 修改默认后台账号密码与 `ADMIN_SECRET`（会话签名密钥），全部通过环境变量注入，不要写进代码仓库。后台账号密码最少 8 位。
- 日常运维使用 `viewer` 只读账号，仅排障或改配置时使用 `admin`。
- 后台路径 `/admin` 不要对外宣传；接口 Token 定期轮换，离职或设备丢失时立即禁用。
- 登录失败 5 次会临时锁定 1 分钟；验证码一次性失效，重复提交同一验证码会被拒绝，可在 Nginx 层再按 IP 限速或加 WAF。

### 3. 防火墙与网络策略

- 云安全组/本机防火墙只放行 `80/443`；`3306` 仅允许本机或内网访问，绝不直接暴露公网。
- 后台与数据库不要绑定 `0.0.0.0` 对外；如必须远程运维，走 VPN 或跳板机。
- 定期检查 `ip_rule`（**接口 IP 黑白名单**）与 `admin_ip_whitelist`（**后台访问 IP 白名单**），清理过期规则。
- 开启系统与 MySQL 的自动安全更新，定期查看「操作审计」中的异常登录、数据导出与敏感配置变更。
- 备份目录放在独立磁盘并限制访问权限，`.sha256` 与 `.meta.json` 一并保留，便于验证与恢复；建议配置 `BACKUP_NOTIFY_WEBHOOK` 及时收到备份失败提醒。

---

## 十二、多实例部署与监控

### 1. 多实例部署

多台机器（或多个进程）同时对外提供服务时，把 `REDIS_URL` 配置到同一个 Redis 即可：

```bash
# 每台实例的 HOST_ID 必须不同，用于区分脚本进程归属
REDIS_URL=redis://127.0.0.1:6379/3
REDIS_PREFIX=apicache:
HOST_ID=api-1
```

- **缓存 / 限流 / nonce**：都写入 Redis（键前缀 `REDIS_PREFIX`），多实例计数合并为全局精确值；Redis 不可用时自动回退进程内实现，不阻断接口。
- **分布式锁**：`app/core/dist_lock.py` 按 Redis `SET NX EX`（释放时 Lua 比对 token）→ MySQL `GET_LOCK` → 进程内锁三级降级。定时任务、自愈巡检、孤儿进程守护都会先抢锁，只有拿到锁的实例执行，避免重复跑脚本 / 重复备份 / 多机同时 Kill 进程。
- **脚本归属**：`script_run.host` 记录运行实例（`HOST_ID` 或主机名）；孤儿进程守护与启动清理只处理本机记录，不会误杀其他机器的进程。
- 未配置 Redis 时，MySQL `GET_LOCK` 仍能保证定时任务不重复执行；但缓存与限流计数是各进程独立的。

### 2. Prometheus 监控

`GET /metrics` 输出 Prometheus 文本格式，与 `/api` 使用同一套 Token（放在请求头 `token` 或查询参数 `?token=` 均可）：

| 指标 | 类型 | 说明 |
| --- | --- | --- |
| `http_request_duration_seconds_bucket` / `_sum` / `_count` | histogram | 请求耗时分布，桶：0.01/0.05/0.1/0.25/0.5/1/2/5/10 秒，另含 `+Inf` |
| `http_requests_total{path,status}` | counter | 按路径与状态码累计请求数（路径数上限 300，超出归并） |
| `db_pool_usage` | gauge | 数据库连接池使用率（0-1） |
| `cache_hit_ratio` | gauge | 缓存命中率（0-1） |

`prometheus.yml` 抓取示例：

```yaml
scrape_configs:
  - job_name: my-api
    metrics_path: /metrics
    params:
      token: ["<你的 API_TOKEN>"]
    static_configs:
      - targets: ["10.0.0.1:8000", "10.0.0.2:8000"]
```

- 多实例直接用 `static_configs` 列出全部地址，Grafana 中按 `instance` 区分；建议图表：请求 QPS、P95 耗时、5xx 比例、连接池使用率、缓存命中率。
- 后台「服务监控」页另有内置视图：延迟分布折线图（可按路径筛选、`compare=1` 与上一周期 P95 对比）与指标摘要（`GET /admin/api/metrics/summary`），无需部署 Prometheus 也能看。

### 3. 日志与告警

- **日志滚动**：`LOG_FILE` 指定文件后启用 `TimedRotatingFileHandler`，每天零点切分并保留 `LOG_ROTATE_DAYS` 份；`LOG_JSON=1` 输出 JSON 行，便于 ELK / Loki 采集。
- **Loki 推送**：`LOKI_URL` 启用异步推送（`QueueHandler` + 独立线程），推送失败只记录本地日志，不影响请求。
- **告警分级**：高优（接口不可用 / 熔断 / 主机资源 / IP 风控）走 `ALERT_WEBHOOK_HIGH` 并按 `ALERT_AT_MOBILES` @ 相关人员；低优（脚本失败 / 限流 / Token 宽限）走 `ALERT_WEBHOOK_LOW`。高/低渠道留空时分别回退到 `ALERT_WEBHOOK`，后台「智能自愈与告警中心」可单独配置。
- **IP 风控**：`IP_BURST_LIMIT` 开启后按分钟计数，超限冻结 `IP_FREEZE_SECONDS`；`IP_GEO_ENABLED=1` 时检测 `IP_GEO_MINUTES` 窗口内的国家/地区漂移并冻结。
- **主机资源告警**：后台 `_host_check_loop` 每 `HOST_CHECK_INTERVAL`（默认 60 秒）采集 CPU/内存/磁盘；CPU、磁盘沿用主机告警，内存交由主动自愈状态机连续确认，**不依赖是否打开监控页**。CPU/内存采集需要安装 `psutil`，未安装时仅能采集磁盘。
- **主动自愈完整链路**：独立线程按 `SELF_HEAL_INTERVAL`（默认 30 秒）检测接口 5xx、脚本失败和内存过高。同一场景必须连续 3 次异常才确认故障；未满 3 次即恢复只写入「异常已自动恢复」事件，不推送。后台点选场景即可独立配置自愈开关、安全动作说明、最大重试次数（默认 3 次）、重试间隔（默认 60 秒）、企业微信/邮件渠道、重复提醒频率（默认 5 分钟）和跨午夜静默期，并实时查看连续异常数、已重试次数、锁定状态与最近动作。
- **重试、聚合与恢复**：接口异常会重建连接并重置外部数据源熔断，内存过高会释放数据库空闲连接并执行垃圾回收，脚本失败会按原参数通过现有执行器安全拉起（禁止 shell 拼接）。达到重试上限仍未恢复时锁定自动重试并转入告警；同一轮到期的多场景异常合并发送，未恢复时按各场景频率持续提醒。自愈成功或人工修复后单独发送「✅ 已恢复」，不受普通告警静默期和重复频率限制，但仍受告警总开关、分类开关及渠道开关约束。
- **脚本安全边界**：服务启动时建立脚本运行基线，只处理基线之后的新失败，不会因重启自动重跑历史失败任务；自动拉起记录的操作人为 `system-self-heal`，复用脚本参数白名单与单实例限制。MySQL、磁盘和连接池继续沿用原有基础资源自愈机制。
- **推送状态**：智能自愈与告警中心会显示「最近推送：成功/失败 + 错误原因」；推送失败同时写入「服务监控 → 启动日志」，便于排查 Webhook 失效、网络不可达等问题。

### 4. Token 轮换

- 后台 Token 列表的「轮换」按钮调用 `POST /admin/api/tokens/{id}/rotate`：立即签发新 Token，旧 Token 进入宽限期（`TOKEN_ROTATE_GRACE_HOURS`，默认 24 小时），期内仍可调用并触发低优告警，便于平滑切换客户端。
- 宽限到期后旧 Token 自动失效；列表中会显示宽限截止时间。

---

## 十四、更新日志

> 按日期倒序记录变更；标注 **破坏性** 的条目升级前需按说明处理。

### 2026-11-08

**后台运维增强（第二批）**
- **备份一键恢复**：`app/services/backup.py` 新增 `restore_backup`，后台「定时任务」备份列表每行增加「恢复」按钮（二次确认 + 输入 `RESTORE`）。恢复前校验文件名、gzip 完整性与 meta SHA-256，并自动先做一次回退备份；使用 `mysql` 客户端流式导入（`MYSQL_PWD` 传密码、`shell=False`），结果写入审计 `backup_restore`。
- **注册审核开关**：`app_setting` 新增 `portal_review_mode`（`auto` / `manual`），后台「C 端用户」页可随时切换；审批线程每轮读取模式，人工模式下只跳过自动审批，不影响其他巡检。
- **敏感词命中统计**：`app/core/sensitive.py` 记录命中次数与 Top 词（内存累计，最多每 60 秒落库一次 `sensitive_hit_stats`），后台「安全设置」新增命中统计面板，可刷新与清零。
- **接口用量看板**：新增 `GET /admin/api/usage-stats?days=7`，按接口聚合调用量、错误率与平均耗时；后台概览新增 7 / 14 / 30 天切换的用量看板，错误率高于 5% 标红。
- **额度邮件预警**：新增后台线程按 `PORTAL_QUOTA_WARN_INTERVAL`（默认 3600 秒）扫描正常用户，剩余额度低于阈值时发邮件提醒，每用户每天最多一次；阈值可在「C 端用户」页修改（`portal_quota_warn_threshold`，0 关闭）。配置新增 `PORTAL_QUOTA_WARN_THRESHOLD` / `PORTAL_QUOTA_WARN_INTERVAL`。
- 新增 `tests/test_backup_restore.py`、`tests/test_portal_quota_warn.py`，测试总数增至 44 项。
- **Token 到期提醒**：新增后台线程，默认每天扫描一次 `api_token`，对 7 天内到期的 C 端 Token 按用户合并发邮件（每用户每天最多一封）；后台 Token 列表新增「续期 30 天」，只延长 `expires_at`，Token 值、权限与掩码不变，并写入审计。配置 `TOKEN_EXPIRY_WARN_DAYS` / `TOKEN_EXPIRY_WARN_INTERVAL`。
- **全站 QPS 总闸**：新增 `GLOBAL_QPS_LIMIT`（默认 0 关闭），超过阈值的 `/api/` 请求统一返回 429，使用 `CACHE.incr` 计数；Redis 后端为全局限流，本地缓存为单进程限流。
- **系统诊断显示运行模式**：启动与后台「系统诊断」共用 `runtime_mode_info()`，顶部直接显示 dev/prod、监听地址、HTTPS 与热重载状态。
- **备份恢复磁盘预检**：恢复前读取 gzip 原始大小并检查备份目录剩余空间（预留 100 MB），不足直接中止并写审计。
- **邮箱验证码日限改为按发送尝试计数**：SMTP 发送失败也占用当日额度，避免失败重试刷爆发信配额。
- **重置数据库安全加固**：默认禁用（`ALLOW_DB_RESET=0`），需临时开启并重启；后台运行需确认项 + `RESET` + 管理员密码，禁止定时任务与自愈自动执行，成功/失败/拒绝均写审计。
- **调用日志只记录接口请求**：`/`、`/robots.txt`、`/success.txt`、`/.env` 等站外扫描与探测不再写入 `api_log`；概览的累计请求、今日请求、错误数与调用趋势、请求日志列表、日志导出均只统计 `/api/` 调用，历史噪音行保留在库中但不再参与统计。

**部署与跨平台适配（Linux / 宝塔）**
- 启动方式不再与 Nginx 抢 443：`python main.py` 现在按 `APP_ENV` 自动切换——dev 监听 `0.0.0.0:443` + `openSSL` 自签证书并开热重载；**prod 监听 `127.0.0.1:8000`、不加载 SSL、关闭 reload**，HTTPS 与 443 全部交给 Nginx。`HOST`/`PORT`/`SSL_CERTFILE`/`SSL_KEYFILE` 仍可覆盖，两个 SSL 变量同时为空则禁用 HTTPS；启动时打印当前模式并写入「系统诊断」启动日志。
- 新增 `TIME_ZONE` 配置（默认 `Asia/Shanghai`）：启动时设置 `TZ` 并调用 `time.tzset()`，修正 Ubuntu 服务器默认 UTC 导致日志、告警与备份时间慢 8 小时的问题。
- 桌面通知在 Linux 上不再静默丢弃：保留 Windows Toast，非 Windows 环境改为写入启动日志，便于在「系统诊断」核对通知时机（服务器端仍以企业微信 + 邮件为主渠道）。
- 核验告警事件跨进程写入：三进程并发各写 80 条，实测 240 条全部落盘、JSON 坏行 0，断网/恢复事件不会再因多实例覆盖而丢失。
- README 新增「十、4 升级上传清单」，明确每次升级要上传与不要上传的路径（尤其 `fonts/`），并给出重启后的自检顺序。
- 新增 `tests/` 单元测试（标准库 unittest，34 项，`python -m unittest discover -s tests -v`）：覆盖告警事件读写与并发写入、通知结果合并、通知冷却占位、网络状态原子更新、验证码图片与一次性校验、签名与防重放、配置环境变量解析。
- 新增 `fonts/` 目录并随代码分发 `DejaVuSans-Bold.ttf`：图形验证码字体按「`FONT_DIR` → 项目 `fonts/` → 系统字体 → Pillow 默认」顺序加载，解决 Linux 缺少 Arial 时验证码退化成 11px 点阵、看不清的问题；验证码尺寸 160×50、字号 28px。
- 后台页 favicon 改为绝对路径并增加内联 SVG 兜底：修复 `/admin`（无结尾斜杠）时相对路径解析成 `/static/favicon.ico` 导致 404、标签图标不显示的问题。
- 「换一张」按钮移除，改为点击验证码图片直接刷新；登录/申请/找回三种模式固定高度，消除滚动条；切回登录模式后底部「游客接口预览（游客）」链接不再丢失。

**运维脚本**
- **破坏性**：删除 `scripts/数据库维护.py`，维护逻辑并入 `scripts/数据初始化.py`（一键执行「补注释 + 灌数据」）；如配置过对应定时任务，请改指向「数据初始化」。
- 新增 `scripts/数据清理.py`：批量 TRIM 文本字段、删除关键字段为空的记录、校验图片 URL / 历史日期 / 取名分类 / 怼人级别格式，只处理 17 张内容表。
- 新增 `scripts/重置数据库.py`：清空全部 44 张表恢复全新状态；默认禁用，仅首次部署时设 `ALLOW_DB_RESET=1` 并重启后可用；后台还需「确认项 + 输入 RESET + 管理员密码」，并禁止定时任务与自愈自动执行；每次运行/拒绝均写入审计。
- 后台「脚本管理」白名单同步更新（新增「数据清理」「重置数据库」，移除「数据库维护」）；「重置数据库」固定在列表最后、按钮标红，未开启总开关时显示「已锁定」。
- 修复 `数据初始化.py` 中诗词种子数据缺少 `dynasty` 字段导致的 `Column 'dynasty' cannot be null`。

### 2026-11-04

**C 端账号、Token 与文档**
- 登录、申请账号、忘记密码三种模式固定为同一窗口高度并美化登录背景；新增邮箱验证码找回密码，注册和重置验证码按用途隔离。
- 接口目录详情页、在线测试、Token 输入和测试结果区域紧凑化。
- 修复用户 Token 重置后权限恢复为 `*` 的越权问题：现在只轮换密钥，保留接口权限、启停状态与过期时间。

**用户工具**
- 实用工具扩展至 12 个，新增 URL 编解码、Base64 文本/图片转换、子网掩码/IP 转 Int、Faker 中文假数据生成器。
- 图片处理小助手支持 1–8 张批量处理，输出由 3 种增加到 PNG、JPEG、WebP、GIF、BMP、TIFF、ICO 共 7 种，多图自动打包 ZIP；依赖新增 Faker、Pillow。

**后台体验与运维**
- 请求日志「列设置」移动到筛选栏下方第二行左侧，并修复弹层列名颜色不可见；系统配置的全局配置台按内容完整展开，不再需要内部滚动；API 调试台左侧接口卡片缩小。
- 数据同步按热榜完整标题查重；脚本子进程通过 `API_SCRIPT_CHILD=1` 跳过主服务恢复和守护清理，避免重启时误清理自身或显示残留子进程失败原因。

### 2026-11-03

- 「告警中心」升级为「智能自愈与告警中心」：接口、脚本、内存形成「检测 → 连续 3 次确认 → 自愈 → 重试 → 锁定告警 → 恢复通知」完整链路，默认每 30 秒主动巡检。
- 新增三场景独立实时配置面板：自愈开关、动作说明、3 次重试、60 秒间隔、企业微信/邮件渠道、5 分钟重复提醒及 00:00–08:00 默认静默期均可分别保存；页面每 5 秒只刷新实时状态，不覆盖正在编辑的配置。
- 新增多异常聚合发送、单场景频率限制、跨午夜静默和独立「✅ 已恢复」通知；瞬时波动只记录自愈事件，不发送告警。
- 脚本自愈复用后台安全执行器，保留原参数、运行记录与单实例限制；启动基线防止历史失败被误重跑。保留 MySQL、磁盘、连接池原有自愈能力。

### 2026-10-31

**目录与体验**
- **（破坏性）代码目录重构**：基础模块统一移入 `app/` 包 —— `app/core/`（config、db、models、cache、dist_lock、metrics、mailer、sensitive、sign_utils）、`app/services/`（backup）、`app/admin/`（panel）；根目录只保留 `main.py`、`README.md`。升级时请整体替换 `app/` 与 `main.py`；手动备份命令改为 `python -m app.services.backup`。
- 移除 Alembic（删除 `alembic.ini` 与 `migrations/`）：建表统一由启动时的 `Base.metadata.create_all` + `_ensure_schema` 完成，减少一个可选依赖。
- 根目录清理：早期导入模板 Excel（约 148MB）归档到 `archive/import_templates/`，删除编译缓存与空目录；`data/` 只保留实际使用的 JSON 数据。
- 概览页去重：「运行信息」精简为备份目录、磁盘剩余、已禁用接口、运行环境，移除与上方卡片重复的数据库状态、数据表数量与服务器时间。
- 智能自愈与告警中心修复：新增「主机使用率阈值」可配置项；主机 CPU/内存/磁盘改由后台巡检线程每 60 秒检测并告警，不再依赖是否打开「服务监控」页面；智能自愈与告警中心显示最近一次 Webhook 推送结果，推送失败写入启动日志，便于定位「有事件但没收到通知」。
- README 更新目录结构、环境变量表，新增「多实例部署与监控」章节（Redis、`HOST_ID`、分布式锁、Prometheus 抓取、告警分级、IP 风控、Token 轮换）。

### 2026-10-30

**多实例与运维**
- `app/core/cache.py` 改为双后端：配置 `REDIS_URL` 时自动使用 Redis（键前缀 `REDIS_PREFIX`），缓存、Token/限流计数在多实例间共享；Redis 不可用自动回退进程内缓存，不阻断业务。
- 限流计数器改用 `INCR + EXPIRE`（Redis 下为全局精确 QPS），首次写入时开始计时。
- 新增 `app/core/dist_lock.py`：Redis `SET NX EX`（Lua 比对释放）→ MySQL `GET_LOCK` → 进程内锁三级降级；附带 `host_id()` 实例标识。
- 定时任务调度加分布式锁：同一任务同一时刻只由一台机器执行，解决多实例重复跑脚本 / 重复备份。
- 脚本运行记录新增 `host` 字段，孤儿进程守护与启动清理只处理本机记录；巡检线程自身也持有分布式锁，避免多机同时 Kill 同一进程。
- 新增 `/metrics`（Prometheus 文本格式）：`http_request_duration_seconds_bucket/_sum/_count`、`http_requests_total{path,status}`、`db_pool_usage`、`cache_hit_ratio`，需 Token 访问；后台「服务监控」新增延迟分布图表与指标摘要。
- 日志支持按天滚动：`LOG_FILE` 启用 `TimedRotatingFileHandler`（保留 `LOG_ROTATE_DAYS` 份）；`LOKI_URL` 启用异步推送 Loki（`QueueHandler` + 独立线程，不阻塞请求）。

**安全**
- 新增 `app/core/sign_utils.py`：标准化 HMAC-SHA256 签名（`METHOD\nPATH\nTIMESTAMP\nNONCE\n + body`），内置 `SignClient` 客户端与服务端 `verify()`、`check_nonce()`；签名中间件改用它并支持 Redis nonce 去重。
- Token 支持**密钥轮换**：`POST /admin/api/tokens/{id}/rotate` 生成新 Token 立即生效，旧 Token 保留 `TOKEN_ROTATE_GRACE_HOURS`（默认 24 小时）宽限期，宽限期内每次使用触发低优告警；后台 Token 列表显示宽限截止时间并提供「轮换」按钮。
- 新增 IP 风控：`IP_BURST_LIMIT`（单 IP 每分钟上限）+ `IP_FREEZE_SECONDS` 自动冻结；可选 `IP_GEO_ENABLED` 地理位置漂移检测（默认窗口 `IP_GEO_MINUTES`），触发即冻结并高优告警。
- 告警分级推送：高优（接口不可用 / 熔断 / 主机资源 / IP 风控）走 `alert_webhook_high` 并 @ `alert_at_mobiles`；低优（脚本失败 / 限流 / Token 宽限）走 `alert_webhook_low`；后台智能自愈与告警中心可分别配置。
- 日志文件与 Loki 推送见上；敏感词隔离、签名校验等既有能力保持不变。

**体验**
- 在线测试新增 **Mock 模式**：勾选后不请求后端，按接口路径生成预期返回 JSON 结构，便于离线理解。
- 延迟分位数图表新增**对比**：可填接口路径只看单个接口，或勾选「对比上一周期」叠加前一天/上一窗口的 P95 虚线。
- 数据预览支持**双击 Inline Edit**：双击单元格直接修改，回车 / 失焦保存、Esc 取消，保存后同步刷新本地缓存。

### 2026-10-29

**功能**
- 后台用户信息（账号 / 角色 / 登录时间）与「退出登录」从左侧栏底部移到右上角工具栏。
- 新增**顶部标签页**：点击菜单即新增标签，重复点击只切换；最多同时保留 10 个，超出时弹窗提示并关闭最早的非当前标签；标签支持单独关闭，关闭当前标签自动切到相邻标签。
- 标签页超限提示增加「本次登录内不再提示」勾选框（默认勾选，记在 `sessionStorage`），勾选后不再弹窗；标签栏新增「全部关闭」按钮，可一键清空并回到平台概览。
- 左侧菜单折叠去掉工具栏按钮，改为侧栏右边缘的圆形箭头按钮，方向随折叠状态翻转。
- 服务器时间移入右上角账号信息下方，改为每秒实时刷新（原先依赖 4 秒一次概览刷新，页面异常时会停住）。
- 标签页尺寸整体加大，标签栏下方增加分隔线与页面内容区分。
- 移除后台内嵌「接口文档」预览入口与 `/admin/api-docs` 页面，需要查看文档请直接访问 `/docs`。
- Token 权限改为列表内「编辑权限」按钮：勾选接口路径即可调整可访问范围（全选 = `*`，全部取消 = 无权限），同一路径的多个请求方法只显示一次。
- 数据质检 `poem_lib` 查重口径改为 `title + author + dynasty + content`。
- 数据统计页的下拉框（数据预览 / 重复数据清理）默认不再自动查询，需手动选择后再加载。
- 数据统计的「数据表统计 / 数据质检 / 数据预览与在线修改」三个面板支持折叠。
- 日志自动清理只保留“保存设置”，“立即清理”并入「手动清理」区（手动清理历史日志与备份文件）。
- 批量导入的「导入记录」与「导入任务记录」合并为一个列表。
- 请求日志日期范围默认当天，重置筛选后也回到当天。
- 环境自检历史在 MySQL 不可用时写入 `data/envcheck_runs.json` 兜底，恢复后与数据库记录合并展示。
- 概览「请求状态分布」悬停时中心文字字号由 20 调整为 12，避免遮挡环形图。
- 概览移除「最近错误请求」面板，错误明细统一到「请求日志」页查看。
- 顶部标签页支持**拖拽排序**，右键菜单可「关闭左侧标签 / 关闭右侧标签 / 关闭全部标签」。
- 概览新增**接口延迟分位数**折线图（P50 / P95 / P99），支持按天（最近 14 天）与按小时（最近 24 小时）切换；后端按 10ms 粒度做直方图聚合估算分位数，结果缓存 120 秒，避免大表全量拉取。
- 概览顶部新增 3 张 iOS 风格大卡片：服务器时间（每秒刷新）、运行时长（可切换单位）、数据库大小；数字类卡片增加从 0 滚动到目标值的动画，并支持 `prefers-reduced-motion` 降级。
- 新增**智能自愈与告警中心**菜单（运维监控下）：统一配置企业微信 Webhook、总开关、冷却时间、接口频率/错误率阈值与四类告警开关（接口异常、熔断、脚本失败、CPU/内存/磁盘超限），支持发送测试消息、按类型筛选告警事件、一键清空事件。
- 全局搜索支持 `Ctrl+K` 快捷聚焦、↑↓ 选择结果、回车跳转、Esc 关闭；搜索范围新增调用日志（按路径 / IP 匹配）。
- `mysqldump` 自动查找：Windows 下会扫描 `%ProgramFiles%` 下的 MySQL / MariaDB 安装目录以及 XAMPP、WAMP 常见路径，`scripts/check_env.py` 未找到时给出加入 PATH 的具体建议。

**修复**
- 概览「热门接口 TOP 8 / 接口调用次数分布」改为只统计 `/api/` 开头的真实接口：自动合并历史 `/v1/api` 路径，过滤 `@vite`、`.well-known` 等浏览器噪音路径；日志中存在但未在接口列表登记的路径标为「未登记接口」。
- 全局搜索点击结果后自动清空搜索框并关闭下拉，不再需要手动删除。
- 登录页提交改为 fetch 校验，不再整页刷新：账号或密码错误只提示并保留已输入内容；验证码错误/过期只提示、自动刷新验证码并清空验证码输入框；验证码改为校验通过后才作废，密码输错可继续用同一验证码重试。
- 备份找不到 `mysqldump` 时容易误判为“备份失败”，现改为自动查找并明确提示回退到纯 Python 导出的原因。
- Token 权限编辑：取消个别接口后仍按 `*` 保存、改动被覆盖的问题已修复，保存结果以勾选状态为准。
- 敏感词库按一级分类树按需加载，避免一次性加载全部词条导致菜单卡顿。
- 数据表导出新增「导出 CSV」：CSV 文件带 UTF-8 BOM，Excel 双击打开不乱码。

**运维与安全**
- 统一响应新增 `timestamp` 与 `request_id`（同时回写 `X-Request-ID` 响应头），用户反馈错误时可直接用 request_id 定位日志。
- 响应启用 Gzip 压缩；ECharts 本地 JS 加载失败时自动降级到 jsDelivr / unpkg CDN，防止静态资源被误删后图表全白。
- 批量导入新增「只重试失败行（N）」：下载错误报告、修正 Excel 后重新上传，只处理上次失败的行，已成功的行自动跳过。
- `scripts/check_env.py` 增强：新增磁盘剩余空间、静态文件完整性、外网接口可达性检查，每项失败都给出中文修复指引。
- 新增自愈巡检线程（`SELF_HEAL_ENABLED`，默认开启，间隔 `SELF_HEAL_INTERVAL` 秒）：检查 MySQL 连接、磁盘剩余（低于 `SELF_HEAL_DISK_MIN`%，默认 10%）与连接池占用（超过 `SELF_HEAL_POOL_PERCENT`%，默认 85%）；触发阈值时除 Webhook 告警外，自动清理过期日志/备份、释放连接池，并按动作冷却（`SELF_HEAL_COOLDOWN`）避免告警风暴。
- 新增可选 JSON 日志：`LOG_JSON=1` 时 Uvicorn 与应用日志改为单行 JSON，便于接入 ELK / Loki，默认关闭保持原文本格式。
- 引入 Alembic 迁移（`alembic.ini` + `migrations/`）：启动自动升级到最新版本，未安装时回退自动建表，`_ensure_schema` 继续兜底补字段（2026-10-31 已移除，改为直接 `create_all`）。
- 新增**敏感词隔离区**：命中敏感词的导入行不再直接拒绝，改为写入隔离区，管理员在后台确认后写入目标表，支持批量通过 / 拒绝。
- 新增可选签名校验（`SIGN_ENABLED=1`）：对 `SIGN_PATHS` 前缀下的接口校验 `X-Timestamp + X-Nonce + X-Sign`（HMAC-SHA256），时间窗口 `SIGN_WINDOW` 秒，nonce 内存去重防重放；默认关闭。
- SQL 与 XSS 复查：后台 SQL 均为参数化查询或白名单表名拼接，前端输出统一 `esc()` 转义，导入错误详情等不再有注入风险。

**破坏性**
- 「权限矩阵」菜单移除，权限统一在 Token 列表「编辑权限」中维护。
- 「分类标签管理」「按条件批量修改」功能下线，`POST /admin/api/data/bulk-update`、`GET /admin/api/data/tags`、`POST /admin/api/data/tags/set` 接口移除。
- 「错误日志」独立菜单移除，错误请求在「请求日志」中通过状态码筛选查看。
- `GET /admin/api/overview` 返回体移除 `recent_errors` 与 `error_codes` 字段。
- 后台页面 `/admin/api-docs`（内嵌接口文档预览）删除，工具栏「接口文档」入口同步移除。

### 2026-10-27

**功能**
- 定时任务新增「数据库备份」类型：可按 cron（如 `0 3 * * *`）或固定间隔执行，支持单独指定备份目录；原「数据库备份」菜单下线，备份列表与「立即备份」并入「定时任务」页面。
- 「自动清理」新增「备份文件自动清理」：可设置开关、保留天数、目录，随日志清理线程每 6 小时执行。
- 接口列表新增**独立日志开关**（`api_switch.log_enabled`）与**独立缓存时长**（`api_switch.cache_ttl`），高频接口可单独关日志、静态内容可拉长缓存。
- 批量导入新增**预览校验（dry-run）**：只解析校验不入库，返回有效/可导入/重复/错误数量与逐行错误。
- 脚本运行新增 PID 落库（`script_run.pid`）、孤儿进程守护清理与全局超时（`SCRIPT_TIMEOUT_MINUTES`）。
- `/health` 新增连接池指标（`database.pool`）与 80% 使用率告警。
- 后台新增磁盘占用、数据库大小卡片；概览底部展示备份目录占用与磁盘剩余。
- 脚本 SSE 控制台新增自动滚动开关；导入详情支持一键复制全部错误；主题切换预加载 ECharts 暗色主题。
- 新增 `scripts/check_env.py` 环境自检脚本；README 补充 systemd 与 nssm 部署示例。
- 备份失败支持企业微信机器人通知（`BACKUP_NOTIFY_WEBHOOK` / `BACKUP_NOTIFY_SUCCESS`）。

**安全**
- 验证码改为一次性失效：无论校验成功或失败都立即作废。
- 批量导入限制为 `.xlsx/.xlsm/.csv`，单文件上限 `IMPORT_MAX_MB`（默认 20MB），并校验文件头。
- 数据导出写入审计日志（表名 + 筛选条件 + 导出条数）。
- 后台账号新建/改密最少 8 位。
- 敏感词校验前移到入库阶段，命中直接拒绝该行。
- 配置缓存由「全部清空」改为信号标记失效，避免配置变更瞬间击穿数据库。
- 在线测试按接口参数注解自动生成参数模板。

**破坏性**
- 「数据库备份」菜单移除，原入口迁移到「系统 → 定时任务」；书签或旧文档中的「数据库备份」路径不再可用。
- `is_deleted` 不再参与任何业务逻辑，后台删除为物理删除，不再提供回收站。
- 后台账号密码长度校验由 6 位提高到 8 位：已有短密码仍可登录，但修改密码时需满足 8 位。
- 新增字段 `api_switch.log_enabled`、`api_switch.cache_ttl`、`script_run.pid`、`script_schedule.task_type/cron_expr/backup_dir` 会在启动时自动 `ALTER TABLE` 补齐，无需手工执行 SQL，但需要**重启服务一次**后生效。

### 2026-10-26

- 新增 Token 多密钥管理（哈希存储 + 掩码展示）、接口 IP 黑白名单、后台访问 IP 白名单、单接口限流覆盖、外网熔断监控。
- 新增批量导入异步任务与任务记录表（`import_job`）、操作审计、敏感词库、后台账号角色（admin/viewer）。
- 新增备份完整性校验（gzip 解压 + 建表语句检查）、`.sha256` 校验文件与 `.invalid` 标记。
- 后台概览改为 ECharts 环形饼图与调用趋势，支持暗色/亮色主题。


