# -*- coding: utf-8 -*-
"""告警中心本地存储：数据库不可用时仍可记录事件、通知结果和重试状态。"""
import json
import os
import threading
import time
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
EVENT_FILE = os.path.join(DATA_DIR, "alert_events.jsonl")
RUNTIME_FILE = os.path.join(DATA_DIR, "alert_runtime.json")
RUNTIME_LOCK_FILE = os.path.join(DATA_DIR, "alert_runtime.lock")
MAX_EVENTS = 5000
_LOCK = threading.RLock()


def _open_runtime_lock():
    os.makedirs(DATA_DIR, exist_ok=True)
    return open(RUNTIME_LOCK_FILE, "a+b")


def _acquire_file_lock(handle):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_file_lock(handle):
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _read_runtime():
    try:
        with open(RUNTIME_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _with_store_lock(callback):
    """跨进程串行化：先拿进程内锁，再拿文件锁，统一锁顺序避免死锁。

    多实例共用同一 data 目录时，追加事件、重写事件、读写 runtime 都必须走这里，
    否则并发重写会互相覆盖事件、runtime 也会被后写者整体覆盖。
    """
    lock_handle = _open_runtime_lock()
    try:
        with _LOCK:
            _acquire_file_lock(lock_handle)
            try:
                return callback()
            finally:
                _release_file_lock(lock_handle)
    finally:
        try:
            lock_handle.close()
        except OSError:
            pass


def append_event(path, kind, value=0, threshold=0, message="", event_type="",
                 notify_status="未通知"):
    """先写本地事件再执行通知；返回事件 ID 供异步通知结果回写。"""
    event_id = f"local-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    event = {
        "id": event_id,
        "path": str(path or "")[:200],
        "kind": str(kind or "")[:50],
        "value": int(value or 0),
        "threshold": int(threshold or 0),
        "message": str(message or "")[:1000],
        "event_type": str(event_type or "")[:30],
        "notify_status": str(notify_status or "未通知")[:100],
        "time": now,
        "timestamp": time.time(),
        "source": "local",
    }
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    def _write_event():
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(EVENT_FILE, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            with open(EVENT_FILE, "r", encoding="utf-8") as handle:
                event_count = sum(1 for _ in handle)
            if event_count > MAX_EVENTS:
                events = _read_events_unlocked()
                _write_events_unlocked(events[-4000:])
        except OSError:
            pass

    _with_store_lock(_write_event)
    return event_id


def set_notification(event_id, status):
    if not event_id:
        return

    def _txn():
        runtime = _read_runtime()
        notifications = runtime.setdefault("notifications", {})
        item = notifications.setdefault(str(event_id), {})
        item["status"] = str(status or "未通知")[:100]
        item["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(RUNTIME_FILE, runtime)

    try:
        _with_store_lock(_txn)
    except OSError:
        pass


def update_notification(event_id, channel, ok, error=""):
    if not event_id:
        return

    def _txn():
        runtime = _read_runtime()
        notifications = runtime.setdefault("notifications", {})
        item = notifications.setdefault(str(event_id), {})
        channels = item.setdefault("channels", {})
        channels[str(channel)] = {
            "ok": bool(ok),
            "error": str(error or "")[:200],
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        item["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(RUNTIME_FILE, runtime)

    try:
        _with_store_lock(_txn)
    except OSError:
        pass


def _notification_text(info):
    if not isinstance(info, dict):
        return "未通知"
    labels = {"wechat": "企微", "mail": "邮件"}
    sent, failed = [], []
    for channel, result in (info.get("channels") or {}).items():
        if not isinstance(result, dict):
            continue
        label = labels.get(channel, str(channel))
        (sent if result.get("ok") else failed).append(label)
    parts = []
    if sent:
        parts.append("已发" + "、".join(sent))
    if failed:
        parts.append("、".join(failed) + "发送失败")
    if parts:
        return "；".join(parts)
    return str(info.get("status") or "未通知")


def _read_events_unlocked():
    events = []
    try:
        with open(EVENT_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict) and item.get("id"):
                        events.append(item)
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass
    return events


def _write_events_unlocked(events):
    os.makedirs(DATA_DIR, exist_ok=True)
    temp = f"{EVENT_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, EVENT_FILE)


def read_events(limit=500):
    """返回本地事件并合并异步通知结果，数据库异常不会进入此路径。"""
    def _txn():
        notifications = _read_runtime().get("notifications") or {}
        events = _read_events_unlocked()
        return notifications, events

    notifications, events = _with_store_lock(_txn)
    result = []
    for event in events:
        item = dict(event)
        info = notifications.get(str(item.get("id"))) or {}
        item["notify_status"] = (_notification_text(info) if info else
                                 str(item.get("notify_status") or "未通知"))
        channels = info.get("channels") if isinstance(info, dict) else None
        item["notify_channels"] = channels if isinstance(channels, dict) else {}
        result.append(item)
    result.sort(key=lambda row: (float(row.get("timestamp") or 0), str(row.get("id") or "")),
                reverse=True)
    return result[:max(1, min(MAX_EVENTS, int(limit or 500)))]


def save_runtime(state):
    """保存巡检与重试快照，同时保留通知结果、通知冷却时间与跨进程网络状态。"""
    if not isinstance(state, dict):
        return

    def _txn():
        runtime = _read_runtime()
        notifications = runtime.get("notifications") or {}
        alert_last = runtime.get("alert_last") or {}
        network_state = runtime.get("network_state") or {}
        runtime.clear()
        runtime.update(state)
        runtime["notifications"] = notifications
        runtime["alert_last"] = alert_last
        if isinstance(network_state, dict) and network_state:
            runtime["network_state"] = network_state
        runtime["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(RUNTIME_FILE, runtime)

    try:
        _with_store_lock(_txn)
    except OSError:
        pass


def reserve_notification(key, cooldown):
    """跨进程占用通知发送名额，避免多实例在同一冷却窗口内重复发送。"""
    try:
        cooldown = max(1, int(cooldown))
    except (TypeError, ValueError):
        cooldown = 30

    def _txn():
        runtime = _read_runtime()
        alert_last = runtime.setdefault("alert_last", {})
        now = time.time()
        if now - float(alert_last.get(str(key)) or 0) < cooldown:
            return False
        alert_last[str(key)] = now
        _write_json(RUNTIME_FILE, runtime)
        return True

    try:
        return bool(_with_store_lock(_txn))
    except OSError:
        return False


def mutate_network_state(mutator):
    """跨进程原子更新网络巡检状态（连续失败计数 + 告警是否已激活）。

    mutator 接收可修改的状态字典，返回业务结果；状态写回 runtime 文件，
    这样多实例共享同一份“是否已告警”的判断，避免重复告警或漏发恢复事件。
    """
    def _txn():
        runtime = _read_runtime()
        state = runtime.get("network_state")
        if not isinstance(state, dict):
            state = {"failures": 0, "alert_active": False}
        result = mutator(state)
        state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        runtime["network_state"] = state
        runtime["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(RUNTIME_FILE, runtime)
        return result

    try:
        return _with_store_lock(_txn)
    except OSError:
        return None


def load_runtime():
    def _txn():
        runtime = _read_runtime()
        runtime.pop("notifications", None)
        return runtime

    return _with_store_lock(_txn)


def clear_events():
    """只清空事件和通知结果，不清除正在进行的重试计数。"""
    def _txn():
        removed = len(_read_events_unlocked())
        _write_events_unlocked([])
        runtime = _read_runtime()
        runtime["notifications"] = {}
        _write_json(RUNTIME_FILE, runtime)
        return removed

    return _with_store_lock(_txn)
