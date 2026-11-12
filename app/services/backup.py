# -*- coding: utf-8 -*-
"""MySQL 自动备份

- 优先调用 mysqldump（一致性快照、速度快）
- 未安装 mysqldump 时回退为纯 Python 逻辑导出（SHOW CREATE TABLE + 分批 INSERT）
- 输出统一 gzip 压缩，按 BACKUP_KEEP_DAYS 清理过期文件
- 调度线程每天在 BACKUP_HOUR 点执行一次，当天已有备份则跳过

也可以直接执行：python -m app.services.backup
"""
import glob
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import date, datetime

from ..core import config
from ..core.db import Base, SessionLocal, engine
from ..core.models import AuditLogDB, ScriptScheduleDB

BACKUP_PREFIX = "my_api_db_"


def _backup_dir(directory=None):
    """返回实际使用的备份目录，未传时使用 config.BACKUP_DIR"""
    return os.path.abspath(directory) if directory else config.BACKUP_DIR


def _sha256(path):
    """计算备份文件 SHA-256，便于核对文件是否被篡改"""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _gzip_uncompressed_size(path):
    """读取 gzip 尾部 ISIZE 字段（原始大小，超过 4GB 会回绕），用于恢复前估算磁盘需求"""
    try:
        if os.path.getsize(path) < 18:
            return 0
        with open(path, "rb") as f:
            f.seek(-4, os.SEEK_END)
            return int.from_bytes(f.read(4), "little")
    except Exception:
        return 0


def _validate(path):
    """校验备份文件是否为空、是否损坏、是否包含建表语句"""
    if not os.path.exists(path):
        return False, "备份文件不存在"
    size = os.path.getsize(path)
    if size <= 0:
        return False, "备份文件为空"
    try:
        has_schema = False
        with gzip.open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                if b"CREATE TABLE" in chunk:
                    has_schema = True
    except Exception as e:
        return False, f"备份文件损坏或不是有效 gzip：{e}"
    if not has_schema:
        return False, "备份内容缺少建表语句，文件可能不完整"
    return True, "校验通过"


