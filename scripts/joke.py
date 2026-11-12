import os
import sys
import requests
from bs4 import BeautifulSoup
import time
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED

import pymysql
from pymysql.cursors import DictCursor

# ---------------- 数据库配置 ----------------
DB_USER = os.getenv("DB_USER", "root")
DB_PWD = os.getenv("DB_PWD", "5454294!")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "my_api_db")

MAX_PAGE = 200
MAX_WORKERS = 2

# ---------------- 请求头 ----------------
HEADERS_52 = {
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
    'cache-control': 'max-age=0',
    'referer': 'https://www.52xiaohua.com/',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0',
}
COOKIES_52 = {
    'server_session_b8c3d62a': 'eeb8cb8466f0369d033f14670056864e',
    '166a32f18f6532f696a74ae0610bbb42': 'a64a53d2dd69eef9f0a9b6b9b67f5a47',
    'Hm_lvt_3e542cce6fa89f2f93acf4d45b84bcdb': '1792641592,1793505139,1793778883',
    'HMACCOUNT': '9370F49559FB46D3',
    'timezone': '8',
    'Hm_lpvt_3e542cce6fa89f2f93acf4d45b84bcdb': '1793778886',
}

HEADERS_ZOL = {
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
    'cache-control': 'max-age=0',
    'referer': 'https://xiaohua.zol.com.cn/',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0',
}
COOKIES_ZOL = {
    'Adshow': '1',
    'ip_ck': '14up1Yu7v8EuMzY1MDcxLjE3OTE4NjE0NzE%3D',
    'm_area_city_new': 'heze',
    'm_area_cityId_new': '240',
    'm_area_provinceId_new': '23',
    '_ga': 'GA1.3.231061081.1792641271',
    '_gid': 'GA1.3.216709881.1792641271',
    'lv': '1792649406',
    'vn': '3',
    'questionnaire_pv': '1792627296',
    '_ga_6XFCZ4WP8R': 'GS2.3.s1792653941$o3$g1$t1792654015$j46$l0$h0',
}


# ---------------- 站点配置 ----------------
SITES = [
    {
        "source": "52xiaohua",
        "categories": {
            5: "笑话大全",
            1: "冷笑话",
            2: "小笑话",
            6: "爆笑笑话",
            8: "情侣笑话",
            7: "校园笑话",
            10: "幽默笑话",
            11: "职场幽默",
            12: "夫妻笑话",
            13: "搞笑段子",
        },
        "list_url": lambda cat, page: (
            f"https://www.52xiaohua.com/category-{cat}.html"
            if page == 1
            else f"https://www.52xiaohua.com/category-{cat}_{page}.html"
        ),
        "list_selector": "div.postcon p a",
        "detail_selector": "div.erx-beautify-log",
        "headers": HEADERS_52,
        "cookies": COOKIES_52,
        "encoding": "utf-8",
        "detail_delay": (1.5, 3.5),
    },
    {
        "source": "zol",
        "categories": {
            "lengxiaohua": "冷笑话",
            "aiqing": "爱情笑话",
            "fuqi": "夫妻笑话",
            "baoxiao": "爆笑笑话",
            "xiaoyuan": "校园笑话",
            "jingdian": "经典笑话",
            "zhengren": "整人笑话",
            "neihan": "内涵笑话",
            "yijuhua": "一句话笑话",
            "jipin": "极品笑话",
            "chaoji": "超级笑话",
            "duanpian": "短篇笑话",
            "qiuaimiji": "求爱秘籍笑话",
            "gaoxiao": "搞笑笑话",
            "gedifangyan": "各地方言笑话",
        },
        "list_url": lambda cat, page: (
            f"https://xiaohua.zol.com.cn/{cat}/"
            if page == 1
            else f"https://xiaohua.zol.com.cn/{cat}/{page}.html"
        ),
        "list_selector": "li.article-summary",
        "detail_selector": "div.article-text",
        "headers": HEADERS_ZOL,
        "cookies": COOKIES_ZOL,
        "encoding": None,
        "detail_delay": (0.5, 1.5),
    },
]


# ---------------- 数据库 ----------------
def get_conn():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PWD,
        database=DB_NAME, charset='utf8mb4', cursorclass=DictCursor,
        autocommit=False,
    )


