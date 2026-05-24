import json
import os
import re
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None
    types = None


st.set_page_config(page_title="舞蹈学术信息看板", layout="wide")

st.title("舞蹈学术信息看板")
st.markdown("按城市整理演出资讯与学术讲座/研讨会，仅展示今天及未来的信息。")

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
DISPLAY_SECTIONS = ["演出资讯", "学术讲座/研讨会"]

PERFORMANCE_KEYWORDS = [
    "舞剧", "演出", "剧目", "购票", "开票", "剧场", "剧院", "专场",
    "上演", "巡演", "芭蕾", "音乐剧", "舞蹈诗剧", "校内演出"
]

ACADEMIC_KEYWORDS = [
    "讲座", "研讨", "论坛", "会议", "学术", "报告", "seminar", "symposium"
]

CITY_ORDER = [
    "上海",
    "南京", "苏州", "无锡", "常州", "南通", "扬州", "镇江", "泰州",
    "盐城", "淮安", "徐州", "连云港", "宿迁",
    "杭州", "宁波", "温州", "绍兴", "嘉兴", "湖州", "金华", "台州",
    "舟山", "丽水", "衢州",
    "北京",
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
    "北京": "北京",
}

LOCATION_HINTS = {
    "上海": "上海",
    "上戏": "上海",
    "上海戏剧学院": "上海",
    "上海国际舞蹈中心": "上海",
    "南京": "南京",
    "南艺": "南京",
    "南京艺术学院": "南京",
    "江苏大剧院": "南京",
    "南京保利": "南京",
    "苏州": "苏州",
    "无锡": "无锡",
    "常州": "常州",
    "南通": "南通",
    "扬州": "扬州",
    "镇江": "镇江",
    "泰州": "泰州",
    "盐城": "盐城",
    "淮安": "淮安",
    "徐州": "徐州",
    "连云港": "连云港",
    "宿迁": "宿迁",
    "杭州": "杭州",
    "浙江音乐学院": "杭州",
    "浙音": "杭州",
    "宁波": "宁波",
    "温州": "温州",
    "绍兴": "绍兴",
    "嘉兴": "嘉兴",
    "湖州": "湖州",
    "金华": "金华",
    "台州": "台州",
    "舟山": "舟山",
    "丽水": "丽水",
    "衢州": "衢州",
    "北京": "北京",
    "北舞": "北京",
    "北京舞蹈学院": "北京",
    "中国艺术研究院": "北京",
}


def clean_value(value, fallback="待公布"):
    if pd.isna(value) or str(value).strip() == "":
        return fallback
    return str(value).strip()


def today_in_china():
    return datetime.now(LOCAL_TZ).date()


def parse_event_dates(date_text, reference_year=None):
    if not date_text:
        return []

    text = str(date_text)
    if any(marker in text for marker in ["待公布", "另行通知", "暂无", "不详"]):
        return []

    year = reference_year or today_in_china().year
    dates = []

    full_date_pattern = re.compile(
        r"(?P<year>20\d{2})\s*[年./-]\s*(?P<month>\d{1,2})\s*[月./-]\s*(?P<day>\d{1,2})"
    )
    for match in full_date_pattern.finditer(text):
        try:
            dates.append(date(int(match.group("year")), int(match.group("month")), int(match.group("day"))))
        except ValueError:
            continue

    month_day_pattern = re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*[月./]\s*(?P<day>\d{1,2})\s*[日号]?")
    for match in month_day_pattern.finditer(text):
        try:
            dates.append(date(year, int(match.group("month")), int(match.group("day"))))
        except ValueError:
            continue

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


def is_current_or_future(date_text):
    event_dates = parse_event_dates(date_text)
    return bool(event_dates) and max(event_dates) >= today_in_china()


def classify_section(row):
    category = clean_value(row.get("category"), "")
    text = " ".join([
        clean_value(row.get("title"), ""),
        clean_value(row.get("summary"), ""),
        category,
    ]).lower()

    if category == "舞剧信息" or any(keyword.lower() in text for keyword in PERFORMANCE_KEYWORDS):
        return "演出资讯"

    if category == "学术讲座" or any(keyword.lower() in text for keyword in ACADEMIC_KEYWORDS):
        return "学术讲座/研讨会"

    return ""


