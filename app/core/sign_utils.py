# -*- coding: utf-8 -*-
"""标准 HMAC 请求签名工具（服务端校验 + 客户端调用，算法统一）。

签名串（换行分隔，body 为原始字节，GET 请求为空）：

    METHOD\nPATH\nTIMESTAMP\nNONCE\n + BODY

签名值 = hex(HMAC-SHA256(secret, 上述内容))

服务端（main.py 的 SignatureMiddleware）与客户端（SignClient）都调用本模块，
避免两处实现漂移。nonce 在多实例下用 Redis 去重，无 Redis 时退化为进程内去重。

客户端示例：

    from sign_utils import SignClient
    client = SignClient("https://your-host", secret="SIGN_SECRET")
    r = client.get("/api/v1/hot/get", params={"source": "local", "limit": 10},
                   token="接口 Token")
    print(r.json())
"""
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import urllib.request
import uuid

NONCES = {}
NONCE_LOCK = threading.Lock()


def canonical(method, path, timestamp, nonce, body=b"") -> bytes:
    """拼接待签名的规范化字符串（bytes），body 原样参与计算"""
    head = f"{str(method or 'GET').upper()}\n{path}\n{int(timestamp)}\n{nonce}\n".encode("utf-8")
    if isinstance(body, str):
        body = body.encode("utf-8")
    return head + (body or b"")


def sign(secret, method, path, timestamp, nonce, body=b"") -> str:
    """计算签名（小写十六进制）"""
    key = (secret or "").encode("utf-8")
    return hmac.new(key, canonical(method, path, timestamp, nonce, body),
                    hashlib.sha256).hexdigest()


def make_headers(secret, method, path, body=b"", timestamp=None, nonce=None) -> dict:
    """生成 X-Timestamp / X-Nonce / X-Sign 三个请求头"""
    ts = int(timestamp or time.time())
    nonce = nonce or uuid.uuid4().hex
    return {
        "X-Timestamp": str(ts),
        "X-Nonce": nonce,
        "X-Sign": sign(secret, method, path, ts, nonce, body),
    }


def verify(secret, method, path, timestamp, nonce, sign_value,
           body=b"", window: int = 300):
    """校验签名与时间窗口，返回 (是否通过, 失败原因)"""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False, "时间戳格式不正确"
    if abs(time.time() - ts) > max(30, int(window or 300)):
        return False, f"请求时间戳超过 {int(window)} 秒允许范围，请校准服务器时间后重试"
    expected = sign(secret, method, path, ts, nonce, body)
    if not hmac.compare_digest(expected, str(sign_value or "").lower()):
        return False, "签名校验失败"
    return True, ""


def check_nonce(nonce, ttl: int = 300) -> bool:
    """登记 nonce：首次使用返回 True，重复使用（重放）返回 False"""
    nonce = str(nonce or "")
    if not nonce:
        return False
    try:
        from .cache import get_redis
        client = get_redis()
    except Exception:
        client = None
    if client is not None:
        try:
            return bool(client.set(f"nonce:{nonce}", "1", nx=True, ex=max(30, int(ttl))))
        except Exception:
            pass
    now = time.time()
    with NONCE_LOCK:
        if len(NONCES) > 5000:
            for k in [k for k, exp in NONCES.items() if exp < now]:
                NONCES.pop(k, None)
        if nonce in NONCES and NONCES[nonce] >= now:
            return False
        NONCES[nonce] = now + max(30, int(ttl))
        return True


def _path_of(url: str) -> str:
    """从完整 URL 取出 path（不含 query），签名只覆盖 path"""
    parsed = urllib.parse.urlparse(url)
    return parsed.path or "/"


class SignClient:
    """调用方签名客户端：自动带 X-Timestamp / X-Nonce / X-Sign

    只签名 path，不包含 query，便于 Nginx 等中间层不改写参数。
    """

    def __init__(self, base_url: str, secret: str, token: str = "", timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.secret = secret or ""
        self.token = token or ""
        self.timeout = timeout

    def request(self, method, path, params=None, body=None, headers=None):
        method = method.upper()
        query = urllib.parse.urlencode(params or {})
        url = self.base_url + path + (f"?{query}" if query else "")
        raw = b""
        all_headers = dict(headers or {})
        if body is not None:
            raw = (body if isinstance(body, bytes)
                   else json.dumps(body, ensure_ascii=False).encode("utf-8"))
            all_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        if self.token:
            all_headers.setdefault("token", self.token)
        all_headers.update(make_headers(self.secret, method, _path_of(url), raw))
        req = urllib.request.Request(url, data=raw if raw else None,
                                     headers=all_headers, method=method)
        return urllib.request.urlopen(req, timeout=self.timeout)

    def get(self, path, params=None, headers=None):
        return self.request("GET", path, params=params, headers=headers)

    def post(self, path, body=None, params=None, headers=None):
        return self.request("POST", path, params=params, body=body, headers=headers)
