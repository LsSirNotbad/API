# -*- coding: utf-8 -*-
"""环境自检脚本：迁移服务器或排查启动问题时先运行一次

检查项：
- 关键环境变量（敏感值自动掩码）
- MySQL 连通性、数据库字符集、版本
- mysqldump 是否可用
- 备份目录 / 数据目录的读写权限
- Python 与主要依赖版本

用法：python scripts/check_env.py
"""
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.parse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

SECRET_KEYS = {"API_TOKEN", "ADMIN_PASSWORD", "ADMIN_SECRET", "DB_PWD",
               "BACKUP_NOTIFY_WEBHOOK", "SM4_KEY"}


def mask(value):
    value = str(value or "")
    if not value:
        return "(空)"
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]


def check_env_vars():
    print("== 关键环境变量 ==")
    keys = ["APP_ENV", "APP_VERSION", "DOCS_ENABLED", "DB_HOST", "DB_PORT",
            "DB_USER", "DB_PWD", "DB_NAME", "API_TOKEN", "ADMIN_USER",
            "ADMIN_PASSWORD", "ADMIN_SECRET", "BACKUP_DIR", "BACKUP_KEEP_DAYS",
            "BACKUP_NOTIFY_WEBHOOK", "CACHE_TTL", "IMPORT_MAX_MB"]
    from app.core import config
    for key in keys:
        value = os.getenv(key)
        if value is None:
            value = getattr(config, key, "")
        print(f"  {key:<24}{mask(value) if key in SECRET_KEYS else value}")


def check_mysql():
    print("\n== MySQL ==")
    from app.core import config
    ok = True
    try:
        import pymysql
        conn = pymysql.connect(
            host=config.DB_HOST, port=int(config.DB_PORT), user=config.DB_USER,
            password=config.DB_PWD, database=config.DB_NAME,
            connect_timeout=5, charset="utf8mb4")
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            print(f"  连接正常，MySQL 版本：{cur.fetchone()[0]}")
            cur.execute("SELECT @@character_set_database, @@collation_database")
            charset, collation = cur.fetchone()
            print(f"  数据库字符集：{charset} / {collation}")
            if charset != "utf8mb4":
                ok = False
                print("  [警告] 数据库字符集不是 utf8mb4，可能出现中文乱码")
            cur.execute("SHOW VARIABLES LIKE 'max_connections'")
            row = cur.fetchone()
            if row:
                print(f"  最大连接数：{row[1]}")
            cur.execute("SHOW STATUS LIKE 'Threads_connected'")
            row = cur.fetchone()
            if row:
                print(f"  当前连接数：{row[1]}")
        conn.close()
    except Exception as e:
        ok = False
        print(f"  [失败] 无法连接 MySQL：{e}")
    return ok


def check_mysqldump():
    print("\n== mysqldump ==")
    try:
        from app.services.backup import find_mysqldump
        exe = find_mysqldump()
    except Exception:
        exe = shutil.which("mysqldump")
    if not exe:
        print("  [警告] 未找到 mysqldump，备份将回退为纯 Python 逻辑导出（较慢）")
        if os.name == "nt":
            print("         Windows 处理：把 MySQL 安装目录下的 bin 加入系统 PATH，"
                  "例如 C:\\Program Files\\MySQL\\MySQL Server 8.0\\bin，然后重开终端。")
        return False
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        print(f"  路径：{exe}")
        print(f"  版本：{(out.stdout or out.stderr).strip()}")
        return True
    except Exception as e:
        print(f"  [警告] mysqldump 执行失败：{e}")
        return False


def check_dir(label, path, need_write=True):
    print(f"  {label}：{path}")
    if not os.path.exists(path):
        try:
            os.makedirs(path, exist_ok=True)
            print("    目录不存在，已自动创建")
        except OSError as e:
            print(f"    [失败] 无法创建目录：{e}")
            return False
    if not os.access(path, os.R_OK):
        print("    [失败] 目录不可读")
        return False
    if need_write:
        probe = os.path.join(path, ".write_test")
        try:
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
            print("    读写权限正常")
        except OSError as e:
            print(f"    [失败] 目录不可写：{e}")
            return False
    return True


def check_dirs():
    print("\n== 目录权限 ==")
    from app.core import config
    ok = True
    ok &= check_dir("备份目录", config.BACKUP_DIR)
    ok &= check_dir("数据目录", os.path.join(BASE_DIR, "data"), need_write=False)
    ok &= check_dir("模板目录", os.path.join(BASE_DIR, "templates"), need_write=False)
    return ok


def check_versions():
    print("\n== 版本信息 ==")
    print(f"  操作系统：{platform.platform()}")
    print(f"  Python：{sys.version.split()[0]}")
    print(f"  主机名：{socket.gethostname()}")
    for mod in ("fastapi", "uvicorn", "sqlalchemy", "pymysql", "requests", "openpyxl"):
        try:
            m = __import__(mod)
            print(f"  {mod}：{getattr(m, '__version__', '已安装')}")
        except Exception:
            print(f"  [警告] {mod} 未安装")