def _write_meta(path, meta):
    """写入备份元信息，失败后也能据此排查"""
    try:
        with open(path + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        with open(path + ".sha256", "w", encoding="utf-8") as f:
            f.write(f"{meta.get('sha256', '')}  {os.path.basename(path)}\n")
    except Exception as e:
        print(f"[备份] 写入校验信息失败：{e}")


def _read_meta(path):
    try:
        with open(path + ".meta.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _notify(result, operator):
    """备份异常通知：配置企业微信机器人 Webhook 后主动推送"""
    webhook = getattr(config, "BACKUP_NOTIFY_WEBHOOK", "") or ""
    if not webhook:
        return
    if result.get("ok") and not getattr(config, "BACKUP_NOTIFY_SUCCESS", False):
        return
    status = "成功" if result.get("ok") else "失败"
    content = (
        f"【数据库备份{status}】\n"
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"操作人：{operator or 'system'}\n"
        f"文件：{result.get('name') or '-'}\n"
        f"大小：{result.get('size', 0)} 字节，耗时：{result.get('duration', 0)}s\n"
        f"方式：{result.get('method') or '-'}\n"
        f"校验：{result.get('verify') or '-'}"
        + (f"\n失败原因：{result.get('error')}" if result.get("error") else "")
    )
    try:
        import requests
        resp = requests.post(webhook, json={"msgtype": "text", "text": {"content": content}},
                             timeout=8)
        if resp.status_code >= 400:
            print(f"[备份] 通知推送失败：HTTP {resp.status_code}")
    except Exception as e:
        print(f"[备份] 通知推送异常：{e}")


def _audit_backup(result, operator, ip, action="backup_run"):
    """备份/恢复成功或失败都写入审计日志，便于事后排查"""
    try:
        detail = (
            f"方式：{result.get('method')}，文件：{result.get('name')}，"
            f"大小：{result.get('size', 0)} 字节，耗时：{result.get('duration', 0)}s，"
            f"校验：{result.get('verify') or '-'}"
            + (f"，失败原因：{result.get('error')}" if result.get("error") else "")
            + f"，清理旧备份：{len(result.get('removed') or [])} 个"
        )
        with SessionLocal() as db:
            db.add(AuditLogDB(
                operator=(operator or "system")[:100], action=action,
                target=str(result.get("name") or "")[:255], detail=detail[:60000],
                ip=(ip or "")[:100],
                result="success" if result.get("ok") else "failed",
            ))
            db.commit()
    except Exception as e:
        print(f"[备份] 写审计日志失败：{e}")
    _notify(result, operator)


def backup_files(directory=None):
    """返回按时间倒序排列的备份文件信息"""
    pattern = os.path.join(_backup_dir(directory), BACKUP_PREFIX + "*.sql.gz")
    items = []
    for path in glob.glob(pattern):
        try:
            st = os.stat(path)
        except OSError:
            continue
        meta = _read_meta(path)
        items.append({
            "name": os.path.basename(path),
            "size": st.st_size,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            "method": meta.get("method", ""),
            "sha256": meta.get("sha256", ""),
            "verified": bool(meta.get("verified")),
            "remark": meta.get("remark", ""),
        })
    return sorted(items, key=lambda x: x["created_at"], reverse=True)


def prune_backup_dir(directory=None, keep_days=None):
    """删除指定目录下超过保留天数的备份（含 .invalid）及校验文件，返回文件列表

    供自动清理任务复用；目录不存在时直接返回空列表。
    """
    directory = _backup_dir(directory)
    days = max(1, int(keep_days if keep_days is not None else config.BACKUP_KEEP_DAYS))
    cutoff = time.time() - days * 86400
    removed = []
    for pattern in (BACKUP_PREFIX + "*.sql.gz", BACKUP_PREFIX + "*.sql.gz.invalid"):
        for path in glob.glob(os.path.join(directory, pattern)):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    for suffix in (".meta.json", ".sha256"):
                        try:
                            os.remove(path + suffix)
                        except OSError:
                            pass
                    removed.append(os.path.basename(path))
            except OSError:
                continue
    return removed


def _prune(directory=None):
    """删除超过保留天数的备份（含校验失败的 .invalid 文件）及校验文件"""
    return prune_backup_dir(directory, config.BACKUP_KEEP_DAYS)


def _find_client(program):
    """在 PATH 与 Windows 常见安装目录中查找 MySQL 客户端（mysqldump / mysql）
    返回可执行文件路径，找不到返回 None。备份与恢复流程共用。
    """
    exe = shutil.which(program)
    if exe:
        return exe
    if os.name != "nt":
        return None
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
    candidates = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for vendor in ("MySQL", "MariaDB"):
            base = os.path.join(root, vendor)
            if not os.path.isdir(base):
                continue
            try:
                names = sorted(os.listdir(base), reverse=True)
            except OSError:
                continue
            for name in names:
                candidates.append(os.path.join(base, name, "bin", f"{program}.exe"))
    candidates += [
        rf"C:\xampp\mysql\bin\{program}.exe",
        rf"C:\wamp64\bin\mysql\{program}.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def find_mysqldump():
    """查找 mysqldump：优先 PATH，Windows 下再扫描常见安装目录"""
    return _find_client("mysqldump")


def _mysqldump(path):
    """用 mysqldump 生成备份；不可用或失败返回 False"""
    exe = find_mysqldump()
    if not exe:
        print("[备份] 未找到 mysqldump，将回退为纯 Python 逻辑导出（较慢）。"
              "Windows 可把 MySQL 的 bin 目录加入系统 PATH。")
        return False
    env = os.environ.copy()
    env["MYSQL_PWD"] = config.DB_PWD          # 密码走环境变量，不出现在命令行/日志
    cmd = [
        exe, "-h", str(config.DB_HOST), "-P", str(config.DB_PORT),
        "-u", str(config.DB_USER), "--single-transaction", "--quick",
        "--skip-lock-tables", "--no-tablespaces",
        "--default-character-set=utf8mb4", config.DB_NAME,
    ]
    try:
        # 注意：不能把 gzip.GzipFile 直接作为 subprocess 的 stdout——它的 fileno()
        # 指向底层原始文件，mysqldump 会绕过压缩器写明文 SQL。这里改为读取管道再压缩。
        with gzip.open(path, "wb", compresslevel=6) as out:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, shell=False)
            deadline = time.time() + 3600
            try:
                while True:
                    chunk = proc.stdout.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    if time.time() > deadline:
                        proc.kill()
                        raise TimeoutError("mysqldump 超时（>3600s）")
                err = proc.stderr.read()
                rc = proc.wait()
            except Exception:
                proc.kill()
                proc.wait()
                raise
        if rc == 0:
            return True
        print(f"[备份] mysqldump 失败：{err.decode('utf-8', 'replace')[:300]}")
    except Exception as e:
        print(f"[备份] mysqldump 异常：{e}")
    try:
        os.remove(path)
    except OSError:
        pass
    return False


def _literal(value):
    """把 Python 值转成 MySQL 字面量（逻辑导出兜底用）"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return "'" + value.isoformat(sep=" ") + "'"
    text = str(value)
    text = (text.replace("\\", "\\\\").replace("'", "\\'")
            .replace("\n", "\\n").replace("\r", "\\r"))
    return "'" + text + "'"


def _python_dump(path):
    """纯 Python 逻辑导出，返回是否成功"""
    try:
        with gzip.open(path, "wt", encoding="utf-8", newline="\n") as f:
            f.write(f"-- 逻辑备份 {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("SET NAMES utf8mb4;\nSET FOREIGN_KEY_CHECKS=0;\n\n")
            with engine.connect() as conn:
                for table in Base.metadata.sorted_tables:
                    name = table.name
                    row = conn.exec_driver_sql(f"SHOW CREATE TABLE `{name}`").first()
                    if not row:
                        continue
                    f.write(f"DROP TABLE IF EXISTS `{name}`;\n{row[1]};\n")
                    cols = ", ".join(f"`{c.name}`" for c in table.columns)
                    result = conn.execution_options(stream_results=True).exec_driver_sql(
                        f"SELECT {cols} FROM `{name}`")
                    batch = []
                    for record in result:
                        batch.append("(" + ",".join(_literal(v) for v in record) + ")")
                        if len(batch) >= 200:
                            f.write(f"INSERT INTO `{name}` ({cols}) VALUES\n"
                                    + ",\n".join(batch) + ";\n")
                            batch = []
                    if batch:
                        f.write(f"INSERT INTO `{name}` ({cols}) VALUES\n"
                                + ",\n".join(batch) + ";\n")
                    f.write("\n")
            f.write("SET FOREIGN_KEY_CHECKS=1;\n")
        return True
    except Exception as e:
        print(f"[备份] 逻辑导出失败：{e}")
        try:
            os.remove(path)
        except OSError:
            pass
        return False


def run_backup(operator="system", ip="", directory=None):
    """执行一次备份并校验有效性，返回 {ok, name, size, duration, method, error, verify, ...}

    directory 为空时使用 config.BACKUP_DIR；定时任务可单独指定备份目录。
    """
    backup_dir = _backup_dir(directory)
    os.makedirs(backup_dir, exist_ok=True)
    name = BACKUP_PREFIX + time.strftime("%Y%m%d_%H%M%S") + ".sql.gz"
    path = os.path.join(backup_dir, name)
    started = time.time()
    method = "mysqldump" if _mysqldump(path) else "python"
    if method == "python" and not _python_dump(path):
        result = {"ok": False, "name": name, "size": 0, "duration": round(time.time() - started, 2),
                  "method": method, "error": "备份失败，详见服务日志", "verify": "",
                  "sha256": "", "removed": []}
        _audit_backup(result, operator, ip)
        return result

    size = os.path.getsize(path) if os.path.exists(path) else 0
    ok, verify = _validate(path)
    digest = ""
    if ok:
        try:
            digest = _sha256(path)
        except Exception as e:
            ok, verify = False, f"计算校验值失败：{e}"

    if not ok:
        # 校验不通过的文件改名标记，避免被当作有效备份，同时保留用于排查
        bad_path = path + ".invalid"
        try:
            os.replace(path, bad_path)
        except OSError:
            bad_path = path
        result = {"ok": False, "name": name, "size": size,
                  "duration": round(time.time() - started, 2), "method": method,
                  "error": f"{verify}（文件已标记为 {os.path.basename(bad_path)}）",
                  "verify": verify, "sha256": "", "removed": []}
        print(f"[备份] 校验失败：{name}，{verify}")
        _audit_backup(result, operator, ip)
        return result

    _write_meta(path, {
        "name": name, "size": size, "sha256": digest, "method": method,
        "duration": round(time.time() - started, 2), "verified": True,
        "message": verify, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    removed = _prune(backup_dir)
    result = {"ok": True, "name": name, "size": size,
              "duration": round(time.time() - started, 2),
              "method": method, "error": "", "verify": verify,
              "sha256": digest, "removed": removed}
    print(f"[备份] 完成：{name}（{size / 1024 / 1024:.1f} MB，{result['duration']}s，{method}，{verify}）")
    _audit_backup(result, operator, ip)
    return result


def restore_backup(name, operator="system", ip="", directory=None):
    """把指定备份恢复到当前数据库（覆盖同名表，属高风险操作）。

    安全措施：文件名白名单校验、gzip 完整性校验、元信息 sha256 比对、
    恢复前自动对当前库再备份一次（保留回退余地）。
    返回 {ok, name, duration, method, error, verify, pre_backup}
    """
    backup_dir = _backup_dir(directory)
    safe_name = os.path.basename(str(name or ""))
    started = time.time()
    result = {"ok": False, "name": safe_name, "duration": 0, "method": "mysql",
              "error": "", "verify": "", "pre_backup": ""}
    if not safe_name.startswith(BACKUP_PREFIX) or not safe_name.endswith(".sql.gz"):
        result["error"] = "备份文件名不合法"
        return result
    path = os.path.join(backup_dir, safe_name)
    if not os.path.isfile(path):
        result["error"] = "备份文件不存在"
        return result

    ok, verify = _validate(path)
    result["verify"] = verify
    if not ok:
        result["error"] = verify
        _audit_backup(result, operator, ip, action="backup_restore")
        return result
    meta = _read_meta(path)
    expected = str(meta.get("sha256") or "")
    if expected:
        actual = _sha256(path)
        if actual != expected:
            result["error"] = "校验值与备份元信息不一致，文件可能被篡改"
            _audit_backup(result, operator, ip, action="backup_restore")
            return result

    # 恢复前检查磁盘空间，避免解压/写入过程把磁盘写满
    need = _gzip_uncompressed_size(path)
    if need:
        free = shutil.disk_usage(backup_dir).free
        margin = 100 * 1024 * 1024
        if free < need + margin:
            result["error"] = (f"磁盘剩余空间不足：解压约需 {need // (1024 * 1024)} MB，"
                               f"当前剩余 {free // (1024 * 1024)} MB，已中止恢复")
            _audit_backup(result, operator, ip, action="backup_restore")
            return result

    exe = _find_client("mysql")
    if not exe:
        result["error"] = "未找到 mysql 客户端，无法执行恢复（Linux 可执行 apt install mysql-client 后重试）"
        _audit_backup(result, operator, ip, action="backup_restore")
        return result

    # 恢复前先备份当前库，便于回退
    pre = run_backup(operator=operator, ip=ip)
    result["pre_backup"] = pre.get("name") or ""
    if not pre.get("ok"):
        result["error"] = f"恢复前自动备份失败，已中止：{pre.get('error') or '未知错误'}"
        _audit_backup(result, operator, ip, action="backup_restore")
        return result

    env = os.environ.copy()
    env["MYSQL_PWD"] = config.DB_PWD      # 密码走环境变量，不出现在命令行
    cmd = [
        exe, "-h", str(config.DB_HOST), "-P", str(config.DB_PORT),
        "-u", str(config.DB_USER), "--default-character-set=utf8mb4",
        config.DB_NAME,
    ]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, shell=False)
        deadline = time.time() + 3600
        with gzip.open(path, "rb") as src:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                if time.time() > deadline:
                    raise TimeoutError("mysql 导入超时（>3600s）")
        proc.stdin.close()
        err = proc.stderr.read()
        rc = proc.wait()
        if rc != 0:
            result["error"] = "导入失败：" + err.decode("utf-8", "replace")[:300]
            _audit_backup(result, operator, ip, action="backup_restore")
            return result
    except Exception as e:
        if proc:
            proc.kill()
            proc.wait()
        result["error"] = f"恢复异常：{e}"
        _audit_backup(result, operator, ip, action="backup_restore")
        return result

    result["ok"] = True
    result["duration"] = round(time.time() - started, 2)
    print(f"[备份] 已恢复 {safe_name}，耗时 {result['duration']}s，回退备份 {result['pre_backup']}")
    _audit_backup(result, operator, ip, action="backup_restore")
    return result


def _has_today_backup():
    today = time.strftime("%Y%m%d")
    return any(os.path.basename(p).startswith(BACKUP_PREFIX + today)
               for p in glob.glob(os.path.join(config.BACKUP_DIR, BACKUP_PREFIX + "*.sql.gz")))


def start_scheduler():
    """启动每日备份线程；未启用或后台已配置备份定时任务时返回 None"""
    if not config.BACKUP_ENABLED:
        return None
    # 后台「定时任务」中已有启用的备份任务时，交给调度线程执行，避免重复备份
    try:
        with SessionLocal() as db:
            exists = db.query(ScriptScheduleDB).filter(
                ScriptScheduleDB.task_type == "backup",
                ScriptScheduleDB.enabled.is_(True)).first()
        if exists:
            print("[备份] 已存在后台备份定时任务，跳过内置每日备份线程")
            return None
    except Exception:
        pass

    def loop():
        time.sleep(30)
        while True:
            try:
                if time.localtime().tm_hour == config.BACKUP_HOUR and not _has_today_backup():
                    run_backup(operator="system")
            except Exception as e:
                print(f"[备份] 定时任务异常：{e}")
            time.sleep(max(300, config.BACKUP_CHECK_INTERVAL))

    thread = threading.Thread(target=loop, daemon=True, name="db-backup")
    thread.start()
    return thread


if __name__ == "__main__":
    print(run_backup())
