# -*- coding: utf-8 -*-
"""轻量 Prometheus 指标：不依赖 prometheus_client，进程内聚合后按文本格式输出。

/metrics 暴露标准指标：
- http_request_duration_seconds_bucket / _sum / _count（请求耗时直方图）
- http_requests_total{path,status}
- db_pool_usage（连接池占用比例）
- cache_hit_ratio（缓存命中率）

多实例部署时每个实例各自统计，由 Prometheus 侧汇总；后台「服务监控」也读取 summary()。
"""
import threading

BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)
MAX_PATHS = 300

_LOCK = threading.Lock()
_STATE = {
    "buckets": [0] * len(BUCKETS),   # 每个桶的累计次数（le 为下界，累计包含）
    "sum": 0.0,
    "count": 0,
    "paths": {},                      # {path: {"count": n, "err": n, "ms": total}}
}


def observe(path: str, status_code: int, duration_ms: float):
    """记录一次请求：路径、状态码、耗时（毫秒）"""
    seconds = max(0.0, float(duration_ms or 0)) / 1000.0
    path = (path or "")[:200]
    with _LOCK:
        _STATE["sum"] += seconds
        _STATE["count"] += 1
        for idx, upper in enumerate(BUCKETS):
            if seconds <= upper:
                _STATE["buckets"][idx] += 1
        item = _STATE["paths"].get(path)
        if item is None:
            if len(_STATE["paths"]) >= MAX_PATHS:
                # 超出上限时不再新增路径，避免高基数路径撑爆内存
                return
            item = {"count": 0, "err": 0, "ms": 0.0}
            _STATE["paths"][path] = item
        item["count"] += 1
        item["ms"] += duration_ms or 0
        if status_code and int(status_code) >= 400:
            item["err"] += 1


def _esc(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render(pool_percent=0.0, cache_ratio=0.0, extra_lines=None):
    """生成 Prometheus 文本格式（text/plain; version=0.0.4）"""
    lines = []
    with _LOCK:
        buckets = list(_STATE["buckets"])
        total = _STATE["count"]
        total_sum = _STATE["sum"]
        paths = {k: dict(v) for k, v in _STATE["paths"].items()}

    lines += ["# HELP http_request_duration_seconds API 请求耗时（秒）",
              "# TYPE http_request_duration_seconds histogram"]
    for idx, upper in enumerate(BUCKETS):
        lines.append(f'http_request_duration_seconds_bucket{{le="{upper}"}} {buckets[idx]}')
    lines.append(f'http_request_duration_seconds_bucket{{le="+Inf"}} {total}')
    lines.append(f"http_request_duration_seconds_sum {total_sum:.3f}")
    lines.append(f"http_request_duration_seconds_count {total}")
    lines += ["# HELP http_requests_total 按路径与状态码的请求数",
              "# TYPE http_requests_total counter"]
    for path, item in sorted(paths.items()):
        ok = max(0, item["count"] - item["err"])
        lines.append(f'http_requests_total{{path="{_esc(path)}",status="ok"}} {ok}')
        lines.append(f'http_requests_total{{path="{_esc(path)}",status="error"}} {item["err"]}')
    lines += ["# HELP db_pool_usage 数据库连接池占用比例（0-1）",
              "# TYPE db_pool_usage gauge",
              f"db_pool_usage {float(pool_percent or 0):.4f}",
              "# HELP cache_hit_ratio 缓存命中率（0-1）",
              "# TYPE cache_hit_ratio gauge",
              f"cache_hit_ratio {float(cache_ratio or 0):.4f}"]
    for line in (extra_lines or []):
        lines.append(line)
    return "\n".join(lines) + "\n"


def summary():
    """后台展示用摘要：总数、错误数、平均耗时、分桶"""
    with _LOCK:
        total = _STATE["count"]
        total_sum = _STATE["sum"]
        err = sum(v["err"] for v in _STATE["paths"].values())
        items = [{"le": upper, "count": _STATE["buckets"][idx]}
                 for idx, upper in enumerate(BUCKETS)]
        top = sorted(_STATE["paths"].items(),
                     key=lambda kv: kv[1]["count"], reverse=True)[:10]
    return {
        "total": total,
        "errors": err,
        "avg_ms": round(total_sum * 1000.0 / total, 2) if total else 0,
        "buckets": items,
        "top_paths": [{"path": k, "count": v["count"], "err": v["err"],
                       "avg_ms": round(v["ms"] / v["count"], 2) if v["count"] else 0}
                      for k, v in top],
    }