def check_disk():
    """磁盘剩余空间检查：低于 10% 判定异常（与自愈巡检阈值一致）"""
    print("\n== 磁盘空间 ==")
    from app.core import config
    ok = True
    try:
        target = os.path.abspath(os.sep)
        usage = shutil.disk_usage(target)
        free_pct = usage.free * 100.0 / max(1, usage.total)
        print(f"  {target} 总容量 {usage.total / 1024 ** 3:.1f} GB，"
              f"剩余 {usage.free / 1024 ** 3:.1f} GB（{free_pct:.1f}%）")
        if free_pct < 10:
            ok = False
            print("  [失败] 磁盘剩余空间不足 10%")
            print(f"        修复指引：清理 {config.BACKUP_DIR} 下的旧备份、"
                  f"{os.path.join(BASE_DIR, 'logs')} 目录旧日志，再重新自检。")
    except Exception as e:
        ok = False
        print(f"  [失败] 无法读取磁盘信息：{e}")
        print("        修复指引：确认服务运行账号对磁盘有读取权限，或改用绝对路径重新运行。")
    return ok


# 后台依赖的静态文件：缺失会导致图表/图标加载失败
STATIC_FILES = [
    os.path.join("static", "echarts.min.js"),
    os.path.join("static", "favicon.ico"),
    os.path.join("templates", "admin_dashboard.html"),
    os.path.join("templates", "admin_login.html"),
]


def check_static():
    """静态文件完整性检查：缺失时给出 CDN 兜底说明"""
    print("\n== 静态文件 ==")
    ok = True
    for rel in STATIC_FILES:
        path = os.path.join(BASE_DIR, rel)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            print(f"  正常：{rel}（{os.path.getsize(path) / 1024:.1f} KB）")
            continue
        ok = False
        print(f"  [失败] 缺失或为空：{rel}")
        if rel.endswith("echarts.min.js"):
            print("        修复指引：图表会自动从 CDN 兜底加载；如需离线可用，"
                  "请把 echarts.min.js 放回 static/ 目录。")
        elif rel.endswith(".ico"):
            print("        修复指引：把 favicon.ico 放回 static/ 目录，否则浏览器标签页图标会 404。")
        else:
            print("        修复指引：后台页面模板缺失，请从代码仓库重新拉取 templates/ 目录。")
    return ok


def check_net_api():
    """外网数据源连通性：DNS + HTTP 各测一次，失败给出排查指引"""
    print("\n== 外网数据源 ==")
    from app.core import config
    api = str(getattr(config, "NET_API", "") or "")
    if not api:
        print("  [警告] 未配置 NET_API，实时数据接口将不可用")
        return False
    host = urllib.parse.urlparse(api).hostname or ""
    print(f"  NET_API：{api}")
    ok = True
    try:
        socket.create_connection((host, 443), timeout=5).close()
        print(f"  DNS / TCP：{host}:443 可达")
    except Exception as e:
        ok = False
        print(f"  [失败] 无法连接 {host}:443（{e}）")
        print("        修复指引：检查服务器外网访问与 DNS，"
              "或在 config.py / 环境变量 NET_API 中改为可访问的地址。")
    try:
        import requests
        resp = requests.get(api, timeout=8,
                            headers=getattr(config, "NET_HEADERS", {}) or {})
        print(f"  HTTP：状态码 {resp.status_code}，耗时 {resp.elapsed.total_seconds() * 1000:.0f} ms")
        if resp.status_code >= 500:
            ok = False
            print("        修复指引：数据源服务端异常，可稍后重试；"
                  "持续失败时检查 NET_API 是否更换了地址。")
    except Exception as e:
        ok = False
        print(f"  [失败] HTTP 请求失败：{e}")
        print("        修复指引：外网访问失败，请检查 NET_API 地址、防火墙或代理设置"
              "（例如需要配置 HTTPS_PROXY）。")
    return ok


def main():
    print("====== 环境自检 ======\n")
    check_versions()
    check_env_vars()
    mysql_ok = check_mysql()
    dump_ok = check_mysqldump()
    dirs_ok = check_dirs()
    disk_ok = check_disk()
    static_ok = check_static()
    net_ok = check_net_api()
    print("\n====== 结论 ======")
    print(f"  MySQL：{'正常' if mysql_ok else '异常'}")
    print(f"  mysqldump：{'可用' if dump_ok else '不可用（可回退逻辑导出）'}")
    print(f"  目录权限：{'正常' if dirs_ok else '异常'}")
    print(f"  磁盘空间：{'正常' if disk_ok else '不足（低于 10%）'}")
    print(f"  静态文件：{'完整' if static_ok else '有缺失'}")
    print(f"  外网数据源：{'正常' if net_ok else '不可达'}")
    if mysql_ok and dirs_ok and disk_ok and static_ok:
        print("  环境可用于启动服务")
        return 0
    print("  请先修复上面的失败项再启动服务（每项均附修复指引）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
