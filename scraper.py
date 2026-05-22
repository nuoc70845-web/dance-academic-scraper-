import os
import sqlite3
import json
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from typing import Optional, List
from google import genai
from google.genai import types

VALID_CATEGORIES = "学术讲座、舞剧信息、展演资讯"

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

# ==================== 3. 核心处理函数 ====================
def fetch_and_analyze_article(url: str, api_key: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    proxies = {"http": None, "https": None}
    
    try:
        print("正在下载微信公众号网页...")
        response = requests.get(url, headers=headers, proxies=proxies, timeout=10)
        response.raise_for_status()
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        content_node = soup.find('div', id='js_content')
        if not content_node:
            return None
            
        text_content = content_node.get_text(separator="\n", strip=True)
        
        prompt = (
            "你是一个专业的艺术学术信息提取助手。请仔细阅读输入的网页文本，并结合附带的所有图片（通常为宣讲海报或演出信息图）。"
            f"只提取属于以下三个板块的信息：{VALID_CATEGORIES}。"
            "如果不是这三类，请不要入选。"
            "如果图片海报中的关键信息（如时间、地点）与网页文本不一致，请以图片海报上的准确信息为准。"
            "没有明确时间或地点时，可以写“待公布”，但不要编造。"
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
                        
                    img_resp = requests.get(img_url, headers=headers, proxies=proxies, timeout=5)
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
        client = genai.Client(api_key=api_key)
        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=EventList,
            temperature=0.1
        )
        
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config
        )
        
        return response.text

    except Exception as e:
        print(f"程序运行异常: {str(e)}")
        return None
# ==================== 4. 执行入口 ====================
if __name__ == "__main__":
    # 初始化创建数据库
    init_database()
    
    default_url = "https://mp.weixin.qq.com/s/UKwDIIvvBqc3hh-c0uvtIA"
    raw_urls = os.environ.get("ARTICLE_URLS", default_url)
    article_urls = [url.strip() for url in raw_urls.replace("\n", ",").split(",") if url.strip()]
    
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

        print(f"\n本次运行完成，共新增 {total_saved} 条信息。")
