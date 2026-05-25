import os
import sqlite3
import json
import requests
import re
from bs4 import BeautifulSoup
from datetime import datetime, date
from zoneinfo import ZoneInfo
from urllib.parse import quote, unquote, urljoin, urlparse
from pydantic import BaseModel, Field
from typing import Optional, List
from google import genai
from google.genai import types

VALID_CATEGORIES = "学术讲座、舞剧信息、展演资讯"
EXCLUDED_SHOW_TITLES = ["无名之辈", "叹春风"]
TARGET_PERFORMANCE_CITIES = [
    "上海", "南京", "杭州", "苏州", "宁波"
]
CITY_TO_REGION = {
    "上海": "上海",
    "南京": "江苏", "苏州": "江苏", "无锡": "江苏", "常州": "江苏",
    "南通": "江苏", "扬州": "江苏", "镇江": "江苏", "泰州": "江苏",
    "盐城": "江苏", "淮安": "江苏", "徐州": "江苏", "连云港": "江苏",
    "宿迁": "江苏",
    "杭州": "浙江", "宁波": "浙江", "温州": "浙江", "绍兴": "浙江",
    "嘉兴": "浙江", "湖州": "浙江", "金华": "浙江", "台州": "浙江",
    "舟山": "浙江", "丽水": "浙江", "衢州": "浙江",
}
LOCATION_HINTS = {
    "上海": "上海", "上海国际舞蹈中心": "上海",
    "南京": "南京", "江苏大剧院": "南京", "南京保利": "南京",
    "苏州": "苏州", "无锡": "无锡", "常州": "常州", "南通": "南通",
    "扬州": "扬州", "镇江": "镇江", "泰州": "泰州", "盐城": "盐城",
    "淮安": "淮安", "徐州": "徐州", "连云港": "连云港", "宿迁": "宿迁",
    "杭州": "杭州", "浙江音乐学院": "杭州", "浙音": "杭州",
    "宁波": "宁波", "温州": "温州", "绍兴": "绍兴", "嘉兴": "嘉兴",
    "湖州": "湖州", "金华": "金华", "台州": "台州", "舟山": "舟山",
    "丽水": "丽水", "衢州": "衢州",
}
DEFAULT_WEBSITE_URLS = [
    "https://njbldjy.polyt.cn/#/",
    "https://www.jsartcentre.org",
    "https://www.nua.edu.cn",
    "https://wd.nua.edu.cn",
    "https://wd.sta.edu.cn",
    "https://www.bda.edu.cn",
    "https://wdxy.zjcm.edu.cn",
    "https://www.zgysyjy.org.cn/dance_research.html",
]
DAMAI_SEARCH_URL_TEMPLATE = (
    "https://search.damai.cn/search.htm?ctl=%E8%88%9E%E8%B9%88%E8%8A%AD%E8%95%BE"
    "&order=1&cty={city}"
)
DAMAI_SEARCH_URLS = [
    DAMAI_SEARCH_URL_TEMPLATE.format(city=quote(city))
    for city in TARGET_PERFORMANCE_CITIES
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

# ==================== 1. 定义预期的结构化 JSON 格式 ====================
class AcademicEvent(BaseModel):
    category: str = Field(description=f"分类，严格限制为：{VALID_CATEGORIES}")
    title: str = Field(description="活动、舞剧或讲座的具体完整名称")
    date_time: Optional[str] = Field(default=None, description="核心时间，如讲座时间、演出时间、开票时间")
    location: Optional[str] = Field(default=None, description="地点（线上活动写明平台如腾讯会议及号，线下写明具体场馆或院校）")
    summary: str = Field(description="100字以内的核心内容摘要，提炼关键干货")
    poster_url: Optional[str] = Field(default=None, description="舞剧或活动海报图片链接，没有则为空")
    ticket_url: Optional[str] = Field(default=None, description="购票入口或原文链接，没有则为空")
    source: Optional[str] = Field(default=None, description="信息来源，如大麦、江苏大剧院、公众号等")

class EventList(BaseModel):
    events: List[AcademicEvent] = Field(description="从文章文本和图片中提取出的所有事件列表")

# ==================== 2. 数据库操作逻辑 ====================
def init_database():
    """初始化数据库，创建学术活动表"""
    conn = sqlite3.connect('academic_events.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS academic_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 修正此处语法
            category TEXT,
            title TEXT,
            date TEXT,
            location TEXT,
            summary TEXT,
            url TEXT,
            UNIQUE(title, date) -- 防止相同活动重复插入
        )
    ''')
    existing_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(academic_events)").fetchall()
    }
    extra_columns = {
        "poster_url": "TEXT",
        "intro": "TEXT",
        "ticket_url": "TEXT",
        "source": "TEXT",
    }
    for column, column_type in extra_columns.items():
        if column not in existing_columns:
            cursor.execute(f"ALTER TABLE academic_events ADD COLUMN {column} {column_type}")
    conn.commit()
    conn.close()

def parse_model_json(json_str: str) -> dict:
    """兼容模型偶尔返回 Markdown 代码块的情况。"""
    cleaned = json_str.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    return json.loads(cleaned)

def today_in_china() -> date:
    return datetime.now(LOCAL_TZ).date()

def parse_event_dates(date_text: Optional[str], reference_year: Optional[int] = None) -> List[date]:
    """从中文/数字日期文本中提取日期，用于判断信息是否过期。"""
    if not date_text:
        return []

    text = str(date_text)
    if any(marker in text for marker in ["待公布", "另行通知", "暂无", "不详"]):
        return []

    year = reference_year or today_in_china().year
    dates = []

    # 2026年5月24日、2026-05-24、2026.05.24、2026/05/24
    full_date_pattern = re.compile(
        r"(?P<year>20\d{2})\s*[年./-]\s*(?P<month>\d{1,2})\s*[月./-]\s*(?P<day>\d{1,2})"
    )
    for match in full_date_pattern.finditer(text):
        try:
            dates.append(date(int(match.group("year")), int(match.group("month")), int(match.group("day"))))
        except ValueError:
            continue

    # 5月24日、5.24、5/24
    month_day_pattern = re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*[月./]\s*(?P<day>\d{1,2})\s*[日号]?")
    for match in month_day_pattern.finditer(text):
        if match.group(0).startswith("20"):
            continue
        try:
            dates.append(date(year, int(match.group("month")), int(match.group("day"))))
        except ValueError:
            continue

    # 5月24日-25日、2026.05.24-25
    same_month_range_pattern = re.compile(
        r"(?:(?P<year>20\d{2})\s*[年./-]\s*)?"
        r"(?P<month>\d{1,2})\s*[月./]\s*(?P<start>\d{1,2})\s*[日号]?"
        r"\s*[-—–~至到]+\s*(?P<end>\d{1,2})\s*[日号]?(?!\s*[月./])"
    )
    for match in same_month_range_pattern.finditer(text):
        try:
            range_year = int(match.group("year")) if match.group("year") else year
            dates.append(date(range_year, int(match.group("month")), int(match.group("end"))))
        except ValueError:
            continue

    return sorted(set(dates))

def is_current_or_future(date_text: Optional[str]) -> bool:
    event_dates = parse_event_dates(date_text)
    return bool(event_dates) and max(event_dates) >= today_in_china()

def normalize_title(title: Optional[str]) -> str:
    text = str(title or "")
    text = re.sub(r"[《》【】\[\]（）()“”\"'·\s]", "", text)
    text = re.sub(r"(舞剧|音乐剧|芭蕾舞剧|芭蕾|演出|中文版|巡演|专场|经典版)", "", text)
    return text.lower()

def is_excluded_show(title: Optional[str]) -> bool:
    normalized = normalize_title(title)
    return any(normalize_title(excluded) in normalized for excluded in EXCLUDED_SHOW_TITLES)

def infer_city_from_text(*values: Optional[str]) -> str:
    text = " ".join(str(value or "") for value in values)
    for hint, city in LOCATION_HINTS.items():
        if hint in text:
            return city
    return ""

def is_target_performance_city(*values: Optional[str]) -> bool:
    city = infer_city_from_text(*values)
    return city in TARGET_PERFORMANCE_CITIES

def source_priority(source: Optional[str], url: Optional[str] = None) -> int:
    text = f"{source or ''} {url or ''}".lower()
    if "大麦" in text or "damai" in text:
        return 100
    if "jsartcentre" in text or "shdancecenter" in text or "polyt" in text:
        return 60
    return 10

def source_name_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "damai" in host:
        return "大麦"
    if "jsartcentre" in host:
        return "江苏大剧院"
    if "shdancecenter" in host:
        return "上海国际舞蹈中心"
    if "polyt" in host:
        return "南京保利大剧院"
    if "nua.edu.cn" in host:
        return "南京艺术学院"
    if "sta.edu.cn" in host:
        return "上海戏剧学院舞蹈学院"
    if "bda.edu.cn" in host:
        return "北京舞蹈学院"
    if "zjcm.edu.cn" in host:
        return "浙江音乐学院舞蹈学院"
    if "zgysyjy" in host:
        return "中国艺术研究院舞蹈研究所"
    if "mp.weixin.qq.com" in host:
        return "微信公众号"
    return host or "未知来源"

def prune_expired_events() -> int:
    """删除数据库中非今天/未来的信息，包括日期无法解析的信息。"""
    conn = sqlite3.connect('academic_events.db')
    cursor = conn.cursor()
    rows = cursor.execute("SELECT id, date FROM academic_events").fetchall()
    expired_ids = [row_id for row_id, event_date in rows if not is_current_or_future(event_date)]

    if expired_ids:
        cursor.executemany("DELETE FROM academic_events WHERE id = ?", [(row_id,) for row_id in expired_ids])
        conn.commit()

    conn.close()
    print(f"已清理过期或无有效日期的信息 {len(expired_ids)} 条。")
    return len(expired_ids)

def dedupe_events_prefer_damai() -> int:
    conn = sqlite3.connect('academic_events.db')
    cursor = conn.cursor()
    rows = cursor.execute('''
        SELECT id, category, title, date, location, source, url
        FROM academic_events
        ORDER BY id ASC
    ''').fetchall()

    groups = {}
    for row_id, category, title, date_text, location, source, url in rows:
        city = infer_city_from_text(location, title)
        event_dates = parse_event_dates(date_text)
        date_key = event_dates[-1].isoformat() if event_dates else str(date_text or "")
        key = (category, normalize_title(title), date_key, city)
        if not key[1]:
            continue
        groups.setdefault(key, []).append((row_id, source, url))

    delete_ids = []
    for grouped_rows in groups.values():
        if len(grouped_rows) <= 1:
            continue
        keep = max(grouped_rows, key=lambda item: (source_priority(item[1], item[2]), -item[0]))
        keep_id = keep[0]
        delete_ids.extend(row_id for row_id, _, _ in grouped_rows if row_id != keep_id)

    if delete_ids:
        cursor.executemany("DELETE FROM academic_events WHERE id = ?", [(row_id,) for row_id in delete_ids])
        conn.commit()

    conn.close()
    print(f"已去重并优先保留大麦信息 {len(delete_ids)} 条。")
    return len(delete_ids)

def find_duplicate_event(cursor, event: dict, source_url: str):
    title = (event.get("title") or "").strip()
    date_text = event.get("date_time")
    city = infer_city_from_text(event.get("location"), title, event.get("summary"))
    normalized = normalize_title(title)

    if not normalized:
        return None

    rows = cursor.execute('''
        SELECT id, title, date, location, source, url
        FROM academic_events
        WHERE category = ?
    ''', (event.get("category") or "舞剧信息",)).fetchall()

    incoming_dates = set(parse_event_dates(date_text))
    for row_id, old_title, old_date, old_location, old_source, old_url in rows:
        old_normalized = normalize_title(old_title)
        if not old_normalized:
            continue

        same_title = normalized == old_normalized or normalized in old_normalized or old_normalized in normalized
        if not same_title:
            continue

        old_dates = set(parse_event_dates(old_date))
        old_city = infer_city_from_text(old_location, old_title)
        same_date = bool(incoming_dates and old_dates and incoming_dates.intersection(old_dates))
        same_city = bool(city and old_city and city == old_city)

        if same_date or same_city:
            return {
                "id": row_id,
                "source": old_source,
                "url": old_url,
            }

    return None

def save_event_record(cursor, event: dict, source_url: str) -> str:
    title = (event.get("title") or "").strip()
    category = (event.get("category") or "").strip()
    date_text = event.get("date_time")
    location = event.get("location")
    summary = str(event.get("summary") or "")
    poster_url = event.get("poster_url")
    ticket_url = event.get("ticket_url") or source_url
    source = event.get("source") or source_name_from_url(source_url)

    if not title or is_excluded_show(title):
        return "skipped"

    if not is_current_or_future(date_text):
        return "skipped"

    if category == "舞剧信息" and not is_target_performance_city(location, title, summary):
        return "skipped"

    duplicate = find_duplicate_event(cursor, event, source_url)
    if duplicate:
        incoming_priority = source_priority(source, ticket_url)
        existing_priority = source_priority(duplicate.get("source"), duplicate.get("url"))
        if incoming_priority > existing_priority:
            cursor.execute('''
                UPDATE academic_events
                SET category = ?, title = ?, date = ?, location = ?, summary = ?, url = ?,
                    poster_url = ?, intro = ?, ticket_url = ?, source = ?
                WHERE id = ?
            ''', (
                category,
                title,
                date_text,
                location,
                summary[:100],
                ticket_url,
                poster_url,
                summary[:100],
                ticket_url,
                source,
                duplicate["id"]
            ))
            return "updated"
        return "duplicate"

    cursor.execute('''
        INSERT OR IGNORE INTO academic_events
        (category, title, date, location, summary, url, poster_url, intro, ticket_url, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        category,
        title,
        date_text,
        location,
        summary[:100],
        ticket_url,
        poster_url,
        summary[:100],
        ticket_url,
        source
    ))
    if cursor.rowcount:
        return "inserted"

    exact_duplicate = cursor.execute('''
        SELECT id, source, url
        FROM academic_events
        WHERE title = ? AND date = ?
        LIMIT 1
    ''', (title, date_text)).fetchone()
    if exact_duplicate:
        row_id, old_source, old_url = exact_duplicate
        if source_priority(source, ticket_url) > source_priority(old_source, old_url):
            cursor.execute('''
                UPDATE academic_events
                SET category = ?, location = ?, summary = ?, url = ?,
                    poster_url = ?, intro = ?, ticket_url = ?, source = ?
                WHERE id = ?
            ''', (
                category,
                location,
                summary[:100],
                ticket_url,
                poster_url,
                summary[:100],
                ticket_url,
                source,
                row_id
            ))
            return "updated"

    return "duplicate"

