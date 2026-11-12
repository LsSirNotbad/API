import os
import requests
from bs4 import BeautifulSoup
import time
import random
import re

import pymysql
from pymysql.cursors import DictCursor
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED

# ---------------- 配置 ----------------
BASE = "https://www.diyifanwen.com"
LIST_URL = "https://www.diyifanwen.com/tool/raokouling/"

DB_USER = os.getenv("DB_USER", "root")
DB_PWD = os.getenv("DB_PWD", "5454294!")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "my_api_db")

HEADERS = {
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'referer': 'https://www.diyifanwen.com/tool/raokouling/',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 Edg/155.0.0.0',
}

MAX_PAGE = 200
MAX_WORKERS = 4

# 模板导语关键词，命中即丢弃
TEMPLATE_KEYWORDS = [
    "第一范文网", "小编为大家", "为你整理", "欢迎大家阅读",
    "以下是", "小编", "通过绕口令练习", "编辑推荐",
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
                CREATE TABLE IF NOT EXISTS tongue_twister (
                    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
                    title VARCHAR(512) DEFAULT NULL COMMENT '绕口令标题',
                    content LONGTEXT NOT NULL COMMENT '绕口令内容',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='绕口令库'
                """
            )
    finally:
        conn.close()


def norm_content(s):
    """内容归一化：去掉所有空白字符，用于重复比对"""
    return s.replace('\n', '').replace('\r', '').replace(' ', '').replace('\u3000', '').strip()


# 启动时加载已有标题和内容指纹，爬虫全程复用
_EXIST_TITLES = set()
_EXIST_CONTENTS = set()


def load_existing():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT title, content FROM tongue_twister")
            for row in cur.fetchall():
                if row['title']:
                    _EXIST_TITLES.add(row['title'])
                if row['content']:
                    _EXIST_CONTENTS.add(norm_content(row['content']))
    finally:
        conn.close()
    print(f"已加载库中 {len(_EXIST_TITLES)} 个标题、{len(_EXIST_CONTENTS)} 条内容指纹")


def save_rows(rows):
    """rows: list of (title, content, source_url)，按标题+内容双重去重"""
    if not rows:
        return 0
    saved = 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for title, content, url in rows:
                key = norm_content(content)
                if title in _EXIST_TITLES or key in _EXIST_CONTENTS:
                    continue
                cur.execute(
                    "INSERT INTO tongue_twister (title, content) VALUES (%s, %s)",
                    (title, content),
                )
                _EXIST_TITLES.add(title)
                _EXIST_CONTENTS.add(key)
                saved += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return saved


# ---------------- 抓取 ----------------
def fetch_html(url):
    time.sleep(random.uniform(0.3, 1.0))
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code in (403, 429, 503):
            print(f"  被限流 {r.status_code}，暂停 60 秒")
            time.sleep(60)
            return None
        if r.status_code != 200:
            print(f"  状态码异常 {r.status_code}: {url}")
            return None
        r.encoding = r.apparent_encoding
        return r.text
    except Exception as e:
        print(f"  请求失败 {url}: {e}")
        return None


def parse_list(html):
    """列表页：只提取 URL 路径含 raokouling 的详情页 (完整href, title)"""
    soup = BeautifulSoup(html, 'html.parser')
    items = []
    for li in soup.find_all('li'):
        a = li.find('a', href=True)
        if not a:
            continue
        href = a.get('href')
        title = a.get('title') or a.get_text(strip=True)
        if not href or not title:
            continue

        if href.startswith('//'):
            href = 'https:' + href
        elif href.startswith('/'):
            href = BASE + href

        path = href.split('://', 1)[-1]

        if 'raokouling' not in path:
            continue
        if path.rstrip('/').endswith('/raokouling'):
            continue
        if re.search(r'/raokouling/(index_|list_)\d+\.html$', path):
            continue

        items.append((href, title))
    return items


def is_template_section(sub_title, body, list_title, has_heading=True):
    """
    判断是否为站点模板导语段，需要丢弃。
    只有「首个 h3/strong 标题之前的导语」才可能是模板段；
    带标题的段落即使标题与列表标题相同，也是真正的绕口令正文，必须保留。
    """
    if len(re.sub(r'\s', '', body)) < 10:
        return True
    if has_heading:
        return False
    if sub_title == list_title:
        return True
    for kw in TEMPLATE_KEYWORDS:
        if kw in body:
            return True
    return False


def split_body(body, sub_title):
    """
    把一段正文按序号（1、2、）或书名号（《xxx》）拆成多条
    标题沿用 sub_title
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
            items.append((sub_title, '\n'.join(current_lines).strip()))
        current_lines = []

    for line in lines:
        m_seq = seq_re.match(line)
        m_book = book_re.match(line)

        if m_seq:
            flush()
            current_label = sub_title
            rest = seq_re.sub('', line, count=1).strip()
            current_lines = [rest] if rest else []
        elif m_book:
            flush()
            current_label = sub_title
            rest = line.strip()
            current_lines = [rest] if rest else []
        else:
            current_lines.append(line)

    flush()
    return items


def parse_detail(html, url, fallback_title=""):
    """
    详情页：按 <h3> 或含 <strong> 的 <p> 分段
    第一个标题之前的内容用 fallback_title 兜底
    过滤模板导语段，再按序号/书名号拆分
    返回 [(标题, 正文), ...]
    """
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('div.content')
    print(f"  [调试] URL: {url}")
    print(f"  [调试] div.content 匹配: {content is not None}")
    if not content:
        return []

    sections = []
    current_title = None
    current_lines = []

    def flush():
        nonlocal current_title, current_lines
        if current_title is not None:
            sections.append(
                (current_title, '\n'.join(current_lines).strip(), True))
        elif current_lines:
            sections.append(
                (fallback_title or "未命名", '\n'.join(current_lines).strip(), False))
        current_lines = []

    for tag in content.find_all(['h3', 'p']):
        if tag.name == 'h3':
            flush()
            current_title = tag.get_text(strip=True)
        else:
            strong = tag.find('strong')
            if strong:
                flush()
                current_title = strong.get_text(strip=True)
            else:
                text = tag.get_text(strip=True)
                if text:
                    current_lines.append(text)

    flush()

    # 过滤模板导语段 + 拆分
    filtered = []
    for sub_title, body, has_heading in sections:
        if is_template_section(sub_title, body, fallback_title, has_heading):
            print(f"  [过滤] 丢弃模板段：{sub_title} | {body[:30]!r}...")
            continue

        sub_items = split_body(body, sub_title)
        for idx, (label, content) in enumerate(sub_items, 1):
            if not content:
                continue
            # 拆出多条时给标题加序号，避免唯一键冲突
            if len(sub_items) > 1:
                label = f"{label}（{idx}）"
            filtered.append((label, content))

    print(f"  [调试] 解析出段落数: {len(sections)}，拆分后: {len(filtered)}")
    return filtered


def build_page_url(page):
    if page == 1:
        return LIST_URL
    return f"{LIST_URL}index_{page}.html"


def iter_all_links():
    """自动探测页数：连续 2 页为空/失败/内容重复则停止"""
    empty_streak = 0
    prev_links = None
    seen = set()

    for page in range(1, MAX_PAGE + 1):
        url = build_page_url(page)
        html = fetch_html(url)
        if html is None:
            empty_streak += 1
            print(f"  第 {page} 页请求失败，连续失败 {empty_streak}")
            if empty_streak >= 2:
                break
            time.sleep(random.uniform(3.0, 5.0))
            continue

        items = parse_list(html)
        if not items:
            empty_streak += 1
            print(f"  第 {page} 页为空，连续空 {empty_streak}")
            if empty_streak >= 2:
                break
            time.sleep(random.uniform(3.0, 5.0))
            continue

        cur_links = {href for href, _ in items}
        if prev_links is not None and cur_links == prev_links:
            print(f"  第 {page} 页内容与上一页相同，停止")
            break

        new_links = cur_links - seen
        if seen and len(new_links) / len(cur_links) < 0.2:
            print(f"  第 {page} 页重复率过高，停止")
            break

        empty_streak = 0
        prev_links = cur_links
        seen |= cur_links

        print(f"  第 {page} 页：{len(items)} 条")
        for href, title in items:
            yield href, title

        time.sleep(random.uniform(2.0, 4.0))


# ---------------- 工作线程 ----------------
def fetch_and_parse(href, list_title):
    """抓详情 + 解析 + 过滤 + 拆分，返回 [(full_title, body, href), ...]"""
    html = fetch_html(href)
    if html is None:
        return []
    sections = parse_detail(html, href, list_title)
    rows = []
    for sub_title, body in sections:
        if not body:
            continue
        # sub_title 已经在 parse_detail 里加了序号，这里直接拼列表标题
        full_title = f"{list_title} - {sub_title}" if sub_title != list_title else list_title
        rows.append((full_title, body, href))
    return rows


# ---------------- 主流程 ----------------
def main():
    init_db()
    load_existing()
    buffer = []
    total_saved = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for href, list_title in iter_all_links():
            fut = pool.submit(fetch_and_parse, href, list_title)
            futures[fut] = (href, list_title)

            if len(futures) >= MAX_WORKERS * 4:
                done, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED)
                for fut in done:
                    h, t = futures.pop(fut)
                    try:
                        rows = fut.result()
                    except Exception as e:
                        print(f"  详情异常 {h}: {e}")
                        continue
                    if rows:
                        buffer.extend(rows)
                        print(f"  解析 {t}：{len(rows)} 条")
                    else:
                        print(f"  跳过（无有效正文）：{t}  ->  {h}")
                if len(buffer) >= 50:
                    total_saved += save_rows(buffer)
                    buffer.clear()

        for fut in as_completed(futures):
            h, t = futures[fut]
            try:
                rows = fut.result()
            except Exception as e:
                print(f"  详情异常 {h}: {e}")
                continue
            if rows:
                buffer.extend(rows)
                print(f"  解析 {t}：{len(rows)} 条")
            else:
                print(f"  跳过（无有效正文）：{t}  ->  {h}")

    total_saved += save_rows(buffer)
    print(f"\n完成，累计新增入库 {total_saved} 条")


if __name__ == '__main__':
    main()