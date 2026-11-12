# -*- coding: utf-8 -*-
"""分布式互斥锁：Redis → MySQL GET_LOCK → 进程内锁 三级降级。

多实例部署时，定时任务、自愈巡检、孤儿进程守护等线程必须"只让一台机器执行"，
统一用这里的 acquire()：

    handle = dist_lock.acquire("schedule:12", ttl=3600)
    if handle:
        try:
            run_task()
        finally:
            handle.release()
    else:
        pass   # 别的实例正在执行，本实例跳过

Redis 用 SET NX EX 抢锁 + Lua 比对令牌释放，避免误删别人的锁；
没有 Redis 时用 MySQL 的 GET_LOCK()（跨实例有效，同一连接持有到释放）。
"""
import os
import socket
import threading
import uuid

_LOCAL = {}
_LOCAL_GUARD = threading.Lock()


def host_id():
    """当前实例标识：优先 HOST_ID 环境变量，其次主机名"""
    return (os.getenv("HOST_ID") or socket.gethostname() or "unknown")[:100]


def _local_lock(name):
    with _LOCAL_GUARD:
        return _LOCAL.setdefault(name, threading.Lock())


class _Handle:
    """已获取的锁：release() 时按后端类型释放"""

    def __init__(self, name, token, backend, local_lock, raw_conn=None, client=None):
        self.name = name
        self.token = token
        self.backend = backend
        self._local = local_lock
        self._conn = raw_conn
        self._client = client
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        try:
            if self.backend == "redis" and self._client is not None:
                script = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
                          "return redis.call('del', KEYS[1]) else return 0 end")
                self._client.eval(script, 1, f"lock:{self.name}", self.token)
            elif self.backend == "mysql" and self._conn is not None:
                cur = self._conn.cursor()
                cur.execute("SELECT RELEASE_LOCK(%s)", (self.name,))
                cur.close()
                self._conn.close()
        except Exception:
            pass
        try:
            self._local.release()
        except Exception:
            pass


def _mysql_acquire(name, wait=0):
    """用 MySQL GET_LOCK 抢锁；成功返回连接（保持到 release），失败返回 None"""
    try:
        from .db import engine
        conn = engine.raw_connection()
    except Exception:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT GET_LOCK(%s, %s)", (name[:64], int(wait)))
        ok = (cur.fetchone() or [0])[0] == 1
        cur.close()
        if ok:
            return conn
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
    return None


def acquire(name, ttl=600, wait=0):
    """尝试获取名为 name 的分布式锁，成功返回 handle，失败返回 None"""
    name = str(name)[:64]
    local_lock = _local_lock(name)
    if not local_lock.acquire(blocking=False):
        return None
    token = uuid.uuid4().hex
    try:
        from .cache import get_redis
        client = get_redis()
    except Exception:
        client = None
    if client is not None:
        try:
            if client.set(f"lock:{name}", token, nx=True, ex=max(1, int(ttl))):
                return _Handle(name, token, "redis", local_lock, client=client)
        except Exception:
            pass
        local_lock.release()
        return None
    conn = _mysql_acquire(name, wait)
    if conn is not None:
        return _Handle(name, token, "mysql", local_lock, raw_conn=conn)
    local_lock.release()
    return None


def release_all():
    """进程退出时无需处理（Redis 锁有 TTL，MySQL 连接关闭自动释放）"""
    return True


def run_once(name, ttl=600):
    """装饰器：同名任务在同一时刻只允许一个实例执行（拿不到锁直接跳过）"""
    def wrapper(fn):
        def inner(*args, **kwargs):
            handle = acquire(name, ttl=ttl)
            if not handle:
                return None
            try:
                return fn(*args, **kwargs)
            finally:
                handle.release()
        return inner
    return wrapper