def fallback_city(row):
    text = " ".join([
        clean_value(row.get("location"), ""),
        clean_value(row.get("title"), ""),
        clean_value(row.get("summary"), ""),
    ])
    for hint, city in LOCATION_HINTS.items():
        if hint in text:
            return city
    return "其他"


def get_api_key():
    try:
        return st.secrets.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    except Exception:
        return os.environ.get("GEMINI_API_KEY", "")


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def ai_city_from_location(title, location, summary, api_key):
    fallback_row = {"title": title, "location": location, "summary": summary}
    fallback = fallback_city(fallback_row)

    if not api_key or genai is None or types is None:
        return fallback

    prompt = (
        "请根据活动名称、地点和摘要判断该活动所在城市。"
        "只允许返回以下城市之一："
        f"{'、'.join(CITY_ORDER)}、其他。"
        "请只返回 JSON，格式为 {\"city\":\"城市名\"}。"
        f"\n活动名称：{title}\n地点：{location}\n摘要：{summary}"
    )

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0
            )
        )
        data = json.loads(response.text)
        city = str(data.get("city", "")).strip()
        return city if city in CITY_TO_REGION else fallback
    except Exception:
        return fallback


def city_sort_key(city):
    try:
        return CITY_ORDER.index(city)
    except ValueError:
        return len(CITY_ORDER)


def prepare_display_data(data):
    display_df = data.copy()
    display_df = display_df[display_df["date"].apply(is_current_or_future)]
    display_df["首页板块"] = display_df.apply(classify_section, axis=1)
    display_df = display_df[display_df["首页板块"].isin(DISPLAY_SECTIONS)]

    api_key = get_api_key()
    with st.spinner("正在判断城市并整理信息..."):
        display_df["城市"] = display_df.apply(
            lambda row: ai_city_from_location(
                clean_value(row.get("title"), ""),
                clean_value(row.get("location"), ""),
                clean_value(row.get("summary"), ""),
                api_key
            ),
            axis=1
        )

    display_df["地区"] = display_df["城市"].map(CITY_TO_REGION).fillna("其他")
    display_df = display_df[
        (
            (display_df["首页板块"] == "演出资讯")
            & display_df["地区"].isin(["浙江", "江苏", "上海"])
        )
        | (
            (display_df["首页板块"] == "学术讲座/研讨会")
            & display_df["地区"].isin(["浙江", "江苏", "上海", "北京"])
        )
    ]

    display_df["名称"] = display_df["title"].apply(lambda value: clean_value(value, "未命名信息"))
    display_df["时间"] = display_df["date"].apply(clean_value)
    display_df["地点"] = display_df["location"].apply(clean_value)
    display_df["城市排序"] = display_df["城市"].apply(city_sort_key)
    return display_df.sort_values(["首页板块", "城市排序", "地点", "时间", "名称"], na_position="last")


def load_data():
    conn = sqlite3.connect("academic_events.db")
    df = pd.read_sql_query("SELECT * FROM academic_events ORDER BY date DESC", conn)
    conn.close()
    return df


try:
    df = load_data()

    if df.empty:
        st.info("暂无数据。请先运行采集脚本，或检查 GitHub Actions 是否成功写入 academic_events.db。")
        st.stop()

    search_term = st.text_input("搜索关键词")
    if search_term:
        df = df[df.apply(lambda row: row.astype(str).str.contains(search_term, case=False).any(), axis=1)]

    display_df = prepare_display_data(df)

    if display_df.empty:
        st.write("当前没有符合地区与时间条件的信息。")
        st.stop()

    metric_cols = st.columns(2)
    for col, section in zip(metric_cols, DISPLAY_SECTIONS):
        col.metric(section, int((display_df["首页板块"] == section).sum()))

    st.divider()

    for section in DISPLAY_SECTIONS:
        section_df = display_df[display_df["首页板块"] == section]
        st.subheader(section)

        if section_df.empty:
            st.caption("暂无相关信息")
            continue

        section_cities = sorted(section_df["城市"].unique(), key=city_sort_key)
        for city in section_cities:
            city_df = section_df[section_df["城市"] == city][["名称", "时间", "地点"]]
            st.markdown(f"**{city}**")
            st.dataframe(
                city_df,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "名称": st.column_config.TextColumn("名称", width="large"),
                    "时间": st.column_config.TextColumn("时间", width="medium"),
                    "地点": st.column_config.TextColumn("地点", width="medium"),
                }
            )

except Exception as e:
    st.error(f"数据库读取失败，请检查文件是否存在: {e}")
