import os
import sqlite3
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from pydantic import BaseModel, Field
from typing import Optional, List
from google import genai
from google.genai import types

VALID_CATEGORIES = "学术讲座、舞剧信息、展演资讯"
DEFAULT_WEBSITE_URLS = [
    "http://www.shdancecenter.com",
    "https://njbldjy.polyt.cn/#/",
    "https://www.jsartcentre.org",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ==================== 1. 定义预期的结构化 JSON 格式 ====================
class AcademicEvent(BaseModel):
    category: str = Field(description=f"分类，严格限制为：{VALID_CATEGORIES}")
    title: str = Field(description="活动、舞剧或讲座的具体完整名称")
    date_time: Optional[str] = Field(default=None, description="核心时间，如讲座时间、演出时间、开票时间")
    location: Optional[str] = Field(default=None, description="地点（线上活动写明平台如腾讯会议及号，线下写明具体场馆或院校）")
    summary: str = Field(description="50字以内的核心内容摘要，提炼关键干货")

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
        
        # 遍历 JSON 中的事件列表并写入
        for event in events:
            title = (event.get("title") or "").strip()
            if not title:
                continue

            before_count = conn.total_changes
            cursor.execute('''
                INSERT OR IGNORE INTO academic_events (category, title, date, location, summary, url)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                event.get("category"),
                title,
                event.get("date_time"),
                event.get("location"),
                event.get("summary"),
                source_url
            ))
            if conn.total_changes > before_count:
                saved_count += 1
            
        conn.commit()
        conn.close()
        print(f"数据已同步至本地 SQLite 数据库：新增 {saved_count} 条，模型共识别 {len(events)} 条。")
        return saved_count
    except Exception as e:
        print(f"数据库写入失败: {e}")
        print(f"模型原始返回前500字：{json_str[:500] if json_str else '空'}")
        return 0
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
        "论坛", "活动", "艺术节", "工作坊", "大师课", "导赏"
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
        for item in candidates[:8]:
            try:
                detail_text = fetch_page_text(item["url"], max_chars=2500)
                details.append(f"标题：{item['title']}\n链接：{item['url']}\n页面文本：{detail_text}")
            except Exception as detail_error:
                print(f"候选页面读取失败，已跳过：{item['url']} ({detail_error})")

        candidate_text = "\n\n---\n\n".join(details)
        prompt = (
            "你是一个舞蹈学术信息筛选助手。请从下面的网站文本与候选页面中筛选信息。"
            f"只保留以下三个板块：{VALID_CATEGORIES}。"
            "舞剧、舞蹈演出、开票、购票归为“舞剧信息”；"
            "艺术节、院团活动、舞蹈展演、工作坊可归为“展演资讯”；"
            "学术讲座、论坛、研讨会、导赏讲座归为“学术讲座”。"
            "不要收录无明确活动含义的导航、栏目名、广告语。"
            "如果时间或地点不清楚，写“待公布”。"
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
    
    raw_urls = os.environ.get("ARTICLE_URLS", "")
    article_urls = normalize_urls(raw_urls)

    raw_website_urls = os.environ.get("WEBSITE_URLS") or ",".join(DEFAULT_WEBSITE_URLS)
    website_urls = normalize_urls(raw_website_urls)

    if not article_urls and not website_urls:
        print("本次没有收到公众号文章链接或网站入口链接，所以不会新增数据。")
        print("请在 GitHub Actions 手动运行时填写 article_urls，或保留默认 website_urls。")
        exit(1)

    print(f"本次共收到 {len(article_urls)} 篇公众号文章链接、{len(website_urls)} 个网站入口。")
    
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

        print(f"\n本次运行完成，共新增 {total_saved} 条信息。")
