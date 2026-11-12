# -*- coding: utf-8 -*-
"""
JSON 数据导入脚本：把歇后语/成语 JSON 文件去重导入数据库
用法：
    python scripts/数据导入.py                # 导入 data/ 下的 xiehouyu.json 和 idiom.json（存在才导）
    python scripts/数据导入.py idiom.json     # 只导入指定文件（相对 data/ 或绝对路径，按JSON字段自动识别类型）
说明：
    - 歇后语格式：[{"riddle": "...", "answer": "..."}]  -> xiehouyu 表（按 riddle 去重）
    - 成语格式：  [{"word": "...", "pinyin": ..., "abbreviation": ..., "explanation": ..., "derivation": ..., "example": ...}] -> idiom_lib 表（按 word 去重）
    - 成语表字段不足时会自动补列；唯一键已存在，重复数据天然被拦截
"""
import sys
import io
import os
import json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import pymysql

DB = dict(host=os.getenv("DB_HOST", "127.0.0.1"),
          port=int(os.getenv("DB_PORT", "3306")),
          user=os.getenv("DB_USER", "root"),
          password=os.getenv("DB_PWD", "5454294!"),
          database=os.getenv("DB_NAME", "my_api_db"), charset="utf8mb4")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
BATCH = 1000


def get_conn():
    return pymysql.connect(autocommit=False, **DB)


def ensure_idiom_columns(cur):
    """idiom_lib 表补充成语 JSON 对应的扩展列"""
    cols = [
        ("pinyin", "VARCHAR(100) NULL COMMENT '拼音' AFTER word"),
        ("abbreviation", "VARCHAR(20) NULL COMMENT '拼音首字母缩写' AFTER pinyin"),
        ("derivation", "TEXT NULL COMMENT '出处典故'"),
        ("example", "TEXT NULL COMMENT '例句'"),
    ]
    for name, ddl in cols:
        cur.execute("""SELECT COUNT(*) FROM information_schema.COLUMNS
                       WHERE TABLE_SCHEMA='my_api_db' AND TABLE_NAME='idiom_lib' AND COLUMN_NAME=%s""",
                    (name,))
        if cur.fetchone()[0] == 0:
            cur.execute(f"ALTER TABLE idiom_lib ADD COLUMN {name} {ddl}")
            print(f"  [结构] idiom_lib 已添加列 {name}")


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("JSON 顶层必须是数组")
    return data


def import_xiehouyu(path):
    data = load_json(path)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT riddle FROM xiehouyu")
            existing = {r[0] for r in cur.fetchall()}
            seen, rows = set(), []
            for it in data:
                r = (it.get("riddle") or "").strip()
                a = (it.get("answer") or "").strip()
                if not r or not a or r in existing or r in seen:
                    continue
                seen.add(r)
                rows.append((r, a))
            for i in range(0, len(rows), BATCH):
                cur.executemany("INSERT IGNORE INTO xiehouyu (riddle, answer) VALUES (%s, %s)",
                                rows[i:i + BATCH])
            conn.commit()
            cur.execute("SELECT COUNT(*) FROM xiehouyu")
            total = cur.fetchone()[0]
        print(f"[歇后语] 文件 {len(data)} 条，新增 {len(rows)} 条，库中现有 {total} 条")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def import_idiom(path):
    data = load_json(path)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            ensure_idiom_columns(cur)
            conn.commit()
            cur.execute("SELECT word FROM idiom_lib")
            existing = {r[0] for r in cur.fetchall()}
            seen, rows = set(), []
            for it in data:
                w = (it.get("word") or "").strip()
                if not w or w in existing or w in seen:
                    continue
                seen.add(w)
                rows.append((
                    w,
                    (it.get("pinyin") or "").strip(),
                    (it.get("abbreviation") or "").strip(),
                    (it.get("explanation") or "").strip(),
                    (it.get("derivation") or "").strip(),
                    (it.get("example") or "").strip(),
                ))
            for i in range(0, len(rows), BATCH):
                cur.executemany(
                    "INSERT IGNORE INTO idiom_lib (word, pinyin, abbreviation, `explain`, derivation, example) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    rows[i:i + BATCH])
            conn.commit()
            cur.execute("SELECT COUNT(*) FROM idiom_lib")
            total = cur.fetchone()[0]
        print(f"[成语] 文件 {len(data)} 条，新增 {len(rows)} 条，库中现有 {total} 条")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def detect_and_import(path):
    data = load_json(path)
    if not data:
        print(f"{path}: 空文件，跳过")
        return
    keys = set(data[0].keys())
    if "word" in keys:
        import_idiom(path)
    elif "riddle" in keys:
        import_xiehouyu(path)
    else:
        print(f"{path}: 无法识别的格式（字段：{sorted(keys)}），跳过")


def main():
    if len(sys.argv) > 1:
        for p in sys.argv[1:]:
            detect_and_import(p if os.path.isabs(p) else os.path.join(DATA_DIR, p))
    else:
        for name in ("xiehouyu.json", "idiom.json"):
            p = os.path.join(DATA_DIR, name)
            if os.path.exists(p):
                detect_and_import(p)
            else:
                print(f"{name} 不存在，跳过")


if __name__ == "__main__":
    main()
