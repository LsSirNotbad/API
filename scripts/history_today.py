# -*- coding: utf-8 -*-
"""
历史上的今天数据抓取脚本（数据源：lishi.yyxw.com）
用法：
    python scripts/history_today.py          # 抓取今天并入库
    python scripts/history_today.py 10-22    # 抓取指定日期并入库
    python scripts/history_today.py all      # 抓取全年 366 天并入库
"""
import os
import sys
import time
import random
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pymysql

DB = dict(host=os.getenv("DB_HOST", "127.0.0.1"),
          port=int(os.getenv("DB_PORT", "3306")),
          user=os.getenv("DB_USER", "root"),
          password=os.getenv("DB_PWD", "5454294!"),
          database=os.getenv("DB_NAME", "my_api_db"), charset="utf8mb4")

COOKIES = {
    'Hm_lvt_1894eb4fbb6c252f1f6947e8ad7c354e': '1792719344',
    'HMACCOUNT': '9370F49559FB46D3',
    'Hm_lpvt_1894eb4fbb6c252f1f6947e8ad7c354e': '1792719427',
}

HEADERS = {
    'accept': '*/*',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
    'referer': 'https://lishi.yyxw.com/',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0',
}

MONTH_DAYS = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

MAX_WORKERS = 6          # 并发抓取天数


def fetch_events(month, day):
    """抓取指定日期的历史事件列表，返回 [{year, content, category, baidu_url}]"""
    params = {'month': str(int(month)), 'day': str(int(day)), 'action': 'getEvents'}
    try:
        r = requests.get('https://lishi.yyxw.com/api.php', params=params,
                         cookies=COOKIES, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"  [{month:02d}-{day:02d}] 请求失败 {r.status_code}")
            return []
        data = r.json()
    except Exception as e:
        print(f"  [{month:02d}-{day:02d}] 抓取异常: {e}")
        return []
    if not data.get("success"):
        return []
    events = []
    for it in data.get("data", []):
        content = (it.get("content") or it.get("description") or "").strip()
        if not content:
            continue
        events.append({
            "year": str(it.get("year") or "").strip(),
            "content": content,
            "category": (it.get("category") or "").strip(),
            "baidu_url": (it.get("baidu_url") or "").strip().strip('`'),
        })
    return events


def save_events(month_day, events):
    """写入 history_today 表（唯一键自动去重），返回新增条数"""
    if not events:
        return 0
    conn = pymysql.connect(autocommit=False, **DB)
    try:
        with conn.cursor() as cur:
            rows = [(month_day, e["year"], e["content"], e["category"], e["baidu_url"])
                    for e in events]
            cur.executemany(
                "INSERT IGNORE INTO history_today (month_day, year, event, category, baidu_url) "
                "VALUES (%s, %s, %s, %s, %s)", rows)
            conn.commit()
            return cur.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def import_date(month, day):
    """抓取单日并入库，返回 (抓取数, 新增数)"""
    month_day = f"{int(month):02d}-{int(day):02d}"
    events = fetch_events(month, day)
    n = save_events(month_day, events)
    print(f"[{month_day}] 抓取 {len(events)} 条，新增入库 {n} 条")
    return len(events), n


def import_all_concurrent():
    """多线程抓取全年，主线程统一入库"""
    tasks = []
    for m in range(1, 13):
        for d in range(1, MONTH_DAYS[m - 1] + 1):
            tasks.append((m, d))

    total_fetched = 0
    total_saved = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {}
        for m, d in tasks:
            fut = pool.submit(fetch_events, m, d)
            future_map[fut] = (m, d)

        # 按完成顺序处理，主线程写库
        for fut in as_completed(future_map):
            m, d = future_map[fut]
            month_day = f"{m:02d}-{d:02d}"
            try:
                events = fut.result()
            except Exception as e:
                print(f"[{month_day}] 线程异常: {e}")
                continue
            total_fetched += len(events)
            try:
                n = save_events(month_day, events)
            except Exception as e:
                print(f"[{month_day}] 入库失败: {e}")
                continue
            total_saved += n
            print(f"[{month_day}] 抓取 {len(events)} 条，新增入库 {n} 条")

    print(f"\n全年抓取完成，累计抓取 {total_fetched} 条，新增 {total_saved} 条")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "all":
        import_all_concurrent()
    elif arg:
        parts = arg.replace('/', '-').split('-')
        import_date(parts[0], parts[1])
    else:
        now = datetime.datetime.now()
        import_date(now.month, now.day)


if __name__ == '__main__':
    main()