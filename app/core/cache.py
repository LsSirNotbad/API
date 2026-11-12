# -*- coding: utf-8 -*-
"""缓存后端：默认进程内 TTL 缓存，配置 REDIS_URL 后自动切换为 Redis。

- 多实例部署时配置 REDIS_URL，缓存与限流计数在实例间共享
- Redis 不可用（未安装 redis 包或连接失败）时自动回退进程内缓存，不影响启动
- 两种后端都支持 incr()，限流用 INCR + EXPIRE 实现固定窗口计数
"""
import json
import threading
import time

from .config import CACHE_MAX_ITEMS, CACHE_TTL, REDIS_PREFIX, REDIS_URL

try:
    import redis
except ImportError:      # 未安装 redis 包时退化为进程内缓存
    redis = None

_CLIENT = None
_CLIENT_FAIL_AT = 0.0
_CLIENT_LOCK = threading.Lock()


def get_redis():
    """返回可用的 Redis 客户端；未配置或连接失败返回 None（失败后 60 秒内不重试）"""
    global _CLIENT, _CLIENT_FAIL_AT
    if not REDIS_URL or redis is None:
        return None
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        if time.time() < _CLIENT_FAIL_AT:
            return None
        try:
            client = redis.Redis.from_url(REDIS_URL, decode_responses=True,
                                          socket_timeout=2, socket_connect_timeout=2)
            client.ping()
            _CLIENT = client
            return client
        except Exception as e:
            _CLIENT_FAIL_AT = time.time() + 60
            print(f"[缓存] Redis 连接失败，回退进程内缓存：{e}")
            return None


class TTLCache:
    """进程内 TTL 缓存（单实例部署或 Redis 不可用时使用）"""

    backend = "memory"

    def __init__(self, ttl: int = 60, max_items: int = 500):
        self.ttl = max(1, int(ttl))
        self.max_items = max(10, int(max_items))
        self._data = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key):
        """命中返回缓存值，未命中或已过期返回 None"""
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            value, expire_at = item
            if expire_at <= now:
                self._data.pop(key, None)
                self.misses += 1
                return None
            self.hits += 1
            return value

    def set(self, key, value, ttl: int = None):
        now = time.time()
        with self._lock:
            if len(self._data) >= self.max_items and key not in self._data:
                self._evict(now)
            self._data[key] = (value, now + (ttl or self.ttl))

    def incr(self, key, ttl: int = 60):
        """计数 +1，返回当前窗口内的计数（固定窗口：首次写入时开始计时）"""
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item is None or item[1] <= now:
                value, expire_at = 0, now + max(1, int(ttl))
            else:
                value, expire_at = item
            value += 1
            self._data[key] = (value, expire_at)
            return value

    def _evict(self, now):
        """先删过期项；仍然超限时删掉最早写入的键（调用方已持锁）"""
        expired = [k for k, (_, exp) in self._data.items() if exp <= now]
        for k in expired:
            self._data.pop(k, None)
        if len(self._data) < self.max_items:
            return
        overflow = len(self._data) - self.max_items + 1
        for k in sorted(self._data, key=lambda x: self._data[x][1])[:overflow]:
            self._data.pop(k, None)

    def clear(self, prefix: str = None):
        """清空缓存；传 prefix 时只清该前缀的键"""
        with self._lock:
            if prefix:
                for k in [k for k in self._data if str(k).startswith(prefix)]:
                    self._data.pop(k, None)
            else:
                self._data.clear()

    def stats(self):
        with self._lock:
            return {"items": len(self._data), "hits": self.hits,
                    "misses": self.misses, "ttl": self.ttl,
                    "backend": self.backend}


class RedisCache:
    """Redis 缓存后端：多实例共享，值以 JSON 序列化存储"""

    backend = "redis"

    def __init__(self, client, prefix: str, ttl: int, max_items: int):
        self.client = client
        self.prefix = prefix or "apicache:"
        self.ttl = max(1, int(ttl))
        self.max_items = max(10, int(max_items))
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()

    def _k(self, key):
        return f"{self.prefix}{key}"

    def get(self, key):
        try:
            raw = self.client.get(self._k(key))
        except Exception:
            return None
        if raw is None:
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    def set(self, key, value, ttl: int = None):
        try:
            self.client.setex(self._k(key), max(1, int(ttl or self.ttl)),
                              json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            pass

    def incr(self, key, ttl: int = 60):
        """INCR + EXPIRE：多实例共享的固定窗口计数器"""
        try:
            k = self._k("cnt:" + str(key))
            value = int(self.client.incr(k))
            if value == 1:
                self.client.expire(k, max(1, int(ttl)))
            return value
        except Exception:
            return 0

    def clear(self, prefix: str = None):
        try:
            pattern = self._k((prefix or "") + "*")
            for k in self.client.scan_iter(match=pattern, count=500):
                self.client.delete(k)
        except Exception:
            pass

    def stats(self):
        try:
            items = int(self.client.dbsize())
        except Exception:
            items = 0
        with self._lock:
            return {"items": items, "hits": self.hits, "misses": self.misses,
                    "ttl": self.ttl, "backend": self.backend}


def _build_cache():
    client = get_redis()
    if client is not None:
        print(f"[缓存] 使用 Redis 后端：{REDIS_URL}")
        return RedisCache(client, REDIS_PREFIX, CACHE_TTL, CACHE_MAX_ITEMS)
    return TTLCache(CACHE_TTL, CACHE_MAX_ITEMS)


CACHE = _build_cache()