def save_to_database(json_str: str, source_url: str) -> int:
    """解析 Gemini 返回的 JSON 字符串并存入 SQLite 数据库"""
    try:
        data = parse_model_json(json_str)
        events = data.get("events", [])

        if not events:
            print("模型返回成功，但没有识别到可入库的信息。")
            print(f"模型原始返回前300字：{json_str[:300]}")
            return 0

        conn = sqlite3.connect('academic_events.db')
        cursor = conn.cursor()
        saved_count = 0
        updated_count = 0
        skipped_count = 0
        duplicate_count = 0
        
        # 遍历 JSON 中的事件列表并写入
        for event in events:
            result = save_event_record(cursor, event, source_url)
            if result == "inserted":
                saved_count += 1
            elif result == "updated":
                updated_count += 1
            elif result == "duplicate":
                duplicate_count += 1
            else:
                skipped_count += 1
            
        conn.commit()
        conn.close()
        print(f"数据已同步至本地 SQLite 数据库：新增 {saved_count} 条，更新 {updated_count} 条，重复跳过 {duplicate_count} 条，筛除 {skipped_count} 条，模型共识别 {len(events)} 条。")
        return saved_count
    except Exception as e:
        print(f"数据库写入失败: {e}")
        print(f"模型原始返回前500字：{json_str[:500] if json_str else '空'}")
        return 0

