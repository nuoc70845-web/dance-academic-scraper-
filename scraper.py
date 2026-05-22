import os
import sqlite3
import json
import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from typing import Optional, List
from google import genai
from google.genai import types

# ==================== 1. 定义预期的结构化 JSON 格式 ====================
class AcademicEvent(BaseModel):
    category: str = Field(description="分类，严格限制为：舞剧开票、学术讲座、期刊征稿、赛事通知、其他")
    title: str = Field(description="活动、舞剧或讲座的具体完整名称")
    date_time: Optional[str] = Field(description="核心时间，如讲座时间、开票时间或截稿日期")
    location: Optional[str] = Field(description="地点（线上活动写明平台如腾讯会议及号，线下写明具体场馆或院校）")
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

def save_to_database(json_str: str, source_url: str):
    """解析 Gemini 返回的 JSON 字符串并存入 SQLite 数据库"""
    try:
        data = json.loads(json_str)
        conn = sqlite3.connect('academic_events.db')
        cursor = conn.cursor()
        
        # 遍历 JSON 中的事件列表并写入
        for event in data.get("events", []):
            cursor.execute('''
                INSERT OR IGNORE INTO academic_events (category, title, date, location, summary, url)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                event.get("category"),
                event.get("title"),
                event.get("date_time"),
                event.get("location"),
                event.get("summary"),
                source_url
            ))
            
        conn.commit()
        conn.close()
        print("🎉 数据已成功同步至本地 SQLite 数据库 (academic_events.db)")
    except Exception as e:
        print(f"数据库写入失败: {e}")

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
            "提取出里面所有的学术讲座、舞剧开票或征稿信息。如果图片海报中的关键信息（如时间、地点）与网页文本不一致，请以图片海报上的准确信息为准。"
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
        
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=EventList,
            temperature=0.1
        )
        
        response = client.models.generate_content(
            model="gemini-3.5-flash",
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
    
    test_url = "https://mp.weixin.qq.com/s/UKwDIIvvBqc3hh-c0uvtIA"
    
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
        json_output = fetch_and_analyze_article(test_url, GEMINI_API_KEY)
