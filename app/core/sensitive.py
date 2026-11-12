# -*- coding: utf-8 -*-
"""敏感词过滤：导入时拦截、对外 JSON 文案替换

- 词库存 `sensitive_word` 表，仅启用的词参与匹配
- 内存缓存 60 秒，后台增删改后调用 reload() 立即生效
- clean_obj 递归过滤接口返回的字符串，命中词替换为等长星号
"""
import json
import re
import threading
import time

from sqlalchemy import func, insert
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .models import SensitiveWordDB, SettingDB

CACHE_TTL = 60
DEFAULT_WORDS = [
    "赌博", "博彩", "毒品", "枪支", "洗钱",
    "代开发票", "办假证", "色情", "嫖娼", "裸聊",
]
SEED_FLAG = "sensitive_seeded"
HIT_KEY = "sensitive_hit_stats"      # 命中统计持久化键（SettingDB）

_CACHE = {"words": [], "ts": 0.0}
_LOCK = threading.Lock()
_HITS = {"words": {}, "total": 0, "updated_at": "", "loaded": False,
         "dirty": False, "flush_ts": 0.0}
_HITS_LOCK = threading.RLock()      # 首次加载会重入，需可重入锁


def _load_words():
    """读取启用中的敏感词（带 TTL 缓存，数据库异常时沿用旧值）"""
    now = time.time()
    with _LOCK:
        if now - _CACHE["ts"] < CACHE_TTL:
            return _CACHE["words"]
        try:
            db = SessionLocal()
            try:
                rows = db.query(SensitiveWordDB.word).filter(
                    SensitiveWordDB.enabled.is_(True)).all()
                words = [r[0] for r in rows if r[0]]
            finally:
                db.close()
        except Exception:
            words = _CACHE["words"]
        _CACHE["words"] = words
        _CACHE["ts"] = now
        return words


def reload():
    """清空缓存，下一次调用重新读库"""
    with _LOCK:
        _CACHE["ts"] = 0.0


def _load_hits_baseline():
    """首次使用时从 SettingDB 读取累计命中数（读取失败按空统计处理）"""
    try:
        db = SessionLocal()
        try:
            row = db.query(SettingDB).filter(SettingDB.key == HIT_KEY).first()
            data = json.loads(row.value) if row is not None and row.value else {}
        finally:
            db.close()
    except Exception:
        data = {}
    words = data.get("words") if isinstance(data.get("words"), dict) else {}
    with _HITS_LOCK:
        _HITS["words"] = {str(k): int(v or 0) for k, v in words.items()}
        _HITS["total"] = int(data.get("total") or sum(_HITS["words"].values()))
        _HITS["updated_at"] = str(data.get("updated_at") or "")
        _HITS["loaded"] = True


def _record_hits(words):
    """累计命中次数；每 60 秒最多落库一次，避免高频接口反复写库"""
    if not words:
        return
    with _HITS_LOCK:
        if not _HITS["loaded"]:
            _load_hits_baseline()
        for word in words:
            _HITS["words"][word] = _HITS["words"].get(word, 0) + 1
        _HITS["total"] += len(words)
        _HITS["dirty"] = True
        force = time.time() - _HITS["flush_ts"] >= 60
    if force:
        flush_hits()