# ==================== 3. 核心处理函数 ====================
def get_model_client(api_key: str):
    return genai.Client(api_key=api_key)

def get_model_config():
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=EventList,
        temperature=0.1
    )

def get_model_name():
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

def fetch_and_analyze_article(url: str, api_key: str):
    proxies = {"http": None, "https": None}
    
    try:
        print("正在下载微信公众号网页...")
        response = requests.get(url, headers=HEADERS, proxies=proxies, timeout=10)
        response.raise_for_status()
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        content_node = soup.find('div', id='js_content')
        if not content_node:
            return None
            
        text_content = content_node.get_text(separator="\n", strip=True)
        today_text = today_in_china().isoformat()
        
        prompt = (
            "你是一个专业的艺术学术信息提取助手。请仔细阅读输入的网页文本，并结合附带的所有图片（通常为宣讲海报或演出信息图）。"
            f"只提取属于以下三个板块的信息：{VALID_CATEGORIES}。"
            f"今天是 {today_text}，只提取今天或未来发生的信息，已经过期的信息不要提取。"
            "如果不是这三类，请不要入选。"
            "如果图片海报中的关键信息（如时间、地点）与网页文本不一致，请以图片海报上的准确信息为准。"
            "必须尽量提取明确的年月日；没有明确时间的信息不要提取。"
            "summary 控制在100字以内；ticket_url 优先使用原文链接；source 写清来源。"
        )
        contents = [prompt, f"网页文本内容如下：\n{text_content}"]
        
        print("正在提取并下载海报图片...")
        img_tags = content_node.find_all('img')
        img_count = 0
        MAX_IMAGES = 6  # 设定安全阀：最多只处理6张图片
        
        for img in img_tags:
            if img_count >= MAX_IMAGES:
                print(f"已达到图片数量上限 ({MAX_IMAGES}张)，跳过剩余排版图片以保障效率。")
                break
                
            img_url = img.get('data-src')
            if img_url:
                try:
                    if "wx_fmt=gif" in img_url:
                        continue
                        
                    img_resp = requests.get(img_url, headers=HEADERS, proxies=proxies, timeout=5)
                    if img_resp.status_code == 200:
                        mime_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                        if "image" in mime_type and "gif" not in mime_type:
                            img_part = types.Part.from_bytes(
                                data=img_resp.content,
                                mime_type=mime_type
                            )
                            contents.append(img_part)
                            img_count += 1
                except Exception:
                    continue
                    
        print(f"网页文本分析完毕。已加载 {img_count} 张图片。")
        
        print("正在请求 Gemini API 进行多模态融合分析...")
        client = get_model_client(api_key)
        
        response = client.models.generate_content(
            model=get_model_name(),
            contents=contents,
            config=get_model_config()
        )
        
        return response.text

    except Exception as e:
        print(f"程序运行异常: {str(e)}")
        return None

