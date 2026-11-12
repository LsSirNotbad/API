# -*- coding: utf-8 -*-
"""
API 平台管理后台
- 登录鉴权（账号密码 + 图形验证码 + 会话 Cookie）
- 平台状态 / 调用日志 / 各表数据统计
- 接口启停管理
- 脚本管理：在后台运行采集/同步/初始化/维护脚本并查看实时输出
- 数据批量导入（Excel/CSV）与模板下载
所有后台页面挂在 /admin 下，不对外暴露 JSON 统计日志接口。
"""
import csv
import asyncio
import hashlib
import hmac
import inspect
import io
import ipaddress
import json
import os
import random
import re
import secrets
import shutil
import signal
import string
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import DateTime, String, Text, and_, func, or_, text
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from ..core import alert_store, config, dist_lock, metrics, sensitive
from ..core.models import PortalQuotaLogDB
from ..portal import (
    auto_approve_mode, ensure_initial_quota_log, quota_warn_threshold,
    record_quota_change, set_auto_approve_mode, set_quota_warn_threshold,
)

# 项目根目录（panel.py 位于 app/admin/，向上 3 级）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
LOGIN_HTML = os.path.join(TEMPLATE_DIR, "admin_login.html")
DASHBOARD_HTML = os.path.join(TEMPLATE_DIR, "admin_dashboard.html")
FIREWORK_HTML = os.path.join(TEMPLATE_DIR, "firework.html")
PARTICLES_HTML = os.path.join(TEMPLATE_DIR, "3D_Particles.html")

CAPTCHA_TTL = 300          # 验证码有效期（秒）
LOGIN_MAX_FAIL = 5         # 连续失败次数上限
LOGIN_LOCK_SECONDS = 60    # 触发上限后锁定时间
META_COLUMNS = {"id", "created_at", "dedup_md5", "is_deleted"}   # 导入时忽略的字段
# 各业务表用于查重的自然键字段（md5 由这些字段拼接计算）
DEDUP_FIELDS = {
    "knowledge": ("category", "title"),
    "retort": ("content",),
    "name": ("type", "text"),
    "joke": ("content",),
    "hot": ("title",),
    "image": ("img_url",),
    "idiom": ("word",),
    "poem": ("title", "author", "dynasty", "content"),
    "copy": ("content",),
    "wisdom": ("content",),
    "love": ("content",),
    "poison": ("content",),
    "riddle": ("question",),
    "xiehouyu": ("riddle",),
    "twister": ("content",),
    "answer": ("content",),
    "history": ("month_day", "year", "event"),
}
LOGIN_FAILS = {}           # {ip: [失败时间戳]} 登录失败记录
REQUIRED_CACHE = {}        # {表名: set(必填字段)}

SCRIPT_OUTPUT_LIMIT = 20000   # 每个脚本保留的输出字符数
SCRIPT_TASKS = {}             # {key: {state, output, proc, ...}} 脚本运行状态
SCRIPT_PID_HOOK = None        # setup_admin 注入：脚本启动后立即把 pid 写入 script_run 表
IMPORT_JOBS = {}              # {job_id: {...}} 异步导入任务状态
IMPORT_LOCK = threading.Lock()
IMPORT_JOB_KEEP = 3600        # 导入任务结果保留秒数，避免无限堆积
SCRIPT_DIR = os.path.join(BASE_DIR, "scripts")   # 脚本目录，禁止越界
SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # 参数强校验白名单

# 外部数据源路径 -> 中文名称（熔断监控展示用；与 net_get 调用中的 key 对应）
BREAKER_LABELS = {
    "/weibo": "微博热榜", "/zhihu": "知乎热榜", "/douyin": "抖音热榜", "/toutiao": "头条热榜",
    "/duanzi": "段子", "/kfc": "KFC 语录", "/hitokoto": "一言", "/bing": "必应壁纸",
    "/today-in-history": "历史上的今天", "/60s": "60秒读懂世界", "/weather": "天气",
    "/exchange-rate": "汇率",
}


# 全局搜索：覆盖全部后台页面，描述中保留常用别名
PAGE_SEARCH = [
    ("平台概览", "服务状态、接口用量看板、调用趋势与数据总览", "overview"),
    ("接口列表", "接口启停、接口开关、缓存 TTL、日志开关", "endpoints"),
    ("API 调试台", "中文业务接口、查询参数、Mock 沙盒、生成代码、参数模板", "tester"),
    ("数据统计", "各数据表记录量、导出、数据管理", "tables"),
    ("安全设置", "单接口限流、全局限流 QPS、IP 黑白名单、熔断、敏感词库与命中统计、后台账号", "security"),
    ("敏感词隔离区", "命中敏感词的导入数据，人工审核后放行", "quarantine"),
    ("Token 列表", "Token 管理、到期邮件提醒、一键续期、接口访问密钥与备注", "tokens"),
    ("C 端用户", "用户注册审批、注册审核开关、封禁、等级与累计额度、额度邮件预警", "c-users"),
    ("请求日志", "调用日志、接口调用记录、状态码与错误请求", "logs"),
    ("限流日志", "被限流的请求记录、客户端 IP", "ratelogs"),
    ("安全审计", "登录日志、登录成功/失败/锁定记录、在线会话与强制下线", "security-audit"),
    ("系统日志", "系统自动检测、自愈、定时执行与通知推送结果", "systemlogs"),
    ("操作审计", "管理员与前台用户操作记录、执行结果与导出", "audit"),
    ("系统配置", "全局限流、后台白名单、密码策略、通知、日志清理、配置导出导入", "settings"),
    ("脚本管理", "运行采集、同步、维护脚本、重置数据库（双重确认）", "scripts"),
    ("批量导入", "数据导入、Excel / CSV 批量导入、导入记录", "import"),
    ("定时任务", "定时脚本、环境自检与执行记录、数据库备份与恢复、备份文件", "schedule"),
    ("系统诊断", "环境自检、运行模式（dev/prod）、服务监控、CPU / 内存 / 磁盘、连接池与启动日志", "diagnostics"),
    ("导出记录", "异步导出文件下载", "exports"),
    ("智能自愈与告警中心", "告警规则、告警事件与自愈场景配置", "alerts"),
]


def _search_match(query, *values):
    """不区分大小写，空格分隔的多个关键词需全部命中"""
    terms = (query or "").lower().split()
    content = " ".join(str(v or "") for v in values).lower()
    return bool(terms) and all(term in content for term in terms)

def _hash_token(raw: str) -> str:
    """Token 统一 SHA-256 哈希，数据库只保存该值"""
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def _token_hint(raw: str) -> str:
    """Token 掩码，仅用于后台辨认：前 4 位 + **** + 后 4 位"""
    raw = raw or ""
    return f"{raw[:4]}****{raw[-4:]}" if len(raw) > 8 else "****"


def _make_password_hash(password: str) -> str:
    """后台账号密码：随机盐 + SHA-256，存储格式 salt$digest"""
    salt = secrets.token_hex(8)
    digest = hashlib.sha256((salt + (password or "")).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def _check_password_hash(password: str, stored: str) -> bool:
    try:
        salt, digest = (stored or "").split("$", 1)
    except ValueError:
        return False
    calc = hashlib.sha256((salt + (password or "")).encode("utf-8")).hexdigest()
    return hmac.compare_digest(calc, digest)


def _script_pid_state(pid):
    """返回 managed/orphan/stopped/unknown，避免把其他服务实例的任务误当孤儿"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "stopped"
    if pid <= 0:
        return "stopped"
    try:
        import psutil
    except ImportError:
        return "unknown"
    try:
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return "stopped"
        parent = proc.parent()
        if parent is None or not parent.is_running() or parent.status() == psutil.STATUS_ZOMBIE:
            return "orphan"
        return "managed"
    except psutil.NoSuchProcess:
        return "stopped"
    except (psutil.Error, OSError):
        # 无权限或状态暂时不可知时宁可跳过，也不能误杀正常采集进程
        return "unknown"


def _kill_pid(pid):
    """按 PID 强制结束进程树：Windows 用 taskkill /T，其它平台用 os.kill"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if pid <= 0:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                           timeout=10)
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _cron_field(spec, lo, hi):
    """解析 cron 单字段：支持 *、数字、*/n、a-b、逗号列表"""
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("空字段")
    if spec == "*":
        return list(range(lo, hi + 1))
    values = set()
    for part in spec.split(","):
        part = part.strip()
        if part == "*":
            return list(range(lo, hi + 1))
        if part.startswith("*/"):
            step = int(part[2:])
            if step <= 0:
                raise ValueError("步长非法")
            values.update(range(lo, hi + 1, step))
        elif "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            if a > b:
                raise ValueError("区间非法")
            values.update(range(a, b + 1))
        else:
            values.add(int(part))
    result = sorted(v for v in values if lo <= v <= hi)
    if not result:
        raise ValueError("超出取值范围")
    return result


def _next_cron(expr, base=None):
    """计算 Cron 表达式下一次执行时间；格式错误返回 None"""
    parts = (expr or "").split()
    if len(parts) != 5:
        return None
    try:
        mins = _cron_field(parts[0], 0, 59)
        hours = _cron_field(parts[1], 0, 23)
        doms = _cron_field(parts[2], 1, 31)
        months = _cron_field(parts[3], 1, 12)
        dows = _cron_field(parts[4], 0, 6)
    except (ValueError, TypeError):
        return None
    cur = (base or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = cur + timedelta(days=400)
    while cur <= limit:
        if (cur.minute in mins and cur.hour in hours and cur.month in months
                and cur.day in doms and ((cur.weekday() + 1) % 7) in dows):
            return cur
        cur += timedelta(minutes=1)
    return None


# 后台可运行的脚本白名单（只允许执行以下预置脚本）
SCRIPTS = {
    "sync": {
        "name": "数据同步", "file": "数据同步.py",
        "desc": "联网抓取热榜/笑话/名言/壁纸/历史上的今天并写入数据库（自动去重）",
        "options": [("全部", "all"), ("热榜", "hot"), ("笑话", "joke"),
                    ("名言", "wisdom"), ("壁纸", "image"), ("历史今天", "history")],
        "default": "all", "accept": "option",
    },
    "init": {
        "name": "数据初始化", "file": "数据初始化.py",
        "desc": "一键完成数据库维护（补表/字段注释）+ 基础数据灌入，已有数据的表自动跳过",
        "options": [], "default": "", "accept": "fixed",
    },
    "import_json": {
        "name": "JSON 数据导入", "file": "数据导入.py",
        "desc": "把 data/ 下的成语、歇后语 JSON 去重导入数据库",
        "options": [("全部（存在才导）", ""), ("成语 idiom.json", "idiom.json"),
                    ("歇后语 xiehouyu.json", "xiehouyu.json")],
        "default": "", "accept": "json",
    },
    "clean": {
        "name": "数据清理", "file": "数据清理.py",
        "desc": "清理业务表中的空白、空格污染和格式异常数据（不影响账号与配置）",
        "options": [], "default": "", "accept": "fixed",
    },
    "history": {
        "name": "历史上的今天采集", "file": "history_today.py",
        "desc": "抓取历史事件入库；可抓今天、指定日期（MM-DD）或全年 366 天",
        "options": [("今天", ""), ("全年 366 天（耗时较长）", "all")],
        "default": "", "accept": "date",
    },
    "joke": {
        "name": "笑话采集", "file": "joke.py",
        "desc": "爬取笑话站点，写入 local_joke 表；可按 52 笑话分类单独采集（自动去重）",
        "options": [("全部类型", "all"), ("笑话大全", "5"), ("冷笑话", "1"),
                    ("小笑话", "2"), ("爆笑笑话", "6"), ("情侣笑话", "8"),
                    ("校园笑话", "7"), ("幽默笑话", "10"), ("职场幽默", "11"),
                    ("夫妻笑话", "12"), ("搞笑段子", "13")],
        "default": "all", "accept": "option",
    },
    "twister": {
        "name": "绕口令采集", "file": "tongue_twister.py",
        "desc": "爬取绕口令站点，写入 tongue_twister 表（自动去重）",
        "options": [], "default": "", "accept": "fixed",
    },
    "answer": {
        "name": "答案之书采集", "file": "answer_book.py",
        "desc": "采集答案之书语句，写入 answer_book 表",
        "options": [], "default": "", "accept": "fixed",
    },
    # 高危操作固定在列表最后：默认禁用，仅首次部署且设置 ALLOW_DB_RESET=1 后可运行
    "reset": {
        "name": "重置数据库", "file": "重置数据库.py",
        "desc": "⚠ 仅首次部署使用：默认禁止运行，需设置 ALLOW_DB_RESET=1 并重启；清空全部数据，不可恢复",
        "options": [("⚠ 确认清空全部数据", "RESET")],
        "default": "", "accept": "option",
    },
}


def _script_snapshot(key):
    """返回某个脚本的运行状态（供后台轮询）"""
    meta = SCRIPTS[key]
    task = SCRIPT_TASKS.get(key) or {}
    started = task.get("started")
    finished = task.get("finished")
    return {
        "key": key,
        "name": meta["name"],
        "file": meta["file"],
        "desc": meta["desc"],
        "options": [{"label": l, "value": v} for l, v in meta["options"]],
        "default": meta["default"],
        "accept": meta["accept"],
        "state": task.get("state", "idle"),
        "args": task.get("args", ""),
        "started_at": time.strftime("%H:%M:%S", time.localtime(started)) if started else "",
        "finished_at": time.strftime("%H:%M:%S", time.localtime(finished)) if finished else "",
        "duration": int((finished - started)) if started and finished else (
            int(time.time() - started) if started else 0),
        "returncode": task.get("returncode"),
        "output": (task.get("output") or "")[-8000:],
        # 高危脚本默认锁定，前端据此禁用运行按钮
        "runnable": key != "reset" or bool(config.ALLOW_DB_RESET),
        "locked_reason": "" if key != "reset" or config.ALLOW_DB_RESET
        else "重置数据库默认禁用：确需首次部署重置，请设置 ALLOW_DB_RESET=1 并重启",
    }


def _valid_script_args(key, raw):
    """校验并返回安全的脚本参数列表（参数只走列表传递，不拼接 shell）"""
    meta = SCRIPTS[key]
    raw = (raw or "").strip()
    mode = meta["accept"]
    if mode == "fixed":
        if raw:
            raise ValueError("该脚本不接受参数")
        return []
    if mode == "option":
        allowed = {v for _, v in meta["options"] if v}
        if raw and raw not in allowed:
            raise ValueError("参数不在允许范围")
        return [raw] if raw else []
    if mode == "date":
        if raw and raw != "all":
            m = re.fullmatch(r"(\d{2})-(\d{2})", raw)
            if not m or not (1 <= int(m.group(1)) <= 12) or not (1 <= int(m.group(2)) <= 31):
                raise ValueError("日期格式应为 MM-DD，且月份 01-12、日期 01-31")
        return [raw] if raw else []
    if mode == "json":
        if raw and raw not in ("idiom.json", "xiehouyu.json"):
            raise ValueError("参数不合法")
        return [raw] if raw else []
    raise ValueError("未知参数类型")


def _run_script(key, args, on_finish=None):
    """运行脚本并收集输出；结束后执行 on_finish（用于失效缓存），参数列表直传禁用 shell"""
    try:
        _run_script_inner(key, args)
    finally:
        if on_finish:
            try:
                on_finish()
            except Exception:
                pass


def _run_script_inner(key, args):
    meta = SCRIPTS[key]
    script_path = os.path.abspath(os.path.join(SCRIPT_DIR, meta["file"]))
    task = SCRIPT_TASKS[key]
    if not script_path.startswith(os.path.abspath(SCRIPT_DIR) + os.sep) or not os.path.isfile(script_path):
        task.update(state="failed", output=f"[执行拦截] 脚本文件不存在或路径越界：{meta['file']}",
                    returncode=-1, finished=time.time(), proc=None)
        return
    for arg in args:
        if not SAFE_ARG_RE.fullmatch(arg):
            task.update(state="failed", output=f"[执行拦截] 参数格式不合法：{arg}",
                        returncode=-1, finished=time.time(), proc=None)
            return
    cmd = [sys.executable, "-X", "utf8", script_path] + list(args)
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 脚本导入 main 时不启动残留进程清理，避免误杀自身或其他正在运行的脚本
    env["API_SCRIPT_CHILD"] = "1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            cmd, cwd=BASE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
            shell=False,
        )
        SCRIPT_TASKS[key]["proc"] = proc
        SCRIPT_TASKS[key]["pid"] = proc.pid
        if SCRIPT_PID_HOOK is not None:
            try:
                SCRIPT_PID_HOOK(key, SCRIPT_TASKS[key].get("run_id"), proc.pid)
            except Exception:
                pass
        # 全局超时守护：到点强制结束进程，避免爬虫脚本卡死残留
        timeout_sec = max(60, int(getattr(config, "SCRIPT_TIMEOUT_MINUTES", 120)) * 60)
        deadline = time.time() + timeout_sec

        def _watchdog():
            while proc.poll() is None:
                if time.time() > deadline:
                    SCRIPT_TASKS[key]["timeout"] = True
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
                time.sleep(1)

        threading.Thread(target=_watchdog, daemon=True).start()
        for line in proc.stdout:
            task = SCRIPT_TASKS.get(key) or {}
            if task.get("state") != "running":
                break
            task["output"] = ((task.get("output") or "") + line)[-SCRIPT_OUTPUT_LIMIT:]
        if SCRIPT_TASKS[key].get("state") != "running" and proc.poll() is None:
            # 手动停止或状态已变更，立即结束子进程，避免残留
            try:
                proc.kill()
            except Exception:
                pass
        code = proc.wait()
        task = SCRIPT_TASKS[key]
        task["returncode"] = code
        task["finished"] = time.time()
        task["proc"] = None
        if task.get("state") == "running":
            if task.get("timeout"):
                task["state"] = "failed"
                task["output"] = ((task.get("output") or "") +
                                  f"\n[超时终止] 运行超过 {timeout_sec // 60} 分钟，已强制结束进程"
                                  )[-SCRIPT_OUTPUT_LIMIT:]
            else:
                task["state"] = "done" if code == 0 else "failed"
                if not task.get("output"):
                    task["output"] = "脚本没有输出内容" if code == 0 else f"脚本退出码 {code}，无输出"
    except Exception as e:
        task = SCRIPT_TASKS[key]
        task["output"] = ((task.get("output") or "") + f"\n[执行异常] {e}")[-SCRIPT_OUTPUT_LIMIT:]
        task["returncode"] = -1
        task["finished"] = time.time()
        task["proc"] = None
        task["state"] = "failed"


def _normalize_log_path(path):
    """归一化日志路径：把历史 /v1/api 前缀还原为 /api，并去掉多余尾部斜杠

    概览的 TOP 接口与调用次数分布都按归一化后的路径统计，避免同一接口被拆成多条。
    """
    p = (path or "/").strip()
    if p.startswith("/v1/api/"):
        p = p[3:]
    elif p == "/v1/api":
        p = "/api"
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/")
    return p


def _api_log_filter(log_model):
    """只统计真正的接口调用日志，排除扫描器探测 /、/robots.txt 等非接口请求"""
    return or_(log_model.path.like("/api/%"), log_model.path.like("/v1/api%"))


def _svg_captcha(code: str) -> str:
    """生成验证码 SVG（无需图片库依赖）"""
    w, h = 132, 44
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
         f'<rect width="100%" height="100%" fill="#f4f7fb"/>']
    for _ in range(6):
        p.append(f'<line x1="{random.randint(0, w)}" y1="{random.randint(0, h)}" '
                 f'x2="{random.randint(0, w)}" y2="{random.randint(0, h)}" '
                 f'stroke="#ccd7e8" stroke-width="1"/>')
    for _ in range(36):
        p.append(f'<circle cx="{random.randint(0, w)}" cy="{random.randint(0, h)}" r="1" fill="#c3cfe2"/>')
    for i, ch in enumerate(code):
        x, y = 12 + i * 29, 31
        rot = random.randint(-18, 18)
        color = f"rgb({random.randint(30, 90)},{random.randint(60, 120)},{random.randint(130, 200)})"
        p.append(f'<text x="{x}" y="{y}" font-size="25" font-weight="bold" font-family="Arial" '
                 f'fill="{color}" transform="rotate({rot} {x} {y})">{ch}</text>')
    p.append("</svg>")
    return "".join(p)


def _xlsx_response(content: bytes, filename: str) -> Response:
    """返回 Excel 下载响应（兼容中文文件名）"""
    quoted = urllib.parse.quote(filename)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=template.xlsx; filename*=UTF-8''{quoted}"},
    )