def init_db():
    conn = pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PWD,
        charset='utf8mb4', autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.select_db(DB_NAME)
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS local_joke (
                    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                    title VARCHAR(512) DEFAULT NULL COMMENT '笑话标题',
                    category VARCHAR(64) DEFAULT NULL COMMENT '笑话分类',
                    source VARCHAR(32) DEFAULT NULL COMMENT '来源站点',
                    content MEDIUMTEXT NOT NULL COMMENT '笑话内容',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_source_title (source, title(200)),
                    KEY idx_source (source),
                    KEY idx_category (category)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='本地笑话表：段子、冷笑话'
                """
            )
    finally:
        conn.close()


def title_exists(cur, source, title):
    cur.execute(
        "SELECT 1 FROM local_joke WHERE source=%s AND title=%s LIMIT 1",
        (source, title),
    )
    return cur.fetchone() is not None


def save_jokes(rows):
    if not rows:
        return 0
    saved = 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for source, category, title, content, url in rows:
                if title_exists(cur, source, title):
                    continue
                cur.execute(
                    "INSERT INTO local_joke (source, category, title, content) "
                    "VALUES (%s, %s, %s, %s)",
                    (source, category, title, content),
                )
                saved += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return saved


# ---------------- 拆分 ----------------
def split_body(body, title_hint=""):
    """
    把一段正文按序号（1、2、）或书名号（《xxx》）拆成多条
    返回 [(label, content), ...]
    """
    lines = body.split('\n')
    items = []
    current_label = None
    current_lines = []

    seq_re = re.compile(r'^\s*(\d+)[、\.．]\s*')
    book_re = re.compile(r'^\s*《([^》]+)》\s*')

    def flush():
        nonlocal current_label, current_lines
        if current_label is not None and current_lines:
            items.append((current_label, '\n'.join(current_lines).strip()))
        elif current_lines:
            items.append((title_hint, '\n'.join(current_lines).strip()))
        current_lines = []

    for line in lines:
        m_seq = seq_re.match(line)
        m_book = book_re.match(line)
        if m_seq:
            flush()
            current_label = title_hint
            rest = seq_re.sub('', line, count=1).strip()
            current_lines = [rest] if rest else []
        elif m_book:
            flush()
            current_label = title_hint
            current_lines = [line.strip()]
        else:
            current_lines.append(line)

    flush()
    return items


# ---------------- 抓取 ----------------
def decode_response(r, encoding):
    if encoding:
        r.encoding = encoding
    else:
        r.encoding = r.apparent_encoding
    return r.text


def fetch_list_page(site, list_url):
    try:
        r = requests.get(
            list_url, cookies=site["cookies"], headers=site["headers"], timeout=15
        )
        if r.status_code in (403, 429, 503):
            print(f"  列表页被限流 {r.status_code}，暂停 60 秒")
            time.sleep(60)
            return None
        if r.status_code != 200:
            return None

        html = decode_response(r, site["encoding"])
        soup = BeautifulSoup(html, 'html.parser')
        items = []

        if site["source"] == "52xiaohua":
            for a in soup.select(site["list_selector"]):
                href = a.get('href')
                title = a.get('title')
                if href and title:
                    items.append((href, title))
        elif site["source"] == "zol":
            for li in soup.select(site["list_selector"]):
                a = li.select_one('span.article-title a')
                if not a:
                    continue
                href = a.get('href')
                title = a.get_text(strip=True)
                if href and title:
                    if href.startswith('/'):
                        href = 'https://xiaohua.zol.com.cn' + href
                    items.append((href, title))
        return items
    except Exception as e:
        print(f"列表页抓取失败 {list_url}: {e}")
        return None


def iter_all_links(site, cat_key):
    empty_streak = 0
    prev_links = None
    seen_in_category = set()

    for page in range(1, MAX_PAGE + 1):
        url = site["list_url"](cat_key, page)
        items = fetch_list_page(site, url)

        if items is None:
            empty_streak += 1
            print(f"  第 {page} 页请求失败，连续失败 {empty_streak}")
            if empty_streak >= 2:
                break
            time.sleep(random.uniform(3.0, 5.0))
            continue

        if not items:
            empty_streak += 1
            print(f"  第 {page} 页为空，连续空 {empty_streak}")
            if empty_streak >= 2:
                break
            time.sleep(random.uniform(3.0, 5.0))
            continue

        cur_links = {href for href, _ in items}

        if prev_links is not None and cur_links == prev_links:
            print(f"  第 {page} 页内容与上一页完全相同，判定被拦截/已到底，停止")
            break

        new_links = cur_links - seen_in_category
        if seen_in_category and len(new_links) / len(cur_links) < 0.2:
            print(f"  第 {page} 页重复率过高（新链接 {len(new_links)}/{len(cur_links)}），停止")
            break

        empty_streak = 0
        prev_links = cur_links
        seen_in_category |= cur_links

        print(f"  第 {page} 页：{len(items)} 条")
        for href, title in items:
            yield href, title

        time.sleep(random.uniform(2.0, 4.0))


def fetch_detail(site, detail_url):
    lo, hi = site["detail_delay"]
    time.sleep(random.uniform(lo, hi))

    try:
        r = requests.get(
            detail_url, cookies=site["cookies"], headers=site["headers"], timeout=15
        )
        if r.status_code in (403, 429, 503):
            print(f"  详情页被限流 {r.status_code}，暂停 60 秒")
            time.sleep(60)
            return ""
        if r.status_code != 200:
            return ""

        html = decode_response(r, site["encoding"])
        soup = BeautifulSoup(html, 'html.parser')

        node = soup.select_one(site["detail_selector"])
        if not node:
            for sel in ['div.article-content', 'div.content', 'article']:
                node = soup.select_one(sel)
                if node:
                    break
        if not node:
            return ""

        for tag in node(['script', 'style']):
            tag.decompose()
        paragraphs = [p.get_text(strip=True) for p in node.find_all('p')]
        paragraphs = [p for p in paragraphs if p]
        if paragraphs:
            return '\n'.join(paragraphs)
        return node.get_text('\n', strip=True)
    except Exception as e:
        print(f"详情页抓取失败 {detail_url}: {e}")
        return ""


# ---------------- 主流程 ----------------
def process_site(site):
    source = site["source"]
    total_saved = 0

    for cat_key, cat_name in site["categories"].items():
        print(f"\n===== [{source}] 分类：{cat_name} =====")
        buffer = []

        def consume(content, href, title):
            """拆分详情内容后加入 buffer"""
            if not content:
                return
            sub_items = split_body(content, title)
            for idx, (label, body) in enumerate(sub_items, 1):
                if not body:
                    continue
                full_title = f"{title}（{idx}）" if len(sub_items) > 1 else title
                buffer.append((source, cat_name, full_title, body, href))

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            for href, title in iter_all_links(site, cat_key):
                fut = pool.submit(fetch_detail, site, href)
                futures[fut] = (href, title)

                if len(futures) >= MAX_WORKERS * 4:
                    done, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED)
                    for fut in done:
                        h, t = futures.pop(fut)
                        try:
                            content = fut.result()
                        except Exception as e:
                            print(f"  详情异常 {h}: {e}")
                            continue
                        consume(content, h, t)
                    if len(buffer) >= 50:
                        total_saved += save_jokes(buffer)
                        buffer.clear()

            for fut in as_completed(futures):
                h, t = futures[fut]
                try:
                    content = fut.result()
                except Exception as e:
                    print(f"  详情异常 {h}: {e}")
                    continue
                consume(content, h, t)

        total_saved += save_jokes(buffer)
        buffer.clear()
        print(f"[{source}][{cat_name}] 累计入库 {total_saved} 条")

    return total_saved


def main():
    category = sys.argv[1] if len(sys.argv) > 1 else "all"
    sites = SITES
    if category != "all":
        source_site = next(site for site in SITES if site["source"] == "52xiaohua")
        try:
            category_key = int(category)
        except ValueError:
            raise SystemExit(f"未知笑话分类：{category}")
        if category_key not in source_site["categories"]:
            raise SystemExit(f"未知笑话分类：{category}")
        sites = [dict(source_site, categories={
            category_key: source_site["categories"][category_key]
        })]

    init_db()
    grand_total = 0
    for site in sites:
        grand_total += process_site(site)
    print(f"\n全部完成，累计新增入库 {grand_total} 条")


if __name__ == '__main__':
    main()