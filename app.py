import streamlit as st
import sqlite3
import pandas as pd

# 设置页面标题
st.set_page_config(page_title="学术活动动态", layout="wide")

st.title("🎓 学术与舞蹈活动追踪")
st.markdown("这里展示的是从公众号实时采集的学术活动信息。")

# 连接数据库
def load_data():
    conn = sqlite3.connect('academic_events.db')
    # 查询所有数据并按时间倒序排列
    df = pd.read_sql_query("SELECT * FROM academic_events ORDER BY date DESC", conn)
    conn.close()
    return df

# 读取数据
try:
    df = load_data()
    
    # 简单的搜索框
    search_term = st.text_input("搜索关键词：")
    if search_term:
        df = df[df.apply(lambda row: row.astype(str).str.contains(search_term, case=False).any(), axis=1)]

    # 使用 Streamlit 的列布局展示数据
    if not df.empty:
        for index, row in df.iterrows():
            with st.container(border=True):
                st.subheader(row['title'])
                st.caption(f"📅 时间: {row['date']} | 📍 地点: {row['location']}")
                st.write(row['summary'])
                st.link_button("查看原文", row['url'])
    else:
        st.write("暂无数据，请稍后再试。")

except Exception as e:
    st.error(f"数据库读取失败，请检查文件是否存在: {e}")