def flush_hits(force=False):
    """把内存命中统计写回 SettingDB；返回是否真正写入"""
    with _HITS_LOCK:
        if not _HITS["dirty"]:
            return False
        if not force and time.time() - _HITS["flush_ts"] < 60:
            return False
        payload = {
            "words": dict(_HITS["words"]),
            "total": int(_HITS["total"]),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _HITS["dirty"] = False
        _HITS["flush_ts"] = time.time()
    try:
        db = SessionLocal()
        try:
            row = db.query(SettingDB).filter(SettingDB.key == HIT_KEY).first()
            value = json.dumps(payload, ensure_ascii=False)
            if row is None:
                db.add(SettingDB(key=HIT_KEY, value=value))
            else:
                row.value = value
            db.commit()
        finally:
            db.close()
        return True
    except Exception:
        with _HITS_LOCK:
            _HITS["dirty"] = True          # 写失败保留脏标记，下轮再试
        return False


def hit_stats(top=10):
    """返回累计命中统计：总数、最近更新时间与命中次数 Top N"""
    flush_hits(force=True)
    with _HITS_LOCK:
        items = sorted(_HITS["words"].items(), key=lambda kv: kv[1], reverse=True)
        return {
            "total": int(_HITS["total"]),
            "updated_at": _HITS["updated_at"],
            "words": [{"word": w, "count": c} for w, c in items[:max(1, int(top or 10))]],
        }


def reset_hits():
    """清零命中统计（词库维护后可重新统计）"""
    with _HITS_LOCK:
        _HITS["words"] = {}
        _HITS["total"] = 0
        _HITS["updated_at"] = ""
        _HITS["dirty"] = True
        _HITS["loaded"] = True
    flush_hits(force=True)
    return True


def find_words(text):
    """返回文本中命中的敏感词列表（去重、保持词库顺序）"""
    if not isinstance(text, str) or not text:
        return []
    words = [w for w in _load_words() if w and w in text]
    _record_hits(words)
    return words


def filter_text(text):
    """把文本中的敏感词替换为等长星号"""
    if not isinstance(text, str) or not text:
        return text
    hits = []
    for w in _load_words():
        if w and w in text:
            hits.append(w)
            text = text.replace(w, "*" * len(w))
    _record_hits(hits)
    return text


def clean_obj(obj):
    """递归过滤 dict/list 中的字符串；无命中时返回原对象"""
    words = _load_words()
    if not words:
        return obj

    def walk(value):
        if isinstance(value, str):
            return filter_text(value)
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        return value

    return walk(obj)


def ensure_seeded():
    """首次启动时写入内置词库（之后用户删空也不重复写入）"""
    db = SessionLocal()
    try:
        flag = db.query(SettingDB).filter(SettingDB.key == SEED_FLAG).first()
        if flag is not None:
            return 0
        exist = {r[0] for r in db.query(SensitiveWordDB.word).all()}
        added = 0
        for word in DEFAULT_WORDS:
            if word not in exist:
                db.add(SensitiveWordDB(word=word, enabled=True))
                added += 1
        if flag is None:
            db.add(SettingDB(key=SEED_FLAG, value="1"))
        db.commit()
        return added
    except Exception:
        db.rollback()
        return 0
    finally:
        db.close()


def list_words(keyword: str = None, category: str = None):
    db = SessionLocal()
    try:
        q = db.query(SensitiveWordDB)
        if category:
            q = q.filter(SensitiveWordDB.category == category)
        if keyword:
            q = q.filter(SensitiveWordDB.word.like(f"%{keyword.strip()}%"))
        return [{
            "id": r.id, "word": r.word, "category": r.category or "默认",
            "enabled": bool(r.enabled),
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        } for r in q.order_by(SensitiveWordDB.category.asc(), SensitiveWordDB.id.desc()).all()]
    finally:
        db.close()


def tree():
    """一级分类树：只返回分类名与条数，不加载具体词条"""
    db = SessionLocal()
    try:
        rows = (db.query(SensitiveWordDB.category, func.count(SensitiveWordDB.id))
                  .group_by(SensitiveWordDB.category).all())
        return sorted(({"name": (c or "默认"), "count": int(n)} for c, n in rows),
                      key=lambda x: x["name"])
    finally:
        db.close()


def count_words():
    """词库总条数（搜索时也能显示总数）"""
    db = SessionLocal()
    try:
        return db.query(SensitiveWordDB).count()
    finally:
        db.close()


def add_words(raw: str, category: str = "默认"):
    """按换行/逗号/顿号/分号拆分批量新增，返回 (新增数, 跳过数)"""
    parts = [p.strip() for p in re.split(r"[\n\r,，、;；]+", raw or "") if p.strip()]
    parts = [p[:100] for p in parts]
    category = (category or "").strip()[:50] or "默认"
    if not parts:
        return 0, 0
    db = SessionLocal()
    try:
        # MySQL 默认不区分大小写，统一按小写比对，减少写入冲突
        exist = {str(r[0]).strip().lower() for r in db.query(SensitiveWordDB.word).all()}
        added = skipped = 0
        for word in dict.fromkeys(parts):
            key = word.strip().lower()
            if key in exist:
                skipped += 1
                continue
            try:
                # SAVEPOINT：单条撞唯一索引只跳过这一条，不影响整批
                with db.begin_nested():
                    db.execute(insert(SensitiveWordDB).values(
                        word=word, category=category, enabled=True))
                exist.add(key)
                added += 1
            except IntegrityError:
                skipped += 1
        db.commit()
        return added, skipped
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def toggle_word(word_id: int):
    db = SessionLocal()
    try:
        row = db.query(SensitiveWordDB).filter(SensitiveWordDB.id == word_id).first()
        if row is None:
            return None
        row.enabled = not bool(row.enabled)
        db.commit()
        return {"id": row.id, "word": row.word, "enabled": bool(row.enabled)}
    finally:
        db.close()


def delete_word(word_id: int):
    db = SessionLocal()
    try:
        row = db.query(SensitiveWordDB).filter(SensitiveWordDB.id == word_id).first()
        if row is None:
            return None
        word = row.word
        db.delete(row)
        db.commit()
        return word
    finally:
        db.close()