def normalize_urls(raw_urls: str) -> List[str]:
    return [url.strip() for url in raw_urls.replace("\n", ",").split(",") if url.strip()]

def same_site(base_url: str, target_url: str) -> bool:
    base_host = urlparse(base_url).netloc.replace("www.", "")
    target_host = urlparse(target_url).netloc.replace("www.", "")
    return bool(target_host) and base_host == target_host

def extract_candidate_links(base_url: str, soup: BeautifulSoup, max_links: int = 30) -> List[dict]:
    keywords = [
        "舞", "演出", "展演", "剧目", "舞剧", "购票", "开票", "讲座",
        "论坛", "活动", "艺术节", "工作坊", "大师课", "导赏", "研讨",
        "会议", "学术", "通知", "公告", "新闻", "舞蹈", "科研"
    ]
    candidates = []
    seen = set()

    for link in soup.find_all("a"):
        title = link.get_text(" ", strip=True)
        href = link.get("href")
        if not title or not href:
            continue

        full_url = urljoin(base_url, href)
        if full_url in seen or not same_site(base_url, full_url):
            continue

        if any(keyword in title for keyword in keywords):
            seen.add(full_url)
            candidates.append({"title": title[:120], "url": full_url})

        if len(candidates) >= max_links:
            break

    return candidates