def _csv_response(headers, rows, filename) -> Response:
    """返回 CSV 下载响应：内容带 UTF-8 BOM，Excel 双击打开不乱码"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow([v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime)
                         else ("" if v is None else v) for v in row])
    content = ("\ufeff" + buf.getvalue()).encode("utf-8")
    quoted = urllib.parse.quote(filename)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=data.csv; filename*=UTF-8''{quoted}"},
    )


def _time_bounds(start, end):
    """解析日志筛选时间范围，返回 (start_dt, end_dt)；格式非法返回 None。
    只给日期时，start 取当天 00:00:00，end 取当天 23:59:59（包含当天）。
    """
    def _parse(value, is_end):
        value = (value or "").strip()
        if not value:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value, fmt)
            except ValueError:
                continue
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=23, minute=59, second=59) if is_end else dt
            return dt
        raise ValueError(value)

    try:
        return _parse(start, False), _parse(end, True)
    except ValueError:
        return None


def _xlsx_bytes(title, headers, rows, operator=""):
    """把表头与数据行写成 xlsx 字节流（日志导出共用）

    末尾追加导出人 / 导出时间水印，便于数据溯源。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = (title or "数据")[:30]
    for idx, name in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=idx, value=name)
        cell.font = Font(bold=True)
        ws.column_dimensions[cell.column_letter].width = min(46, max(12, len(name) * 2 + 4))
    for r_idx, row in enumerate(rows, start=2):
        for c_idx, value in enumerate(row, start=1):
            if isinstance(value, datetime):
                value = value.strftime("%Y-%m-%d %H:%M:%S")
            ws.cell(row=r_idx, column=c_idx, value=value)
    mark_row = len(rows) + 3
    ws.cell(row=mark_row, column=1,
            value=f"导出人：{operator or 'system'}    导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def setup_admin(app, *, session_factory, table_map, switches, log_model, switch_model,
                token_model=None, ip_rule_model=None, setting_model=None, import_log_model=None,
                import_job_model=None, breaker_model=None,
                breaker_reset=None, script_run_model=None, script_schedule_model=None,
                audit_log_model=None, admin_user_model=None,
                export_job_model=None, alert_rule_model=None, alert_event_model=None,
                rate_log_model=None, session_model=None, login_log_model=None,
                schedule_run_model=None, breaker_event_model=None, pool_metric_model=None,
                quarantine_model=None, c_user_model=None,
                security_refresh=None, cache_invalidate=None,
                log_switches=None, cache_ttls=None,
                master_token="", master_token_hash="", cookie_secure=False,
                admin_user, admin_password, secret, started_at, app_version):
    """挂载管理后台到 FastAPI 实例"""
    # 接口路径 -> 中文名称（取 FastAPI 路由的 summary）
    from fastapi.routing import APIRoute
    ENDPOINT_NAMES = {
        r.path: (r.summary or r.name)
        for r in app.routes
        if isinstance(r, APIRoute) and r.path.startswith("/api/")
        and not r.path.startswith("/api/portal/")
    }

    @app.middleware("http")
    async def admin_auth_guard(request: Request, call_next):
        """管理路径强制隔离；仅登录、验证码和登录页所需静态资源公开"""
        path = request.url.path
        if path == "/admin" or path.startswith("/admin/"):
            public = path in ("/admin/login", "/admin/captcha") or path.startswith("/admin/static/")
            if not public and not request.session.get("admin"):
                return JSONResponse(status_code=403,
                                    content={"code": 403, "msg": "无权访问管理后台"})
        return await call_next(request)

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="admin_session",
        max_age=8 * 3600,
        same_site="lax",        # 跨站请求不携带会话 Cookie
        https_only=cookie_secure,  # 部署 HTTPS 后置 ADMIN_COOKIE_SECURE=1 追加 Secure
    )

    @contextmanager
    def db_scope():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    # ---------------- 在线会话 / 登录日志 ----------------
    SESSION_CACHE = {"ts": 0.0, "map": {}}
    SESSION_TOUCH = {}       # {sid: 上次刷新 last_seen 的时间戳}

    def _session_alive(sid):
        """判断会话是否仍有效（5 秒缓存）：被踢下线后立即失效"""
        if not sid or session_model is None:
            return True
        now = time.time()
        if now - SESSION_CACHE["ts"] > 5:
            try:
                with db_scope() as db:
                    SESSION_CACHE["map"] = {
                        r.id: bool(r.active) for r in db.query(session_model).all()}
            except Exception:
                SESSION_CACHE["map"] = {}
            SESSION_CACHE["ts"] = now
        info = SESSION_CACHE["map"].get(sid)
        return True if info is None else info

    def _session_touch(request):
        """刷新会话最近活跃时间（同一会话 60 秒最多写一次）"""
        sid = request.session.get("sid")
        if not sid or session_model is None:
            return
        now = time.time()
        if now - SESSION_TOUCH.get(sid, 0) < 60:
            return
        SESSION_TOUCH[sid] = now
        try:
            with db_scope() as db:
                row = db.query(session_model).filter(session_model.id == sid).first()
                if row is not None:
                    row.last_seen = datetime.now()
                    db.commit()
        except Exception:
            pass

    def _login_log(username, ip, ua, state, message):
        """登录日志：成功 / 失败 / 锁定"""
        if login_log_model is None:
            return
        try:
            with db_scope() as db:
                db.add(login_log_model(username=str(username or "-")[:50], ip=str(ip or "")[:100],
                                       user_agent=str(ua or "")[:255], state=state,
                                       message=str(message or "")[:255]))
                db.commit()
        except Exception:
            pass

    def require_admin(request: Request):
        """后台 JSON 接口鉴权：未登录返回 401；只读账号禁止写操作返回 403"""
        if not request.session.get("admin"):
            raise HTTPException(status_code=401, detail="未登录或登录已过期")
        if not _session_alive(request.session.get("sid")):
            request.session.clear()
            raise HTTPException(status_code=401, detail="该会话已被管理员强制下线")
        _session_touch(request)
        if request.session.get("role", "admin") != "admin" and \
                request.method not in ("GET", "HEAD", "OPTIONS"):
            raise HTTPException(status_code=403, detail="只读账号无权执行该操作")

    def page_guard(request: Request, path: str):
        if not request.session.get("admin"):
            return RedirectResponse("/admin/login", status_code=303)
        if not _session_alive(request.session.get("sid")):
            request.session.clear()
            return RedirectResponse("/admin/login?error=kicked", status_code=303)
        _session_touch(request)
        return FileResponse(path, media_type="text/html; charset=utf-8")

    # ---------------- 审计与缓存失效 ----------------
    def _human_audit_query(db):
        """操作审计只查询管理员和前台用户操作，系统行为统一归入系统日志。"""
        system_record = or_(
            audit_log_model.action.in_(("self_heal", "breaker_open")),
            and_(audit_log_model.action == "backup_run",
                 audit_log_model.operator.in_(("system", "schedule", "system-self-heal"))),
        )
        return db.query(audit_log_model).filter(~system_record)

    def _audit(request, action, target="", detail="", result="success", before=None, after=None):
        """记录后台敏感操作；写审计失败不影响主流程

        before / after 用于敏感配置变更快照，普通查看类操作无需传入。
        """
        if audit_log_model is None:
            return
        try:
            if before is not None or after is not None:
                try:
                    snapshot = json.dumps({"before": before, "after": after},
                                          ensure_ascii=False, default=str)
                except Exception:
                    snapshot = str({"before": before, "after": after})
                detail = str(detail) + "\n变更快照：" + snapshot
            ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                  or (request.client.host if request.client else ""))
            operator = request.session.get("admin") or "system"
            with db_scope() as db:
                db.add(audit_log_model(
                    operator=str(operator)[:100], action=str(action)[:64],
                    target=str(target)[:255], detail=str(detail)[:60000],
                    ip=ip[:100], result="success" if result == "success" else "failed",
                ))
                db.commit()
        except Exception:
            pass

    def _audit_identity(operator, ip, action, target="", detail="", result="success"):
        """按已知操作人与 IP 写审计，供脚本执行完成后的补记使用"""
        if audit_log_model is None:
            return
        try:
            with db_scope() as db:
                db.add(audit_log_model(
                    operator=str(operator or "system")[:100], action=str(action)[:64],
                    target=str(target)[:255], detail=str(detail)[:60000], ip=str(ip or "")[:100],
                    result="success" if result == "success" else "failed",
                ))
                db.commit()
        except Exception:
            pass

    def _invalidate_cache():
        """数据发生写入后清空本地缓存，保证只读接口立即返回新数据"""
        if not cache_invalidate:
            return
        try:
            cache_invalidate()
        except Exception:
            pass

    # ---------------- 导入任务持久化（重启后仍可查看） ----------------
    def _job_to_dict(row):
        """数据库任务记录转前端使用的结构"""
        created = row.created_at.timestamp() if row.created_at else None
        finished = row.finished_at.timestamp() if row.finished_at else None
        return {
            "job_id": row.id, "state": row.state or "", "stage": row.stage or "",
            "table_key": row.table_key or "", "filename": row.filename or "",
            "operator": row.operator or "", "created": created,
            "updated": finished or created, "finished": finished,
            "total_rows": row.total or 0, "processed": row.processed or 0,
            "total": row.total or 0, "inserted": row.inserted or 0,
            "skipped": row.skipped or 0, "errors": row.failed or 0,
            "msg": row.message or "",
            "result": {
                "code": 200 if row.state == "done" else 400,
                "msg": row.message or "", "inserted": row.inserted or 0,
                "skipped": row.skipped or 0, "errors": row.failed or 0,
                "total": row.total or 0, "detail": row.errors or "",
            },
        }

    def _persist_job(job_id, **kw):
        """把任务进度同步到数据库，写失败不影响导入主流程"""
        if import_job_model is None:
            return
        try:
            with db_scope() as db:
                row = db.query(import_job_model).filter(import_job_model.id == job_id).first()
                if row is None:
                    return
                if "stage" in kw:
                    row.stage = str(kw["stage"] or "")[:50]
                if "state" in kw:
                    row.state = str(kw["state"] or "")[:20]
                    if row.state in ("done", "failed", "interrupted") and "finished" not in kw:
                        row.finished_at = datetime.now()
                if "total_rows" in kw:
                    row.total = int(kw["total_rows"] or 0)
                if "processed" in kw:
                    row.processed = int(kw["processed"] or 0)
                if "inserted" in kw:
                    row.inserted = int(kw["inserted"] or 0)
                if "skipped" in kw:
                    row.skipped = int(kw["skipped"] or 0)
                if "errors" in kw:
                    row.failed = int(kw["errors"] or 0)
                if "msg" in kw:
                    row.message = str(kw["msg"] or "")[:60000]
                if "finished" in kw:
                    row.finished_at = (datetime.fromtimestamp(kw["finished"])
                                       if kw["finished"] else None)
                if "result" in kw:
                    res = kw["result"] or {}
                    if res.get("detail"):
                        row.errors = str(res["detail"])[:60000]
                    if res.get("errors") is not None:
                        row.failed = int(res.get("errors") or 0)
                db.commit()
        except Exception:
            pass

    def _cleanup_jobs():
        """清理超过保留天数的导入任务记录"""
        if import_job_model is None:
            return
        try:
            days = max(1, int(getattr(config, "IMPORT_JOB_KEEP_DAYS", 7)))
            cutoff = datetime.now() - timedelta(days=days)
            with db_scope() as db:
                db.query(import_job_model).filter(or_(
                    import_job_model.finished_at < cutoff,
                    and_(import_job_model.finished_at.is_(None),
                         import_job_model.created_at < cutoff),
                )).delete(synchronize_session=False)
                db.commit()
        except Exception:
            pass

    def _recover_jobs():
        """服务启动时把上次残留的 running 任务标记为 interrupted"""
        if import_job_model is None:
            return
        try:
            with db_scope() as db:
                rows = db.query(import_job_model).filter(
                    import_job_model.state == "running").all()
                for row in rows:
                    row.state = "interrupted"
                    row.stage = "已中断"
                    row.finished_at = datetime.now()
                    row.message = ((row.message or "") + " 服务重启，任务中断").strip()[:60000]
                db.commit()
        except Exception:
            pass

    _recover_jobs()
    _cleanup_jobs()

    # ---------------- 系统配置读写 ----------------
    def _refresh_security():
        if security_refresh:
            try:
                security_refresh(force=True)
            except Exception:
                pass

    def get_setting(key, default=""):
        if setting_model is None:
            return default
        with db_scope() as db:
            row = db.query(setting_model).filter(setting_model.key == key).first()
            return row.value if row is not None and row.value is not None else default

    def save_setting(key, value):
        if setting_model is None:
            return
        with db_scope() as db:
            row = db.query(setting_model).filter(setting_model.key == key).first()
            if row:
                row.value = str(value)
            else:
                db.add(setting_model(key=key, value=str(value)))
            db.commit()
        _refresh_security()

    # ---------------- 后台独立 IP 白名单（只作用于 /admin） ----------------
    ADMIN_IP_CACHE = {"ts": 0.0, "enabled": False, "nets": []}

    def _parse_ip_nets(raw):
        nets = []
        for item in re.split(r"[,;\s]+", raw or ""):
            item = item.strip()
            if not item:
                continue
            try:
                if "/" in item:
                    nets.append(ipaddress.ip_network(item, strict=False))
                else:
                    suffix = "/128" if ":" in item else "/32"
                    nets.append(ipaddress.ip_network(item + suffix, strict=False))
            except ValueError:
                continue
        return nets

    def _valid_ip_entry(raw):
        try:
            if "/" in raw:
                ipaddress.ip_network(raw, strict=False)
            else:
                ipaddress.ip_address(raw)
            return True
        except ValueError:
            return False

    def _admin_ip_policy():
        """读取后台 IP 准入配置，5 秒缓存避免每个请求都查库"""
        now = time.time()
        if now - ADMIN_IP_CACHE["ts"] < 5:
            return ADMIN_IP_CACHE["enabled"], ADMIN_IP_CACHE["nets"]
        try:
            enabled = str(get_setting("admin_ip_guard", "0")).strip().lower() in (
                "1", "true", "yes", "on")
            nets = _parse_ip_nets(get_setting("admin_ip_whitelist", ""))
        except Exception:
            enabled, nets = False, []
        ADMIN_IP_CACHE.update(ts=now, enabled=enabled, nets=nets)
        return enabled, nets

    def _client_ip(scope):
        for key, value in (scope.get("headers") or []):
            if key.lower() == b"x-forwarded-for":
                ip = value.decode("latin-1").split(",")[0].strip()
                if ip:
                    return ip[7:] if ip.lower().startswith("::ffff:") else ip
        client = scope.get("client")
        ip = client[0] if client else ""
        return ip[7:] if ip.lower().startswith("::ffff:") else ip

    def _ip_allowed(raw, nets):
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            return False
        return any(addr in net for net in nets)

    class AdminIpGuard:
        """后台准入中间件：开启且白名单非空时，仅白名单 IP 可访问 /admin"""

        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                path = scope.get("path", "")
                if path == "/admin" or path.startswith("/admin/"):
                    enabled, nets = _admin_ip_policy()
                    if enabled and nets and not _ip_allowed(_client_ip(scope), nets):
                        resp = Response("当前 IP 不在后台访问白名单内",
                                        status_code=403, media_type="text/plain; charset=utf-8")
                        await resp(scope, receive, send)
                        return
            await self.app(scope, receive, send)

    app.add_middleware(AdminIpGuard)

    def parse_dt(raw):
        raw = (raw or "").strip()
        if not raw:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        raise ValueError("时间格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS")

    page_router = APIRouter(prefix="/admin")
    api_router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)], include_in_schema=False)

    # ---------------- 登录相关页面 ----------------
    @page_router.get("", include_in_schema=False)
    def admin_home(request: Request):
        return page_guard(request, DASHBOARD_HTML)

    @page_router.get("/login", include_in_schema=False)
    def admin_login_page(request: Request):
        if request.session.get("admin"):
            return RedirectResponse("/admin", status_code=303)
        return FileResponse(LOGIN_HTML, media_type="text/html; charset=utf-8")

    @page_router.get("/firework", include_in_schema=False)
    def admin_firework_page(request: Request):
        """烟花 / 息屏页（需登录）"""
        return page_guard(request, FIREWORK_HTML)

    @page_router.get("/particles", include_in_schema=False)
    def admin_particles_page(request: Request):
        """3D 粒子互动页（需登录）"""
        return page_guard(request, PARTICLES_HTML)

    @page_router.get("/static/{filename:path}", include_in_schema=False)
    def admin_static(filename: str):
        """后台静态资源（仅允许 static 目录内的文件）"""
        path = os.path.abspath(os.path.join(BASE_DIR, "static", filename))
        static_root = os.path.abspath(os.path.join(BASE_DIR, "static")) + os.sep
        if not path.startswith(static_root) or not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    @page_router.get("/captcha", include_in_schema=False)
    def admin_captcha(request: Request):
        chars = "".join(random.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(4))
        request.session["captcha"] = {"code": chars.lower(), "ts": time.time()}
        return Response(_svg_captcha(chars), media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    @page_router.post("/login", include_in_schema=False)
    def admin_login(request: Request, username: str = Form(""), password: str = Form(""),
                    captcha: str = Form("")):
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else "unknown"))
        fails = LOGIN_FAILS
        now = time.time()
        ua = request.headers.get("user-agent", "")
        # 登录页用 fetch 提交时返回 JSON，避免整页刷新导致已输入的账号密码丢失
        wants_json = request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"

        def fail(msg, field="credentials"):
            history.append(now)
            fails[ip] = history
            _audit(request, "login", username or "-", f"登录失败：{msg}", result="failed")
            _login_log(username, ip, ua, "failed", msg)
            if wants_json:
                return JSONResponse({"code": 400, "msg": msg, "field": field})
            return RedirectResponse("/admin/login?error=" + urllib.parse.quote(msg), status_code=303)

        history = [t for t in fails.get(ip, []) if now - t < LOGIN_LOCK_SECONDS]
        if len(history) >= LOGIN_MAX_FAIL:
            _audit(request, "login", username or "-", "登录失败：尝试次数过多，账号临时锁定", result="failed")
            _login_log(username, ip, ua, "locked", "尝试次数过多，账号临时锁定")
            if wants_json:
                return JSONResponse({"code": 429, "msg": "尝试次数过多，请1分钟后再试",
                                     "field": "credentials"})
            return RedirectResponse("/admin/login?error=" + urllib.parse.quote("尝试次数过多，请1分钟后再试"),
                                    status_code=303)

        # 验证码只校验、不立即作废：账号密码输错时仍可用同一验证码重试，无需全部重填
        cap = request.session.get("captcha") or {}
        if not cap or now - cap.get("ts", 0) > CAPTCHA_TTL:
            return fail("验证码已过期，请刷新后重试", "captcha")
        if captcha.strip().lower() != cap.get("code", ""):
            return fail("验证码错误", "captcha")

        role = None
        if username == admin_user and password == admin_password:
            role = "admin"  # 内置管理员，恒定最高权限
        elif admin_user_model is not None and username:
            with db_scope() as db:
                row = db.query(admin_user_model).filter(
                    admin_user_model.username == username).first()
                if row is not None and row.enabled and _check_password_hash(password, row.password_hash):
                    role = "admin" if (row.role or "") == "admin" else "viewer"
                    row.last_login = datetime.now()
                    db.commit()
        if role is None:
            return fail("账号或密码错误", "credentials")

        request.session.pop("captcha", None)
        fails.pop(ip, None)
        request.session["admin"] = username
        request.session["role"] = role
        request.session["login_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # 记录在线会话：后台可查看并强制下线
        sid = uuid.uuid4().hex
        request.session["sid"] = sid
        if session_model is not None:
            try:
                with db_scope() as db:
                    db.add(session_model(id=sid, username=username[:50], ip=ip[:100],
                                         user_agent=ua[:255], active=True,
                                         created_at=datetime.now(), last_seen=datetime.now()))
                    db.commit()
                SESSION_CACHE["ts"] = 0
            except Exception:
                pass
        _audit(request, "login", username, f"登录成功（角色：{role}）")
        _login_log(username, ip, ua, "success", f"登录成功（角色：{role}）")
        if wants_json:
            return JSONResponse({"code": 200, "msg": "登录成功", "redirect": "/admin"})
        return RedirectResponse("/admin", status_code=303)

    @page_router.get("/logout", include_in_schema=False)
    def admin_logout(request: Request):
        _audit(request, "logout", request.session.get("admin") or "-", "退出登录")
        sid = request.session.get("sid")
        if sid and session_model is not None:
            try:
                with db_scope() as db:
                    row = db.query(session_model).filter(session_model.id == sid).first()
                    if row is not None:
                        row.active = False
                        db.commit()
                SESSION_CACHE["ts"] = 0
            except Exception:
                pass
        request.session.clear()
        return RedirectResponse("/admin/login", status_code=303)

    # ---------------- 概览 ----------------
    count_cache = {"ts": 0.0, "total": 0}
    disk_cache = {"ts": 0.0, "data": {}}
    latency_cache = {"ts": {}, "data": {}}

    def _disk_metrics(db):
        """数据库容量、备份目录占用与磁盘剩余空间（缓存 30 秒）"""
        if disk_cache["data"] and time.time() - disk_cache["ts"] < 30:
            return disk_cache["data"]
        info = {"db_size_bytes": 0, "backup_dir": config.BACKUP_DIR,
                "backup_size_bytes": 0, "backup_files": 0,
                "disk_total_bytes": 0, "disk_used_bytes": 0, "disk_free_bytes": 0}
        try:
            row = db.execute(text(
                "SELECT COALESCE(SUM(data_length + index_length), 0) "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")).first()
            info["db_size_bytes"] = int(row[0] or 0)
        except Exception:
            pass
        try:
            for name in os.listdir(config.BACKUP_DIR):
                path = os.path.join(config.BACKUP_DIR, name)
                if os.path.isfile(path):
                    info["backup_files"] += 1
                    info["backup_size_bytes"] += os.path.getsize(path)
        except OSError:
            pass
        try:
            du = shutil.disk_usage(BASE_DIR)
            info.update(disk_total_bytes=du.total, disk_used_bytes=du.used,
                        disk_free_bytes=du.free)
        except OSError:
            pass
        disk_cache["ts"] = time.time()
        disk_cache["data"] = info
        return info

    @api_router.get("/session")
    def api_session(request: Request):
        return {"code": 200, "user": request.session.get("admin"),
                "role": request.session.get("role", "admin"),
                "login_at": request.session.get("login_at"),
                "is_admin": request.session.get("role", "admin") == "admin"}

    # ---------------- 环境自检 ----------------
    ENV_CHECK = {"state": "idle", "output": "", "started": 0.0, "finished": 0.0,
                 "returncode": None, "operator": "", "ok": None}
    # 自检历史文件兜底：数据库不可用时仍能保存并展示失败记录
    ENVCHECK_FILE = os.path.join(BASE_DIR, "data", "envcheck_runs.json")
    ENVCHECK_FILE_MAX = 200

    def _append_envcheck_file(record):
        """把一次自检结果追加到本地 JSON 文件（最多保留 200 条）"""
        try:
            os.makedirs(os.path.dirname(ENVCHECK_FILE), exist_ok=True)
            items = []
            if os.path.exists(ENVCHECK_FILE):
                try:
                    with open(ENVCHECK_FILE, "r", encoding="utf-8") as fh:
                        items = json.load(fh)
                    if not isinstance(items, list):
                        items = []
                except (ValueError, OSError):
                    items = []
            items.append(record)
            tmp = ENVCHECK_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(items[-ENVCHECK_FILE_MAX:], fh, ensure_ascii=False, indent=1)
            os.replace(tmp, ENVCHECK_FILE)
        except Exception:
            pass

    def _read_envcheck_file(limit):
        """读取文件兜底的自检记录（按时间倒序）"""
        try:
            if not os.path.exists(ENVCHECK_FILE):
                return []
            with open(ENVCHECK_FILE, "r", encoding="utf-8") as fh:
                items = json.load(fh)
            if not isinstance(items, list):
                return []
            items = [x for x in items if isinstance(x, dict)]
            items.sort(key=lambda x: x.get("time") or "", reverse=True)
            return items[:limit]
        except Exception:
            return []

    def _envcheck_job(operator):
        """在后台线程运行 scripts/check_env.py，结果写入内存与执行记录"""
        script = os.path.join(SCRIPT_DIR, "check_env.py")
        ENV_CHECK.update(state="running", output="", started=time.time(), finished=0,
                         returncode=None, operator=operator, ok=None, mail_error="")
        if not os.path.exists(script):
            ENV_CHECK.update(state="failed", returncode=-1,
                             output=f"未找到自检脚本：{script}", finished=time.time(), ok=False)
            _append_envcheck_file({
                "state": "failed", "operator": operator, "returncode": -1,
                "duration": 0, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "output": f"未找到自检脚本：{script}",
            })
            return
        try:
            proc = subprocess.run([sys.executable, script], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=300,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else ""))[-20000:]
            code = proc.returncode
        except Exception as e:
            output, code = f"自检执行失败：{e}", -1
        ok = code == 0
        ENV_CHECK.update(state="done" if ok else "failed", output=output,
                         returncode=code, finished=time.time(), ok=ok)
        # 先写本地文件：数据库连不上时也能保留这次（尤其是失败）记录
        _append_envcheck_file({
            "state": "done" if ok else "failed", "operator": operator,
            "returncode": code,
            "duration": int(ENV_CHECK["finished"] - ENV_CHECK["started"]),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ENV_CHECK["started"])),
            "output": output[-4000:],
        })
        if script_run_model is not None:
            try:
                with db_scope() as db:
                    db.add(script_run_model(
                        script_key="envcheck", args="", state="done" if ok else "failed",
                        returncode=code, output=output[-4000:],
                        started_at=datetime.fromtimestamp(ENV_CHECK["started"]),
                        finished_at=datetime.now(), operator=operator,
                        host=dist_lock.host_id(),
                        duration=int(ENV_CHECK["finished"] - ENV_CHECK["started"])))
                    db.commit()
            except Exception:
                pass
        # 自检发现问题时推送邮件（需在后台配置 SMTP；数据库不可用时读取设置会抛错，默认仍发）
        mail_on = True
        try:
            mail_on = str(get_setting("envcheck_mail", "1")) in ("1", "true", "yes", "on")
        except Exception:
            pass
        if not ok and mail_on:
            from ..core.mailer import send_mail

            def _send():
                ok_mail, err = send_mail("【API 平台】环境自检发现问题",
                                         "环境自检未通过，请及时处理。\n\n" + (output[-6000:] or "无输出"))
                if not ok_mail:
                    ENV_CHECK["mail_error"] = err or "邮件发送失败"
            threading.Thread(target=_send, daemon=True).start()

    @api_router.post("/envcheck/run")
    def api_envcheck_run(request: Request):
        if ENV_CHECK.get("state") == "running":
            return {"code": 429, "msg": "自检正在运行，请稍后查看结果"}
        operator = request.session.get("admin") or "system"
        _audit(request, "envcheck_run", "环境自检", "手动运行环境自检")
        threading.Thread(target=_envcheck_job, args=(operator,), daemon=True).start()
        return {"code": 200, "msg": "自检已开始，稍候查看结果"}

    @api_router.get("/envcheck/status")
    def api_envcheck_status():
        return {"code": 200, "data": {
            "state": ENV_CHECK.get("state", "idle"),
            "output": ENV_CHECK.get("output", ""),
            "returncode": ENV_CHECK.get("returncode"),
            "ok": ENV_CHECK.get("ok"),
            "operator": ENV_CHECK.get("operator", ""),
            "started": time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(ENV_CHECK["started"])) if ENV_CHECK.get("started") else "",
            "finished": time.strftime("%Y-%m-%d %H:%M:%S",
                                      time.localtime(ENV_CHECK["finished"])) if ENV_CHECK.get("finished") else "",
            "duration": int((ENV_CHECK.get("finished") or 0) - (ENV_CHECK.get("started") or 0)),
            "mail_error": ENV_CHECK.get("mail_error", ""),
        }}

    @api_router.get("/envcheck/history")
    def api_envcheck_history(limit: int = Query(20, ge=1, le=100)):
        """自检历史：数据库记录 + 本地文件兜底（MySQL 不可用时也能看到失败记录）"""
        db_items = []
        if script_run_model is not None:
            try:
                with db_scope() as db:
                    rows = (db.query(script_run_model)
                            .filter(script_run_model.script_key == "envcheck")
                            .order_by(script_run_model.id.desc()).limit(limit).all())
                db_items = [{
                    "id": r.id, "state": r.state or "", "operator": r.operator or "",
                    "returncode": r.returncode, "duration": int(r.duration or 0),
                    "time": r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "",
                    "output": (r.output or "")[-4000:],
                } for r in rows]
            except Exception:
                db_items = []
        merged, seen = [], set()
        for item in db_items + _read_envcheck_file(limit):
            key = (item.get("time"), item.get("state"), item.get("returncode"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        merged.sort(key=lambda x: x.get("time") or "", reverse=True)
        return {"code": 200, "data": merged[:limit]}

    # ---------------- 关键配置迁移 ----------------
    CONFIG_MODELS = {
        "app_setting": (setting_model, ("key",)),
        "api_token": (token_model, ("token",)),
        "ip_rule": (ip_rule_model, ("ip", "rule_type")),
        "script_schedule": (script_schedule_model, ("id",)),
    }

    def _config_export_value(value):
        return value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value

    @api_router.get("/config/export")
    def api_config_export(request: Request):
        tables = {}
        with db_scope() as db:
            for name, (model, _) in CONFIG_MODELS.items():
                if model is None:
                    return {"code": 400, "msg": f"配置表 {name} 未启用"}
                columns = list(model.__table__.columns)
                rows = db.query(model).order_by(model.id).all()
                tables[name] = [{column.name: _config_export_value(getattr(row, column.name))
                                 for column in columns} for row in rows]
        payload = {"format": "api-platform-config", "version": 1,
                   "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "tables": tables}
        _audit(request, "config_export", "关键配置",
               "导出 " + "、".join(f"{name}({len(rows)})" for name, rows in tables.items()))
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        filename = "api_config_" + time.strftime("%Y%m%d_%H%M%S") + ".json"
        return Response(content=content, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @api_router.post("/config/import")
    async def api_config_import(request: Request, file: UploadFile = File(...)):
        raw = await file.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            return {"code": 400, "msg": "配置文件不能超过 5 MB"}
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            return {"code": 400, "msg": "不是有效的 UTF-8 JSON 文件"}
        if (not isinstance(payload, dict) or payload.get("format") != "api-platform-config"
                or not isinstance(payload.get("tables"), dict)):
            return {"code": 400, "msg": "配置文件格式不正确"}
        tables = payload["tables"]
        counts = {}
        try:
            with db_scope() as db:
                for name, (model, natural_keys) in CONFIG_MODELS.items():
                    rows = tables.get(name, [])
                    if not isinstance(rows, list):
                        raise ValueError(f"{name} 必须是数组")
                    columns = {column.name: column for column in model.__table__.columns}
                    inserted = updated = 0
                    for index, raw_row in enumerate(rows, 1):
                        if not isinstance(raw_row, dict):
                            raise ValueError(f"{name} 第 {index} 行不是对象")
                        values = {}
                        for key, value in raw_row.items():
                            column = columns.get(key)
                            if column is None:
                                continue
                            if name != "script_schedule" and column.primary_key:
                                continue
                            if name == "script_schedule" and key in ("last_run", "last_state", "last_duration", "next_run"):
                                continue
                            if isinstance(column.type, DateTime) and isinstance(value, str) and value.strip():
                                try:
                                    value = datetime.fromisoformat(value.strip().replace("Z", "+00:00")).replace(tzinfo=None)
                                except ValueError:
                                    raise ValueError(f"{name} 第 {index} 行字段 {key} 时间格式错误")
                            values[key] = value
                        if any(key not in values or values[key] is None for key in natural_keys):
                            raise ValueError(f"{name} 第 {index} 行缺少标识字段")
                        query = db.query(model)
                        for key in natural_keys:
                            query = query.filter(getattr(model, key) == values[key])
                        row = query.first()
                        if row is None:
                            row = model()
                            db.add(row)
                            inserted += 1
                        else:
                            updated += 1
                        for key, value in values.items():
                            setattr(row, key, value)
                        if name == "script_schedule":
                            row.last_run = None
                            row.last_state = None
                            row.last_duration = None
                            row.next_run = _calc_next_run(row, datetime.now())
                    counts[name] = {"inserted": inserted, "updated": updated}
                db.commit()
        except ValueError as e:
            return {"code": 400, "msg": str(e)}
        except Exception as e:
            return {"code": 400, "msg": "导入失败，未写入任何配置：" + str(e)[:200]}
        _refresh_security()
        _invalidate_cache()
        _audit(request, "config_import", "关键配置", "导入配置", after=counts)
        return {"code": 200, "msg": "配置导入完成", "data": counts}

    # ---------------- 邮件通知配置 ----------------
    MAIL_FIELDS = ("mail_enabled", "mail_host", "mail_port", "mail_user", "mail_pwd",
                   "mail_from", "mail_to", "mail_ssl", "mail_tls", "envcheck_mail")

    @api_router.get("/mail-settings")
    def api_mail_settings():
        from ..core.mailer import mail_config
        cfg = mail_config()
        cfg["envcheck_mail"] = str(get_setting("envcheck_mail", "1"))
        cfg["pwd"] = "******" if cfg.get("pwd") else ""
        return {"code": 200, "data": cfg}

    @api_router.post("/mail-settings")
    def api_mail_settings_save(request: Request, payload: dict):
        before = {k: get_setting(k, "") for k in MAIL_FIELDS}
        for key in MAIL_FIELDS:
            if key not in payload:
                continue
            value = payload.get(key)
            if key == "mail_pwd" and str(value).strip() in ("", "******"):
                continue
            if key in ("mail_enabled", "mail_ssl", "mail_tls", "envcheck_mail"):
                value = "1" if str(value).lower() in ("1", "true", "yes", "on") else "0"
            if key == "mail_port":
                try:
                    value = str(max(1, int(value or 465)))
                except (TypeError, ValueError):
                    return {"code": 400, "msg": "端口必须是数字"}
            if key == "mail_to":
                value = ",".join(x.strip() for x in str(value or "").replace(";", ",").split(",")
                                 if x.strip())
            save_setting(key, str(value)[:500])
        _audit(request, "mail_settings", "邮件通知",
               "更新 SMTP 配置", before=before,
               after={k: ("******" if k == "mail_pwd" else get_setting(k, "")) for k in MAIL_FIELDS})
        return {"code": 200, "msg": "邮件配置已保存"}

    @api_router.post("/mail-settings/test")
    def api_mail_test(request: Request):
        from ..core.mailer import send_mail
        ok, err = send_mail("【API 平台】邮件通知测试",
                            "这是一封测试邮件，收到即表示 SMTP 配置可用。"
                            f"\n发送时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        _audit(request, "mail_test", "邮件通知", "发送测试邮件", result="success" if ok else "failed")
        return {"code": 200 if ok else 400,
                "msg": "测试邮件已发送" if ok else f"发送失败：{err}"}

    # ---------------- 接口告警 ----------------
    @api_router.get("/alerts/settings")
    def api_alert_settings():
        return {"code": 200, "data": {
            "alert_enabled": get_setting("alert_enabled", str(config.ALERT_ENABLED and 1 or 0)),
            "alert_wechat_enabled": get_setting(
                "alert_wechat_enabled", "1" if config.ALERT_WECHAT_ENABLED else "0"),
            "alert_push_start": get_setting("alert_push_start", config.ALERT_PUSH_START),
            "alert_push_end": get_setting("alert_push_end", config.ALERT_PUSH_END),
            "alert_silent_start": get_setting("alert_silent_start", "00:00"),
            "alert_silent_end": get_setting("alert_silent_end", "00:00"),
            "alert_push_time_active": __import__("main")._alert_push_time_allowed(),
            "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "alert_minute_limit": get_setting("alert_minute_limit", "100"),
            "alert_error_ratio": get_setting("alert_error_ratio", "5"),
            "alert_cooldown": get_setting("alert_cooldown", str(config.ALERT_COOLDOWN)),
            "alert_repeat_window": get_setting("alert_repeat_window", str(config.ALERT_REPEAT_WINDOW)),
            "alert_repeat_limit": get_setting("alert_repeat_limit", str(config.ALERT_REPEAT_LIMIT)),
            "alert_check_interval": get_setting("alert_check_interval", str(config.HOST_CHECK_INTERVAL)),
            "alert_webhook": get_setting("alert_webhook", config.ALERT_WEBHOOK),
            "alert_webhook_high": get_setting("alert_webhook_high", config.ALERT_WEBHOOK_HIGH),
            "alert_webhook_low": get_setting("alert_webhook_low", config.ALERT_WEBHOOK_LOW),
            "alert_at_mobiles": get_setting("alert_at_mobiles", config.ALERT_AT_MOBILES),
            "alert_host_percent": get_setting("alert_host_percent", str(config.ALERT_HOST_PERCENT)),
            "alert_cpu_percent": get_setting("alert_cpu_percent", get_setting("alert_host_percent", str(config.ALERT_HOST_PERCENT))),
            "alert_memory_percent": get_setting("alert_memory_percent", get_setting("alert_host_percent", str(config.ALERT_HOST_PERCENT))),
            "alert_disk_percent": get_setting("alert_disk_percent", "95"),
            "mysql_fail_count": get_setting("mysql_fail_count", "3"),
            "mysql_max_retries": get_setting("mysql_max_retries", get_setting("self_heal_max_attempts", "3")),
            "mysql_retry_interval": get_setting("mysql_retry_interval", "60"),
            "battery_notify_minutes": get_setting("battery_notify_minutes", "10"),
            "battery_check_enabled": get_setting("battery_check_enabled", "1" if config.BATTERY_CHECK_ENABLED else "0"),
            "battery_low_percent": get_setting("battery_low_percent", str(config.BATTERY_LOW_PERCENT)),
            "network_check_enabled": get_setting("network_check_enabled", "1" if config.NETWORK_CHECK_ENABLED else "0"),
            "network_ping_targets": get_setting("network_ping_targets", config.NETWORK_PING_TARGETS),
            "network_fail_count": get_setting("network_fail_count", str(config.NETWORK_FAIL_COUNT)),
            "network_ping_timeout": get_setting("network_ping_timeout", str(config.NETWORK_PING_TIMEOUT)),
            "alert_push_state": dict(__import__("main").ALERT_PUSH_STATE),
            "alert_mail_state": dict(__import__("main").ALERT_MAIL_STATE),
            "alert_mail": get_setting("alert_mail", "0"),
            "self_heal_enabled": get_setting("self_heal_enabled", "1"),
            "self_heal_max_attempts": get_setting("self_heal_max_attempts", "3"),
            "self_heal_interval": get_setting("self_heal_interval", str(config.SELF_HEAL_INTERVAL)),
            "self_heal_state": dict(__import__("main").SELF_HEAL_STATE),
            "healing_scenarios": __import__("main")._healing_settings_snapshot(),
            "alert_kind_rate": get_setting("alert_kind_rate", "1"),
            "alert_kind_breaker": get_setting("alert_kind_breaker", "1"),
            "alert_kind_script": get_setting("alert_kind_script", "1"),
            "alert_kind_host": get_setting("alert_kind_host", "1"),
            "alert_kind_battery": get_setting("alert_kind_battery", "1"),
            "alert_kind_network": get_setting("alert_kind_network", "1"),
        }}

    @api_router.get("/alerts/overview")
    def api_alert_overview():
        """告警中心七类场景的实时只读指标，单场景失败不影响其它卡片"""
        try:
            main = __import__("main")
        except Exception:
            main = None

        def card(status, status_text, detail, **values):
            return {"status": status, "status_text": status_text, "detail": detail, **values}

        def number_setting(key, default):
            try:
                return float(get_setting(key, str(default)) or default)
            except Exception:
                return float(default)

        data = {
            "interface": card("unknown", "状态未知", "接口日志暂不可用",
                              error_ratio=None, max_qps=None),
            "script": card("unknown", "状态未知", "脚本记录暂不可用",
                           running=None, failures_1h=None),
            "host": card("unknown", "状态未知", "主机指标暂不可用",
                         cpu=None, memory=None, disk=None),
            "mysql": card("unknown", "状态未知", "数据库状态暂不可用",
                          connected=False, pool_percent=None),
            "battery": card("unknown", "状态未知", "电池状态尚未检测",
                            supported=None, percent=None, charging=None),
            "network": card("unknown", "状态未知", "网络状态尚未检测",
                            enabled=False, online=None, latency_ms=None, targets=[]),
            "healing": card("unknown", "状态未知", "自愈状态暂不可用",
                            healthy=False, active_count=0, items=[]),
        }

        try:
            now = datetime.now()
            five_minutes_ago = now - timedelta(minutes=5)
            one_minute_ago = now - timedelta(minutes=1)
            with db_scope() as db:
                total = int(db.query(func.count(log_model.id)).filter(
                    log_model.time >= five_minutes_ago).scalar() or 0)
                errors = int(db.query(func.count(log_model.id)).filter(
                    log_model.time >= five_minutes_ago,
                    log_model.status_code >= 500).scalar() or 0)
                top_ip = (db.query(log_model.ip, func.count(log_model.id))
                          .filter(log_model.time >= one_minute_ago)
                          .group_by(log_model.ip)
                          .order_by(func.count(log_model.id).desc()).first())
            error_ratio = round(errors * 100.0 / total, 1) if total else 0.0
            max_qps = int(top_ip[1]) if top_ip else 0
            error_limit = number_setting("alert_error_ratio", 5)
            minute_limit = int(number_setting("alert_minute_limit", 100))
            ratio_exceeded = error_limit > 0 and total >= 5 and error_ratio >= error_limit
            traffic_exceeded = minute_limit > 0 and max_qps > minute_limit
            if ratio_exceeded or traffic_exceeded:
                status, status_text = "error", "超过告警阈值"
            elif errors:
                status, status_text = "warn", "存在 5xx 响应"
            else:
                status, status_text = "ok", "接口正常"
            data["interface"] = card(
                status, status_text,
                f"近5分钟调用 {total} 次，5xx {errors} 次；近1分钟单 IP 最高 {max_qps} 次",
                error_ratio=error_ratio, max_qps=max_qps)
        except Exception:
            pass

        try:
            one_hour_ago = datetime.now() - timedelta(hours=1)
            with db_scope() as db:
                running = int(db.query(func.count(script_run_model.id)).filter(
                    script_run_model.state == "running").scalar() or 0)
                failures = int(db.query(func.count(script_run_model.id)).filter(
                    script_run_model.state == "failed",
                    func.coalesce(script_run_model.finished_at,
                                  script_run_model.started_at) >= one_hour_ago).scalar() or 0)
                latest = db.query(script_run_model).order_by(script_run_model.id.desc()).first()
            latest_state = (latest.state or "") if latest else ""
            failed = failures > 0 or latest_state == "failed"
            detail = f"当前运行 {running} 个，近1小时失败 {failures} 个"
            if latest:
                detail += f"；最近任务 {latest.script_key or '-'}：{latest_state or '未知'}"
            data["script"] = card(
                "error" if failed else "ok",
                "脚本执行失败" if failed else ("脚本运行中" if running else "脚本正常"),
                detail, running=running, failures_1h=failures)
        except Exception:
            pass

        try:
            if main is None:
                raise RuntimeError("main unavailable")
            # 兼容现有 host_metrics；提供 _host_metrics 时优先复用新辅助函数
            host_metrics = getattr(main, "_host_metrics", None) or main.host_metrics
            raw = host_metrics(False) or {}
            metrics_data = {
                key: (round(float(raw.get(key)), 1) if raw.get(key) is not None else None)
                for key in ("cpu", "memory", "disk")
            }
            available = [value for value in metrics_data.values() if value is not None]
            if not available:
                status, status_text = "unknown", "主机指标不可用"
            else:
                warn_limit = number_setting("monitor_warn", 80)
                alert_limits = {
                    "cpu": number_setting("alert_cpu_percent", number_setting(
                        "alert_host_percent", config.ALERT_HOST_PERCENT)),
                    "memory": number_setting("alert_memory_percent", number_setting(
                        "alert_host_percent", config.ALERT_HOST_PERCENT)),
                    "disk": number_setting("alert_disk_percent", 95),
                }
                if any(metrics_data[key] is not None and metrics_data[key] >= limit
                       for key, limit in alert_limits.items()):
                    status, status_text = "error", "资源使用率过高"
                elif max(available) >= warn_limit:
                    status, status_text = "warn", "资源使用率偏高"
                else:
                    status, status_text = "ok", "主机资源正常"
            def percent_text(value):
                return "--" if value is None else f"{value:.1f}%"
            data["host"] = card(
                status, status_text,
                f"CPU {percent_text(metrics_data['cpu'])}，内存 {percent_text(metrics_data['memory'])}，"
                f"磁盘 {percent_text(metrics_data['disk'])}", **metrics_data)
        except Exception:
            pass

        try:
            if main is None:
                raise RuntimeError("main unavailable")
            connected = False
            db = None
            try:
                db = main.SessionLocal()
                db.execute(text("SELECT 1")).scalar()
                connected = True
            except Exception:
                connected = False
            finally:
                if db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass
            try:
                pool_status = getattr(main, "_db_pool_status", None)
                if callable(pool_status):
                    pool = pool_status() or {}
                else:
                    engine_pool = main.engine.pool
                    checked_out = int(engine_pool.checkedout())
                    size = int(getattr(engine_pool, "size", lambda: 0)() or 0)
                    max_overflow = int(getattr(engine_pool, "_max_overflow", 0) or 0)
                    pool = {"usage_percent": round(
                        checked_out * 100.0 / max(1, size + max_overflow), 1)}
                pool_percent = pool.get(
                    "pool_percent", pool.get("usage_percent", pool.get("percent")))
                pool_percent = round(float(pool_percent), 1) if pool_percent is not None else None
            except Exception:
                pool_percent = None
            if not connected:
                status, status_text = "error", "MySQL 连接失败"
            elif pool_percent is None:
                status, status_text = "warn", "连接池指标不可用"
            elif pool_percent >= number_setting(
                    "self_heal_pool_percent", config.SELF_HEAL_POOL_PERCENT):
                status, status_text = "warn", "连接池占用偏高"
            else:
                status, status_text = "ok", "MySQL 连接正常"
            pool_text = "--" if pool_percent is None else f"{pool_percent:.1f}%"
            data["mysql"] = card(
                status, status_text,
                f"SELECT 1 {'成功' if connected else '失败'}；连接池占用 {pool_text}",
                connected=connected, pool_percent=pool_percent)
        except Exception:
            pass

        try:
            if main is None:
                raise RuntimeError("main unavailable")
            raw = dict(main.HOST_DEVICE_STATE["battery"])
            supported = raw.get("available", raw.get("supported"))
            percent = raw.get("percent")
            percent = round(float(percent), 1) if percent is not None else None
            charging = raw.get("charging")
            if not raw.get("enabled"):
                status, status_text, detail = "off", "电池监测未启用", "电池巡检已关闭"
            elif supported is None:
                status, status_text, detail = "unknown", "电池状态未知", "尚未完成电池检测"
            elif not supported:
                status, status_text, detail = "off", "当前设备无电池", "当前主机不支持电池监测"
            elif percent is None:
                status, status_text, detail = "unknown", "电量未知", "未读取到电池电量"
            elif charging is False and percent <= number_setting(
                    "battery_low_percent", config.BATTERY_LOW_PERCENT):
                status, status_text = "warn", "电池电量偏低"
                detail = f"当前电量 {percent:.1f}%，正在使用电池供电"
            else:
                status, status_text = "ok", "电池状态正常"
                power_text = "充电中" if charging else "使用电池供电"
                detail = f"当前电量 {percent:.1f}%，{power_text}"
            data["battery"] = card(status, status_text, detail, supported=supported,
                                   percent=percent, charging=charging)
        except Exception:
            pass

        try:
            if main is None:
                raise RuntimeError("main unavailable")
            raw = dict(main.HOST_DEVICE_STATE["network"])
            enabled = bool(raw.get("enabled"))
            online = raw.get("online")
            targets = [dict(item) if isinstance(item, dict) else item
                       for item in (raw.get("targets") or [])]
            if not enabled:
                status, status_text, detail = "off", "网络监测未启用", "网络巡检已关闭"
            elif online is False:
                status, status_text = "error", "网络连接异常"
                detail = raw.get("error") or "所有 Ping 检测目标均不可达"
            elif online is True:
                status, status_text = "ok", "网络连接正常"
                detail = f"已记录 {len(targets)} 个检测目标，至少一个目标可达"
            else:
                status, status_text = "unknown", "网络状态未知"
                detail = raw.get("error") or "尚未完成网络检测"
            data["network"] = card(status, status_text, detail, enabled=enabled, online=online,
                                   latency_ms=raw.get("latency_ms"), targets=targets)
        except Exception:
            pass

        try:
            if main is None:
                raise RuntimeError("main unavailable")
            snapshot = main._healing_settings_snapshot()
            self_heal = dict(main.SELF_HEAL_STATE)
            critical = set(self_heal.get("critical") or [])
            failures = {key for key, value in (self_heal.get("failures") or {}).items() if value}
            items = []
            scene_labels = {
                "interface": "接口异常与高频调用", "script": "脚本运行监控",
                "host": "服务器资源", "mysql": "MySQL",
                "battery": "电池", "network": "Ping",
            }
            for key in ("interface", "script", "host", "mysql", "battery", "network"):
                state_key = "memory" if key == "host" else key
                state = (snapshot.get(state_key) or {}).get("state") or {}
                card_data = data.get(key) or {}
                self_active = bool(state.get("active"))
                if key == "host":
                    self_active = self_active or bool({"disk", "pool"} & (critical | failures))
                elif key == "mysql":
                    self_active = bool({"mysql"} & (critical | failures))
                active = self_active or card_data.get("status") == "error"
                if key == "battery" and card_data.get("status") == "warn":
                    active = True
                if self_active:
                    status_text = state.get("status") or "自愈处理中"
                    detail_text = state.get("last_action") or state.get("detail") or card_data.get("detail") or ""
                else:
                    status_text = card_data.get("status_text") or "正常"
                    detail_text = card_data.get("detail") or "暂无自愈动作"
                items.append({
                    "key": key, "label": scene_labels[key], "status": status_text,
                    "active": active, "detail": detail_text,
                })
            active_count = sum(1 for item in items if item["active"])
            has_unknown = any((data.get(key) or {}).get("status") == "unknown"
                              for key in scene_labels)
            healthy = active_count == 0 and not has_unknown
            if active_count:
                detail = "；".join(f"{item['label']}：{item['status']}"
                                   for item in items if item["active"])
                status, status_text = "error", "存在待恢复异常"
            elif has_unknown:
                detail = "部分场景尚未完成首次巡检"
                status, status_text = "unknown", "等待巡检数据"
            else:
                detail = "前 6 个场景均无待处理异常或自愈动作"
                status, status_text = "ok", "全部正常"
            if self_heal.get("last_run"):
                detail += f"；最近巡检 {self_heal['last_run']}"
            data["healing"] = card(status, status_text, detail, healthy=healthy,
                                   active_count=active_count, items=items)
        except Exception:
            pass

        return {"code": 200, "data": data}

    @api_router.post("/alerts/settings")
    def api_alert_settings_save(request: Request, payload: dict):
        scenes = tuple(__import__("main").HEALING_SCENARIOS)
        scene_keys = [f"heal_{scene}_{suffix}" for scene in scenes for suffix in
                      ("enabled", "max_retries", "retry_interval", "wechat", "mail",
                       "notify_minutes", "quiet_start", "quiet_end")]
        audit_keys = [
            "alert_enabled", "alert_wechat_enabled", "alert_push_start", "alert_push_end",
            "alert_silent_start", "alert_silent_end",
            "alert_minute_limit", "alert_error_ratio", "alert_check_interval",
            "alert_cooldown", "alert_repeat_window", "alert_repeat_limit",
            "alert_webhook", "alert_webhook_high", "alert_webhook_low",
            "alert_at_mobiles", "alert_mail", "alert_kind_rate",
            "alert_kind_breaker", "alert_kind_script", "alert_kind_host",
            "alert_kind_battery", "alert_kind_network", "alert_host_percent",
            "alert_cpu_percent", "alert_memory_percent", "alert_disk_percent",
            "mysql_fail_count", "mysql_max_retries", "mysql_retry_interval", "battery_notify_minutes",
            "battery_check_enabled", "battery_low_percent", "network_check_enabled",
            "network_ping_targets", "network_fail_count", "network_ping_timeout",
            "self_heal_enabled", "self_heal_max_attempts", "self_heal_interval",
        ] + scene_keys
        before = {key: get_setting(key, "") for key in audit_keys}
        flag_keys = [
            "alert_enabled", "alert_wechat_enabled", "alert_mail", "alert_kind_rate",
            "alert_kind_breaker", "alert_kind_script", "alert_kind_host",
            "alert_kind_battery", "alert_kind_network", "battery_check_enabled",
            "network_check_enabled", "self_heal_enabled",
        ] + [f"heal_{scene}_{suffix}" for scene in scenes for suffix in
             ("enabled", "wechat", "mail")]
        for key in flag_keys:
            if key in payload:
                save_setting(key, "1" if str(payload.get(key)).lower() in
                             ("1", "true", "yes", "on") else "0")
        number_rules = [
            ("alert_minute_limit", 0, 1000000), ("alert_error_ratio", 0, 100),
            ("alert_cooldown", 30, 86400), ("alert_repeat_window", 60, 86400),
            ("alert_repeat_limit", 1, 20), ("alert_check_interval", 30, 3600),
            ("alert_host_percent", 1, 100), ("alert_cpu_percent", 1, 100),
            ("alert_memory_percent", 1, 100), ("alert_disk_percent", 1, 100),
            ("mysql_fail_count", 1, 20), ("mysql_max_retries", 1, 20),
            ("mysql_retry_interval", 10, 86400), ("battery_notify_minutes", 1, 1440),
            ("battery_low_percent", 1, 100), ("network_fail_count", 1, 10),
            ("network_ping_timeout", 1, 10),
            ("self_heal_max_attempts", 1, 20), ("self_heal_interval", 10, 3600),
        ] + [(f"heal_{scene}_{suffix}", lo, hi) for scene in scenes
             for suffix, lo, hi in (("max_retries", 1, 20),
                                    ("retry_interval", 10, 86400),
                                    ("notify_minutes", 1, 1440))]
        for key, lo, hi in number_rules:
            if key in payload:
                try:
                    value = int(payload.get(key) or 0)
                except (TypeError, ValueError):
                    return {"code": 400, "msg": f"{key} 必须是数字"}
                if value < lo or value > hi:
                    return {"code": 400, "msg": f"{key} 取值范围 {lo}-{hi}"}
                save_setting(key, value)
        time_keys = ["alert_push_start", "alert_push_end", "alert_silent_start", "alert_silent_end"] + [
            f"heal_{scene}_{suffix}" for scene in scenes for suffix in ("quiet_start", "quiet_end")]
        for key in time_keys:
            if key in payload:
                value = str(payload.get(key) or "").strip()
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                    return {"code": 400, "msg": f"{key} 必须使用 HH:MM 格式"}
                save_setting(key, value)
        if "alert_webhook" in payload:
            save_setting("alert_webhook", str(payload.get("alert_webhook") or "").strip()[:500])
        for key in ("alert_webhook_high", "alert_webhook_low", "alert_at_mobiles"):
            if key in payload:
                save_setting(key, str(payload.get(key) or "").strip()[:500])
        if "network_ping_targets" in payload:
            save_setting("network_ping_targets", str(payload.get("network_ping_targets") or "").strip()[:500])
        _audit(request, "alert_settings", "智能自愈与告警中心", "更新推送渠道、时间与巡检配置",
               before=before, after={k: get_setting(k, "") for k in before})
        return {"code": 200, "msg": "智能自愈与告警配置已保存"}

    @api_router.get("/alerts/rules")
    def api_alert_rules():
        if alert_rule_model is None:
            return {"code": 200, "data": []}
        with db_scope() as db:
            rows = db.query(alert_rule_model).order_by(alert_rule_model.id).all()
            return {"code": 200, "data": [{
                "id": r.id, "path": r.path or "", "minute_limit": int(r.minute_limit or 0),
                "error_ratio": int(r.error_ratio or 0), "enabled": bool(r.enabled),
                "remark": r.remark or "",
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
            } for r in rows]}

    @api_router.post("/alerts/rules")
    def api_alert_rule_save(request: Request, payload: dict):
        if alert_rule_model is None:
            return {"code": 400, "msg": "告警功能未启用"}
        path = str(payload.get("path") or "").strip()
        if path and not path.startswith("/api/"):
            return {"code": 400, "msg": "接口路径必须以 /api/ 开头"}
        try:
            minute_limit = max(0, int(payload.get("minute_limit") or 0))
            error_ratio = int(payload.get("error_ratio") or 0)
        except (TypeError, ValueError):
            return {"code": 400, "msg": "阈值必须是数字"}
        if error_ratio < 0 or error_ratio > 100:
            return {"code": 400, "msg": "错误占比阈值应在 0-100 之间"}
        enabled = str(payload.get("enabled", True)).lower() in ("1", "true", "yes", "on")
        remark = str(payload.get("remark") or "")[:255]
        with db_scope() as db:
            row = db.query(alert_rule_model).filter(alert_rule_model.path == path).first()
            if row is None:
                row = alert_rule_model(path=path)
                db.add(row)
            row.minute_limit = minute_limit
            row.error_ratio = error_ratio
            row.enabled = enabled
            row.remark = remark
            db.commit()
        _audit(request, "alert_rule", path or "全局默认",
               f"每分钟阈值 {minute_limit}，5xx 占比 {error_ratio}%",
               after={"path": path, "minute_limit": minute_limit,
                      "error_ratio": error_ratio, "enabled": enabled})
        return {"code": 200, "msg": "告警规则已保存"}

    @api_router.post("/alerts/rules/{rule_id}/delete")
    def api_alert_rule_delete(request: Request, rule_id: int):
        if alert_rule_model is None:
            return {"code": 400, "msg": "告警功能未启用"}
        with db_scope() as db:
            row = db.query(alert_rule_model).filter(alert_rule_model.id == rule_id).first()
            if not row:
                return {"code": 404, "msg": "规则不存在"}
            db.delete(row)
            db.commit()
        _audit(request, "alert_rule_delete", f"ID {rule_id}", "删除告警规则")
        return {"code": 200, "msg": "已删除"}

    @api_router.get("/alerts/events")
    def api_alert_events(limit: int = Query(100, ge=1, le=500), kind: str = None,
                         recovered: str = None, start: str = None, end: str = None):
        """本地事件为主，MySQL 只补充旧记录；数据库故障不影响新事件查询。"""
        bounds = _time_bounds(start, end)
        if bounds is None:
            return JSONResponse({"code": 400, "msg": "时间格式不正确"}, status_code=400)
        t_from, t_to = bounds
        recovery_markers = ("已恢复", "已自动恢复", "已接通电源", "低电量告警解除", "处理后复检通过")
        recovered_value = str(recovered or "").strip().lower()
        if recovered_value in ("1", "true", "yes", "on"):
            recovered_filter = True
        elif recovered_value in ("0", "false", "no", "off"):
            recovered_filter = False
        elif not recovered_value:
            recovered_filter = None
        else:
            return JSONResponse({"code": 400, "msg": "recovered 必须是 0 或 1"}, status_code=400)

        def is_recovered(event):
            event_type = str(event.get("event_type") or "")
            if event_type in ("recovered", "manual_recovered"):
                return True
            if event_type in ("fault", "heal_attempt", "heal_failed"):
                return False
            return any(marker in (event.get("message") or "") for marker in recovery_markers)

        rows = []
        for event in alert_store.read_events(5000):
            item = dict(event)
            timestamp = float(item.get("timestamp") or 0)
            if kind and str(item.get("kind") or "") != kind.strip():
                continue
            if t_from and timestamp < t_from.timestamp():
                continue
            if t_to and timestamp > t_to.timestamp():
                continue
            item["recovered"] = is_recovered(item)
            if recovered_filter is not None and item["recovered"] != recovered_filter:
                continue
            item["source"] = "local"
            rows.append(item)

        if alert_event_model is not None:
            try:
                with db_scope() as db:
                    q = db.query(alert_event_model)
                    if kind:
                        q = q.filter(alert_event_model.kind == kind.strip())
                    if t_from:
                        q = q.filter(alert_event_model.created_at >= t_from)
                    if t_to:
                        q = q.filter(alert_event_model.created_at <= t_to)
                    legacy = q.order_by(alert_event_model.id.desc()).limit(5000).all()
                for row in legacy:
                    item = {
                        "id": row.id, "path": row.path or "", "kind": row.kind or "",
                        "value": int(row.value or 0), "threshold": int(row.threshold or 0),
                        "message": row.message or "", "event_type": "",
                        "notify_status": "未记录", "source": "legacy",
                        "time": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
                        "timestamp": row.created_at.timestamp() if row.created_at else 0,
                    }
                    item["recovered"] = is_recovered(item)
                    if recovered_filter is None or item["recovered"] == recovered_filter:
                        rows.append(item)
            except Exception:
                pass
        rows.sort(key=lambda item: (float(item.get("timestamp") or 0), str(item.get("id") or "")),
                  reverse=True)
        return {"code": 200, "data": rows[:limit], "local_count": sum(
            1 for item in rows if item.get("source") == "local")}

    @api_router.get("/system-logs")
    def api_system_logs(source: str = "", keyword: str = "", start: str = None, end: str = None,
                        page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
        """统一查询告警、自愈、环境自检和其他定时任务的系统运行记录"""
        source = (source or "").strip()
        keyword = (keyword or "").strip()[:100]
        source_names = {"alert": "系统告警与通知", "heal": "系统检测与自愈",
                        "envcheck": "自动环境自检", "schedule": "定时任务"}
        if source and source not in source_names:
            return {"code": 400, "msg": "不支持的日志来源"}
        bounds = _time_bounds(start, end)
        if bounds is None:
            return JSONResponse({"code": 400, "msg": "时间格式不正确"}, status_code=400)
        t_from, t_to = bounds

        with db_scope() as db:
            def filtered(q, time_column, *fields):
                if t_from:
                    q = q.filter(time_column >= t_from)
                if t_to:
                    q = q.filter(time_column <= t_to)
                if keyword:
                    like = f"%{keyword}%"
                    q = q.filter(or_(*[field.like(like) for field in fields]))
                return q

            queries = {}
            if alert_event_model is not None:
                queries["alert"] = (filtered(
                    db.query(alert_event_model), alert_event_model.created_at,
                    alert_event_model.path, alert_event_model.kind, alert_event_model.message),
                    alert_event_model.id)
            if audit_log_model is not None:
                q = db.query(audit_log_model).filter(
                    audit_log_model.action.in_(("self_heal", "breaker_open")))
                queries["heal"] = (filtered(
                    q, audit_log_model.created_at, audit_log_model.target, audit_log_model.detail),
                    audit_log_model.id)
            if script_run_model is not None:
                q = db.query(script_run_model).filter(
                    script_run_model.script_key == "envcheck",
                    script_run_model.operator.in_(("system", "schedule", "system-self-heal")))
                queries["envcheck"] = (filtered(
                    q, script_run_model.started_at, script_run_model.operator,
                    script_run_model.output), script_run_model.id)
            if schedule_run_model is not None:
                q = db.query(schedule_run_model).filter(and_(
                    schedule_run_model.task_type != "envcheck",
                    or_(schedule_run_model.name.is_(None),
                        ~schedule_run_model.name.like("%环境自检%"))))
                queries["schedule"] = (filtered(
                    q, schedule_run_model.started_at, schedule_run_model.name,
                    schedule_run_model.task_type, schedule_run_model.message,
                    schedule_run_model.output), schedule_run_model.id)

            # 新版告警与通知结果保存在本地 JSONL，与历史数据库告警一起归入系统日志。
            local_alerts = []
            if not source or source == "alert":
                for r in alert_store.read_events(5000):
                    try:
                        occurred = datetime.fromtimestamp(float(r.get("timestamp") or 0))
                    except (TypeError, ValueError, OSError):
                        continue
                    if t_from and occurred < t_from:
                        continue
                    if t_to and occurred > t_to:
                        continue
                    notify = str(r.get("notify_status") or "未通知")
                    event_type = str(r.get("event_type") or "故障")
                    searchable = " ".join(str(r.get(key) or "") for key in
                                          ("path", "kind", "message", "event_type")) + " " + notify
                    if keyword and keyword.lower() not in searchable.lower():
                        continue
                    detail = str(r.get("message") or "")
                    detail += f"\n事件类型：{event_type}\n通知状态：{notify}"
                    state = ("success" if "恢复" in event_type or event_type == "recovered" else
                             "failed" if "失败" in event_type or "failed" in event_type else "warning")
                    local_alerts.append({
                        "_sort": occurred, "_id": 0,
                        "time": str(r.get("time") or occurred.strftime("%Y-%m-%d %H:%M:%S")),
                        "source_name": source_names["alert"], "object": r.get("path") or "-",
                        "state": state, "detail": detail,
                    })
            counts = {key: (queries[key][0].count() if key in queries else 0)
                      for key in source_names}
            counts["alert"] += len(local_alerts)
            total = counts.get(source, 0) if source else sum(counts.values())
            pages = max(1, (total + size - 1) // size)
            page = min(page, pages)
            fetch_limit = (page - 1) * size + size
            items = list(local_alerts)
            for key in (source_names if not source else (source,)):
                if key not in queries:
                    continue
                q, id_column = queries[key]
                rows = q.order_by(id_column.desc()).limit(fetch_limit).all()
                if key == "alert":
                    for r in rows:
                        message = r.message or ""
                        detail = message
                        if r.threshold:
                            detail += f"\n实测值：{int(r.value or 0)} / 阈值：{int(r.threshold or 0)}"
                        items.append({
                            "_sort": r.created_at or datetime.min, "_id": r.id,
                            "time": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                            "source_name": source_names[key], "object": r.path or "-",
                            "state": "critical" if "重大异常" in message else "warning",
                            "detail": detail,
                        })
                elif key == "heal":
                    for r in rows:
                        detail = r.detail or ""
                        unresolved = ("复检仍未恢复", "自动处理失败", "检查失败",
                                      "自动处理已关闭", "冷却中")
                        state = "failed" if r.result != "success" or any(
                            marker in detail for marker in unresolved) else "success"
                        items.append({
                            "_sort": r.created_at or datetime.min, "_id": r.id,
                            "time": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                            "source_name": source_names[key], "object": r.target or "定时巡检",
                            "state": state, "detail": detail,
                        })
                elif key == "envcheck":
                    for r in rows:
                        output = (r.output or "")[-2000:]
                        detail = f"耗时：{int(r.duration or 0)} 秒 · 退出码：{r.returncode}\n{output}"
                        items.append({
                            "_sort": r.started_at or datetime.min, "_id": r.id,
                            "time": r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "",
                            "source_name": source_names[key], "object": r.operator or "system",
                            "state": r.state or "", "detail": detail,
                        })
                else:
                    for r in rows:
                        message = r.message or ""
                        output = (r.output or "")[-2000:]
                        detail = f"类型：{r.task_type or '-'} · 耗时：{int(r.duration or 0)} 秒"
                        if message:
                            detail += "\n" + message
                        if output and output != message:
                            detail += "\n" + output
                        items.append({
                            "_sort": r.started_at or datetime.min, "_id": r.id,
                            "time": r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "",
                            "source_name": source_names[key], "object": r.name or "-",
                            "state": r.state or "", "detail": detail,
                        })

        items.sort(key=lambda x: (x["_sort"], x["_id"], x["source_name"]), reverse=True)
        offset = (page - 1) * size
        data = [{k: v for k, v in item.items() if not k.startswith("_")}
                for item in items[offset:offset + size]]
        return {"code": 200, "total": total, "page": page, "size": size,
                "counts": counts, "data": data}

    @api_router.post("/alerts/test")
    def api_alert_test(request: Request, payload: dict = None):
        """按高优或低优渠道发送测试消息，不受总开关和推送时段限制"""
        level = str((payload or {}).get("level") or "high").lower()
        if level not in ("high", "low"):
            return {"code": 400, "msg": "不支持的测试渠道"}
        common_webhook = get_setting("alert_webhook", config.ALERT_WEBHOOK)
        if level == "high":
            webhook = common_webhook or get_setting("alert_webhook_high", config.ALERT_WEBHOOK_HIGH)
            mobiles = get_setting("alert_at_mobiles", config.ALERT_AT_MOBILES)
            label = "高优"
        else:
            webhook = common_webhook or get_setting("alert_webhook_low", config.ALERT_WEBHOOK_LOW)
            mobiles = ""
            label = "低优"
        message = (f"【API 平台{label}告警测试】\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   f"收到这条消息说明{label}告警 Webhook 配置可用。")
        ok, err = __import__("main")._send_alert(message, webhook, mobiles)
        _audit(request, "alert_test", "智能自愈与告警中心", f"发送{label} Webhook 测试消息",
               result="success" if ok else "failed")
        return {"code": 200 if ok else 400,
                "msg": f"{label}测试消息已发送" if ok else f"发送失败：{err}"}

    @api_router.post("/alerts/test-mail")
    def api_alert_test_mail(request: Request):
        message = (f"【API 平台告警邮件测试】\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   "收到这封邮件说明接口异常、熔断等告警邮件配置可用。")
        ok, err = __import__("main")._send_alert_mail("【API 平台】告警邮件测试", message)
        _audit(request, "alert_mail_test", "智能自愈与告警中心", "发送告警测试邮件",
               result="success" if ok else "failed")
        return {"code": 200 if ok else 400,
                "msg": "测试邮件已发送" if ok else f"发送失败：{err}"}

    @api_router.post("/alerts/events/clear")
    def api_alert_events_clear(request: Request):
        """清空本地及历史告警事件；MySQL 断开时仍允许清空本地事件。"""
        local_removed = alert_store.clear_events()
        legacy_removed = 0
        if alert_event_model is not None:
            try:
                with db_scope() as db:
                    legacy_removed = db.query(alert_event_model).delete(synchronize_session=False)
                    db.commit()
            except Exception:
                pass
        removed = local_removed + legacy_removed
        _audit(request, "alert_clear", "智能自愈与告警中心", f"清空告警事件 {removed} 条")
        return {"code": 200, "msg": f"已清空 {removed} 条告警事件（本地 {local_removed} 条）"}

    # ---------------- 限流日志 ----------------
    @api_router.get("/rate-logs")
    def api_rate_logs(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                      ip: str = None, path: str = None, token: str = None,
                      start: str = None, end: str = None):
        if rate_log_model is None:
            return {"code": 200, "total": 0, "page": page, "size": size,
                    "data": [], "top_ips": [], "top_tokens": []}
        bounds = _time_bounds(start, end)
        if bounds is None:
            return JSONResponse({"code": 400, "msg": "时间格式不正确"}, status_code=400)
        t_from, t_to = bounds
        with db_scope() as db:
            q = db.query(rate_log_model)
            if ip:
                q = q.filter(rate_log_model.ip == ip.strip())
            if path:
                q = q.filter(rate_log_model.path.like(f"%{path.strip()}%"))
            if token:
                q = q.filter(rate_log_model.token_hint.like(f"%{token.strip()}%"))
            if t_from:
                q = q.filter(rate_log_model.created_at >= t_from)
            if t_to:
                q = q.filter(rate_log_model.created_at <= t_to)
            total = q.count()
            page = min(page, max(1, (total + size - 1) // size))
            rows = q.order_by(rate_log_model.id.desc()).offset((page - 1) * size).limit(size).all()
            top_ips = (q.with_entities(rate_log_model.ip, func.count(rate_log_model.id))
                       .group_by(rate_log_model.ip)
                       .order_by(func.count(rate_log_model.id).desc()).limit(10).all())
            top_tokens = (q.filter(rate_log_model.token_hint != "")
                          .with_entities(rate_log_model.token_hint, func.count(rate_log_model.id))
                          .group_by(rate_log_model.token_hint)
                          .order_by(func.count(rate_log_model.id).desc()).limit(10).all())
            return {"code": 200, "total": total, "page": page, "size": size, "data": [{
                "id": r.id, "ip": r.ip or "", "path": r.path or "",
                "token_hint": r.token_hint or "", "limit_value": int(r.limit_value or 0),
                "time": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
            } for r in rows],
                "top_ips": [{"ip": a or "-", "count": int(b)} for a, b in top_ips],
                "top_tokens": [{"token": a, "count": int(b)} for a, b in top_tokens]}

    # ---------------- 在线会话 / 登录日志 ----------------
    @api_router.get("/sessions")
    def api_sessions():
        if session_model is None:
            return {"code": 200, "data": []}
        current = None
        with db_scope() as db:
            rows = (db.query(session_model)
                    .filter(session_model.active.is_(True))
                    .order_by(session_model.last_seen.desc()).limit(200).all())
            data = [{
                "id": r.id, "username": r.username or "", "ip": r.ip or "",
                "user_agent": r.user_agent or "", "active": bool(r.active),
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                "last_seen": r.last_seen.strftime("%Y-%m-%d %H:%M:%S") if r.last_seen else "",
            } for r in rows]
        return {"code": 200, "data": data}

    @api_router.post("/sessions/{sid}/kick")
    def api_session_kick(request: Request, sid: str):
        if session_model is None:
            return {"code": 400, "msg": "会话功能未启用"}
        if request.session.get("sid") == sid:
            return {"code": 400, "msg": "不能强制下线当前登录的会话"}
        with db_scope() as db:
            row = db.query(session_model).filter(session_model.id == sid).first()
            if not row:
                return {"code": 404, "msg": "会话不存在"}
            row.active = False
            db.commit()
            username = row.username or ""
        SESSION_CACHE["ts"] = 0
        _audit(request, "session_kick", username, f"强制下线会话 {sid[:8]}…")
        return {"code": 200, "msg": "已强制下线"}

    @api_router.get("/login-logs")
    def api_login_logs(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                       state: str = None, username: str = None, ip: str = None,
                       source: str = None, start: str = None, end: str = None):
        if login_log_model is None:
            return {"code": 200, "total": 0, "page": page, "size": size, "data": []}
        bounds = _time_bounds(start, end)
        if bounds is None:
            return JSONResponse({"code": 400, "msg": "时间格式不正确"}, status_code=400)
        t_from, t_to = bounds
        with db_scope() as db:
            q = db.query(login_log_model)
            if state:
                q = q.filter(login_log_model.state == state)
            if source in ("admin", "portal"):
                q = q.filter(login_log_model.source == source)
            if username:
                q = q.filter(login_log_model.username.like(f"%{username.strip()}%"))
            if ip:
                q = q.filter(login_log_model.ip == ip.strip())
            if t_from:
                q = q.filter(login_log_model.created_at >= t_from)
            if t_to:
                q = q.filter(login_log_model.created_at <= t_to)
            total = q.count()
            page = min(page, max(1, (total + size - 1) // size))
            rows = q.order_by(login_log_model.id.desc()).offset((page - 1) * size).limit(size).all()
            return {"code": 200, "total": total, "page": page, "size": size, "data": [{
                "id": r.id, "username": r.username or "", "source": r.source or "admin", "ip": r.ip or "",
                "user_agent": r.user_agent or "", "state": r.state or "",
                "message": r.message or "",
                "time": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
            } for r in rows]}

    # ---------------- 系统监控 / 启动日志 / 连接池曲线 ----------------
    @api_router.get("/monitor/host")
    def api_monitor_host():
        from main import host_metrics
        # notify=False：页面轮询只读数据，告警由后台巡检线程负责，避免打开页面才检测
        data = host_metrics(notify=False)
        data["interval"] = int(get_setting("monitor_refresh", "10") or 10)
        data["warn"] = int(get_setting("monitor_warn", "80") or 80)
        data["alert_limit"] = int(get_setting("alert_host_percent", str(config.ALERT_HOST_PERCENT)) or 90)
        return {"code": 200, "data": data}

    @api_router.post("/monitor/ping")
    def api_monitor_ping():
        from main import _run_network_check
        data = _run_network_check()
        return {"code": 200, "msg": "Ping 检测已完成", "data": data}

    @api_router.post("/monitor/settings")
    def api_monitor_settings(request: Request, payload: dict):
        for key, lo, hi in (("monitor_refresh", 5, 300), ("monitor_warn", 50, 100)):
            if key in payload:
                try:
                    value = int(payload.get(key))
                except (TypeError, ValueError):
                    return {"code": 400, "msg": "刷新频率与阈值必须是数字"}
                if value < lo or value > hi:
                    return {"code": 400, "msg": f"取值范围 {lo}-{hi}"}
                save_setting(key, value)
        _audit(request, "monitor_settings", "系统监控", "更新监控刷新频率/阈值")
        return {"code": 200, "msg": "已保存"}

    @api_router.get("/monitor/pool")
    def api_monitor_pool(limit: int = Query(120, ge=10, le=1000)):
        if pool_metric_model is None:
            return {"code": 200, "data": []}
        with db_scope() as db:
            rows = (db.query(pool_metric_model)
                    .order_by(pool_metric_model.id.desc()).limit(limit).all())
            rows.reverse()
            return {"code": 200, "data": [{
                "time": r.created_at.strftime("%H:%M:%S") if r.created_at else "",
                "checked_out": int(r.checked_out or 0), "overflow": int(r.overflow or 0),
                "usage_percent": float(r.usage_percent or 0),
            } for r in rows]}

    @api_router.get("/metrics/summary")
    def api_metrics_summary():
        """Prometheus 指标的进程内摘要：延迟分布、请求量、缓存命中率、连接池"""
        from main import engine
        from ..core.cache import CACHE
        pool = {"size": 0, "checked_out": 0, "overflow": 0, "usage_percent": 0.0}
        try:
            p = engine.pool
            size = int(getattr(p, "size", lambda: 0)())
            max_overflow = int(getattr(p, "_max_overflow", 0) or 0)
            checked = int(p.checkedout())
            capacity = max(1, size + max_overflow)
            pool = {"size": size, "checked_out": checked,
                    "overflow": max(0, checked - size),
                    "usage_percent": round(checked * 100.0 / capacity, 1)}
        except Exception:
            pass
        cache = {"backend": "memory", "hit_ratio": 0.0, "hits": 0, "misses": 0}
        try:
            stats = CACHE.stats()
            hits = int(stats.get("hits") or 0)
            misses = int(stats.get("misses") or 0)
            cache = {"backend": stats.get("backend") or "memory",
                     "hits": hits, "misses": misses,
                     "hit_ratio": round(hits * 100.0 / (hits + misses), 1)
                     if (hits + misses) else 0.0}
        except Exception:
            pass
        return {"code": 200, "data": {"latency": metrics.summary(),
                                      "cache": cache, "pool": pool}}

    @api_router.get("/monitor/startup-log")
    def api_monitor_startup():
        from main import STARTUP_LOG, engine, runtime_mode_info
        pool = {}
        try:
            p = engine.pool
            pool = {"size": int(getattr(p, "size", lambda: 0)()),
                    "checked_out": int(p.checkedout()),
                    "overflow": int(p.overflow())}
        except Exception:
            pass
        return {"code": 200, "data": {"log": list(STARTUP_LOG), "pool": pool,
                                      "runtime": runtime_mode_info(),
                                      "version": app_version, "build": config.APP_BUILD_TIME}}

    @api_router.get("/breakers/events")
    def api_breaker_events(limit: int = Query(100, ge=1, le=500)):
        if breaker_event_model is None:
            return {"code": 200, "data": []}
        with db_scope() as db:
            rows = (db.query(breaker_event_model)
                    .order_by(breaker_event_model.id.desc()).limit(limit).all())
            return {"code": 200, "data": [{
                "id": r.id, "key": r.key or "", "kind": r.kind or "",
                "failures": int(r.failures or 0),
                "time": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
            } for r in rows]}

    # ---------------- 全局快捷搜索 / 清空缓存 / OpenAPI ----------------
    @api_router.get("/search")
    def api_global_search(q: str = Query("", max_length=64)):
        q = (q or "").strip()
        if not q:
            return {"code": 200, "data": []}
        like = f"%{q}%"
        results = []
        for path, name in ENDPOINT_NAMES.items():
            if _search_match(q, path, name):
                results.append({"type": "接口", "title": path, "desc": name or "",
                                "tab": "endpoints"})
                if len(results) >= 8:
                    break
        for title, desc, tab in PAGE_SEARCH:
            if _search_match(q, title, desc, tab):
                results.append({"type": "页面", "title": title, "desc": desc, "tab": tab})
        script_items = list(SCRIPTS.items()) + [("envcheck", {
            "name": "环境自检", "desc": "检查 Python、数据库、目录与网络运行环境",
        })]
        for key, meta in script_items:
            if _search_match(q, key, meta["name"], meta["desc"]):
                results.append({"type": "脚本", "title": meta["name"],
                                "desc": meta["desc"], "tab": "scripts" if key in SCRIPTS else "diagnostics"})
        try:
            with db_scope() as db:
                if token_model is not None:
                    for r in db.query(token_model).filter(
                            or_(token_model.name.like(like), token_model.remark.like(like))
                    ).limit(5).all():
                        results.append({"type": "Token", "title": r.name or "",
                                        "desc": r.remark or "", "tab": "tokens"})
                if script_schedule_model is not None:
                    rows = (db.query(script_schedule_model)
                            .order_by(script_schedule_model.id.desc()).limit(50).all())
                    for r in rows:
                        name = _schedule_name(r.task_type or "script", r.script_key or "")
                        if _search_match(q, r.script_key, name, r.task_type,
                                         getattr(r, "args", ""), getattr(r, "cron_expr", "")):
                            results.append({"type": "定时任务", "title": name,
                                            "desc": (r.task_type or "script"), "tab": "schedule"})
                if log_model is not None:
                    for r in (db.query(log_model)
                              .filter(or_(log_model.path.like(like), log_model.ip.like(like)))
                              .order_by(log_model.id.desc()).limit(3).all()):
                        results.append({"type": "请求日志", "title": r.path or "",
                                        "desc": f"{r.ip or '-'} · {r.status_code} · {r.duration_ms}ms",
                                        "tab": "logs"})
                if rate_log_model is not None:
                    for r in (db.query(rate_log_model)
                              .filter(or_(rate_log_model.path.like(like),
                                          rate_log_model.ip.like(like),
                                          rate_log_model.token_hint.like(like)))
                              .order_by(rate_log_model.id.desc()).limit(3).all()):
                        results.append({"type": "限流日志", "title": r.path or "",
                                        "desc": f"{r.ip or '-'} · {r.token_hint or '-'}",
                                        "tab": "ratelogs"})
                if login_log_model is not None:
                    for r in (db.query(login_log_model)
                              .filter(or_(login_log_model.username.like(like),
                                          login_log_model.ip.like(like),
                                          login_log_model.state.like(like),
                                          login_log_model.message.like(like)))
                              .order_by(login_log_model.id.desc()).limit(3).all()):
                        results.append({"type": "登录日志", "title": r.username or "",
                                        "desc": f"{r.ip or '-'} · {r.state or '-'} · {r.message or '-'}",
                                        "tab": "security-audit"})
        except Exception:
            pass
        for key, model in table_map.items():
            comment = (getattr(model, "__doc__", "") or "").strip().splitlines()
            comment = comment[0].strip() if comment else ""
            table_name = getattr(model, "__tablename__", "")
            if _search_match(q, key, table_name, comment):
                results.append({"type": "数据表", "title": key,
                                "desc": comment or table_name, "tab": "tables"})
        return {"code": 200, "data": results[:20]}

    @api_router.post("/cache/clear")
    def api_cache_clear(request: Request):
        _invalidate_cache()
        _audit(request, "cache_clear", "本地缓存", "一键清空进程内缓存")
        return {"code": 200, "msg": "缓存已清空"}

    @api_router.get("/openapi.json")
    def api_openapi_download():
        import json as _json
        payload = app.openapi()
        content = _json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return Response(content=content, media_type="application/json",
                        headers={"Content-Disposition":
                                 f"attachment; filename=openapi_{app_version}.json"})

    @api_router.get("/overview")
    def api_overview():
        now = time.time()
        api_only = _api_log_filter(log_model)
        with db_scope() as db:
            try:
                db.execute(text("SELECT 1"))
                db_status = "ok"
            except Exception:
                db_status = "error"

            total_logs = db.query(log_model).filter(api_only).count()
            today_start = time.strftime("%Y-%m-%d 00:00:00")
            today_logs = db.query(log_model).filter(
                api_only, log_model.time >= today_start).count()
            errors = db.query(log_model).filter(
                api_only, log_model.status_code >= 400).count()
            avg_duration = db.query(func.avg(log_model.duration_ms)).filter(api_only).scalar()

            ok_cnt = db.query(log_model).filter(
                api_only, log_model.status_code < 400).count()
            err4 = db.query(log_model).filter(
                api_only, log_model.status_code >= 400,
                log_model.status_code < 500).count()
            err5 = db.query(log_model).filter(
                api_only, log_model.status_code >= 500).count()

            # 统计来源是调用日志，可能包含历史路径（/v1/api 前缀）、健康检查、404 等
            # 未在接口列表登记的路径，这里先按原路径分组，再在 Python 里归一化合并
            raw_paths = (db.query(log_model.path, func.count(log_model.id).label("cnt"))
                         .filter(api_only)
                         .group_by(log_model.path).all())
            merged = {}
            for p, c in raw_paths:
                key = _normalize_log_path(p)
                # 只统计真正的接口调用，过滤浏览器/开发工具产生的噪音路径
                # （/@vite/client、/.well-known/...、静态资源等），它们仍保留在请求日志里
                if not key.startswith("/api/"):
                    continue
                merged[key] = merged.get(key, 0) + c
            ordered = sorted(merged.items(), key=lambda x: -x[1])
            top = ordered[:8]
            path_dist = ordered[:30]

            # 数据总量变化慢，缓存 30 秒，避免每次自动刷新都全表 count
            if now - count_cache["ts"] > 30:
                count_cache["total"] = sum(db.query(m).count() for m in table_map.values())
                count_cache["ts"] = now
            total_rows = count_cache["total"]

            return {
                "code": 200,
                "data": {
                    "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "server_ts": int(now * 1000),
                    "uptime_seconds": int(now - started_at),
                    "version": app_version,
                    "database": db_status,
                    "python": sys.version.split()[0],
                    "endpoints": len(ENDPOINT_NAMES),
                    "disabled_endpoints": len([v for v in switches.values() if v is False]),
                    "tables": len(table_map),
                    "total_rows": total_rows,
                    "disk": _disk_metrics(db),
                    "logs": {"total": total_logs, "today": today_logs, "errors": errors,
                             "avg_duration_ms": round(float(avg_duration), 2) if avg_duration is not None else 0},
                    "status_dist": {"ok": ok_cnt, "err4": err4, "err5": err5},
                    "top_paths": [{"path": p, "name": ENDPOINT_NAMES.get(p, ""),
                                   "registered": p in ENDPOINT_NAMES, "count": c}
                                  for p, c in top],
                    "path_dist": [{"path": p, "name": ENDPOINT_NAMES.get(p, ""),
                                   "registered": p in ENDPOINT_NAMES, "count": c}
                                  for p, c in path_dist],
                },
            }

    # ---------------- 调用趋势（按小时 / 按天） ----------------
    @api_router.get("/call-trend")
    def api_call_trend(mode: str = Query("hour", pattern="^(hour|day)$")):
        """按小时（最近 24 小时）或按天（最近 30 天）聚合调用量"""
        points = 24 if mode == "hour" else 30
        now = datetime.now()
        if mode == "hour":
            anchor = now.replace(minute=0, second=0, microsecond=0)
            buckets = [(anchor - timedelta(hours=i)).strftime("%Y-%m-%d %H:00")
                       for i in range(points - 1, -1, -1)]
            labels = [b[5:] for b in buckets]
            fmt = "%Y-%m-%d %H:00"
            start = (anchor - timedelta(hours=points - 1)).strftime("%Y-%m-%d %H:00:00")
        else:
            anchor = now.replace(hour=0, minute=0, second=0, microsecond=0)
            buckets = [(anchor - timedelta(days=i)).strftime("%Y-%m-%d")
                       for i in range(points - 1, -1, -1)]
            labels = [b[5:] for b in buckets]
            fmt = "%Y-%m-%d"
            start = (anchor - timedelta(days=points - 1)).strftime("%Y-%m-%d 00:00:00")
        with db_scope() as db:
            api_only = _api_log_filter(log_model)
            bucket = func.date_format(log_model.time, fmt)
            total_map = dict(db.query(bucket, func.count(log_model.id))
                             .filter(api_only, log_model.time >= start)
                             .group_by(bucket).all())
            err_map = dict(db.query(bucket, func.count(log_model.id))
                           .filter(api_only, log_model.time >= start,
                                   log_model.status_code >= 400)
                           .group_by(bucket).all())
        return {"code": 200, "data": {
            "mode": mode,
            "labels": labels,
            "total": [int(total_map.get(b, 0)) for b in buckets],
            "errors": [int(err_map.get(b, 0)) for b in buckets],
        }}

    # ---------------- 接口用量看板（按接口聚合） ----------------
    @api_router.get("/usage-stats")
    def api_usage_stats(days: int = Query(7, ge=1, le=30)):
        """最近 N 天按接口聚合调用量、错误率与平均耗时"""
        start = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                 - timedelta(days=days - 1)).strftime("%Y-%m-%d %H:%M:%S")
        with db_scope() as db:
            api_only = _api_log_filter(log_model)
            total = int(db.query(log_model).filter(
                api_only, log_model.time >= start).count() or 0)
            err_total = int(db.query(log_model).filter(
                api_only, log_model.time >= start, log_model.status_code >= 400).count() or 0)
            rows = (db.query(log_model.path,
                             func.count(log_model.id).label("cnt"),
                             func.sum(log_model.duration_ms).label("dur"))
                    .filter(api_only, log_model.time >= start)
                    .group_by(log_model.path).all())
            err_rows = (db.query(log_model.path, func.count(log_model.id))
                        .filter(api_only, log_model.time >= start,
                                log_model.status_code >= 400)
                        .group_by(log_model.path).all())
        counts, durations, errors = {}, {}, {}
        for path, cnt, dur in rows:
            key = _normalize_log_path(path)
            if not key.startswith("/api/"):
                continue
            counts[key] = counts.get(key, 0) + int(cnt or 0)
            durations[key] = durations.get(key, 0) + int(dur or 0)
        for path, cnt in err_rows:
            key = _normalize_log_path(path)
            if not key.startswith("/api/"):
                continue
            errors[key] = errors.get(key, 0) + int(cnt or 0)
        items = []
        for path, cnt in sorted(counts.items(), key=lambda x: -x[1]):
            if path not in ENDPOINT_NAMES:
                continue
            errs = errors.get(path, 0)
            if len(items) >= 15:
                break
            items.append({
                "path": path, "name": ENDPOINT_NAMES.get(path, ""),
                "registered": path in ENDPOINT_NAMES, "count": cnt,
                "errors": errs,
                "error_rate": round(errs * 100.0 / cnt, 1) if cnt else 0.0,
                "avg_duration_ms": round(durations.get(path, 0) / cnt, 1) if cnt else 0.0,
            })
        return {"code": 200, "data": {
            "days": days, "start": start, "total": total, "errors": err_total,
            "error_rate": round(err_total * 100.0 / total, 1) if total else 0.0,
            "items": items,
        }}

    # ---------------- 延迟分位数（P50 / P95 / P99） ----------------
    @api_router.get("/latency")
    def api_latency(mode: str = Query("day", pattern="^(hour|day)$"),
                    path: str = None, compare: int = Query(0, ge=0, le=1)):
        """按小时（最近 24 小时）或按天（最近 14 天）统计响应耗时分位数

        为避免把百万级日志全部拉进内存，先在数据库里按 10ms 粒度做直方图聚合，
        再在 Python 中按累计占比估算分位数；结果缓存 120 秒。

        - path：只看指定接口（模糊匹配），用于对比不同接口的延迟表现
        - compare=1：额外返回上一周期同一时间段的 P95（p95_prev），叠加对比用
        """
        path = (path or "").strip() or None
        cache_key = f"{mode}|{path or ''}|{compare}"
        cached = latency_cache["data"].get(cache_key)
        if cached and time.time() - latency_cache["ts"].get(cache_key, 0) < 120:
            return {"code": 200, "data": cached}
        points = 24 if mode == "hour" else 14
        now = datetime.now()
        if mode == "hour":
            anchor = now.replace(minute=0, second=0, microsecond=0)
            buckets = [(anchor - timedelta(hours=i)).strftime("%Y-%m-%d %H:00")
                       for i in range(points - 1, -1, -1)]
            prev_buckets = [(anchor - timedelta(hours=i + points)).strftime("%Y-%m-%d %H:00")
                            for i in range(points - 1, -1, -1)]
            prev_start = (anchor - timedelta(hours=points * 2 - 1)).strftime("%Y-%m-%d %H:00:00")
            fmt = "%Y-%m-%d %H:00"
            start = (anchor - timedelta(hours=points - 1)).strftime("%Y-%m-%d %H:00:00")
        else:
            anchor = now.replace(hour=0, minute=0, second=0, microsecond=0)
            buckets = [(anchor - timedelta(days=i)).strftime("%Y-%m-%d")
                       for i in range(points - 1, -1, -1)]
            prev_buckets = [(anchor - timedelta(days=i + points)).strftime("%Y-%m-%d")
                            for i in range(points - 1, -1, -1)]
            prev_start = (anchor - timedelta(days=points * 2 - 1)).strftime("%Y-%m-%d 00:00:00")
            fmt = "%Y-%m-%d"
            start = (anchor - timedelta(days=points - 1)).strftime("%Y-%m-%d 00:00:00")
        labels = [b[5:] for b in buckets]

        hist = {}
        with db_scope() as db:
            bucket = func.date_format(log_model.time, fmt)
            bin_expr = (func.floor(log_model.duration_ms / 10) * 10).label("ms")
            q = (db.query(bucket.label("b"), bin_expr,
                          func.count(log_model.id).label("c"))
                 .filter(log_model.time >= (prev_start if compare else start),
                         log_model.duration_ms.isnot(None)))
            if path:
                q = q.filter(log_model.path.like(f"%{path}%"))
            rows = q.group_by(bucket, bin_expr).all()
        for b, ms, c in rows:
            hist.setdefault(b, []).append((int(ms or 0), int(c or 0)))

        def pick(bins, p):
            """按累计占比取分位点；无数据返回 None，前端断线显示"""
            total = sum(c for _, c in bins)
            if not total:
                return None
            target = total * p
            acc = 0
            for ms, c in bins:
                acc += c
                if acc >= target:
                    return ms
            return bins[-1][0]

        data = {
            "mode": mode,
            "path": path or "",
            "labels": labels,
            "p50": [], "p95": [], "p99": [],
        }
        for b in buckets:
            bins = sorted(hist.get(b, []))
            data["p50"].append(pick(bins, 0.50))
            data["p95"].append(pick(bins, 0.95))
            data["p99"].append(pick(bins, 0.99))
        if compare:
            data["p95_prev"] = [pick(sorted(hist.get(b, [])), 0.95) for b in prev_buckets]
        latency_cache["data"][cache_key] = data
        latency_cache["ts"][cache_key] = time.time()
        return {"code": 200, "data": data}

    # ---------------- 调用日志 ----------------
    @api_router.get("/logs")
    def api_logs(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                 path: str = None, ip: str = None, status_code: int = None,
                 min_status: int = None, max_status: int = None, method: str = None,
                 start: str = None, end: str = None, user_id: int = None):
        with db_scope() as db:
            q = db.query(log_model).filter(_api_log_filter(log_model))
            if path:
                q = q.filter(log_model.path.like(f"%{path}%"))
            if ip:
                q = q.filter(log_model.ip == ip)
            if status_code:
                q = q.filter(log_model.status_code == status_code)
            if min_status:
                q = q.filter(log_model.status_code >= min_status)
            if max_status:
                q = q.filter(log_model.status_code <= max_status)
            if method:
                q = q.filter(log_model.method == method.upper())
            if start:
                q = q.filter(log_model.time >= start)
            if end:
                q = q.filter(log_model.time <= end)
            if user_id is not None:
                q = q.filter(log_model.user_id == user_id)
            total = q.count()
            rows = q.order_by(log_model.id.desc()).offset((page - 1) * size).limit(size).all()
            user_ids = {i.user_id for i in rows if i.user_id is not None}
            token_ids = {i.token_id for i in rows if i.token_id is not None}
            user_map = {}
            token_map = {}
            if user_ids and c_user_model is not None:
                user_map = {u.id: u.email for u in db.query(c_user_model).filter(
                    c_user_model.id.in_(user_ids)).all()}
            if token_ids and token_model is not None:
                token_map = {t.id: t.token_hint for t in db.query(token_model).filter(
                    token_model.id.in_(token_ids)).all()}
            return {
                "code": 200, "total": total, "page": page, "size": size,
                "data": [{
                    "id": i.id,
                    "time": i.time.strftime("%Y-%m-%d %H:%M:%S") if i.time else "",
                    "method": i.method, "path": i.path, "ip": i.ip, "params": i.params,
                    "status_code": i.status_code, "duration_ms": i.duration_ms,
                    "user_agent": i.user_agent, "user_id": i.user_id,
                    "user_email": user_map.get(i.user_id, ""),
                    "token_id": i.token_id, "token_hint": token_map.get(i.token_id, ""),
                } for i in rows],
            }

    @api_router.get("/logs/export")
    def api_logs_export(request: Request, path: str = None, ip: str = None, status_code: int = None,
                        min_status: int = None, max_status: int = None, method: str = None,
                        start: str = None, end: str = None, user_id: int = None,
                        limit: int = Query(100000, ge=1, le=500000)):
        """按筛选条件导出调用日志为 Excel"""
        bounds = _time_bounds(start, end)
        if bounds is None:
            return JSONResponse({"code": 400, "msg": "时间格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"},
                                status_code=400)
        t_from, t_to = bounds
        with db_scope() as db:
            q = db.query(log_model).filter(_api_log_filter(log_model))
            if path:
                q = q.filter(log_model.path.like(f"%{path}%"))
            if ip:
                q = q.filter(log_model.ip == ip)
            if status_code:
                q = q.filter(log_model.status_code == status_code)
            if min_status:
                q = q.filter(log_model.status_code >= min_status)
            if max_status:
                q = q.filter(log_model.status_code <= max_status)
            if method:
                q = q.filter(log_model.method == method.upper())
            if t_from:
                q = q.filter(log_model.time >= t_from)
            if t_to:
                q = q.filter(log_model.time <= t_to)
            if user_id is not None:
                q = q.filter(log_model.user_id == user_id)
            rows = q.order_by(log_model.id.desc()).limit(limit).all()
            user_ids = {r.user_id for r in rows if r.user_id is not None}
            token_ids = {r.token_id for r in rows if r.token_id is not None}
            user_map = {u.id: u.email for u in db.query(c_user_model).filter(
                c_user_model.id.in_(user_ids)).all()} if user_ids and c_user_model is not None else {}
            token_map = {t.id: t.token_hint for t in db.query(token_model).filter(
                token_model.id.in_(token_ids)).all()} if token_ids and token_model is not None else {}

        headers = ["时间", "方法", "路径", "IP", "C端用户", "Token", "参数", "状态码", "耗时(ms)", "User-Agent"]
        data = [[r.time, r.method, r.path, r.ip, user_map.get(r.user_id, ""),
                 token_map.get(r.token_id, ""), r.params, r.status_code, r.duration_ms, r.user_agent]
                for r in rows]
        operator = request.session.get("admin") or "system"
        filename = f"调用日志_{operator}_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
        _audit(request, "log_export", "调用日志", f"导出 {len(data)} 条调用日志")
        return _xlsx_response(_xlsx_bytes("调用日志", headers, data, operator), filename)

    @api_router.post("/logs/clear")
    def api_logs_clear(request: Request, payload: dict):
        days = max(1, int(payload.get("days", 30)))
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
        with db_scope() as db:
            deleted = db.query(log_model).filter(log_model.time < cutoff).delete(synchronize_session=False)
            db.commit()
        _audit(request, "log_clear", "调用日志", f"清理 {days} 天前日志，删除 {deleted} 条")
        return {"code": 200, "msg": f"已清理 {deleted} 条日志", "cutoff": cutoff, "deleted": deleted}

    # ---------------- 异步导出任务 ----------------
    EXPORT_DIR = os.path.join(BASE_DIR, "exports")

    def _export_job_to_dict(row):
        return {
            "id": row.id, "kind": row.kind or "", "state": row.state or "",
            "filename": row.filename or "", "rows": int(row.rows or 0),
            "size": int(row.size or 0), "message": row.message or "",
            "operator": row.operator or "",
            "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
            "finished_at": row.finished_at.strftime("%Y-%m-%d %H:%M:%S") if row.finished_at else "",
            "expires_at": row.expires_at.strftime("%Y-%m-%d %H:%M:%S") if row.expires_at else "",
        }

    def _export_collect(kind):
        """按导出类型收集表头与数据（日志量大，限制最大行数）"""
        if kind == "audit":
            if audit_log_model is None:
                raise ValueError("审计日志未启用")
            with db_scope() as db:
                rows = (_human_audit_query(db)
                        .order_by(audit_log_model.id.desc()).limit(200000).all())
            headers = ["时间", "操作人", "操作", "对象", "详情", "IP", "结果"]
            data = [[r.created_at, r.operator, r.action, r.target, r.detail, r.ip, r.result]
                    for r in rows]
            return "审计日志", headers, data
        with db_scope() as db:
            rows = db.query(log_model).order_by(log_model.id.desc()).limit(200000).all()
        headers = ["时间", "方法", "路径", "IP", "参数", "状态码", "耗时(ms)", "User-Agent"]
        data = [[r.time, r.method, r.path, r.ip, r.params, r.status_code,
                 r.duration_ms, r.user_agent] for r in rows]
        return "调用日志", headers, data

    def _export_job_worker(job_id, kind, operator):
        """后台生成 Excel 文件并回写任务状态"""
        try:
            title, headers, data = _export_collect(kind)
            content = _xlsx_bytes(title, headers, data, operator)
            os.makedirs(EXPORT_DIR, exist_ok=True)
            filename = f"{kind}_{operator}_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
            path = os.path.join(EXPORT_DIR, filename)
            with open(path, "wb") as f:
                f.write(content)
            with db_scope() as db:
                row = db.query(export_job_model).filter(export_job_model.id == job_id).first()
                if row is not None:
                    row.state = "done"
                    row.filename = filename
                    row.file_path = path
                    row.rows = len(data)
                    row.size = len(content)
                    row.finished_at = datetime.now()
                    row.expires_at = datetime.now() + timedelta(
                        days=max(1, int(getattr(config, "EXPORT_JOB_KEEP_DAYS", 7))))
                    db.commit()
        except Exception as e:
            try:
                with db_scope() as db:
                    row = db.query(export_job_model).filter(export_job_model.id == job_id).first()
                    if row is not None:
                        row.state = "failed"
                        row.message = str(e)[:500]
                        row.finished_at = datetime.now()
                        db.commit()
            except Exception:
                pass

    @api_router.post("/export-jobs")
    def api_export_job_create(request: Request, payload: dict):
        if export_job_model is None:
            return {"code": 400, "msg": "导出任务功能未启用"}
        kind = str(payload.get("kind") or "logs").strip()
        if kind not in ("logs", "audit"):
            return {"code": 400, "msg": "导出类型只支持 logs / audit"}
        operator = request.session.get("admin") or "system"
        job_id = uuid.uuid4().hex[:16]
        with db_scope() as db:
            db.add(export_job_model(id=job_id, kind=kind, state="running",
                                    operator=operator[:100],
                                    created_at=datetime.now()))
            db.commit()
        _audit(request, "export_job", kind, f"创建异步导出任务 {job_id}")
        threading.Thread(target=_export_job_worker,
                         args=(job_id, kind, operator), daemon=True).start()
        return {"code": 200, "msg": "导出任务已提交，完成后可在导出记录中下载", "id": job_id}

    @api_router.get("/export-jobs")
    def api_export_jobs(limit: int = Query(50, ge=1, le=200)):
        if export_job_model is None:
            return {"code": 200, "data": []}
        with db_scope() as db:
            rows = (db.query(export_job_model)
                    .order_by(export_job_model.created_at.desc()).limit(limit).all())
            return {"code": 200, "data": [_export_job_to_dict(r) for r in rows]}

    @api_router.get("/export-jobs/{job_id}/download")
    def api_export_job_download(job_id: str):
        if export_job_model is None:
            raise HTTPException(status_code=404, detail="导出任务未启用")
        with db_scope() as db:
            row = db.query(export_job_model).filter(export_job_model.id == job_id).first()
            if not row or row.state != "done":
                raise HTTPException(status_code=404, detail="文件不存在或未生成完成")
            path, filename = row.file_path, row.filename
        if not path or not os.path.exists(path):
            raise HTTPException(status_code=404, detail="文件已过期或被清理")
        return FileResponse(path, media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            filename=filename or os.path.basename(path))

    @api_router.post("/export-jobs/{job_id}/delete")
    def api_export_job_delete(request: Request, job_id: str):
        if export_job_model is None:
            return {"code": 400, "msg": "导出任务未启用"}
        with db_scope() as db:
            row = db.query(export_job_model).filter(export_job_model.id == job_id).first()
            if not row:
                return {"code": 404, "msg": "任务不存在"}
            path = row.file_path
            db.delete(row)
            db.commit()
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        _audit(request, "export_job_delete", job_id, "删除导出任务与文件")
        return {"code": 200, "msg": "已删除"}

    # ---------------- C 端用户审批 / 额度管理 ----------------
    @api_router.get("/c-users")
    def api_c_users():
        if c_user_model is None:
            return {"code": 400, "msg": "C 端用户功能未启用"}
        data = []
        with db_scope() as db:
            token_rows = db.query(token_model).filter(token_model.user_id.isnot(None)).all()
            token_map = {}
            for token in token_rows:
                token_map.setdefault(token.user_id, []).append(token)
            for row in db.query(c_user_model).order_by(c_user_model.id.desc()).all():
                tokens = token_map.get(row.id, [])
                used = int(row.today_used or 0)
                bonus = int(row.bonus_quota or 0)
                data.append({
                    "id": row.id, "email": row.email or "", "status": row.status or "pending",
                    "level": row.level or "free", "daily_quota": int(row.daily_quota or 0),
                    "bonus_quota": bonus,
                    "today_used": used, "register_ip": row.register_ip or "",
                    "remaining": max(0, int(row.daily_quota or 0) + bonus - used),
                    "token_hint": ", ".join(t.token_hint or "****" for t in tokens),
                    "tokens": [{"id": t.id, "hint": t.token_hint or "****",
                                "enabled": bool(t.enabled)} for t in tokens],
                    "token_enabled": any(bool(t.enabled) for t in tokens),
                    "approved_at": row.approved_at.strftime("%Y-%m-%d %H:%M:%S") if row.approved_at else "",
                    "last_login_at": row.last_login_at.strftime("%Y-%m-%d %H:%M:%S") if row.last_login_at else "",
                    "created_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
                })
        return {"code": 200, "data": data}

    @api_router.post("/c-users/{user_id}/update")
    def api_c_user_update(request: Request, user_id: int, payload: dict):
        if c_user_model is None:
            return {"code": 400, "msg": "C 端用户功能未启用"}
        before, after = {}, {}
        with db_scope() as db:
            row = db.query(c_user_model).filter(c_user_model.id == user_id).first()
            if row is None:
                return {"code": 404, "msg": "用户不存在"}
            before = {"status": row.status, "level": row.level,
                      "daily_quota": row.daily_quota}
            if "status" in payload:
                status = str(payload.get("status") or "")
                if status not in ("pending", "active", "banned"):
                    return {"code": 400, "msg": "状态仅支持 pending / active / banned"}
                row.status = status
                if status == "active" and before["status"] != "active":
                    row.approved_at = datetime.now()
                if status != "active":
                    db.query(token_model).filter(token_model.user_id == user_id).update(
                        {token_model.enabled: False}, synchronize_session=False)
            if "level" in payload:
                level = str(payload.get("level") or "").strip()
                if not level or len(level) > 20:
                    return {"code": 400, "msg": "等级应为 1-20 个字符"}
                row.level = level
            if "daily_quota" in payload or "bonus_quota" in payload:
                return {"code": 400, "msg": "额度不可直接修改，请使用增加、减少或清零操作"}
            db.commit()
            after = {"status": row.status, "level": row.level,
                     "daily_quota": row.daily_quota}
            user_email = row.email
        _refresh_security()
        _audit(request, "c_user_update", user_email,
               "更新 C 端用户审批状态 / 等级", before=before, after=after)
        return {"code": 200, "msg": "已保存；通过后用户首次登录时自动发放 Token"}

    @api_router.post("/c-users/{user_id}/quota")
    def api_c_user_quota_adjust(request: Request, user_id: int, payload: dict):
        if c_user_model is None:
            return {"code": 400, "msg": "C 端用户功能未启用"}
        action = str(payload.get("action") or "").strip().lower()
        if action not in ("add", "subtract", "clear"):
            return {"code": 400, "msg": "额度操作仅支持 add / subtract / clear"}
        operator = str(request.session.get("user") or request.session.get("username") or "admin")
        with db_scope() as db:
            row = db.query(c_user_model).filter(c_user_model.id == user_id).with_for_update().first()
            if row is None:
                return {"code": 404, "msg": "用户不存在"}
            ensure_initial_quota_log(db, row)
            old_base = int(row.daily_quota or 0)
            old_bonus = int(row.bonus_quota or 0)
            amount = 0
            if action in ("add", "subtract"):
                try:
                    amount = int(payload.get("amount"))
                except (TypeError, ValueError):
                    return {"code": 400, "msg": "调整数量必须是正整数"}
                if not 0 < amount <= 100000000:
                    return {"code": 400, "msg": "单次调整数量范围为 1-100000000"}
                if action == "add" and old_base + amount > 100000000:
                    return {"code": 400, "msg": "调整后基础额度不能超过 100000000"}
                if action == "subtract" and amount > old_base:
                    return {"code": 400, "msg": "减少数量不能超过当前基础额度；如需清零请使用清零操作"}
                row.daily_quota = old_base + amount if action == "add" else old_base - amount
                change = amount if action == "add" else -amount
                title = "管理员增加额度" if action == "add" else "管理员减少额度"
                detail = f"基础额度由 {old_base} 调整为 {row.daily_quota}"
            else:
                change = -(old_base + old_bonus)
                row.daily_quota = 0
                row.bonus_quota = 0
                title = "管理员清零额度"
                detail = f"清零基础额度 {old_base} 与任务奖励 {old_bonus}"
            balance = max(0, int(row.daily_quota or 0) + int(row.bonus_quota or 0)
                          - int(row.today_used or 0))
            record_quota_change(
                db, row.id, change, "admin", title, detail, operator=operator,
                balance_after=balance)
            db.commit()
            user_email = row.email
            result = {
                "daily_quota": int(row.daily_quota or 0),
                "bonus_quota": int(row.bonus_quota or 0),
                "today_used": int(row.today_used or 0), "remaining": balance,
            }
        _audit(request, "c_user_quota", user_email, detail,
               before={"daily_quota": old_base, "bonus_quota": old_bonus}, after=result)
        return {"code": 200, "msg": f"{title}成功", "data": result}

    @api_router.get("/portal/review-mode")
    def api_portal_review_mode():
        """读取 C 端注册审核模式（auto 自动 / manual 人工）"""
        return {"code": 200, "mode": auto_approve_mode()}

    @api_router.post("/portal/review-mode")
    def api_portal_set_review_mode(request: Request, payload: dict):
        """切换注册审核模式：自动审批立即生效，人工审批后需在待审批列表手动处理"""
        try:
            mode = set_auto_approve_mode(payload.get("mode"))
        except ValueError as exc:
            return {"code": 400, "msg": str(exc)}
        _audit(request, "portal_review_mode", mode, f"注册审核模式切换为 {mode}")
        return {"code": 200, "mode": mode,
                "msg": "已切换为自动审批" if mode == "auto" else "已切换为人工审批"}

    @api_router.get("/portal/quota-warn")
    def api_portal_quota_warn():
        """读取额度预警阈值（剩余次数），0 表示关闭"""
        return {"code": 200, "threshold": quota_warn_threshold()}

    @api_router.post("/portal/quota-warn")
    def api_portal_set_quota_warn(request: Request, payload: dict):
        """保存额度预警阈值；修改后由后台线程按新阈值扫描"""
        try:
            threshold = set_quota_warn_threshold(payload.get("threshold"))
        except ValueError as exc:
            return {"code": 400, "msg": str(exc)}
        _audit(request, "portal_quota_warn", "threshold", f"额度预警阈值设为 {threshold}")
        return {"code": 200, "threshold": threshold,
                "msg": "已关闭额度预警" if threshold == 0 else f"额度预警阈值已设为 {threshold} 次"}

    @api_router.get("/sensitive/hits")
    def api_sensitive_hits():
        """敏感词命中统计：累计次数与 Top N"""
        return {"code": 200, **sensitive.hit_stats()}

    @api_router.post("/sensitive/hits/reset")
    def api_sensitive_hits_reset(request: Request):
        """清零敏感词命中统计"""
        sensitive.reset_hits()
        _audit(request, "sensitive_hits_reset", "hit_stats", "清零敏感词命中统计")
        return {"code": 200, "msg": "命中统计已清零"}

    @api_router.get("/c-users/{user_id}/quota-logs")
    def api_c_user_quota_logs(user_id: int, limit: int = Query(200, ge=1, le=500)):
        if c_user_model is None:
            return {"code": 400, "msg": "C 端用户功能未启用"}
        with db_scope() as db:
            user = db.query(c_user_model).filter(c_user_model.id == user_id).first()
            if user is None:
                return {"code": 404, "msg": "用户不存在"}
            ensure_initial_quota_log(db, user)
            db.commit()
            rows = db.query(PortalQuotaLogDB).filter(
                PortalQuotaLogDB.user_id == user_id,
            ).order_by(PortalQuotaLogDB.id.desc()).limit(limit).all()
            used = int(user.today_used or 0)
            bonus = int(user.bonus_quota or 0)
            return {"code": 200, "user": {
                "id": user.id, "email": user.email, "daily_quota": int(user.daily_quota or 0),
                "bonus_quota": bonus, "today_used": used,
                "remaining": max(0, int(user.daily_quota or 0) + bonus - used),
            }, "data": [{
                "time": row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "",
                "change": int(row.change or 0), "kind": row.kind or "", "title": row.title or "",
                "detail": row.detail or "", "path": row.path or "",
                "balance_after": int(row.balance_after or 0), "operator": row.operator or "system",
            } for row in rows]}

    # ---------------- Token 密钥管理 ----------------
    @api_router.get("/tokens")
    def api_tokens():
        data = []
        if token_model is not None:
            with db_scope() as db:
                user_map = {}
                if c_user_model is not None:
                    user_map = {u.id: u.email for u in db.query(c_user_model).all()}
                for r in db.query(token_model).order_by(token_model.id.desc()).all():
                    data.append({
                        "id": r.id, "name": r.name or "",
                        "token": r.token_hint or "****",  # 只回显掩码，明文不落库
                        "permissions": r.permissions or "*", "enabled": bool(r.enabled),
                        "remark": r.remark or "",
                        "expires_at": r.expires_at.strftime("%Y-%m-%d %H:%M:%S") if r.expires_at else "",
                        "grace_until": r.grace_until.strftime("%Y-%m-%d %H:%M:%S") if r.grace_until else "",
                        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                        "user_email": user_map.get(r.user_id, "") if r.user_id else "",
                        "master": False,
                    })
        data.insert(0, {
            "id": 0, "name": "主 Token（内置）", "token": _token_hint(master_token),
            "permissions": "*", "enabled": True,
            "remark": "main.py 配置区 API_TOKEN，恒定拥有全部权限",
            "expires_at": "", "created_at": "", "user_email": "", "master": True,
        })
        return {"code": 200, "data": data}

    @api_router.post("/tokens")
    def api_token_create(request: Request, payload: dict):
        if token_model is None:
            return {"code": 400, "msg": "Token 功能未启用"}
        name = (payload.get("name") or "").strip()
        if not name:
            return {"code": 400, "msg": "请填写密钥名称"}
        token = (payload.get("token") or "").strip() or secrets.token_urlsafe(24)
        if len(token) > 64:
            return {"code": 400, "msg": "Token 长度不能超过 64 个字符"}
        digest = _hash_token(token)
        if digest == (master_token_hash or _hash_token(master_token)):
            return {"code": 400, "msg": "该 Token 与主 Token 重复"}
        try:
            expires_at = parse_dt(payload.get("expires_at"))
        except ValueError as e:
            return {"code": 400, "msg": str(e)}
        permissions = (payload.get("permissions") or "*").strip() or "*"
        enabled_flag = str(payload.get("enabled", True)).lower() not in ("false", "0", "no")
        remark = (payload.get("remark") or "")[:255]
        with db_scope() as db:
            if db.query(token_model).filter(token_model.token == digest).first():
                return {"code": 400, "msg": "Token 已存在，请更换或留空自动生成"}
            db.add(token_model(
                name=name[:100], token=digest, token_hint=_token_hint(token),
                permissions=permissions, enabled=enabled_flag,
                remark=remark, expires_at=expires_at,
            ))
            db.commit()
        _refresh_security()
        _audit(request, "token_create", name,
               f"新建 Token，权限：{permissions}，过期：{payload.get('expires_at') or '永久'}",
               after={"name": name[:100], "permissions": permissions,
                      "enabled": enabled_flag, "remark": remark,
                      "expires_at": str(expires_at) if expires_at else "永久",
                      "token_hint": _token_hint(token)})
        return {"code": 200, "msg": "Token 已创建，明文仅本次显示，请立即保存", "token": token}

    @api_router.post("/tokens/{token_id}/update")
    def api_token_update(request: Request, token_id: int, payload: dict):
        if token_model is None:
            return {"code": 400, "msg": "Token 功能未启用"}
        before, after = {}, {}

        def snapshot(row):
            return {"name": row.name or "", "permissions": row.permissions or "*",
                    "enabled": bool(row.enabled), "remark": row.remark or "",
                    "expires_at": (row.expires_at.strftime("%Y-%m-%d %H:%M:%S")
                                   if row.expires_at else ""),
                    "token_hint": row.token_hint or ""}

        with db_scope() as db:
            row = db.query(token_model).filter(token_model.id == token_id).first()
            if not row:
                return {"code": 404, "msg": "Token 不存在"}
            before = snapshot(row)
            if "name" in payload:
                row.name = (payload.get("name") or "").strip()[:100]
            if "permissions" in payload:
                row.permissions = (payload.get("permissions") or "*").strip() or "*"
            if "enabled" in payload:
                row.enabled = str(payload.get("enabled")).lower() not in ("false", "0", "no")
            if "remark" in payload:
                row.remark = (payload.get("remark") or "")[:255]
            if "expires_at" in payload:
                try:
                    row.expires_at = parse_dt(payload.get("expires_at"))
                except ValueError as e:
                    return {"code": 400, "msg": str(e)}
            if "token" in payload:
                new_token = (payload.get("token") or "").strip()
                new_digest = _hash_token(new_token)
                if new_token and new_digest != row.token:
                    if len(new_token) > 64:
                        return {"code": 400, "msg": "Token 长度不能超过 64 个字符"}
                    if new_digest == (master_token_hash or _hash_token(master_token)) or db.query(
                            token_model).filter(token_model.token == new_digest,
                                               token_model.id != token_id).first():
                        return {"code": 400, "msg": "Token 已存在，请更换"}
                    row.token = new_digest
                    row.token_hint = _token_hint(new_token)
            db.commit()
            db.refresh(row)
            after = snapshot(row)
        _refresh_security()
        changes = [k for k in ("name", "permissions", "enabled", "remark", "expires_at", "token")
                   if k in payload]
        _audit(request, "token_update", f"ID {token_id}",
               "修改字段：" + "、".join(changes or ["无"]),
               before=before, after=after)
        return {"code": 200, "msg": "已保存"}

    @api_router.post("/tokens/{token_id}/rotate")
    def api_token_rotate(request: Request, token_id: int):
        """密钥轮换：新 Token 立即生效，旧 Token 保留宽限期（默认 24 小时）后失效

        旧 Token 在宽限期内仍可调用接口，但每次使用都会触发低优告警，
        提醒调用方尽快替换，实现平滑迁移。
        """
        if token_model is None:
            return {"code": 400, "msg": "Token 功能未启用"}
        now = datetime.now()
        grace_hours = max(1, int(getattr(config, "TOKEN_ROTATE_GRACE_HOURS", 24)))
        new_token = secrets.token_urlsafe(24)
        new_digest = _hash_token(new_token)
        before = {}
        with db_scope() as db:
            row = db.query(token_model).filter(token_model.id == token_id).first()
            if not row:
                return {"code": 404, "msg": "Token 不存在"}
            before = {"name": row.name or "", "token_hint": row.token_hint or "",
                      "expires_at": (row.expires_at.strftime("%Y-%m-%d %H:%M:%S")
                                     if row.expires_at else "")}
            if new_digest == (master_token_hash or _hash_token(master_token)) or db.query(
                    token_model).filter(token_model.token == new_digest,
                                        token_model.id != token_id).first():
                return {"code": 400, "msg": "生成的新 Token 冲突，请重试"}
            old_expires = row.expires_at
            row.expires_at = now
            row.grace_until = now + timedelta(hours=grace_hours)
            row.remark = ((row.remark or "") +
                          f" [已轮换 {now.strftime('%Y-%m-%d %H:%M')}]")[:255]
            db.add(token_model(
                name=(row.name or "")[:100], token=new_digest,
                token_hint=_token_hint(new_token),
                permissions=row.permissions or "*", enabled=bool(row.enabled),
                remark=row.remark or "",
                expires_at=(old_expires + timedelta(hours=grace_hours)) if old_expires else None,
                user_id=row.user_id,
            ))
            db.commit()
        _refresh_security()
        _audit(request, "token_rotate", f"ID {token_id}",
               f"轮换 Token，旧 Token 宽限 {grace_hours} 小时",
               before=before, after={"token_hint": _token_hint(new_token),
                                     "grace_hours": grace_hours})
        return {"code": 200,
                "msg": f"已轮换：新 Token 立即生效，旧 Token 保留 {grace_hours} 小时宽限期",
                "token": new_token, "grace_hours": grace_hours}

    @api_router.post("/tokens/{token_id}/renew")
    def api_token_renew(request: Request, token_id: int, payload: dict):
        """Token 一键续期：只延长有效期，Token 值、权限与掩码保持不变"""
        if token_model is None:
            return {"code": 400, "msg": "Token 功能未启用"}
        try:
            days = int(payload.get("days") or 30)
        except (TypeError, ValueError):
            days = 30
        days = max(1, min(days, 3650))
        now = datetime.now()
        with db_scope() as db:
            row = db.query(token_model).filter(token_model.id == token_id).first()
            if not row:
                return {"code": 404, "msg": "Token 不存在"}
            before = row.expires_at.strftime("%Y-%m-%d %H:%M:%S") if row.expires_at else "永久"
            base = max(now, row.expires_at) if row.expires_at else now
            row.expires_at = base + timedelta(days=days)
            db.commit()
            after = row.expires_at.strftime("%Y-%m-%d %H:%M:%S")
        _refresh_security()
        _audit(request, "token_renew", f"ID {token_id}",
               f"有效期延长 {days} 天：{before} → {after}",
               before={"expires_at": before}, after={"expires_at": after, "days": days})
        return {"code": 200, "msg": f"已续期 {days} 天，至 {after}", "expires_at": after}

    @api_router.post("/tokens/{token_id}/delete")
    def api_token_delete(request: Request, token_id: int):
        if token_model is None:
            return {"code": 400, "msg": "Token 功能未启用"}
        before = {}
        with db_scope() as db:
            row = db.query(token_model).filter(token_model.id == token_id).first()
            if not row:
                return {"code": 404, "msg": "Token 不存在"}
            before = {"name": row.name or "", "permissions": row.permissions or "*",
                      "enabled": bool(row.enabled), "remark": row.remark or "",
                      "token_hint": row.token_hint or ""}
            db.delete(row)
            db.commit()
        _refresh_security()
        _audit(request, "token_delete", f"ID {token_id}", "删除 Token",
               before=before, after=None)
        return {"code": 200, "msg": "已删除"}

    @api_router.get("/master-token")
    def api_master_token():
        """只返回掩码，明文仅保存在服务端配置"""
        return {"code": 200, "hint": _token_hint(master_token)}

    # ---------------- IP 黑白名单 ----------------
    @api_router.get("/ip-rules")
    def api_ip_rules():
        data = []
        if ip_rule_model is not None:
            with db_scope() as db:
                for r in db.query(ip_rule_model).order_by(ip_rule_model.id.desc()).all():
                    data.append({
                        "id": r.id, "ip": r.ip or "",
                        "rule_type": r.rule_type or "black",
                        "remark": r.remark or "", "enabled": bool(r.enabled),
                        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                    })
        return {"code": 200, "data": data}

    @api_router.post("/ip-rules")
    def api_ip_rule_add(request: Request, payload: dict):
        if ip_rule_model is None:
            return {"code": 400, "msg": "IP 规则功能未启用"}
        ip = (payload.get("ip") or "").strip()
        if not ip:
            return {"code": 400, "msg": "请填写 IP 地址"}
        if len(ip) > 100:
            return {"code": 400, "msg": "IP 地址过长"}
        rule_type = "white" if (payload.get("rule_type") or "").lower() == "white" else "black"
        with db_scope() as db:
            if db.query(ip_rule_model).filter(ip_rule_model.ip == ip,
                                              ip_rule_model.rule_type == rule_type).first():
                return {"code": 400, "msg": "该 IP 已在名单中"}
            db.add(ip_rule_model(ip=ip, rule_type=rule_type,
                                 remark=(payload.get("remark") or "")[:255],
                                 enabled=True))
            db.commit()
        _refresh_security()
        _audit(request, "ip_rule_add", ip, f"新增 {rule_type} 名单规则")
        return {"code": 200, "msg": "已添加"}

    @api_router.post("/ip-rules/{rule_id}/update")
    def api_ip_rule_update(request: Request, rule_id: int, payload: dict):
        if ip_rule_model is None:
            return {"code": 400, "msg": "IP 规则功能未启用"}
        with db_scope() as db:
            row = db.query(ip_rule_model).filter(ip_rule_model.id == rule_id).first()
            if not row:
                return {"code": 404, "msg": "规则不存在"}
            if "enabled" in payload:
                row.enabled = str(payload.get("enabled")).lower() not in ("false", "0", "no")
            if "remark" in payload:
                row.remark = (payload.get("remark") or "")[:255]
            db.commit()
        _refresh_security()
        _audit(request, "ip_rule_update", f"ID {rule_id}",
               "修改字段：" + "、".join(k for k in ("enabled", "remark") if k in payload))
        return {"code": 200, "msg": "已保存"}

    @api_router.post("/ip-rules/{rule_id}/delete")
    def api_ip_rule_delete(request: Request, rule_id: int):
        if ip_rule_model is None:
            return {"code": 400, "msg": "IP 规则功能未启用"}
        with db_scope() as db:
            row = db.query(ip_rule_model).filter(ip_rule_model.id == rule_id).first()
            if not row:
                return {"code": 404, "msg": "规则不存在"}
            db.delete(row)
            db.commit()
        _refresh_security()
        _audit(request, "ip_rule_delete", f"ID {rule_id}", "删除 IP 名单规则")
        return {"code": 200, "msg": "已删除"}

    # ---------------- 后台独立 IP 白名单 ----------------
    @api_router.get("/admin-ip")
    def api_admin_ip():
        return {"code": 200, "data": {
            "enabled": get_setting("admin_ip_guard", "0") == "1",
            "whitelist": get_setting("admin_ip_whitelist", ""),
        }}

    @api_router.post("/admin-ip")
    def api_admin_ip_save(request: Request, payload: dict):
        enabled = str(payload.get("enabled")).lower() in ("1", "true", "yes", "on")
        raw = str(payload.get("whitelist") or "")
        entries = [x.strip() for x in re.split(r"[,;\s]+", raw) if x.strip()]
        bad = [x for x in entries if not _valid_ip_entry(x)]
        if bad:
            return {"code": 400, "msg": "IP 格式不正确：" + "、".join(bad[:5])}
        if enabled and not entries:
            return {"code": 400, "msg": "启用后台准入前请先填写至少一个白名单 IP"}
        before = {
            "enabled": get_setting("admin_ip_guard", "0") == "1",
            "whitelist": [x.strip() for x in re.split(
                r"[,;\s]+", get_setting("admin_ip_whitelist", "")) if x.strip()],
        }
        save_setting("admin_ip_guard", "1" if enabled else "0")
        save_setting("admin_ip_whitelist", "\n".join(entries))
        ADMIN_IP_CACHE["ts"] = 0
        _audit(request, "admin_ip_setting", "后台 IP 白名单",
               f"启用={enabled}，白名单：{', '.join(entries) or '空'}",
               before=before, after={"enabled": enabled, "whitelist": entries})
        return {"code": 200, "msg": "后台 IP 白名单已保存并立即生效"}

    # ---------------- 外网接口熔断监控 ----------------
    @api_router.get("/breakers")
    def api_breakers():
        if breaker_model is None:
            return {"code": 200, "data": [], "history": []}
        now = time.time()
        data = []
        with db_scope() as db:
            rows = db.query(breaker_model).order_by(breaker_model.key.asc()).all()
            for r in rows:
                ok = int(r.total_ok or 0)
                fail = int(r.total_fail or 0)
                total = ok + fail
                data.append({
                    "key": r.key,
                    "label": BREAKER_LABELS.get(r.key, r.key),
                    "failures": int(r.failures or 0),
                    "open": float(r.open_until or 0) > now,
                    "open_until": (datetime.fromtimestamp(float(r.open_until))
                                   .strftime("%Y-%m-%d %H:%M:%S")
                                   if float(r.open_until or 0) > now else ""),
                    "total_ok": ok,
                    "total_fail": fail,
                    "success_rate": round(ok * 100.0 / total, 1) if total else 0.0,
                    "last_error": r.last_error or "",
                    "last_success_at": (r.last_success_at.strftime("%Y-%m-%d %H:%M:%S")
                                        if r.last_success_at else ""),
                })
            history = []
            if audit_log_model is not None:
                logs = (db.query(audit_log_model)
                        .filter(audit_log_model.action == "breaker_open")
                        .order_by(audit_log_model.id.desc()).limit(30).all())
                history = [{
                    "target": x.target or "",
                    "label": BREAKER_LABELS.get(x.target or "", x.target or ""),
                    "detail": x.detail or "",
                    "time": x.created_at.strftime("%Y-%m-%d %H:%M:%S") if x.created_at else "",
                } for x in logs]
        return {"code": 200, "data": data, "history": history}

    @api_router.post("/breakers/reset")
    def api_breakers_reset(request: Request, payload: dict):
        key = str(payload.get("key") or "").strip()
        if breaker_reset is None:
            return {"code": 400, "msg": "熔断监控未启用"}
        try:
            breaker_reset(key or None)
        except Exception as e:
            return {"code": 500, "msg": f"重置失败：{e}"}
        _audit(request, "breaker_reset", key or "全部外网接口", "手动重置熔断状态")
        return {"code": 200, "msg": "熔断状态已重置"}

    # ---------------- 敏感词库 ----------------
    @api_router.get("/sensitive-words")
    def api_sensitive_words(keyword: str = None, category: str = None):
        return {"code": 200, "total": sensitive.count_words(),
                "data": sensitive.list_words(keyword, category)}

    @api_router.get("/sensitive-words/tree")
    def api_sensitive_tree():
        """只加载一级分类与条数，点在分类上再拉该分类的词条"""
        return {"code": 200, "total": sensitive.count_words(), "data": sensitive.tree()}

    @api_router.post("/sensitive-words")
    def api_sensitive_add(request: Request, payload: dict):
        raw = str(payload.get("words") or "")
        category = str(payload.get("category") or "默认")
        added, skipped = sensitive.add_words(raw, category)
        sensitive.reload()
        if added:
            _audit(request, "sensitive_add", "敏感词库",
                   f"新增 {added} 条（分类：{category}），跳过重复 {skipped} 条",
                   after={"added": added, "skipped": skipped, "category": category})
        return {"code": 200, "msg": f"已新增 {added} 条，跳过重复 {skipped} 条",
                "added": added, "skipped": skipped}

    @api_router.post("/sensitive-words/{word_id}/toggle")
    def api_sensitive_toggle(request: Request, word_id: int):
        row = sensitive.toggle_word(word_id)
        if row is None:
            return {"code": 404, "msg": "敏感词不存在"}
        sensitive.reload()
        state = "启用" if row["enabled"] else "停用"
        _audit(request, "sensitive_toggle", row["word"], f"状态改为{state}",
               before={"enabled": not row["enabled"]}, after={"enabled": row["enabled"]})
        return {"code": 200, "msg": f"已{state}", "data": row}

    @api_router.post("/sensitive-words/{word_id}/delete")
    def api_sensitive_delete(request: Request, word_id: int):
        word = sensitive.delete_word(word_id)
        if word is None:
            return {"code": 404, "msg": "敏感词不存在"}
        sensitive.reload()
        _audit(request, "sensitive_delete", word, "删除敏感词")
        return {"code": 200, "msg": "已删除"}

    # ---------------- 敏感词隔离区（命中不直接拒绝，人工审核后放行） ----------------
    def _quarantine_items(limit=50, status="pending"):
        if quarantine_model is None:
            return []
        with db_scope() as db:
            q = db.query(quarantine_model)
            if status:
                q = q.filter(quarantine_model.status == status)
            rows = q.order_by(quarantine_model.id.desc()).limit(limit).all()
            return [{
                "id": r.id, "table_key": r.table_key or "", "filename": r.filename or "",
                "line_no": r.line_no or 0, "content": r.content or "", "hits": r.hits or "",
                "status": r.status or "pending", "operator": r.operator or "",
                "reviewer": r.reviewer or "", "result": r.result or "",
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                "reviewed_at": r.reviewed_at.strftime("%Y-%m-%d %H:%M:%S") if r.reviewed_at else "",
                "row_data": (r.row_data or "")[:4000],
            } for r in rows]

    @api_router.get("/quarantine")
    def api_quarantine(status: str = Query("pending"), limit: int = Query(100, ge=1, le=500)):
        if quarantine_model is None:
            return {"code": 200, "total": 0, "data": []}
        with db_scope() as db:
            q = db.query(quarantine_model)
            if status:
                q = q.filter(quarantine_model.status == status)
            total = q.count()
        return {"code": 200, "total": total, "data": _quarantine_items(limit, status)}

    def _quarantine_write(row):
        """把隔离区数据写入目标表，只使用模型中存在的字段"""
        model = table_map.get(row.table_key)
        if model is None:
            return False, "目标数据表不存在"
        try:
            data = json.loads(row.row_data or "{}")
        except (TypeError, ValueError):
            return False, "行数据解析失败"
        if not isinstance(data, dict):
            return False, "行数据格式不正确"
        valid = {c.name for c in model.__table__.columns}
        record = {k: v for k, v in data.items() if k in valid and k != "id"}
        if not record:
            return False, "没有可写入的有效字段"
        try:
            with db_scope() as db:
                db.execute(model.__table__.insert(), [record])
                db.commit()
            return True, ""
        except Exception as e:
            return False, str(e)[:150]

    @api_router.post("/quarantine/{qid}/approve")
    def api_quarantine_approve(request: Request, qid: int):
        if quarantine_model is None:
            return {"code": 400, "msg": "隔离区未启用"}
        operator = request.session.get("admin") or ""
        with db_scope() as db:
            row = db.query(quarantine_model).filter(quarantine_model.id == qid).first()
            if row is None:
                return {"code": 404, "msg": "记录不存在"}
            if (row.status or "pending") != "pending":
                return {"code": 400, "msg": "该记录已审核，无需重复操作"}
            ok, err = _quarantine_write(row)
            row.status = "approved" if ok else "pending"
            row.reviewer = operator[:100]
            row.result = (("已放行" if ok else f"放行失败：{err}"))[:255]
            row.reviewed_at = datetime.now()
            db.commit()
        _audit(request, "quarantine_approve", f"隔离区#{qid}",
               ("审核通过并写入数据库" if ok else f"审核通过但写入失败：{err}"))
        if ok:
            _invalidate_cache()
            return {"code": 200, "msg": "已审核通过并写入目标表"}
        return {"code": 400, "msg": f"写入目标表失败：{err}"}

    @api_router.post("/quarantine/{qid}/reject")
    def api_quarantine_reject(request: Request, qid: int):
        if quarantine_model is None:
            return {"code": 400, "msg": "隔离区未启用"}
        operator = request.session.get("admin") or ""
        with db_scope() as db:
            row = db.query(quarantine_model).filter(quarantine_model.id == qid).first()
            if row is None:
                return {"code": 404, "msg": "记录不存在"}
            row.status = "rejected"
            row.reviewer = operator[:100]
            row.result = "已驳回"
            row.reviewed_at = datetime.now()
            db.commit()
        _audit(request, "quarantine_reject", f"隔离区#{qid}", "驳回该条数据，不入库")
        return {"code": 200, "msg": "已驳回"}

    @api_router.post("/quarantine/batch")
    def api_quarantine_batch(request: Request, payload: dict):
        """批量审核：ids 列表 + action=approve/reject"""
        if quarantine_model is None:
            return {"code": 400, "msg": "隔离区未启用"}
        action = str(payload.get("action") or "")
        ids = payload.get("ids") or []
        if action not in ("approve", "reject") or not isinstance(ids, list) or not ids:
            return {"code": 400, "msg": "参数不正确"}
        operator = request.session.get("admin") or ""
        done, failed = 0, 0
        with db_scope() as db:
            rows = db.query(quarantine_model).filter(
                quarantine_model.id.in_([int(i) for i in ids[:500]])).all()
            for row in rows:
                if (row.status or "pending") != "pending":
                    continue
                if action == "approve":
                    ok, _err = _quarantine_write(row)
                    row.status = "approved" if ok else "pending"
                    row.result = ("已放行" if ok else "放行失败")[:255]
                    failed += 0 if ok else 1
                    done += 1 if ok else 0
                else:
                    row.status = "rejected"
                    row.result = "已驳回"
                    done += 1
                row.reviewer = operator[:100]
                row.reviewed_at = datetime.now()
            db.commit()
        _audit(request, f"quarantine_{action}", "隔离区批量审核",
               f"处理 {done} 条，失败 {failed} 条")
        if action == "approve":
            _invalidate_cache()
        return {"code": 200, "msg": f"已处理 {done} 条" + (f"，失败 {failed} 条" if failed else ""),
                "done": done, "failed": failed}

    # ---------------- 后台账号（admin / viewer） ----------------
    def _user_to_dict(r):
        return {
            "id": r.id, "username": r.username, "role": r.role or "viewer",
            "enabled": bool(r.enabled),
            "last_login": r.last_login.strftime("%Y-%m-%d %H:%M:%S") if r.last_login else "",
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        }

    # ---------------- 密码复杂度（可选开关，默认不强制） ----------------
    def _pwd_policy_on():
        return str(get_setting("pwd_policy", "0")).lower() in ("1", "true", "yes", "on")

    def _check_pwd_policy(password):
        """返回错误提示；通过返回 None"""
        if not _pwd_policy_on():
            return None
        pwd = password or ""
        if not re.search(r"[a-z]", pwd) or not re.search(r"[A-Z]", pwd):
            return "密码需同时包含大写和小写字母"
        if not re.search(r"\d", pwd):
            return "密码需包含数字"
        if not re.search(r"[^A-Za-z0-9]", pwd):
            return "密码需包含符号（如 !@#$%）"
        return None

    @api_router.get("/password-policy")
    def api_pwd_policy():
        return {"code": 200, "data": {"enabled": _pwd_policy_on()}}

    @api_router.post("/password-policy")
    def api_pwd_policy_save(request: Request, payload: dict):
        enabled = str(payload.get("enabled", "")).lower() in ("1", "true", "yes", "on")
        before = {"enabled": _pwd_policy_on()}
        save_setting("pwd_policy", "1" if enabled else "0")
        _audit(request, "pwd_policy", "密码复杂度",
               "开启强密码校验" if enabled else "关闭强密码校验",
               before=before, after={"enabled": enabled})
        return {"code": 200, "msg": "已保存"}

    @api_router.get("/admin-users")
    def api_admin_users():
        if admin_user_model is None:
            return {"code": 200, "enabled": False, "data": []}
        with db_scope() as db:
            rows = db.query(admin_user_model).order_by(admin_user_model.id.asc()).all()
            return {"code": 200, "enabled": True, "data": [_user_to_dict(r) for r in rows]}

    @api_router.post("/admin-users")
    def api_admin_user_add(request: Request, payload: dict):
        if admin_user_model is None:
            return {"code": 400, "msg": "账号管理未启用"}
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        role = "admin" if str(payload.get("role") or "").lower() == "admin" else "viewer"
        if not re.fullmatch(r"[A-Za-z0-9_@.\-]{2,50}", username):
            return {"code": 400, "msg": "账号需 2-50 位，仅支持字母、数字、_ @ . -"}
        if username == admin_user:
            return {"code": 400, "msg": "该账号名与内置管理员冲突"}
        if len(password) < 8:
            return {"code": 400, "msg": "密码至少 8 位"}
        err = _check_pwd_policy(password)
        if err:
            return {"code": 400, "msg": err}
        with db_scope() as db:
            if db.query(admin_user_model).filter(admin_user_model.username == username).first():
                return {"code": 400, "msg": "账号已存在"}
            db.add(admin_user_model(username=username, password_hash=_make_password_hash(password),
                                    role=role, enabled=True))
            db.commit()
        _audit(request, "admin_user_add", username, f"新增后台账号（角色：{role}）",
               after={"username": username, "role": role})
        return {"code": 200, "msg": "账号已创建"}

    @api_router.post("/admin-users/{uid}/role")
    def api_admin_user_role(request: Request, uid: int, payload: dict):
        if admin_user_model is None:
            return {"code": 400, "msg": "账号管理未启用"}
        role = "admin" if str(payload.get("role") or "").lower() == "admin" else "viewer"
        with db_scope() as db:
            row = db.query(admin_user_model).filter(admin_user_model.id == uid).first()
            if row is None:
                return {"code": 404, "msg": "账号不存在"}
            before = row.role or "viewer"
            row.role = role
            db.commit()
            username = row.username
        _audit(request, "admin_user_role", username, f"角色 {before} -> {role}",
               before={"role": before}, after={"role": role})
        return {"code": 200, "msg": "角色已更新"}

    @api_router.post("/admin-users/{uid}/toggle")
    def api_admin_user_toggle(request: Request, uid: int):
        if admin_user_model is None:
            return {"code": 400, "msg": "账号管理未启用"}
        with db_scope() as db:
            row = db.query(admin_user_model).filter(admin_user_model.id == uid).first()
            if row is None:
                return {"code": 404, "msg": "账号不存在"}
            row.enabled = not bool(row.enabled)
            db.commit()
            username, enabled = row.username, bool(row.enabled)
        _audit(request, "admin_user_toggle", username, "启用" if enabled else "停用",
               before={"enabled": not enabled}, after={"enabled": enabled})
        return {"code": 200, "msg": "已启用" if enabled else "已停用"}

    @api_router.post("/admin-users/{uid}/password")
    def api_admin_user_password(request: Request, uid: int, payload: dict):
        if admin_user_model is None:
            return {"code": 400, "msg": "账号管理未启用"}
        password = str(payload.get("password") or "")
        if len(password) < 8:
            return {"code": 400, "msg": "密码至少 8 位"}
        err = _check_pwd_policy(password)
        if err:
            return {"code": 400, "msg": err}
        with db_scope() as db:
            row = db.query(admin_user_model).filter(admin_user_model.id == uid).first()
            if row is None:
                return {"code": 404, "msg": "账号不存在"}
            row.password_hash = _make_password_hash(password)
            db.commit()
            username = row.username
        _audit(request, "admin_user_password", username, "重置密码")
        return {"code": 200, "msg": "密码已重置"}

    @api_router.post("/admin-users/{uid}/delete")
    def api_admin_user_delete(request: Request, uid: int):
        if admin_user_model is None:
            return {"code": 400, "msg": "账号管理未启用"}
        if request.session.get("admin") and request.session.get("role") != "admin":
            return {"code": 403, "msg": "只读账号无权删除账号"}
        with db_scope() as db:
            row = db.query(admin_user_model).filter(admin_user_model.id == uid).first()
            if row is None:
                return {"code": 404, "msg": "账号不存在"}
            if row.username == request.session.get("admin"):
                return {"code": 400, "msg": "不能删除当前登录账号"}
            username = row.username
            db.delete(row)
            db.commit()
        _audit(request, "admin_user_delete", username, "删除后台账号")
        return {"code": 200, "msg": "账号已删除"}

    # ---------------- 限流设置 ----------------
    @api_router.get("/rate-settings")
    def api_rate_settings():
        try:
            per_minute = int(get_setting("rate_limit_per_minute", "60") or 60)
        except (TypeError, ValueError):
            per_minute = 60
        scope = get_setting("rate_limit_scope", "token") or "token"
        return {"code": 200, "data": {
            "enabled": get_setting("rate_limit_enabled", "0") == "1",
            "scope": "ip" if scope == "ip" else "token",
            "per_minute": per_minute,
        }}

    @api_router.post("/rate-settings")
    def api_rate_save(request: Request, payload: dict):
        enabled = "1" if str(payload.get("enabled")).lower() in ("1", "true", "yes") else "0"
        scope = "ip" if (payload.get("scope") or "").lower() == "ip" else "token"
        try:
            per_minute = int(payload.get("per_minute", 60))
        except (TypeError, ValueError):
            return {"code": 400, "msg": "每分钟次数必须是数字"}
        if per_minute < 1 or per_minute > 100000:
            return {"code": 400, "msg": "每分钟次数应在 1 - 100000 之间"}
        try:
            old_per = int(get_setting("rate_limit_per_minute", "60") or 60)
        except (TypeError, ValueError):
            old_per = 60
        before = {
            "enabled": get_setting("rate_limit_enabled", "0") == "1",
            "scope": "ip" if get_setting("rate_limit_scope", "token") == "ip" else "token",
            "per_minute": old_per,
        }
        save_setting("rate_limit_enabled", enabled)
        save_setting("rate_limit_scope", scope)
        save_setting("rate_limit_per_minute", per_minute)
        _audit(request, "rate_setting", "接口限流",
               f"启用={enabled}，维度={scope}，每分钟={per_minute}",
               before=before,
               after={"enabled": enabled == "1", "scope": scope, "per_minute": per_minute})
        return {"code": 200, "msg": "限流设置已保存并立即生效"}

    # ---------------- 单接口独立限流 ----------------
    @api_router.get("/rate-overrides")
    def api_rate_overrides():
        raw = get_setting("rate_limit_overrides", "")
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            data = {}
        items = []
        if isinstance(data, dict):
            for path, limit in data.items():
                try:
                    limit = int(limit)
                except (TypeError, ValueError):
                    continue
                path = str(path).strip()
                if path.startswith("/api/") and limit > 0:
                    items.append({"path": path, "per_minute": limit})
        items.sort(key=lambda x: x["path"])
        return {"code": 200, "data": items, "paths": sorted(ENDPOINT_NAMES)}

    @api_router.post("/rate-overrides")
    def api_rate_overrides_save(request: Request, payload: dict):
        raw = payload.get("overrides")
        if not isinstance(raw, dict):
            return {"code": 400, "msg": "参数格式错误"}
        clean = {}
        for path, limit in raw.items():
            path = str(path).strip()
            if not path.startswith("/api/"):
                return {"code": 400, "msg": f"路径必须以 /api/ 开头：{path}"}
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                return {"code": 400, "msg": f"{path} 的上限必须是数字"}
            if limit < 1 or limit > 100000:
                return {"code": 400, "msg": f"{path} 的上限应在 1 - 100000 之间"}
            clean[path] = limit
        try:
            before_data = json.loads(get_setting("rate_limit_overrides", "") or "{}")
        except (TypeError, ValueError):
            before_data = {}
        if not isinstance(before_data, dict):
            before_data = {}
        save_setting("rate_limit_overrides", json.dumps(clean, ensure_ascii=False))
        _audit(request, "rate_override", "单接口限流",
               "配置：" + ("，".join(f"{k} = {v}/分钟" for k, v in clean.items()) or "全部清空"),
               before=before_data, after=clean)
        return {"code": 200, "msg": "单接口限流已保存并立即生效"}

    # ---------------- 日志自动清理设置 ----------------
    @api_router.get("/log-settings")
    def api_log_settings():
        try:
            days = int(get_setting("log_clean_days", "30") or 30)
        except (TypeError, ValueError):
            days = 30
        try:
            max_rows = int(get_setting("log_max_rows", "0") or 0)
        except (TypeError, ValueError):
            max_rows = 0
        try:
            backup_days = int(get_setting(
                "backup_clean_days", str(config.BACKUP_KEEP_DAYS))
                or config.BACKUP_KEEP_DAYS)
        except (TypeError, ValueError):
            backup_days = config.BACKUP_KEEP_DAYS
        return {"code": 200, "data": {
            "enabled": get_setting("log_clean_enabled", "0") == "1",
            "days": days,
            "max_rows": max_rows,
            "last_clean": get_setting("log_last_clean", ""),
            "last_deleted": get_setting("log_last_deleted", ""),
            "last_capacity_clean": get_setting("log_last_capacity_clean", ""),
            "last_capacity_deleted": get_setting("log_last_capacity_deleted", ""),
            "backup_clean_enabled": get_setting("backup_clean_enabled", "0") == "1",
            "backup_clean_days": backup_days,
            "backup_clean_dir": get_setting("backup_clean_dir", ""),
            "backup_last_clean": get_setting("backup_last_clean", ""),
            "backup_last_deleted": get_setting("backup_last_deleted", ""),
        }}

    @api_router.post("/log-settings")
    def api_log_settings_save(request: Request, payload: dict):
        enabled = "1" if str(payload.get("enabled")).lower() in ("1", "true", "yes") else "0"
        try:
            days = int(payload.get("days", 30))
        except (TypeError, ValueError):
            return {"code": 400, "msg": "保留天数必须是数字"}
        if days < 1 or days > 3650:
            return {"code": 400, "msg": "保留天数应在 1 - 3650 之间"}
        try:
            max_rows = int(payload.get("max_rows", 0) or 0)
        except (TypeError, ValueError):
            return {"code": 400, "msg": "最大日志条数必须是数字"}
        if max_rows < 0 or max_rows > 10000000:
            return {"code": 400, "msg": "最大日志条数应在 0 - 10000000 之间（0 表示不限制）"}
        backup_enabled = "1" if str(payload.get("backup_clean_enabled", "0")).lower() in (
            "1", "true", "yes", "on") else "0"
        try:
            backup_days = int(payload.get("backup_clean_days", config.BACKUP_KEEP_DAYS)
                              or config.BACKUP_KEEP_DAYS)
        except (TypeError, ValueError):
            return {"code": 400, "msg": "备份保留天数必须是数字"}
        if backup_days < 1 or backup_days > 3650:
            return {"code": 400, "msg": "备份保留天数应在 1 - 3650 之间"}
        backup_dir = str(payload.get("backup_clean_dir") or "").strip()
        if backup_dir:
            backup_dir = os.path.abspath(os.path.expanduser(backup_dir))
            if not os.path.isdir(backup_dir):
                return {"code": 400, "msg": "备份目录不存在或不可访问"}
        try:
            old_days = int(get_setting("log_clean_days", "30") or 30)
        except (TypeError, ValueError):
            old_days = 30
        try:
            old_rows = int(get_setting("log_max_rows", "0") or 0)
        except (TypeError, ValueError):
            old_rows = 0
        before = {
            "enabled": get_setting("log_clean_enabled", "0") == "1",
            "days": old_days,
            "max_rows": old_rows,
            "backup_clean_enabled": get_setting("backup_clean_enabled", "0") == "1",
            "backup_clean_days": get_setting("backup_clean_days", ""),
            "backup_clean_dir": get_setting("backup_clean_dir", ""),
        }
        save_setting("log_clean_enabled", enabled)
        save_setting("log_clean_days", days)
        save_setting("log_max_rows", max_rows)
        save_setting("backup_clean_enabled", backup_enabled)
        save_setting("backup_clean_days", backup_days)
        save_setting("backup_clean_dir", backup_dir)
        _audit(request, "log_setting", "日志/备份自动清理",
               f"日志启用={enabled}，保留={days} 天，容量上限={max_rows or '不限制'}；"
               f"备份清理启用={backup_enabled}，保留={backup_days} 天，"
               f"目录={backup_dir or '默认'}",
               before=before,
               after={"enabled": enabled == "1", "days": days, "max_rows": max_rows,
                      "backup_clean_enabled": backup_enabled == "1",
                      "backup_clean_days": backup_days,
                      "backup_clean_dir": backup_dir})
        return {"code": 200, "msg": "自动清理设置已保存"}

    # ---------------- 批量导入记录 ----------------
    @api_router.get("/import-logs")
    def api_import_logs(page: int = Query(1, ge=1), size: int = Query(10, ge=1, le=100)):
        if import_log_model is None:
            return {"code": 200, "total": 0, "page": page, "size": size, "data": []}
        with db_scope() as db:
            q = db.query(import_log_model)
            total = q.count()
            rows = q.order_by(import_log_model.id.desc()).offset((page - 1) * size).limit(size).all()
            return {"code": 200, "total": total, "page": page, "size": size,
                    "data": [{
                        "id": r.id, "table_key": r.table_key or "",
                        "physical_table": r.physical_table or "",
                        "filename": r.filename or "",
                        "total": r.total or 0, "inserted": r.inserted or 0,
                        "skipped": r.skipped or 0, "truncated": r.truncated or 0,
                        "status": r.status or "", "message": r.message or "",
                        "operator": r.operator or "",
                        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                    } for r in rows]}

    @api_router.get("/import-logs/{log_id}")
    def api_import_log_detail(log_id: int):
        if import_log_model is None:
            return {"code": 404, "msg": "功能未启用"}
        with db_scope() as db:
            r = db.query(import_log_model).filter(import_log_model.id == log_id).first()
            if not r:
                return {"code": 404, "msg": "记录不存在"}
            return {"code": 200, "data": {
                "id": r.id, "table_key": r.table_key or "",
                "physical_table": r.physical_table or "", "filename": r.filename or "",
                "total": r.total or 0, "inserted": r.inserted or 0,
                "skipped": r.skipped or 0, "truncated": r.truncated or 0,
                "status": r.status or "", "message": r.message or "",
                "detail": r.detail or "", "operator": r.operator or "",
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
            }}

    # ---------------- 各表数据统计 ----------------
    @api_router.get("/tables")
    def api_tables():
        with db_scope() as db:
            comment_map = {r.TABLE_NAME: (r.TABLE_COMMENT or "暂无注释")
                           for r in db.execute(text(
                               "SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES "
                               "WHERE TABLE_SCHEMA = DATABASE()")).all()}
            data = []
            for key, model in table_map.items():
                columns = [{
                    "name": c.name, "type": str(c.type), "comment": c.comment or "",
                    "required": False,
                } for c in _import_columns(model)]
                data.append({
                    "key": key,
                    "table": model.__tablename__,
                    "comment": comment_map.get(model.__tablename__, "暂无注释"),
                    "count": db.query(model).count(),
                    "columns": columns,
                })
            data.sort(key=lambda x: x["count"], reverse=True)
            return {"code": 200, "data": data}

    # ---------------- 数据质检 ----------------
    @api_router.get("/quality")
    def api_quality():
        """扫描各数据表：空内容、超长文本、重复数据，生成质检报告"""
        import sqlalchemy as sa
        max_len = 5000
        report, total_issues = [], 0
        for key, model in table_map.items():
            cols = [c for c in model.__table__.columns if c.name not in META_COLUMNS]
            text_cols = [c for c in cols if isinstance(c.type, (sa.String, sa.Text))]
            with db_scope() as db:
                rows = db.query(model).count()
                item = {"key": key, "table": model.__tablename__, "rows": rows,
                        "empty": [], "overlong": [], "dup": None, "issues": 0}
                if rows:
                    for col in text_cols:
                        cnt = (db.query(func.count(model.__table__.columns[col.name]))
                               .filter(or_(col.is_(None), col == "")).scalar() or 0)
                        if cnt:
                            samples = [r[0] for r in db.query(model.__table__.columns["id"])
                                       .filter(or_(col.is_(None), col == ""))
                                       .limit(5).all()] if "id" in model.__table__.columns else []
                            item["empty"].append({"field": col.name, "count": cnt, "ids": samples})
                            item["issues"] += cnt
                        over = (db.query(func.count(model.__table__.columns[col.name]))
                                .filter(func.char_length(col) > max_len).scalar() or 0)
                        if over:
                            samples = [r[0] for r in db.query(model.__table__.columns["id"])
                                       .filter(func.char_length(col) > max_len)
                                       .limit(5).all()] if "id" in model.__table__.columns else []
                            item["overlong"].append({"field": col.name, "count": over,
                                                     "max_length": max_len, "ids": samples})
                            item["issues"] += over
                    dup_fields = DEDUP_FIELDS.get(key)
                    if dup_fields:
                        dup_cols = [model.__table__.columns[f] for f in dup_fields
                                   if f in model.__table__.columns]
                        if dup_cols:
                            all_dup = (db.query(*dup_cols, func.count().label("n"))
                                       .group_by(*dup_cols).having(func.count() > 1)
                                       .order_by(func.count().desc()).all())
                            extra = sum(int(g[-1]) - 1 for g in all_dup)
                            samples = [{"values": [str(x)[:80] for x in g[:-1]],
                                        "count": int(g[-1])} for g in all_dup[:5]]
                            if extra:
                                item["dup"] = {"fields": dup_fields, "extra": extra,
                                               "samples": samples}
                                item["issues"] += extra
            if item["issues"]:
                total_issues += item["issues"]
            report.append(item)
        report.sort(key=lambda x: x["issues"], reverse=True)
        return {"code": 200, "data": report, "summary": {
            "tables": len(report),
            "rows": sum(x["rows"] for x in report),
            "issues": total_issues,
            "problem_tables": len([x for x in report if x["issues"]]),
        }}

    # ---------------- 数据管理：分页预览 / 重复数据 / 离线数据 ----------------
    DATA_FILE_DIR = os.path.join(BASE_DIR, "data")

    def _text_columns(model):
        import sqlalchemy as sa
        return [c for c in _import_columns(model)
                if isinstance(c.type, (sa.String, sa.Text))]

    def _row_to_dict(row, model):
        return {c.name: (row.__dict__.get(c.name) if not isinstance(row.__dict__.get(c.name),
                datetime) else row.__dict__.get(c.name).strftime("%Y-%m-%d %H:%M:%S"))
                for c in model.__table__.columns if c.name != "is_deleted"}

    @api_router.get("/data/rows")
    def api_data_rows(table_key: str, keyword: str = "", field: str = "",
                      mode: str = "all", page: int = Query(1, ge=1),
                      size: int = Query(20, ge=1, le=200)):
        """数据分页预览：支持关键词筛选与脏数据模式（empty 空内容 / overlong 超长 / dup 重复）"""
        import sqlalchemy as sa
        if table_key not in table_map:
            return {"code": 404, "msg": "数据表不存在"}
        model = table_map[table_key]
        texts = _text_columns(model)
        with db_scope() as db:
            q = db.query(model)
            if field and field in model.__table__.columns:
                q = q.filter(getattr(model, field).isnot(None))
            if keyword:
                likes = [getattr(model, c.name).like(f"%{keyword}%") for c in texts]
                if likes:
                    q = q.filter(or_(*likes))
            if mode == "empty" and texts:
                q = q.filter(or_(*[or_(c.is_(None), c == "") for c in texts]))
            elif mode == "overlong" and texts:
                q = q.filter(or_(*[func.char_length(c) > 5000 for c in texts]))
            elif mode == "dup":
                fields = DEDUP_FIELDS.get(table_key) or ()
                cols = [model.__table__.columns[f] for f in fields
                        if f in model.__table__.columns]
                if cols:
                    sub = (db.query(*cols).group_by(*cols).having(func.count() > 1).subquery())
                    cond = and_(*[getattr(model, cols[i].name) == sub.c[cols[i].name]
                                 for i in range(len(cols))])
                    q = q.filter(cond)
            total = q.count()
            rows = (q.order_by(model.id.desc()).offset((page - 1) * size)
                    .limit(size).all())
            return {"code": 200, "data": [_row_to_dict(r, model) for r in rows],
                    "total": total, "page": page, "size": size,
                    "columns": [{"name": c.name, "comment": c.comment or c.name}
                                for c in _import_columns(model)]}

    @api_router.post("/data/rows/update")
    def api_data_row_update(request: Request, payload: dict):
        """在线修改单条数据"""
        table_key = str(payload.get("table_key") or "")
        if table_key not in table_map:
            return {"code": 404, "msg": "数据表不存在"}
        model = table_map[table_key]
        try:
            rid = int(payload.get("id"))
        except (TypeError, ValueError):
            return {"code": 400, "msg": "缺少有效的记录 ID"}
        values = payload.get("values") or {}
        if not isinstance(values, dict) or not values:
            return {"code": 400, "msg": "没有要修改的字段"}
        with db_scope() as db:
            row = db.query(model).filter(model.id == rid).first()
            if row is None:
                return {"code": 404, "msg": "记录不存在"}
            for name, raw in values.items():
                col = model.__table__.columns.get(name)
                if col is None or name in ("id", "created_at", "dedup_md5", "is_deleted"):
                    continue
                try:
                    val, _trunc = _coerce(col, raw)
                except ValueError as e:
                    return {"code": 400, "msg": f"字段 {name} 不合法：{e}"}
                setattr(row, name, val)
            db.commit()
        _audit(request, "data_row_update", f"{table_key}#{rid}", "在线修改数据",
               after={"table": table_key, "id": rid, "values": values})
        return {"code": 200, "msg": "已保存"}

    @api_router.get("/data/duplicates")
    def api_data_duplicates(table_key: str, page: int = Query(1, ge=1),
                            size: int = Query(20, ge=1, le=100)):
        """按查重字段分组，列出重复数据（每组保留一行，其余可删除）"""
        import sqlalchemy as sa
        if table_key not in table_map:
            return {"code": 404, "msg": "数据表不存在"}
        fields = DEDUP_FIELDS.get(table_key)
        model = table_map[table_key]
        if not fields:
            return {"code": 200, "data": [], "total": 0, "fields": []}
        cols = [model.__table__.columns[f] for f in fields if f in model.__table__.columns]
        with db_scope() as db:
            groups = (db.query(*cols, func.count().label("n"))
                      .group_by(*cols).having(func.count() > 1)
                      .order_by(func.count().desc()).all())
            total = sum(int(g[-1]) - 1 for g in groups)
            sliced = groups[(page - 1) * size: page * size]
            data = []
            for g in sliced:
                cond = and_(*[cols[i] == g[i] for i in range(len(cols))])
                ids = [r[0] for r in (db.query(model.id).filter(cond)
                                      .order_by(model.id.asc()).all())]
                data.append({
                    "values": [str(x)[:100] for x in g[:-1]],
                    "count": int(g[-1]),
                    "keep_id": ids[0] if ids else None,
                    "delete_ids": ids[1:],
                })
            return {"code": 200, "data": data, "total": total, "fields": list(fields)}

    @api_router.post("/data/duplicates/delete")
    def api_data_duplicates_delete(request: Request, payload: dict):
        """批量删除重复数据（每组仅保留 id 最小的一条）"""
        import sqlalchemy as sa
        table_key = str(payload.get("table_key") or "")
        if table_key not in table_map:
            return {"code": 404, "msg": "数据表不存在"}
        fields = DEDUP_FIELDS.get(table_key)
        model = table_map[table_key]
        if not fields:
            return {"code": 400, "msg": "该表未配置查重字段"}
        cols = [model.__table__.columns[f] for f in fields if f in model.__table__.columns]
        with db_scope() as db:
            groups = (db.query(*cols).group_by(*cols).having(func.count() > 1).all())
            deleted = 0
            for g in groups:
                cond = and_(*[cols[i] == g[i] for i in range(len(cols))])
                ids = [r[0] for r in (db.query(model.id).filter(cond)
                                      .order_by(model.id.asc()).all())]
                if len(ids) > 1:
                    deleted += db.query(model).filter(model.id.in_(ids[1:])).delete(
                        synchronize_session=False)
            db.commit()
        _audit(request, "data_dup_delete", table_key, f"删除重复数据 {deleted} 行",
               after={"table": table_key, "deleted": deleted})
        return {"code": 200, "msg": f"已删除 {deleted} 行重复数据", "deleted": deleted}

    def _safe_data_file(name):
        """离线数据文件名安全校验：仅允许 data 目录下的 .json"""
        name = os.path.basename(str(name or ""))
        if not name.lower().endswith(".json") or name.startswith("."):
            return None
        return name

    @api_router.get("/data/files")
    def api_data_files():
        """离线数据目录文件列表（文件名、大小、修改时间）"""
        if not os.path.isdir(DATA_FILE_DIR):
            return {"code": 200, "data": []}
        files = []
        for name in sorted(os.listdir(DATA_FILE_DIR)):
            path = os.path.join(DATA_FILE_DIR, name)
            if not os.path.isfile(path) or not name.lower().endswith(".json"):
                continue
            st = os.stat(path)
            files.append({
                "name": name, "size": st.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            })
        return {"code": 200, "data": files}

    @api_router.post("/data/files/upload")
    async def api_data_files_upload(request: Request, file: UploadFile = File(...),
                                    overwrite: str = Form("0")):
        """上传替换离线 json 数据源（覆盖前自动备份原文件为 .bak）"""
        name = _safe_data_file(file.filename)
        if not name:
            return {"code": 400, "msg": "仅支持上传 .json 文件"}
        os.makedirs(DATA_FILE_DIR, exist_ok=True)
        raw = await file.read()
        if len(raw) > 50 * 1024 * 1024:
            return {"code": 400, "msg": "文件不能超过 50MB"}
        try:
            json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            return {"code": 400, "msg": f"不是合法的 JSON 文件：{e}"}
        path = os.path.join(DATA_FILE_DIR, name)
        exist = os.path.exists(path)
        if exist and str(overwrite).lower() not in ("1", "true", "yes", "on"):
            return {"code": 409, "msg": "同名文件已存在，请勾选允许覆盖"}
        if exist:
            shutil.copy2(path, path + ".bak")
        with open(path, "wb") as fh:
            fh.write(raw)
        _audit(request, "data_file_upload", name, f"上传离线数据文件，{len(raw)} 字节")
        return {"code": 200, "msg": "已上传" + ("（原文件已备份为 .bak）" if exist else "")}

    @api_router.post("/data/files/delete")
    def api_data_files_delete(request: Request, payload: dict):
        name = _safe_data_file(payload.get("name"))
        if not name:
            return {"code": 400, "msg": "文件名不合法"}
        path = os.path.join(DATA_FILE_DIR, name)
        if not os.path.exists(path):
            return {"code": 404, "msg": "文件不存在"}
        os.remove(path)
        _audit(request, "data_file_delete", name, "删除离线数据文件")
        return {"code": 200, "msg": "已删除"}

    # ---------------- 接口管理 ----------------
    def _query_template(route):
        """读取接口函数签名，自动生成查询参数模板（跳过 Header/Request 等依赖）"""
        try:
            sig = inspect.signature(route.endpoint)
        except (TypeError, ValueError):
            return ""
        parts = []
        for name, p in sig.parameters.items():
            if "{" + name + "}" in route.path:
                continue
            default = p.default
            cname = default.__class__.__name__
            if cname in ("Header", "Depends", "Request", "Session", "Path", "Body",
                         "File", "Form", "UploadFile"):
                continue
            ann = str(p.annotation)
            if "Request" in ann or "Session" in ann or "UploadFile" in ann:
                continue
            if cname == "Query":
                val = getattr(default, "default", None)
            else:
                val = default
            if (val is inspect._empty or val is None or val is Ellipsis
                    or isinstance(val, type)
                    or val.__class__.__name__ in ("PydanticUndefinedType", "UndefinedType")):
                parts.append(f"{name}=")
            else:
                parts.append(f"{name}={val}")
        return "&".join(parts)

    def _response_model_mock(route):
        """按 response_model 的 JSON Schema 生成递归示例，供在线测试沙盒使用"""
        model = getattr(route, "response_model", None)
        if model is None:
            return None
        try:
            from pydantic import TypeAdapter
            schema = TypeAdapter(model).json_schema()
        except Exception:
            try:
                schema = model.model_json_schema() if hasattr(model, "model_json_schema") else model.schema()
            except Exception:
                return None

        def sample(node, root, depth=0):
            if not isinstance(node, dict) or depth > 8:
                return None
            if "$ref" in node:
                ref = str(node["$ref"])
                if not ref.startswith("#/"):
                    return None
                target = root
                for part in ref[2:].split("/"):
                    target = target.get(part.replace("~1", "/").replace("~0", "~"), {})
                return sample(target, root, depth + 1)
            if "default" in node:
                return node["default"]
            if "example" in node:
                return node["example"]
            if node.get("examples"):
                return node["examples"][0]
            if node.get("enum"):
                return node["enum"][0]
            for union in ("anyOf", "oneOf"):
                choices = [x for x in node.get(union, []) if x.get("type") != "null"]
                if choices:
                    return sample(choices[0], root, depth + 1)
            kind = node.get("type")
            if isinstance(kind, list):
                kind = next((x for x in kind if x != "null"), "null")
            if kind == "object" or "properties" in node:
                return {key: sample(value, root, depth + 1)
                        for key, value in node.get("properties", {}).items()}
            if kind == "array":
                return [sample(node.get("items", {}), root, depth + 1)]
            if kind == "string":
                return {"date-time": "2026-01-01T12:00:00", "date": "2026-01-01",
                        "email": "user@example.com", "uri": "https://example.com",
                        "uuid": "00000000-0000-0000-0000-000000000000"}.get(node.get("format"), "string")
            if kind == "integer":
                return 1
            if kind == "number":
                return 1.0
            if kind == "boolean":
                return True
            if kind == "null":
                return None
            return "string"

        return sample(schema, schema)

    @api_router.get("/endpoints")
    def api_endpoints():
        items = []
        for route in app.routes:
            if (not isinstance(route, APIRoute) or not route.path.startswith("/api/")
                    or route.path.startswith("/api/portal/")):
                continue
            for method in sorted(m for m in route.methods if m not in ("HEAD", "OPTIONS")):
                items.append({
                    "method": method,
                    "path": route.path,
                    "summary": route.summary or route.name,
                    "query": _query_template(route),
                    "response_mock": _response_model_mock(route),
                    "enabled": switches.get(route.path, True) is not False,
                    "log_enabled": (log_switches or {}).get(route.path, True) is not False,
                    "cache_ttl": int((cache_ttls or {}).get(route.path, 0) or 0),
                })
        items.sort(key=lambda x: x["path"])
        return {"code": 200, "data": items}

    @api_router.post("/endpoints/toggle")
    def api_endpoints_toggle(request: Request, payload: dict):
        path = (payload.get("path") or "").strip()
        if not path.startswith("/api/"):
            return {"code": 400, "msg": "路径不合法"}
        before = {"enabled": switches.get(path, True) is not False,
                  "log_enabled": (log_switches or {}).get(path, True) is not False,
                  "cache_ttl": int((cache_ttls or {}).get(path, 0) or 0)}

        def _to_bool(v, default=True):
            if v is None:
                return default
            return str(v).strip().lower() in ("1", "true", "yes", "on")

        with db_scope() as db:
            row = db.query(switch_model).filter(switch_model.path == path).first()
            if row is None:
                row = switch_model(path=path, enabled=True, log_enabled=True, cache_ttl=0)
                db.add(row)
            if "enabled" in payload:
                row.enabled = _to_bool(payload.get("enabled"), True)
            if "log_enabled" in payload:
                row.log_enabled = _to_bool(payload.get("log_enabled"), True)
            if "cache_ttl" in payload:
                try:
                    row.cache_ttl = max(0, min(86400, int(payload.get("cache_ttl") or 0)))
                except (TypeError, ValueError):
                    return {"code": 400, "msg": "缓存时长应为 0-86400 的整数"}
            db.commit()
            enabled = bool(row.enabled)
            log_enabled = True if row.log_enabled is None else bool(row.log_enabled)
            cache_ttl = int(row.cache_ttl or 0)
        switches[path] = enabled
        if log_switches is not None:
            log_switches[path] = log_enabled
        if cache_ttls is not None:
            cache_ttls[path] = cache_ttl
            _invalidate_cache()
        after = {"enabled": enabled, "log_enabled": log_enabled, "cache_ttl": cache_ttl}
        detail = "启用接口" if enabled else "禁用接口"
        if before["log_enabled"] != log_enabled:
            detail += f"，日志记录{'开启' if log_enabled else '关闭'}"
        if before["cache_ttl"] != cache_ttl:
            detail += f"，缓存 {cache_ttl} 秒"
        _audit(request, "endpoint_toggle", path, detail, before=before, after=after)
        return {"code": 200, "path": path, "enabled": enabled,
                "log_enabled": log_enabled, "cache_ttl": cache_ttl}

    # ---------------- 脚本管理（采集/同步/初始化/维护） ----------------
    global SCRIPT_PID_HOOK

    def _persist_script_pid(key, run_id, pid):
        """脚本进程启动后立即把 pid 写入 script_run，供守护线程清理孤儿进程"""
        if script_run_model is None or not run_id:
            return
        try:
            with db_scope() as db:
                db.query(script_run_model).filter(script_run_model.id == run_id).update(
                    {"pid": int(pid)}, synchronize_session=False)
                db.commit()
        except Exception:
            pass

    SCRIPT_PID_HOOK = _persist_script_pid

    def _persist_script_run(key, run_id):
        """把脚本最终状态、耗时、退出码、输出尾部写入数据库"""
        if script_run_model is None or not run_id:
            return
        task = SCRIPT_TASKS.get(key) or {}
        started = task.get("started")
        finished = task.get("finished") or time.time()
        try:
            with db_scope() as db:
                row = db.query(script_run_model).filter(script_run_model.id == run_id).first()
                if not row:
                    return
                row.state = task.get("state") or "failed"
                row.returncode = task.get("returncode")
                row.duration = int(finished - started) if started else 0
                row.finished_at = datetime.fromtimestamp(finished)
                row.output = (task.get("output") or "")[-4000:]
                db.commit()
        except Exception:
            pass
        if task.get("state") == "failed":
            try:
                from main import notify_script_failed
                notify_script_failed(key, task.get("returncode"), task.get("output") or "")
            except Exception:
                pass

    def _script_worker(key, args, run_id, on_finish=None):
        """运行脚本，结束后落库运行记录（耗时、退出码、状态）"""
        try:
            _run_script(key, args, _invalidate_cache)
        finally:
            _persist_script_run(key, run_id)
            if on_finish is not None:
                try:
                    on_finish(SCRIPT_TASKS.get(key) or {})
                except Exception:
                    pass

    def _self_heal_script_runner(key, raw_args):
        """供主动巡检安全拉起脚本；复用参数白名单、单实例限制和运行记录"""
        if key not in SCRIPTS:
            return False, f"脚本 {key} 不在允许列表"
        if key == "reset":
            return False, "重置数据库不支持自动执行，请人工双重确认"
        current = SCRIPT_TASKS.get(key) or {}
        if current.get("state") == "running":
            return False, f"{SCRIPTS[key]['name']} 正在运行"
        try:
            args = _valid_script_args(key, raw_args)
        except ValueError as err:
            return False, f"参数校验失败：{err}"
        run_id = None
        if script_run_model is not None:
            try:
                with db_scope() as db:
                    row = script_run_model(
                        script_key=key, args=" ".join(args), state="running",
                        started_at=datetime.now(), operator="system-self-heal",
                        host=dist_lock.host_id())
                    db.add(row)
                    db.commit()
                    run_id = row.id
            except Exception as err:
                return False, f"创建自愈运行记录失败：{str(err)[:100]}"
        SCRIPT_TASKS[key] = {
            "state": "running", "output": "", "started": time.time(), "finished": None,
            "returncode": None, "args": " ".join(args), "proc": None, "pid": None,
            "run_id": run_id,
        }
        threading.Thread(target=_script_worker, args=(key, args, run_id), daemon=True).start()
        return True, f"{SCRIPTS[key]['name']}（参数：{' '.join(args) or '无'}）"

    __import__("main").SCRIPT_SELF_HEAL_RUNNER = _self_heal_script_runner

    # ---------------- 脚本孤儿进程守护 ----------------
    def _recover_script_runs():
        """服务启动时清理本实例上次残留的运行记录与子进程

        多实例部署时只处理 host 为空或属于本机的记录，避免杀掉另一台机器
        正在运行的脚本进程。其它实例的残留由该实例启动时自行清理。
        """
        if script_run_model is None:
            return
        me = dist_lock.host_id()
        try:
            with db_scope() as db:
                rows = db.query(script_run_model).filter(
                    script_run_model.state == "running").all()
                for row in rows:
                    if (row.host or "") not in ("", me):
                        continue
                    process_state = _script_pid_state(row.pid) if row.pid else "stopped"
                    if process_state in ("managed", "unknown"):
                        # 同机其他服务实例仍在管理该进程，不属于本次启动的残留
                        continue
                    if process_state == "orphan":
                        _kill_pid(row.pid)
                        note = "[启动自检] 服务重启，残留子进程已清理"
                    else:
                        note = "[启动自检] 脚本进程已退出，未完成的运行记录已关闭"
                    row.host = me
                    row.state = "failed"
                    row.finished_at = datetime.now()
                    row.output = ((row.output or "") + "\n" + note)[-4000:]
                db.commit()
        except Exception:
            pass

    def _orphan_guard_once():
        """校验一次进程状态：超时强杀，主进程已停止但子进程仍在时清理"""
        now = time.time()
        timeout_sec = max(60, int(getattr(config, "SCRIPT_TIMEOUT_MINUTES", 120)) * 60)
        # 内存态：状态已不是 running 但进程仍在
        for key, task in list(SCRIPT_TASKS.items()):
            proc = task.get("proc")
            if proc is not None and task.get("state") != "running":
                try:
                    proc.kill()
                except Exception:
                    pass
        if script_run_model is None:
            return
        changed = False
        me = dist_lock.host_id()
        with db_scope() as db:
            rows = db.query(script_run_model).filter(
                script_run_model.state == "running").all()
            for row in rows:
                # 只清理本机记录：其它实例的进程由那台机器的守护线程负责
                if (row.host or "") not in ("", me):
                    continue
                key = row.script_key or ""
                task = SCRIPT_TASKS.get(key) or {}
                started = row.started_at.timestamp() if row.started_at else now
                in_process = (task.get("state") == "running" and task.get("run_id") == row.id)
                if in_process and now - started <= timeout_sec:
                    continue
                if in_process:
                    _kill_pid(task.get("pid") or row.pid)
                    task["state"] = "failed"
                    task["finished"] = now
                    task["proc"] = None
                    task["output"] = ((task.get("output") or "") +
                                      "\n[守护线程] 运行超时，已强制结束进程")[-SCRIPT_OUTPUT_LIMIT:]
                    note = "[守护线程] 运行超时，已强制结束进程"
                else:
                    # 非本进程任务必须核验实际进程归属：热重载父进程、其他服务实例
                    # 都没有这里的 SCRIPT_TASKS，但其采集进程可能仍在正常运行。
                    pid = row.pid
                    process_state = _script_pid_state(pid) if pid else "stopped"
                    if process_state in ("managed", "unknown"):
                        continue
                    if process_state == "orphan":
                        _kill_pid(pid)
                        note = "[守护线程] 主进程已停止，残留子进程已清理"
                    else:
                        note = "[守护线程] 脚本进程已退出，运行记录未正常回写"
                row.state = "failed"
                row.finished_at = datetime.now()
                row.duration = int(max(0, now - started))
                row.output = ((row.output or "") + "\n" + note)[-4000:]
                changed = True
            if changed:
                db.commit()

    def _orphan_guard_loop():
        # 分布式锁（按实例维度）：同一 HOST_ID 同时只允许一个守护线程巡检；
        # 叠加 host 过滤后，跨机器不会误杀彼此的进程
        ttl = max(60, int(getattr(config, "SCRIPT_ORPHAN_CHECK", 30)) * 2)
        lock_name = f"orphan-guard:{dist_lock.host_id()}"
        while True:
            time.sleep(max(10, int(getattr(config, "SCRIPT_ORPHAN_CHECK", 30))))
            handle = dist_lock.acquire(lock_name, ttl=ttl)
            if not handle:
                continue
            try:
                _orphan_guard_once()
            except Exception:
                pass
            finally:
                handle.release()

    # 脚本为复用同步函数会导入 main；清理工作只由真正的后台服务执行
    if os.environ.get("API_SCRIPT_CHILD") != "1":
        _recover_script_runs()
        if script_run_model is not None:
            threading.Thread(target=_orphan_guard_loop, daemon=True).start()

    # ---------------- 可视化定时任务 ----------------
    SCHEDULE_CHECK = 20   # 调度线程检查间隔（秒）

    def _schedule_name(task_type, key):
        """定时任务展示名称：备份 / 环境自检 / 脚本"""
        if task_type == "backup":
            return "数据库备份"
        if task_type == "envcheck" or key == "envcheck":
            return "环境自检"
        return (SCRIPTS.get(key) or {}).get("name", key or "")

    def _schedule_to_dict(r):
        task_type = (getattr(r, "task_type", "") or "script")
        return {
            "id": r.id,
            "script_key": r.script_key or "",
            "task_type": task_type,
            "name": _schedule_name(task_type, r.script_key or ""),
            "args": r.args or "",
            "cron_expr": getattr(r, "cron_expr", "") or "",
            "backup_dir": getattr(r, "backup_dir", "") or "",
            "interval_minutes": int(r.interval_minutes or 0),
            "enabled": bool(r.enabled),
            "last_run": r.last_run.strftime("%Y-%m-%d %H:%M:%S") if r.last_run else "",
            "last_state": r.last_state or "",
            "last_duration": int(r.last_duration or 0),
            "next_run": r.next_run.strftime("%Y-%m-%d %H:%M:%S") if r.next_run else "",
            "operator": r.operator or "",
        }

    def _calc_next_run(row, now):
        """按 cron 表达式或间隔分钟计算下次执行时间"""
        cron = (getattr(row, "cron_expr", "") or "").strip()
        if cron:
            nxt = _next_cron(cron, now)
            if nxt is not None:
                return nxt
        return now + timedelta(minutes=max(1, int(row.interval_minutes or 60)))

    def _run_record_start(schedule_id, name, task_type):
        """创建一条定时任务执行记录，返回记录 ID"""
        if schedule_run_model is None:
            return None
        try:
            with db_scope() as db:
                rec = schedule_run_model(schedule_id=schedule_id, name=str(name or "")[:100],
                                         task_type=task_type, state="running",
                                         started_at=datetime.now())
                db.add(rec)
                db.commit()
                return rec.id
        except Exception:
            return None

    def _run_record_finish(run_id, state, output="", message="", started=None):
        """回写定时任务执行记录结果"""
        if schedule_run_model is None or run_id is None:
            return
        try:
            with db_scope() as db:
                rec = db.query(schedule_run_model).filter(
                    schedule_run_model.id == run_id).first()
                if rec is not None:
                    rec.state = state
                    rec.duration = int(time.time() - (started or time.time()))
                    rec.output = str(output or "")[-4000:]
                    rec.message = str(message or "")[:2000]
                    rec.finished_at = datetime.now()
                    db.commit()
        except Exception:
            pass

    def _schedule_run(schedule_id):
        """执行一条到期的定时任务；先占用下次执行时间，避免调度线程重复触发"""
        if script_schedule_model is None:
            return
        now = datetime.now()
        with db_scope() as db:
            row = db.query(script_schedule_model).filter(
                script_schedule_model.id == schedule_id).first()
            if not row or not row.enabled:
                return
            task_type = (getattr(row, "task_type", "") or "script")
            key = row.script_key or ""
            args_raw = row.args or ""
            backup_dir = getattr(row, "backup_dir", "") or ""
            row.next_run = _calc_next_run(row, now)
            db.commit()
        started = time.time()
        name = _schedule_name(task_type, key)
        run_rec_id = _run_record_start(schedule_id, name, task_type)
        if task_type == "backup":
            _run_schedule_backup(schedule_id, backup_dir, run_rec_id, started)
            return
        if task_type == "envcheck" or key == "envcheck":
            _run_schedule_envcheck(schedule_id, run_rec_id, started)
            return
        if key == "reset":
            _run_record_finish(run_rec_id, "failed", "",
                               "重置数据库不支持定时执行，已拦截", started)
            return
        meta = SCRIPTS.get(key)
        if not meta:
            _run_record_finish(run_rec_id, "failed", "", "脚本不存在", started)
            return
        if (SCRIPT_TASKS.get(key) or {}).get("state") == "running":
            # 同一脚本正在运行，本次跳过，等待下个周期
            _run_record_finish(run_rec_id, "failed", "", "上一次执行尚未结束，本次跳过", started)
            return
        try:
            args = _valid_script_args(key, args_raw)
        except ValueError as e:
            with db_scope() as db:
                row = db.query(script_schedule_model).filter(
                    script_schedule_model.id == schedule_id).first()
                if row:
                    row.last_run = now
                    row.last_state = "failed"
                    db.commit()
            _run_record_finish(run_rec_id, "failed", "", f"参数不合法：{e}", started)
            return
        run_id = None
        if script_run_model is not None:
            try:
                with db_scope() as db:
                    run_row = script_run_model(
                        script_key=key, args=args_raw, state="running",
                        started_at=now, operator="schedule",
                        host=dist_lock.host_id())
                    db.add(run_row)
                    db.commit()
                    run_id = run_row.id
            except Exception:
                run_id = None
        SCRIPT_TASKS[key] = {
            "state": "running", "output": "", "started": time.time(), "finished": None,
            "returncode": None, "args": args_raw, "proc": None, "pid": None,
            "run_id": run_id,
        }
        try:
            _run_script(key, args, _invalidate_cache)
        finally:
            _persist_script_run(key, run_id)
        task = SCRIPT_TASKS.get(key) or {}
        state = task.get("state") or "failed"
        try:
            with db_scope() as db:
                row = db.query(script_schedule_model).filter(
                    script_schedule_model.id == schedule_id).first()
                if row:
                    row.last_run = datetime.now()
                    row.last_state = state
                    row.last_duration = int((task.get("finished") or time.time())
                                            - (task.get("started") or time.time()))
                    db.commit()
        except Exception:
            pass
        _run_record_finish(run_rec_id, state, task.get("output") or "",
                           "" if state == "done" else f"退出码：{task.get('returncode')}", started)

    def _run_schedule_backup(schedule_id, backup_dir, run_rec_id=None, started=None):
        """执行定时备份任务：可指定备份目录，结果写回任务状态与执行记录"""
        from ..services.backup import run_backup

        started = started or time.time()
        try:
            result = run_backup(operator="schedule", ip="", directory=backup_dir or None)
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        state = "done" if result.get("ok") else "failed"
        message = ""
        if result.get("ok"):
            message = (f"文件：{result.get('file', '')}，大小：{result.get('size', 0)} 字节，"
                       f"校验：{result.get('valid') and '通过' or '异常'}")
        else:
            message = str(result.get("error") or "备份失败")[:500]
        try:
            with db_scope() as db:
                row = db.query(script_schedule_model).filter(
                    script_schedule_model.id == schedule_id).first()
                if row:
                    row.last_run = datetime.now()
                    row.last_state = state
                    row.last_duration = int(time.time() - started)
                    db.commit()
        except Exception:
            pass
        _run_record_finish(run_rec_id, state, json.dumps(result, ensure_ascii=False, default=str),
                           message, started)

    def _run_schedule_envcheck(schedule_id, run_rec_id=None, started=None):
        """执行定时环境自检任务：复用自检逻辑，结果写回任务状态与执行记录"""
        started = started or time.time()
        try:
            _envcheck_job("schedule")
        except Exception as e:
            state, message = "failed", f"自检执行异常：{e}"
        else:
            ok = bool(ENV_CHECK.get("ok"))
            state = "done" if ok else "failed"
            message = "" if ok else (ENV_CHECK.get("output") or "自检未通过")[-500:]
        try:
            with db_scope() as db:
                row = db.query(script_schedule_model).filter(
                    script_schedule_model.id == schedule_id).first()
                if row:
                    row.last_run = datetime.now()
                    row.last_state = state
                    row.last_duration = int(time.time() - started)
                    db.commit()
        except Exception:
            pass
        _run_record_finish(run_rec_id, state, ENV_CHECK.get("output") or "", message, started)

    def _schedule_run_locked(schedule_id, handle):
        """执行任务并在结束时释放分布式锁"""
        try:
            _schedule_run(schedule_id)
        finally:
            handle.release()

    def _schedule_loop():
        while True:
            time.sleep(SCHEDULE_CHECK)
            try:
                now = datetime.now()
                with db_scope() as db:
                    due = [r.id for r in db.query(script_schedule_model)
                           .filter(script_schedule_model.enabled.is_(True)).all()
                           if r.next_run is None or r.next_run <= now]
                for sid in due:
                    # 分布式锁：多实例部署时同一任务只会被一台机器执行
                    handle = dist_lock.acquire(f"schedule:{sid}", ttl=3600)
                    if not handle:
                        continue
                    threading.Thread(target=_schedule_run_locked,
                                     args=(sid, handle), daemon=True).start()
            except Exception:
                pass

    if script_schedule_model is not None:
        threading.Thread(target=_schedule_loop, daemon=True).start()

    def _schedule_script_options():
        """定时任务提供脚本和环境自检，数据库备份仍使用独立入口"""
        options = [{"key": k, "name": v["name"],
                    "options": [{"label": l, "value": x} for l, x in v["options"]]}
                   for k, v in SCRIPTS.items()]
        options.append({"key": "envcheck", "name": "环境自检", "options": []})
        return options

    @api_router.get("/schedules")
    def api_schedules():
        if script_schedule_model is None:
            return {"code": 200, "data": [], "scripts": _schedule_script_options()}
        with db_scope() as db:
            rows = db.query(script_schedule_model).order_by(
                script_schedule_model.id.desc()).all()
            return {"code": 200, "data": [_schedule_to_dict(r) for r in rows],
                    "scripts": _schedule_script_options()}

    @api_router.post("/schedules")
    def api_schedule_save(request: Request, payload: dict):
        if script_schedule_model is None:
            return {"code": 400, "msg": "定时任务功能未启用"}
        task_type = (payload.get("task_type") or "script").strip()
        if task_type != "script":
            return {"code": 400, "msg": "仅支持创建脚本任务"}
        key = (payload.get("script_key") or "").strip()
        args_raw = str(payload.get("args") or "").strip()
        cron_expr = str(payload.get("cron_expr") or "").strip()
        if key == "envcheck":
            args_raw = ""
        elif key == "reset":
            return {"code": 400, "msg": "重置数据库不支持定时执行，请在脚本管理中双重确认后手动运行"}
        elif key not in SCRIPTS:
            return {"code": 400, "msg": "脚本不存在"}
        else:
            try:
                _valid_script_args(key, args_raw)
            except ValueError as e:
                return {"code": 400, "msg": f"参数不合法：{e}"}
        if cron_expr and _next_cron(cron_expr) is None:
            return {"code": 400, "msg": "cron 表达式不合法，应为 5 段：分 时 日 月 周"}
        try:
            interval = int(payload.get("interval_minutes", 60))
        except (TypeError, ValueError):
            return {"code": 400, "msg": "执行周期必须是数字"}
        if interval < 1 or interval > 10080:
            return {"code": 400, "msg": "执行周期应在 1 - 10080 分钟之间"}
        enabled = str(payload.get("enabled", True)).lower() in ("1", "true", "yes", "on")
        now = datetime.now()
        next_run = _next_cron(cron_expr, now) if cron_expr else None
        if next_run is None:
            next_run = now + timedelta(minutes=interval)
        with db_scope() as db:
            row = script_schedule_model(
                script_key=key, args=args_raw, interval_minutes=interval,
                task_type=task_type, cron_expr=cron_expr or None,
                backup_dir=None, enabled=enabled, next_run=next_run,
                operator=str(request.session.get("admin") or "system")[:100])
            db.add(row)
            db.commit()
            sid = row.id
        name = _schedule_name(task_type, key)
        cycle = f"cron：{cron_expr}" if cron_expr else f"每 {interval} 分钟"
        _audit(request, "schedule_add", name,
               f"{cycle}" + (f"，参数：{args_raw}" if args_raw else ""),
               after={"task_type": task_type, "script_key": key, "args": args_raw,
                      "cron_expr": cron_expr, "interval_minutes": interval, "enabled": enabled})
        return {"code": 200, "msg": "定时任务已创建", "id": sid}

    @api_router.post("/schedules/{schedule_id}/toggle")
    def api_schedule_toggle(request: Request, schedule_id: int, payload: dict):
        if script_schedule_model is None:
            return {"code": 400, "msg": "定时任务功能未启用"}
        enabled = str(payload.get("enabled")).lower() in ("1", "true", "yes", "on")
        with db_scope() as db:
            row = db.query(script_schedule_model).filter(
                script_schedule_model.id == schedule_id).first()
            if not row:
                return {"code": 404, "msg": "任务不存在"}
            before = {"enabled": bool(row.enabled)}
            row.enabled = enabled
            if enabled:
                row.next_run = _calc_next_run(row, datetime.now())
            db.commit()
        _audit(request, "schedule_toggle", f"ID {schedule_id}",
               "启用定时任务" if enabled else "停用定时任务",
               before=before, after={"enabled": enabled})
        return {"code": 200, "msg": "已保存"}

    @api_router.post("/schedules/{schedule_id}/run")
    def api_schedule_run_now(request: Request, schedule_id: int):
        if script_schedule_model is None:
            return {"code": 400, "msg": "定时任务功能未启用"}
        with db_scope() as db:
            row = db.query(script_schedule_model).filter(
                script_schedule_model.id == schedule_id).first()
            if not row:
                return {"code": 404, "msg": "任务不存在"}
            row.next_run = datetime.now() - timedelta(seconds=1)
            db.commit()
        _audit(request, "schedule_run_now", f"ID {schedule_id}", "立即执行定时任务")
        return {"code": 200, "msg": "已提交，调度线程将在 20 秒内执行"}

    @api_router.post("/schedules/{schedule_id}/delete")
    def api_schedule_delete(request: Request, schedule_id: int):
        if script_schedule_model is None:
            return {"code": 400, "msg": "定时任务功能未启用"}
        with db_scope() as db:
            row = db.query(script_schedule_model).filter(
                script_schedule_model.id == schedule_id).first()
            if not row:
                return {"code": 404, "msg": "任务不存在"}
            db.delete(row)
            db.commit()
        _audit(request, "schedule_delete", f"ID {schedule_id}", "删除定时任务")
        return {"code": 200, "msg": "已删除"}

    @api_router.get("/schedule-runs")
    def api_schedule_runs(schedule_id: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        """定时任务执行记录：记录开始/结束、是否成功、耗时、失败原因"""
        if schedule_run_model is None:
            return {"code": 200, "data": []}
        with db_scope() as db:
            q = db.query(schedule_run_model)
            if schedule_id:
                q = q.filter(schedule_run_model.schedule_id == schedule_id)
            rows = q.order_by(schedule_run_model.id.desc()).limit(limit).all()
            return {"code": 200, "data": [{
                "id": r.id,
                "schedule_id": r.schedule_id,
                "name": r.name or "",
                "task_type": r.task_type or "",
                "state": r.state or "",
                "duration": int(r.duration or 0),
                "message": r.message or "",
                "output": (r.output or "")[-4000:],
                "started": r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "",
                "finished": r.finished_at.strftime("%Y-%m-%d %H:%M:%S") if r.finished_at else "",
            } for r in rows]}

    @api_router.get("/scripts")
    def api_scripts():
        last = {}
        if script_run_model is not None:
            try:
                with db_scope() as db:
                    rows = (db.query(script_run_model)
                            .order_by(script_run_model.id.desc()).limit(200).all())
                    for r in rows:
                        if r.script_key not in last:
                            last[r.script_key] = r
            except Exception:
                last = {}
        data = []
        for k in SCRIPTS:
            snap = _script_snapshot(k)
            r = last.get(k)
            snap["last_run_at"] = (r.finished_at or r.started_at).strftime(
                "%Y-%m-%d %H:%M:%S") if r and (r.finished_at or r.started_at) else ""
            snap["last_duration"] = int(r.duration or 0) if r else 0
            snap["last_state"] = (r.state or "") if r else ""
            snap["last_returncode"] = r.returncode if r else None
            snap["last_operator"] = (r.operator or "") if r else ""
            snap["last_output"] = (r.output or "")[-8000:] if r else ""
            error_lines = [line for line in (r.output or "").splitlines() if line.strip()] if r else []
            snap["last_error"] = ("\n".join(error_lines[-8:])[-1200:]
                                  if r and r.state == "failed" and error_lines else
                                  (f"进程异常退出，退出码：{r.returncode}"
                                   if r and r.state == "failed" else ""))
            # 没有正在执行的任务时，直接以最近一次落库记录填充状态和运行信息，
            # 避免服务重启后前面显示“未运行”，结果只出现在“最近一次”列。
            if r and snap["state"] == "idle":
                snap.update(
                    state=r.state or "idle", args=r.args or "",
                    started_at=r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "",
                    finished_at=r.finished_at.strftime("%Y-%m-%d %H:%M:%S") if r.finished_at else "",
                    duration=int(r.duration or 0), returncode=r.returncode,
                    output=snap["last_output"],
                )
            limit = max(1, int(config.SCRIPT_LONG_MINUTES)) * 60
            snap["long_warning"] = bool(
                (snap["state"] == "running" and snap["duration"] >= limit)
                or (r and int(r.duration or 0) >= limit))
            snap["long_minutes"] = int(config.SCRIPT_LONG_MINUTES)
            data.append(snap)
        return {"code": 200, "data": data}

    @api_router.post("/scripts/run")
    def api_script_run(request: Request, payload: dict):
        key = (payload.get("key") or "").strip()
        if key not in SCRIPTS:
            return {"code": 400, "msg": "脚本不存在"}
        current = SCRIPT_TASKS.get(key) or {}
        if current.get("state") == "running":
            return {"code": 400, "msg": f"{SCRIPTS[key]['name']} 正在运行中，请等待完成"}
        try:
            args = _valid_script_args(key, payload.get("args"))
        except ValueError as e:
            return {"code": 400, "msg": f"参数不合法：{e}"}
        reset_operator = ""
        reset_ip = ""
        if key == "reset":
            # 第 0 道锁：环境变量总开关，默认禁用
            if not config.ALLOW_DB_RESET:
                _audit(request, "db_reset", "重置数据库",
                       "未启用 ALLOW_DB_RESET，已拒绝运行", result="failed")
                return {"code": 400, "msg": "重置数据库默认禁用：确需首次部署重置，请设置 ALLOW_DB_RESET=1 并重启"}
            # 两道锁：① 选择确认项并输入 RESET；② 当前登录管理员密码
            reset_operator = str(request.session.get("admin") or "system")
            reset_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                        or (request.client.host if request.client else ""))
            confirm = str(payload.get("confirm") or "").strip().upper()
            password = str(payload.get("password") or "")
            if confirm != "RESET" or args != ["RESET"]:
                _audit(request, "db_reset", "重置数据库",
                       "第 1 道锁未通过：确认项或 RESET 文字不正确", result="failed")
                return {"code": 400, "msg": "第 1 道锁未通过：需选择确认项并输入 RESET"}
            username = str(request.session.get("admin") or "")
            ok = username == admin_user and password == admin_password
            if not ok and admin_user_model is not None and username:
                with db_scope() as db:
                    row = db.query(admin_user_model).filter(
                        admin_user_model.username == username).first()
                ok = bool(row is not None and row.enabled
                          and _check_password_hash(password, row.password_hash))
            if not ok:
                _audit(request, "db_reset", "重置数据库",
                       f"第 2 道锁未通过：当前管理员密码错误（操作人 {username or '-'}）",
                       result="failed")
                return {"code": 400, "msg": "第 2 道锁未通过：当前管理员密码错误"}
        run_id = None
        if script_run_model is not None:
            try:
                with db_scope() as db:
                    row = script_run_model(
                        script_key=key, args=" ".join(args), state="running",
                        started_at=datetime.now(),
                        operator=str(request.session.get("admin") or "system")[:100],
                        host=dist_lock.host_id())
                    db.add(row)
                    db.commit()
                    run_id = row.id
            except Exception:
                run_id = None
        SCRIPT_TASKS[key] = {
            "state": "running", "output": "", "started": time.time(), "finished": None,
            "returncode": None, "args": " ".join(args), "proc": None, "pid": None,
            "run_id": run_id,
        }
        if key == "reset":
            def _reset_audit(task):
                # 重置会清空审计表，因此成功/失败的补记放在脚本执行完成之后
                code = task.get("returncode")
                _audit_identity(
                    reset_operator, reset_ip, "db_reset", "重置数据库",
                    f"双重确认通过；参数：RESET；退出码：{code}；"
                    f"结果：{'成功' if code == 0 else '失败'}",
                    result="success" if code == 0 else "failed",
                )
            threading.Thread(target=_script_worker,
                             args=(key, args, run_id, _reset_audit), daemon=True).start()
        else:
            threading.Thread(target=_script_worker, args=(key, args, run_id), daemon=True).start()
            _audit(request, "script_run", SCRIPTS[key]["name"],
                   f"参数：{' '.join(args) or '无'}，文件：{SCRIPTS[key]['file']}")
        return {"code": 200, "msg": f"{SCRIPTS[key]['name']} 已开始运行", "key": key}

    @api_router.post("/scripts/stop")
    def api_script_stop(request: Request, payload: dict):
        key = (payload.get("key") or "").strip()
        task = SCRIPT_TASKS.get(key) or {}
        proc = task.get("proc")
        if task.get("state") != "running" or proc is None:
            return {"code": 400, "msg": "该脚本没有在运行"}
        task["state"] = "stopped"
        task["finished"] = time.time()
        try:
            proc.terminate()
        except Exception:
            pass
        _audit(request, "script_stop", SCRIPTS[key]["name"], "手动停止脚本运行")
        return {"code": 200, "msg": f"{SCRIPTS[key]['name']} 已停止"}

    @api_router.get("/scripts/{key}/stream")
    async def api_script_stream(key: str, request: Request):
        """SSE 实时推送脚本输出；前端优先使用，失败时回退轮询"""
        if key not in SCRIPTS:
            raise HTTPException(status_code=404, detail="脚本不存在")

        async def gen():
            last_sig = None
            idle_rounds = 0
            while True:
                if await request.is_disconnected():
                    break
                snap = _script_snapshot(key)
                payload = {
                    "key": key, "state": snap["state"], "output": snap["output"],
                    "duration": snap["duration"], "returncode": snap["returncode"],
                    "started_at": snap["started_at"], "finished_at": snap["finished_at"],
                }
                sig = (snap["state"], len(snap["output"]), snap["returncode"])
                if sig != last_sig:
                    last_sig = sig
                    yield f"event: output\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if snap["state"] in ("done", "failed", "stopped"):
                    yield f"event: end\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    break
                if snap["state"] == "idle":
                    idle_rounds += 1
                    if idle_rounds >= 3:
                        yield f"event: end\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        break
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------------- 模板下载与批量导入 ----------------
    @api_router.get("/template")
    def api_template(table_key: str, fields: str = ""):
        if table_key not in table_map:
            return {"code": 404, "msg": "数据表不存在"}
        model = table_map[table_key]
        columns = _import_columns(model)
        if not columns:
            return {"code": 400, "msg": "该表没有可导入的字段"}
        # 自定义模板字段：只保留用户勾选的字段，必填字段始终保留
        wanted = [f.strip() for f in (fields or "").split(",") if f.strip()]
        if wanted:
            picked = [c for c in columns if c.name in wanted or _is_required(model, c.name)]
            if picked:
                columns = picked
        from openpyxl import Workbook
        from openpyxl.comments import Comment

        wb = Workbook()
        ws = wb.active
        ws.title = table_key[:30]
        for idx, col in enumerate(columns, start=1):
            cell = ws.cell(row=1, column=idx, value=col.name)
            cell.comment = Comment(col.comment or col.name, "admin")
            ws.column_dimensions[cell.column_letter].width = 22

        help_ws = wb.create_sheet("字段说明")
        help_ws.append(["字段名", "类型", "必填", "说明"])
        for col in columns:
            required = "是" if _is_required(model, col.name) else "否"
            help_ws.append([col.name, str(col.type), required, col.comment or ""])
        help_ws.append([])
        help_ws.append(["填写说明", "第1个sheet表头请勿修改，从第2行开始填写数据；留空的必填字段会导致整批导入失败。"])
        for letter, width in zip("ABCD", (18, 22, 10, 60)):
            help_ws.column_dimensions[letter].width = width

        buf = io.BytesIO()
        wb.save(buf)
        return _xlsx_response(buf.getvalue(), f"{table_key}_导入模板.xlsx")

    @api_router.get("/export")
    def api_export(request: Request, table_key: str, keyword: str = None,
                   date_from: str = None, date_to: str = None,
                   fmt: str = "xlsx",
                   limit: int = Query(50000, ge=1, le=200000)):
        """导出单张表为 Excel 或 CSV，支持关键词与创建时间范围筛选"""
        if table_key not in table_map:
            return JSONResponse({"code": 404, "msg": "数据表不存在"}, status_code=404)
        import sqlalchemy as sa
        from openpyxl import Workbook
        from openpyxl.styles import Font

        model = table_map[table_key]
        columns = [c for c in model.__table__.columns if c.name not in ("dedup_md5", "is_deleted")]
        keyword = (keyword or "").strip()

        def parse_date(value):
            if not value:
                return None
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(value.strip(), fmt)
                except ValueError:
                    continue
            return "bad"

        d_from, d_to = parse_date(date_from), parse_date(date_to)
        if d_from == "bad" or d_to == "bad":
            return JSONResponse({"code": 400, "msg": "日期格式应为 YYYY-MM-DD"}, status_code=400)

        with db_scope() as db:
            q = db.query(model)
            if keyword:
                conds = [c.like(f"%{keyword}%") for c in columns
                         if isinstance(c.type, (sa.String, sa.Text))]
                if conds:
                    q = q.filter(or_(*conds))
            created = model.__table__.columns.get("created_at")
            if created is not None:
                if d_from:
                    q = q.filter(created >= d_from)
                if d_to:
                    q = q.filter(created < d_to + timedelta(days=1))
            total = q.count()
            id_col = model.__table__.columns.get("id")
            if id_col is not None:
                q = q.order_by(id_col.asc())
            rows = q.limit(limit).all()

        _audit(request, "export", table_key,
               f"导出表 {table_key}，筛选：关键词={keyword or '无'}，"
               f"创建时间={date_from or '不限'} ~ {date_to or '不限'}，"
               f"匹配 {total} 条，导出 {len(rows)} 条")

        if fmt == "csv":
            data = [[getattr(row, col.name, None) for col in columns] for row in rows]
            if total > len(rows):
                data.append([])
                data.append([f"说明：共匹配 {total} 条，本次仅导出前 {len(rows)} 条；"
                             f"可缩小筛选范围后分批导出。"])
            filename = f"{table_key}_导出_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            return _csv_response([c.name for c in columns], data, filename)

        wb = Workbook()
        ws = wb.active
        ws.title = table_key[:30]
        for idx, col in enumerate(columns, start=1):
            cell = ws.cell(row=1, column=idx, value=col.name)
            cell.font = Font(bold=True)
            ws.column_dimensions[cell.column_letter].width = min(48, max(14, len(col.name) + 8))
        for r_idx, row in enumerate(rows, start=2):
            for c_idx, col in enumerate(columns, start=1):
                value = getattr(row, col.name, None)
                if isinstance(value, datetime):
                    value = value.strftime("%Y-%m-%d %H:%M:%S")
                ws.cell(row=r_idx, column=c_idx, value=value)

        if total > len(rows):
            note = wb.create_sheet("导出说明")
            note.append(["说明"])
            note.append([f"共匹配 {total} 条，本次仅导出前 {len(rows)} 条；可缩小筛选范围后分批导出。"])

        buf = io.BytesIO()
        wb.save(buf)
        filename = f"{table_key}_导出_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
        return _xlsx_response(buf.getvalue(), filename)

    def _record_import(**kw):
        """写入一条导入记录（无论成功失败），失败不影响主流程"""
        if import_log_model is None:
            return
        try:
            kw.setdefault("operator", "")
            with db_scope() as db:
                db.add(import_log_model(**kw))
                db.commit()
        except Exception:
            pass

    def _job_update(job_id, **kw):
        """更新导入任务进度：内存供实时轮询，数据库供重启后查看"""
        with IMPORT_LOCK:
            job = IMPORT_JOBS.get(job_id)
            if job is not None:
                job.update(kw)
                job["updated"] = time.time()
        _persist_job(job_id, **kw)

    def _import_worker(job_id, table_key, filename, raw, operator, dry_run=False,
                       only_lines=None):
        """真正执行导入的同步函数，在后台线程运行，不阻塞 Web 服务

        dry_run=True 时只做解析与校验，统计可导入/重复/错误数量，不写入数据库。
        only_lines 为失败行号集合：只处理这些行，用于“只重试失败行”。
        """
        model = table_map.get(table_key)
        prefix = "[预览] " if dry_run else ""
        failed_lines = set()   # 本次导入失败的行号，供下次“只重试失败行”使用
        quarantine_items = []  # 命中敏感词的行：进入隔离区，等待人工审核

        def failed(msg, detail="", code=400, physical="", total=0):
            msg = prefix + msg
            _record_import(table_key=table_key[:64], physical_table=(physical or "")[:64],
                           filename=filename[:255], total=total, inserted=0, skipped=0,
                           truncated=0, status="failed", message=msg,
                           detail=(detail or msg)[:60000], operator=operator[:100])
            return {"code": code, "msg": msg, "detail": detail or msg, "dry_run": dry_run}

        if model is None:
            return failed("数据表不存在", f"未找到数据表标识：{table_key}")

        physical = model.__tablename__
        columns = _import_columns(model)
        valid = {c.name for c in columns}
        _job_update(job_id, stage="解析文件", physical=physical)
        if not raw:
            return failed("文件为空", "上传的文件内容为空，请重新选择文件", physical=physical)

        lower_name = filename.lower()
        try:
            if lower_name.endswith(".csv"):
                headers, rows = _parse_csv(raw)
            else:
                headers, rows = _parse_xlsx(raw)
        except Exception as e:
            return failed(f"文件解析失败：{e}",
                          f"文件：{filename}\n大小：{len(raw) / 1024:.1f} KB\n异常：{e}",
                          physical=physical)

        headers = [h.strip() for h in headers]
        unknown = [h for h in headers if h and h not in valid]
        if unknown:
            return failed("模板列不匹配，未知字段：" + "、".join(unknown),
                          f"物理表：{physical}\n文件表头：{', '.join(h for h in headers if h)}\n"
                          f"未知字段：{', '.join(unknown)}\n"
                          f"允许字段：{', '.join(valid)}", physical=physical)
        if not rows:
            return failed("没有可导入的数据行",
                          f"文件：{filename}\n表头：{', '.join(h for h in headers if h)}\n"
                          "除表头外没有检测到非空数据行", physical=physical)

        # 校验必填字段
        required = [c.name for c in columns if _is_required(model, c.name)]
        missing_headers = [r for r in required if r not in headers]
        if missing_headers:
            return failed("缺少必填列：" + "、".join(missing_headers),
                          f"文件：{filename}\n当前表头：{', '.join(h for h in headers if h)}\n"
                          f"缺少必填列：{', '.join(missing_headers)}", physical=physical)

        # ---------- 逐行解析：格式错误单独记录，不再整批中断 ----------
        records, errors, truncated = [], [], 0
        if only_lines:
            # 只重试模式：按原文件行号筛选，跳过上次已成功的行
            rows = [(ln, vals) for ln, vals in rows if ln in only_lines]
            if not rows:
                return failed("文件中没有找到上次失败的行",
                              "上次失败行号：" + "、".join(str(x) for x in sorted(only_lines)[:100])
                              + "\n请保持行号不变，修正这些行后重新上传", physical=physical)
        _job_update(job_id, stage="逐行校验", total_rows=len(rows), processed=0)
        for row_no, (lineno, values) in enumerate(rows, start=1):
            if row_no % 200 == 0:
                _job_update(job_id, processed=row_no)
            record, empty, row_errors = {}, True, []
            for idx, h in enumerate(headers):
                if not h:
                    continue
                val = values[idx] if idx < len(values) else None
                if isinstance(val, str):
                    val = val.strip()
                if val == "" or val is None:
                    continue
                empty = False
                col = _column_obj(model, h)
                if col is None:
                    continue
                try:
                    val, cut = _coerce(col, val)
                except ValueError as e:
                    row_errors.append(f"字段 {h} 格式错误（{e}，原值：{str(val)[:30]}）")
                    continue
                truncated += 1 if cut else 0
                record[h] = val
            if empty:
                continue
            lack = [r for r in required if r not in record]
            if lack:
                row_errors.append("缺少必填字段：" + "、".join(lack))
            hits = []
            for v in record.values():
                if isinstance(v, str):
                    hits.extend(w for w in sensitive.find_words(v) if w not in hits)
            if hits:
                row_errors.append("包含敏感词：" + "、".join(hits[:3]))
            # 仅因敏感词被拦下的行进入隔离区，由管理员在后台审核放行
            if hits and len(row_errors) == 1:
                quarantine_items.append({
                    "table_key": table_key[:64], "physical_table": physical[:64],
                    "filename": filename[:255], "line_no": lineno,
                    "content": "；".join(f"{k}={str(v)[:60]}"
                                        for k, v in list(record.items())[:3])[:2000],
                    "hits": "、".join(hits[:10])[:500],
                    "row_data": json.dumps(record, ensure_ascii=False, default=str)[:60000],
                    "operator": operator[:100],
                })
                continue
            if row_errors:
                errors.append(f"第{lineno}行：" + "；".join(row_errors))
                failed_lines.add(lineno)
                continue
            record["__line__"] = lineno
            records.append(record)
        if not records and not errors:
            return failed("没有有效数据行",
                          f"文件：{filename}\n表头：{', '.join(h for h in headers if h)}\n"
                          "所有数据行均为空", physical=physical)

        # ---------- md5 查重：先文件内去重，再对比数据库已有数据 ----------
        key_fields = [f for f in (DEDUP_FIELDS.get(table_key) or []) if _column_obj(model, f) is not None]
        if not key_fields:
            key_fields = [columns[0].name] if columns else ["id"]

        def dedup_md5(record):
            joined = "|".join("" if record.get(f) is None else str(record.get(f)).strip()
                              for f in key_fields)
            return hashlib.md5(joined.encode("utf-8")).hexdigest()

        seen, unique, dup_lines = {}, [], []
        first_key = key_fields[0]
        for record in records:
            # 查重字段为空的行无法判重，按格式错误记录，避免每次导入都重复写入脏数据
            if not str(record.get(first_key) or "").strip():
                errors.append(f"第{record['__line__']}行：查重字段 {first_key} 为空，无法判重，已跳过")
                failed_lines.add(record["__line__"])
                continue
            digest = dedup_md5(record)
            if digest in seen:
                dup_lines.append(record["__line__"])
                continue
            seen[digest] = record["__line__"]
            record["__md5__"] = digest
            unique.append(record)

        existing = set()
        with db_scope() as db:
            values = list({str(r.get(first_key) or "").strip() for r in unique})
            if values:
                qcols = [getattr(model, f) for f in key_fields]
                for row in (db.query(*qcols)
                            .filter(_column_obj(model, first_key).in_(values[:2000])).all()):
                    joined = "|".join("" if v is None else str(v).strip() for v in row)
                    existing.add(hashlib.md5(joined.encode("utf-8")).hexdigest())

        to_insert, insert_lines, db_dups = [], [], 0
        for record in unique:
            if record["__md5__"] in existing:
                db_dups += 1
                continue
            to_insert.append({k: v for k, v in record.items() if not k.startswith("__")})
            insert_lines.append(record["__line__"])
        skipped = len(dup_lines) + db_dups

        # ---------- 入库：不使用 INSERT IGNORE，异常逐条定位 ----------
        table = model.__table__
        inserted, db_errors = 0, []
        if dry_run:
            # 预览模式：不写库，把待写入条数作为“可导入”统计返回
            inserted = len(to_insert)
            _job_update(job_id, stage="预览完成（未入库）")
        else:
            _job_update(job_id, stage="写入数据库")
            with db_scope() as db:
                try:
                    for i in range(0, len(to_insert), 500):
                        db.execute(table.insert(), to_insert[i:i + 500])
                    db.commit()
                    inserted = len(to_insert)
                except Exception:
                    db.rollback()
                    for idx, row in enumerate(to_insert):
                        try:
                            db.execute(table.insert(), [row])
                            db.commit()
                            inserted += 1
                        except Exception as e:
                            db.rollback()
                            line = insert_lines[idx] if idx < len(insert_lines) else 0
                            failed_lines.add(line)
                            brief = "、".join(f"{k}={str(v)[:20]}" for k, v in list(row.items())[:3])
                            db_errors.append(f"第{line}行写入失败（{brief}）：{e}")

        error_total = len(errors) + len(db_errors)
        # 敏感词隔离区：命中敏感词的行不直接丢弃，写入隔离区等待人工审核
        quarantined = 0
        if quarantine_items:
            quarantined = len(quarantine_items)
            if not dry_run and quarantine_model is not None:
                try:
                    with db_scope() as db:
                        for item in quarantine_items[:5000]:
                            db.add(quarantine_model(**item))
                        db.commit()
                except Exception:
                    quarantined = 0
        status = "success" if (inserted > 0 or error_total == 0) else "failed"
        if dry_run:
            msg = (f"{prefix}校验完成（未写入数据库）：有效 {len(records)} 条，"
                   f"可导入 {inserted} 条")
        else:
            msg = f"导入完成：有效 {len(records)} 条，新增 {inserted} 条"
        if skipped:
            msg += f"，重复跳过 {skipped} 条"
        if error_total:
            msg += f"，格式错误 {error_total} 条"
        if quarantined:
            msg += f"，敏感词隔离待审核 {quarantined} 条"
        if truncated:
            msg += f"，超长内容已截断 {truncated} 处"

        detail_lines = [
            f"物理表：{physical}", f"文件：{filename}", f"大小：{len(raw) / 1024:.1f} KB",
            f"表头：{', '.join(h for h in headers if h)}",
            f"查重字段：{', '.join(key_fields)}",
        ]
        if dry_run:
            detail_lines.append("模式：预览（dry-run），仅解析校验，未写入数据库")
        detail_lines.append(
            f"有效数据：{len(records)} 条，{'可导入' if dry_run else '新增'} {inserted} 条，"
            f"重复跳过 {skipped} 条，格式错误 {error_total} 条，截断 {truncated} 处")
        if dup_lines:
            detail_lines.append("重复跳过行：第 " + "、".join(str(x) for x in dup_lines[:100])
                                + " 行" + ("（仅列前 100 条）" if len(dup_lines) > 100 else ""))
        if errors:
            detail_lines.append("格式错误详情：")
            detail_lines.extend(errors[:200])
            if len(errors) > 200:
                detail_lines.append(f"...共 {len(errors)} 条，仅显示前 200 条")
        if db_errors:
            detail_lines.append("数据库写入失败详情：")
            detail_lines.extend(db_errors[:50])
        detail = "\n".join(detail_lines)

        _record_import(table_key=table_key[:64], physical_table=physical[:64],
                       filename=filename[:255], total=len(records), inserted=inserted,
                       skipped=skipped, truncated=truncated, status=status,
                       message=msg, detail=detail, operator=operator[:100])
        return {"code": 200 if status == "success" else 400, "msg": msg,
                "inserted": inserted, "skipped": skipped, "errors": error_total,
                "total": len(records), "detail": detail, "dry_run": dry_run,
                "quarantined": quarantined,
                "failed_lines": sorted(failed_lines)[:2000]}

    def _run_import_job(job_id, table_key, filename, raw, operator, dry_run=False,
                        only_lines=None):
        """后台线程入口：执行导入并把结果写回任务状态"""
        try:
            result = _import_worker(job_id, table_key, filename, raw, operator, dry_run,
                                    only_lines=only_lines)
        except Exception as e:
            result = {"code": 500, "msg": f"导入异常：{e}", "detail": str(e)}
        _job_update(job_id, state="done" if result.get("code") == 200 else "failed",
                    stage="已完成", finished=time.time(), msg=result.get("msg", ""),
                    result=result, processed=result.get("total", 0),
                    inserted=result.get("inserted", 0), skipped=result.get("skipped", 0),
                    errors=result.get("errors", 0),
                    failed_lines=result.get("failed_lines") or [])
        if not dry_run:
            _invalidate_cache()

    @api_router.post("/import")
    async def api_import(request: Request, table_key: str = Form(...), file: UploadFile = File(...),
                         dry_run: bool = Form(False)):
        """上传文件后立即返回任务编号，导入在后台线程执行，由前端轮询进度

        dry_run=1 时只解析校验并统计，不写入数据库。
        """
        operator = request.session.get("admin") or ""
        filename = file.filename or ""
        if table_key not in table_map:
            return {"code": 400, "msg": "数据表不存在"}
        lower_name = filename.lower()
        if not lower_name.endswith((".xlsx", ".xlsm", ".csv")):
            return {"code": 400, "msg": "仅支持 .xlsx / .xlsm / .csv 文件"}
        max_bytes = max(1, int(getattr(config, "IMPORT_MAX_MB", 20))) * 1024 * 1024
        raw = await file.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return {"code": 400,
                    "msg": f"文件超过大小上限 {max_bytes // 1024 // 1024} MB，请拆分后导入"}
        if not raw:
            return {"code": 400, "msg": "文件为空，请重新选择文件"}
        # 文件头校验：不只依赖后缀，xlsx 必须是 ZIP 容器，CSV 不能含二进制内容
        if lower_name.endswith((".xlsx", ".xlsm")):
            if not raw.startswith(b"PK\x03\x04"):
                return {"code": 400, "msg": "文件头校验失败：不是有效的 xlsx 文件"}
        elif b"\x00" in raw[:8192]:
            return {"code": 400, "msg": "文件头校验失败：CSV 含二进制内容，请另存为文本格式"}

        job_id = uuid.uuid4().hex[:16]
        now = time.time()
        with IMPORT_LOCK:
            # 顺带清理过期任务，避免内存无限增长
            for old in [k for k, v in IMPORT_JOBS.items()
                        if now - (v.get("finished") or v.get("created", now)) > IMPORT_JOB_KEEP]:
                IMPORT_JOBS.pop(old, None)
            IMPORT_JOBS[job_id] = {
                "job_id": job_id, "state": "running", "stage": "排队中",
                "table_key": table_key, "filename": filename[:255], "dry_run": dry_run,
                "operator": operator[:100], "created": now, "updated": now,
                "finished": None, "total_rows": 0, "processed": 0, "total": 0,
                "inserted": 0, "skipped": 0, "errors": 0, "msg": "", "result": None,
                "failed_lines": [],
            }
        if import_job_model is not None:
            try:
                _cleanup_jobs()
                with db_scope() as db:
                    db.add(import_job_model(
                        id=job_id, table_key=table_key[:50], filename=filename[:255],
                        state="running", stage="排队中", total=0, processed=0,
                        inserted=0, skipped=0, failed=0, operator=operator[:100],
                    ))
                    db.commit()
            except Exception:
                pass
        threading.Thread(target=_run_import_job,
                         args=(job_id, table_key, filename, raw, operator, dry_run),
                         daemon=True).start()
        _audit(request, "import", table_key,
               f"上传文件：{filename}（{len(raw) / 1024:.1f} KB），"
               f"{'预览模式' if dry_run else '正常导入'}，任务号：{job_id}")
        return {"code": 200, "job_id": job_id,
                "msg": "预览任务已创建，正在后台校验" if dry_run else "导入任务已创建，正在后台处理"}

    @api_router.post("/import/retry")
    async def api_import_retry(request: Request, job_id: str = Form(...),
                               file: UploadFile = File(...)):
        """只重试失败行：按原文件行号筛选，已成功的行不会被重复处理"""
        operator = request.session.get("admin") or ""
        with IMPORT_LOCK:
            src = IMPORT_JOBS.get(job_id)
            failed_lines = list((src or {}).get("failed_lines") or [])
            table_key = (src or {}).get("table_key", "")
        if not failed_lines:
            return {"code": 400, "msg": "该任务没有可重试的失败行，或任务记录已过期，请重新导入"}
        if table_key not in table_map:
            return {"code": 400, "msg": "原任务的数据表不存在"}
        filename = file.filename or ""
        if not filename.lower().endswith((".xlsx", ".xlsm", ".csv")):
            return {"code": 400, "msg": "仅支持 .xlsx / .xlsm / .csv 文件"}
        max_bytes = max(1, int(getattr(config, "IMPORT_MAX_MB", 20))) * 1024 * 1024
        raw = await file.read(max_bytes + 1)
        if len(raw) > max_bytes:
            return {"code": 400,
                    "msg": f"文件超过大小上限 {max_bytes // 1024 // 1024} MB，请拆分后导入"}
        if not raw:
            return {"code": 400, "msg": "文件为空，请重新选择文件"}
        if filename.lower().endswith((".xlsx", ".xlsm")) and not raw.startswith(b"PK\x03\x04"):
            return {"code": 400, "msg": "文件头校验失败：不是有效的 xlsx 文件"}
        new_job_id = uuid.uuid4().hex[:16]
        now = time.time()
        with IMPORT_LOCK:
            for old in [k for k, v in IMPORT_JOBS.items()
                        if now - (v.get("finished") or v.get("created", now)) > IMPORT_JOB_KEEP]:
                IMPORT_JOBS.pop(old, None)
            IMPORT_JOBS[new_job_id] = {
                "job_id": new_job_id, "state": "running", "stage": "排队中",
                "table_key": table_key, "filename": filename[:255], "dry_run": False,
                "operator": operator[:100], "created": now, "updated": now,
                "finished": None, "total_rows": 0, "processed": 0, "total": 0,
                "inserted": 0, "skipped": 0, "errors": 0, "msg": "", "result": None,
                "failed_lines": [],
            }
        if import_job_model is not None:
            try:
                with db_scope() as db:
                    db.add(import_job_model(
                        id=new_job_id, table_key=table_key[:50], filename=filename[:255],
                        state="running", stage="排队中", total=0, processed=0,
                        inserted=0, skipped=0, failed=0, operator=operator[:100]))
                    db.commit()
            except Exception:
                pass
        threading.Thread(target=_run_import_job,
                         args=(new_job_id, table_key, filename, raw, operator, False,
                               set(failed_lines)), daemon=True).start()
        _audit(request, "import", table_key,
               f"重试失败行（原任务 {job_id}，{len(failed_lines)} 行）：{filename}")
        return {"code": 200, "job_id": new_job_id,
                "msg": f"已创建重试任务，仅处理上次失败的 {len(failed_lines)} 行"}

    @api_router.get("/import/jobs")
    def api_import_jobs(page: int = Query(1, ge=1), size: int = Query(10, ge=1, le=100)):
        """导入任务记录列表：包含服务重启前的历史任务"""
        if import_job_model is None:
            return {"code": 200, "total": 0, "page": page, "size": size, "data": []}
        with db_scope() as db:
            query = db.query(import_job_model)
            total = query.count()
            rows = (query.order_by(import_job_model.created_at.desc())
                    .offset((page - 1) * size).limit(size).all())
            with IMPORT_LOCK:
                failed_map = {k: list(v.get("failed_lines") or [])
                              for k, v in IMPORT_JOBS.items()}
            return {"code": 200, "total": total, "page": page, "size": size,
                    "data": [{
                        "job_id": r.id, "state": r.state or "", "stage": r.stage or "",
                        "table_key": r.table_key or "", "filename": r.filename or "",
                        "operator": r.operator or "",
                        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                        "finished_at": r.finished_at.strftime("%Y-%m-%d %H:%M:%S") if r.finished_at else "",
                        "duration": (int((r.finished_at - r.created_at).total_seconds())
                                     if r.finished_at and r.created_at else 0),
                        "total": r.total or 0, "processed": r.processed or 0,
                        "inserted": r.inserted or 0, "skipped": r.skipped or 0,
                        "errors": r.failed or 0, "message": r.message or "",
                        "detail": r.errors or "",
                        "failed_lines": failed_map.get(r.id, []),
                    } for r in rows]}

    @api_router.get("/import/jobs/{job_id}")
    def api_import_job(job_id: str):
        with IMPORT_LOCK:
            mem_job = dict(IMPORT_JOBS.get(job_id) or {})
        if import_job_model is not None:
            with db_scope() as db:
                row = db.query(import_job_model).filter(
                    import_job_model.id == job_id).first()
                if row is not None:
                    data = _job_to_dict(row)
                    if mem_job:
                        data["failed_lines"] = mem_job.get("failed_lines") or []
                    return {"code": 200, "data": data}
        if not mem_job:
            return {"code": 404, "msg": "任务不存在或已过期"}
        return {"code": 200, "data": mem_job}

    def _import_columns(model):
        return [c for c in model.__table__.columns if c.name not in META_COLUMNS]

    def _column_obj(model, name):
        return model.__table__.columns.get(name)

    def _is_required(model, name):
        table = model.__tablename__
        if table not in REQUIRED_CACHE:
            try:
                with db_scope() as db:
                    rows = db.execute(text(
                        "SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT, EXTRA "
                        "FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
                    ), {"t": table}).all()
                REQUIRED_CACHE[table] = {
                    r[0] for r in rows
                    if r[1] == "NO" and r[2] is None
                    and "auto_increment" not in (r[3] or "").lower()
                    and "GENERATED" not in (r[3] or "").upper()
                }
            except Exception:
                REQUIRED_CACHE[table] = set()
        return name in REQUIRED_CACHE[table]

    def _coerce(col, value):
        """按列类型转换值，超长字符串截断；类型不符抛出 ValueError 由调用方记录"""
        import sqlalchemy as sa
        if isinstance(col.type, (sa.Integer, sa.BigInteger, sa.SmallInteger)):
            try:
                return int(float(str(value).strip())), False
            except (TypeError, ValueError):
                raise ValueError("应为整数")
        if isinstance(col.type, sa.Float):
            try:
                return float(str(value).strip()), False
            except (TypeError, ValueError):
                raise ValueError("应为数字")
        if isinstance(col.type, sa.Boolean):
            return str(value).lower() in ("1", "true", "是", "yes", "y"), False
        text_val = str(value)
        length = getattr(col.type, "length", None)
        if length and len(text_val) > length:
            return text_val[:length], True
        return text_val, False

    def _parse_csv(raw: bytes):
        content = raw.decode("utf-8-sig", errors="replace")
        rows_all = list(csv.reader(io.StringIO(content)))
        if not rows_all:
            return [], []
        headers = rows_all[0]
        rows = [(i + 2, r) for i, r in enumerate(rows_all[1:]) if any(x.strip() for x in r)]
        return headers, rows

    def _parse_xlsx(raw: bytes):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        iterator = ws.iter_rows(values_only=True)
        try:
            headers = [str(h) if h is not None else "" for h in next(iterator)]
        except StopIteration:
            return [], []
        rows = []
        for i, r in enumerate(iterator, start=2):
            vals = ["" if v is None else str(v) for v in r]
            if any(v.strip() for v in vals):
                rows.append((i, vals))
        wb.close()
        return headers, rows

    # ---------------- 操作审计 ----------------
    @api_router.get("/audit-logs")
    def api_audit_logs(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                       action: str = None, operator: str = None, result: str = None,
                       start: str = None, end: str = None, q: str = None):
        if audit_log_model is None:
            return {"code": 200, "total": 0, "page": page, "size": size,
                    "actions": [], "data": []}
        with db_scope() as db:
            query = _human_audit_query(db)
            if action:
                query = query.filter(audit_log_model.action == action)
            if operator:
                query = query.filter(audit_log_model.operator == operator)
            if result:
                query = query.filter(audit_log_model.result == result)
            if start:
                query = query.filter(audit_log_model.created_at >= start)
            if end:
                query = query.filter(audit_log_model.created_at <= end)
            if q:
                like = f"%{q.strip()}%"
                query = query.filter(or_(audit_log_model.target.like(like),
                                         audit_log_model.detail.like(like)))
            total = query.count()
            rows = query.order_by(audit_log_model.id.desc()).offset((page - 1) * size).limit(size).all()
            actions = [r[0] for r in _human_audit_query(db).with_entities(audit_log_model.action)
                       .distinct().order_by(audit_log_model.action).all() if r[0]]
            return {"code": 200, "total": total, "page": page, "size": size, "actions": actions,
                    "data": [{
                        "id": r.id, "operator": r.operator or "", "action": r.action or "",
                        "target": r.target or "", "detail": r.detail or "",
                        "ip": r.ip or "", "result": r.result or "",
                        "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                    } for r in rows]}

    @api_router.get("/audit-logs/export")
    def api_audit_logs_export(request: Request, action: str = None, operator: str = None, result: str = None,
                              start: str = None, end: str = None, q: str = None,
                              limit: int = Query(50000, ge=1, le=200000)):
        """按筛选条件导出后台审计日志为 Excel"""
        if audit_log_model is None:
            return JSONResponse({"code": 404, "msg": "审计日志未启用"}, status_code=404)
        bounds = _time_bounds(start, end)
        if bounds is None:
            return JSONResponse({"code": 400, "msg": "时间格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS"},
                                status_code=400)
        t_from, t_to = bounds
        with db_scope() as db:
            query = _human_audit_query(db)
            if action:
                query = query.filter(audit_log_model.action == action)
            if operator:
                query = query.filter(audit_log_model.operator == operator)
            if result:
                query = query.filter(audit_log_model.result == result)
            if t_from:
                query = query.filter(audit_log_model.created_at >= t_from)
            if t_to:
                query = query.filter(audit_log_model.created_at <= t_to)
            if q:
                like = f"%{q.strip()}%"
                query = query.filter(or_(audit_log_model.target.like(like),
                                         audit_log_model.detail.like(like)))
            rows = query.order_by(audit_log_model.id.desc()).limit(limit).all()

        headers = ["时间", "操作人", "操作", "对象", "详情", "IP", "结果"]
        data = [[r.created_at, r.operator, r.action, r.target, r.detail, r.ip, r.result]
                for r in rows]
        who = request.session.get("admin") or "system"
        filename = f"审计日志_{who}_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
        _audit(request, "audit_export", "审计日志", f"导出 {len(data)} 条审计日志")
        return _xlsx_response(_xlsx_bytes("审计日志", headers, data, who), filename)

    # ---------------- 数据库备份 ----------------
    @api_router.get("/backups")
    def api_backups():
        from ..core import config
        from ..services.backup import backup_files

        return {"code": 200, "enabled": config.BACKUP_ENABLED,
                "dir": config.BACKUP_DIR, "hour": config.BACKUP_HOUR,
                "keep_days": config.BACKUP_KEEP_DAYS,
                "data": backup_files()[:30]}

    @api_router.post("/backups/run")
    def api_backup_run(request: Request):
        from ..services.backup import run_backup

        operator = request.session.get("admin") or "system"
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else ""))
        result = run_backup(operator=str(operator), ip=ip)
        return {"code": 200 if result.get("ok") else 500, **result}

    @api_router.post("/backups/restore")
    def api_backup_restore(request: Request, payload: dict):
        """把指定备份恢复到当前数据库（高风险，需前端二次输入确认）"""
        from ..services.backup import restore_backup

        name = str(payload.get("name") or "").strip()
        if not name:
            return {"code": 400, "msg": "请选择要恢复的备份文件"}
        if str(payload.get("confirm") or "").strip().upper() != "RESTORE":
            return {"code": 400, "msg": "缺少恢复确认，操作已取消"}
        operator = request.session.get("admin") or "system"
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else ""))
        result = restore_backup(name, operator=str(operator), ip=ip)
        return {"code": 200 if result.get("ok") else 500, **result}

    @api_router.post("/backups/clean")
    def api_backup_clean(request: Request, payload: dict):
        """手动清理过期备份文件（按保留天数删除，含 .invalid 与校验文件）"""
        from ..services.backup import prune_backup_dir

        try:
            days = int(payload.get("days", 30))
        except (TypeError, ValueError):
            return {"code": 400, "msg": "保留天数必须是数字"}
        if days < 1 or days > 3650:
            return {"code": 400, "msg": "保留天数应在 1 - 3650 之间"}
        directory = str(payload.get("dir") or "").strip() or None
        if directory:
            directory = os.path.abspath(os.path.expanduser(directory))
            if not os.path.isdir(directory):
                return {"code": 400, "msg": "备份目录不存在或不可访问"}
        try:
            removed = prune_backup_dir(directory, days)
        except Exception as e:
            return {"code": 500, "msg": f"清理失败：{e}"}
        save_setting("backup_last_clean", time.strftime("%Y-%m-%d %H:%M:%S"))
        save_setting("backup_last_deleted", len(removed))
        _audit(request, "backup_clean", "备份文件",
               f"手动清理 {days} 天前备份，删除 {len(removed)} 个文件")
        return {"code": 200, "msg": f"已删除 {len(removed)} 个过期备份文件",
                "deleted": len(removed), "removed": removed[:50]}

    app.include_router(page_router)
    app.include_router(api_router)