def fetch_page_text(url: str, max_chars: int = 5000) -> str:
    response = requests.get(url, headers=HEADERS, timeout=12)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    soup = BeautifulSoup(response.text, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return soup.get_text("\n", strip=True)[:max_chars]

def find_nearby_image_url(node):
    container = node
    for _ in range(4):
        if container is None:
            break
        image = container.find("img") if hasattr(container, "find") else None
        if image:
            src = image.get("src") or image.get("data-src") or image.get("data-spm")
            if src and src.startswith("//"):
                return "https:" + src
            if src:
                return src
        container = container.parent
    return None

def extract_damai_candidates(search_url: str, soup: BeautifulSoup, city: str) -> List[dict]:
    candidates = []
    seen = set()

    for link in soup.find_all("a"):
        href = link.get("href")
        if not href:
            continue

        full_url = urljoin(search_url, href)
        if full_url.startswith("//"):
            full_url = "https:" + full_url

        if "damai.cn" not in full_url:
            continue

        title = link.get_text(" ", strip=True)
        container_text = ""
        container = link
        for _ in range(4):
            if container is None:
                break
            container_text = container.get_text(" ", strip=True)
            if len(container_text) >= len(title) + 20:
                break
            container = container.parent

        combined_text = container_text or title
        if not combined_text or full_url in seen:
            continue

        if not any(keyword in combined_text for keyword in ["舞", "芭蕾", "剧", "演出", "剧场", "剧院"]):
            continue

        seen.add(full_url)
        candidates.append({
            "title": title[:120] or combined_text[:120],
            "city": city,
            "url": full_url,
            "poster_url": find_nearby_image_url(link),
            "text": combined_text[:1000],
        })

    if candidates:
        return candidates[:20]

    # 大麦搜索页常把数据藏在脚本里；这里兜底提取脚本里的项目链接和图片。
    page_source = str(soup)
    item_urls = re.findall(r"https?:\\?/\\?/item\.damai\.cn/item\.htm\?id=\d+|//item\.damai\.cn/item\.htm\?id=\d+", page_source)
    image_urls = re.findall(r"https?:\\?/\\?/[^\"']+?(?:alicdn|damai)[^\"']+?\.(?:jpg|jpeg|png|webp)", page_source)
    clean_images = []
    for image_url in image_urls:
        cleaned = image_url.replace("\\/", "/")
        if cleaned.startswith("//"):
            cleaned = "https:" + cleaned
        clean_images.append(cleaned)

    for index, item_url in enumerate(item_urls[:20]):
        cleaned_url = item_url.replace("\\/", "/")
        if cleaned_url.startswith("//"):
            cleaned_url = "https:" + cleaned_url
        if cleaned_url in seen:
            continue
        seen.add(cleaned_url)
        candidates.append({
            "title": "",
            "city": city,
            "url": cleaned_url,
            "poster_url": clean_images[index] if index < len(clean_images) else None,
            "text": "",
        })

    return candidates[:20]

def fetch_and_analyze_damai(search_url: str, api_key: str):
    try:
        city = ""
        parsed = urlparse(search_url)
        for part in parsed.query.split("&"):
            if part.startswith("cty="):
                city = unquote(part.split("=", 1)[1])
                break
        city = city or infer_city_from_text(search_url)

        print(f"正在抓取大麦舞蹈芭蕾：{city or search_url}")
        response = requests.get(search_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        candidates = extract_damai_candidates(search_url, soup, city)
        print(f"大麦候选演出：{len(candidates)} 条。")

        if not candidates:
            return None

        candidates = candidates[:5]

        prompt = (
            "你是一个舞蹈演出数据整理助手。请只从下面的大麦候选演出中筛选舞剧、舞蹈、芭蕾相关信息。"
            f"今天是 {today_in_china().isoformat()}，只保留今天或未来的演出。"
            "只保留浙江、江苏、上海主要城市的信息。"
            "排除《无名之辈》和《叹春风》。"
            "不要虚构信息；没有明确日期的候选不要输出。"
            "输出 category 必须为“舞剧信息”。"
            "summary 写100字以内简介；poster_url 使用候选里的海报链接；ticket_url 使用候选里的大麦链接；source 写“大麦”。"
            "如果同一剧目有多场，只要城市、日期或场馆不同，可以分别输出。"
        )
        contents = [
            prompt,
            "大麦候选数据：\n" + json.dumps(candidates, ensure_ascii=False)
        ]

        print("正在请求 Gemini API 整理大麦演出信息...")
        client = get_model_client(api_key)
        response = client.models.generate_content(
            model=get_model_name(),
            contents=contents,
            config=get_model_config()
        )
        return response.text

    except Exception as e:
        print(f"大麦采集异常：{search_url} ({e})")
        return None

def fetch_and_analyze_website(url: str, api_key: str):
    try:
        print(f"正在抓取网站入口：{url}")
        response = requests.get(url, headers=HEADERS, timeout=12)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        for node in soup(["script", "style", "noscript"]):
            node.decompose()

        page_text = soup.get_text("\n", strip=True)[:6000]
        candidates = extract_candidate_links(url, soup)
        print(f"网站入口文本长度：{len(page_text)}；候选链接：{len(candidates)} 条。")

        details = []
        for item in candidates[:12]:
            try:
                detail_text = fetch_page_text(item["url"], max_chars=2500)
                details.append(f"标题：{item['title']}\n链接：{item['url']}\n页面文本：{detail_text}")
            except Exception as detail_error:
                print(f"候选页面读取失败，已跳过：{item['url']} ({detail_error})")

        candidate_text = "\n\n---\n\n".join(details)
        prompt = (
            "你是一个舞蹈学术信息筛选助手。请从下面的网站文本与候选页面中筛选信息。"
            f"只保留以下三个板块：{VALID_CATEGORIES}。"
            f"今天是 {today_in_china().isoformat()}，只提取今天或未来发生的信息，已经过期的信息不要提取。"
            "舞剧、舞蹈演出、开票、购票归为“舞剧信息”；"
            "艺术节、院团活动、舞蹈展演、工作坊可归为“展演资讯”；"
            "学术讲座、论坛、研讨会、导赏讲座归为“学术讲座”。"
            "不要收录无明确活动含义的导航、栏目名、广告语。"
            "必须尽量提取明确的年月日；没有明确时间的信息不要提取。"
            "summary 控制在100字以内；ticket_url 使用候选页面链接；source 根据网站来源填写。"
        )
        contents = [
            prompt,
            f"网站入口：{url}\n\n入口页面文本：\n{page_text}\n\n候选页面：\n{candidate_text}"
        ]

        print("正在请求 Gemini API 进行网站信息筛选...")
        client = get_model_client(api_key)
        response = client.models.generate_content(
            model=get_model_name(),
            contents=contents,
            config=get_model_config()
        )
        return response.text

    except Exception as e:
        print(f"网站采集异常：{url} ({e})")
        return None

# ==================== 4. 执行入口 ====================
if __name__ == "__main__":
    # 初始化创建数据库
    init_database()
    prune_expired_events()
    
    raw_urls = os.environ.get("ARTICLE_URLS", "")
    article_urls = normalize_urls(raw_urls)

    raw_website_urls = os.environ.get("WEBSITE_URLS") or ",".join(DEFAULT_WEBSITE_URLS)
    website_urls = normalize_urls(raw_website_urls)
    raw_damai_urls = os.environ.get("DAMAI_URLS") or ",".join(DAMAI_SEARCH_URLS)
    damai_urls = normalize_urls(raw_damai_urls)

    if not article_urls and not website_urls and not damai_urls:
        print("本次没有收到公众号文章链接或网站入口链接，所以不会新增数据。")
        print("请在 GitHub Actions 手动运行时填写 article_urls，或保留默认 website_urls。")
        exit(1)

    print(f"本次共收到 {len(article_urls)} 篇公众号文章链接、{len(website_urls)} 个网站入口、{len(damai_urls)} 个大麦城市入口。")
    
   # 优先从 GitHub Actions 环境变量读取密钥
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    
    # --- 新增的 Debug 代码 ---
    if GEMINI_API_KEY:
        print(f"系统成功读取到环境变量。")
        print(f"当前使用的密钥长度: {len(GEMINI_API_KEY)}")
        print(f"当前使用的密钥前5位: {GEMINI_API_KEY[:5]}***")
    else:
        print("致命错误：系统未能读取到任何环境变量，GEMINI_API_KEY 为空。")
        exit(1)
    # -------------------------

    if GEMINI_API_KEY:
        total_saved = 0
        for article_url in article_urls:
            print(f"\n开始处理文章：{article_url}")
            json_output = fetch_and_analyze_article(article_url, GEMINI_API_KEY)
            if json_output:
                print(f"模型返回前300字：{json_output[:300]}")
                total_saved += save_to_database(json_output, article_url)
            else:
                print("本篇文章没有返回可保存的提取结果。")

        for website_url in website_urls:
            print(f"\n开始处理网站：{website_url}")
            json_output = fetch_and_analyze_website(website_url, GEMINI_API_KEY)
            if json_output:
                print(f"模型返回前300字：{json_output[:300]}")
                total_saved += save_to_database(json_output, website_url)
            else:
                print("本网站没有返回可保存的筛选结果。")

        for damai_url in damai_urls:
            print(f"\n开始处理大麦：{damai_url}")
            json_output = fetch_and_analyze_damai(damai_url, GEMINI_API_KEY)
            if json_output:
                print(f"模型返回前300字：{json_output[:300]}")
                total_saved += save_to_database(json_output, damai_url)
            else:
                print("本大麦入口没有返回可保存的演出结果。")

        prune_expired_events()
        dedupe_events_prefer_damai()
        print(f"\n本次运行完成，共新增 {total_saved} 条信息。")
