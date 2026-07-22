import streamlit as st
import pandas as pd
import re
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials

# ページ基本設定
st.set_page_config(page_title="コール分析＆収益管理ダッシュボード", layout="wide")

# --- 1. 定数設定 ---
HOURLY_WAGE = 2000              # 時給2,000円
MINUTE_WAGE = HOURLY_WAGE / 60  # 分単価
DOCUMENT_UNIT_PRICE = 4500      # 資料1件あたりの売上単価 (4,500円)

USER_PASSWORDS = st.secrets.get("passwords", {"admin": "admin123"})
ADMIN_PASSWORD = USER_PASSWORDS.get("admin", "admin123")

st.title("📞 コール分析＆収益管理ダッシュボード")

# --- 日付パース用の補助関数 ---
def parse_custom_date(date_str):
    """'7/22' などの文字列を 2025年9月〜2026年8月のルールで西暦補正する"""
    if not date_str:
        return None, "不明"
    
    # すでに YYYY/MM/DD の形式の場合
    parsed = pd.to_datetime(date_str, errors='coerce')
    if pd.notnull(parsed) and parsed.year > 2000:
        return parsed.strftime('%Y-%m-%d'), parsed.strftime('%Y-%m')
    
    # M/D または MM/DD の形式を解析
    match = re.search(r'(\d{1,2})/(\d{1,2})', str(date_str))
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        
        # 2025年9月以降〜現在(2026年)の運用に合わせた年判定
        # 9月〜12月は2025年、1月〜8月は2026年とみなす
        year = 2025 if month >= 9 else 2026
        
        try:
            dt = datetime(year, month, day)
            return dt.strftime('%Y-%m-%d'), dt.strftime('%Y-%m')
        except ValueError:
            return None, "不明"
            
    return None, "不明"

# --- 2. スプレッドシート読み込み＆前処理 ---
@st.cache_data(ttl=600, show_spinner="スプレッドシートから高速データ取得中...")
def load_and_process_all_data(spreadsheet_id):
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    sh = client.open_by_key(spreadsheet_id)
    worksheets = sh.worksheets()
    
    all_records = []
    
    call_pairs = [
        (2, 3, 4, 7),     # 1巡目: C, D, E, H
        (8, 9, 10, 13),   # 2巡目: I, J, K, N
        (14, 15, 16, 19), # 3巡目: O, P, Q, T
        (20, 21, 22, 25)  # 4巡目: U, V, W, Z
    ]
    
    circle_num_map = {'⑨': 9, '⑩': 10, '⑪': 11, '⑫': 12, '⑬': 13, '⑭': 14, '⑮': 15, '⑯': 16, '⑰': 17, '⑱': 18}
    result_keywords = ["NG", "ng", "Ng", "許諾", "不通", "留守", "着拒", "繋がらない", "折TEL", "折tel", "結果", "キャンセル"]

    for ws in worksheets:
        lp_name = ws.title
        raw_values = ws.get_all_values()
        
        if len(raw_values) <= 1:
            continue
            
        for row in raw_values[1:]:
            if not row or not any(row):
                continue
                
            current_lp = str(row[0]).strip() if len(row) > 0 and pd.notnull(row[0]) and str(row[0]).strip() != "" else lp_name
            
            for idx_call, (col_date, col_res, col_staff, col_note) in enumerate(call_pairs, 1):
                if len(row) <= max(col_date, col_res, col_staff, col_note):
                    continue
                
                date_val = str(row[col_date]).strip() if row[col_date] else ""
                res_val = str(row[col_res]).strip() if row[col_res] else ""
                staff_val = str(row[col_staff]).strip() if row[col_staff] else ""
                note_val = str(row[col_note]).strip() if row[col_note] else ""
                
                # --- A. 担当者名の抽出＆フィルタリング ---
                staff_name = ""
                if staff_val:
                    match_name = re.match(r'^([^\d①-⑳]+)', staff_val)
                    if match_name:
                        temp_name = match_name.group(1).replace('r', '').strip()
                        if not any(kw in temp_name for kw in result_keywords):
                            staff_name = temp_name
                    else:
                        if not any(kw in staff_val for kw in result_keywords):
                            staff_name = staff_val.replace('r', '').strip()

                # 必須判定: 日付・結果・担当者の3つが揃っていない場合は除外
                if not date_val or not res_val or res_val == "結果" or not staff_name:
                    continue

                # --- B. 時間帯 ---
                primary_hour = None
                for char in staff_val:
                    if char in circle_num_map:
                        primary_hour = circle_num_map[char]
                        break
                if primary_hour is None:
                    digits = re.findall(r'\d+', staff_val)
                    valid_digits = [int(d) for d in digits if 8 <= int(d) <= 20]
                    if valid_digits:
                        primary_hour = valid_digits[0]
                
                # --- C. 資料数の判定（💡 厳格化: 数字のみ かつ 1〜15の正数のみ） ---
                doc_count = 0
                clean_note = note_val.strip()
                if clean_note.isdigit():
                    val = int(clean_note)
                    if 1 <= val <= 15:
                        doc_count = val
                
                # --- D. フラグ判定 ---
                is_connected = 1 if any(kw in res_val for kw in ["許諾", "NG", "ng", "Ng", "再"]) else 0
                is_cv = 1 if "許諾" in res_val else 0
                
                # --- E. 日付補正 ---
                formatted_date, month_str = parse_custom_date(date_val)
                if not formatted_date:
                    continue
                
                all_records.append({
                    "年月": month_str,
                    "日付": formatted_date,
                    "LP": current_lp,
                    "巡目": f"{idx_call}巡目",
                    "結果": res_val,
                    "担当者": staff_name,
                    "時間帯": f"{primary_hour}時台" if primary_hour else "不明",
                    "通電フラグ": is_connected,
                    "CVフラグ": is_cv,
                    "資料数": doc_count
                })
                
    return pd.DataFrame(all_records)

# --- 集計テーブル作成ヘルパー関数 ---
def create_summary_table(df, group_col):
    if df.empty:
        return pd.DataFrame()
    
    summary = df.groupby(group_col).agg(
        架電数=("結果", "count"),
        通電数=("通電フラグ", "sum"),
        CV数=("CVフラグ", "sum"),
        獲得資料数=("資料数", "sum")
    ).reset_index()
    
    summary["通電率(%)"] = (summary["通電数"] / summary["架電数"] * 100).round(2)
    summary["通電CVR(%)"] = (summary["CV数"] / summary["通電数"] * 100).fillna(0).round(2)
    summary["架電CVR(%)"] = (summary["CV数"] / summary["架電数"] * 100).round(2)
    
    return summary

# --- 3. メイン処理 ---
spreadsheet_id = st.secrets.get("SPREADSHEET_ID", "")

if st.sidebar.button("🔄 データを最新に更新"):
    st.cache_data.clear()
    st.rerun()

if spreadsheet_id:
    try:
        df_all = load_and_process_all_data(spreadsheet_id)

        if not df_all.empty:
            # LPフィルター
            lp_list = ["全LP合計"] + sorted([str(x) for x in df_all["LP"].unique()])
            selected_lp = st.sidebar.selectbox("対象LP（タブ）を選択", lp_list)
            
            df_lp = df_all if selected_lp == "全LP合計" else df_all[df_all["LP"] == selected_lp]

            # 月フィルター（最新月を最上部に）
            available_months = sorted([m for m in df_lp["年月"].unique() if m != "不明"], reverse=True)
            month_options = available_months + ["全期間"] if available_months else ["全期間"]
            
            selected_month = st.sidebar.selectbox("対象月を選択", month_options, index=0)
            
            df_filtered = df_lp if selected_month == "全期間" else df_lp[df_lp["年月"] == selected_month]
        else:
            df_filtered = pd.DataFrame()

        all_staffs = sorted([s for s in df_filtered["担当者"].unique() if s != "不明"]) if not df_filtered.empty else []

        # --- 4. 画面表示 ---
        tab1, tab2, tab3 = st.tabs(["📊 全体パフォーマンス", "⏰ 時間帯・巡目別分析", "👤 個人レポート＆稼働時間"])

        # TAB 1: 全体集計
        with tab1:
            st.subheader(f"📌 集計対象: {selected_lp} ({selected_month})")
            
            total_calls = len(df_filtered)
            total_connects = df_filtered["通電フラグ"].sum() if not df_filtered.empty else 0
            total_cv = df_filtered["CVフラグ"].sum() if not df_filtered.empty else 0
            total_docs = df_filtered["資料数"].sum() if not df_filtered.empty else 0
            
            tsuuden_cvr = (total_cv / total_connects * 100) if total_connects > 0 else 0
            kaden_cvr = (total_cv / total_calls * 100) if total_calls > 0 else 0
            tsuuden_rate = (total_connects / total_calls * 100) if total_calls > 0 else 0

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("総架電数", f"{total_calls:,} 件")
            c2.metric("通電数 (通電率)", f"{total_connects:,} 件 ({tsuuden_rate:.1f}%)")
            c3.metric("CV(許諾)数", f"{total_cv:,} 件")
            c4.metric("通電CVR / 架電CVR", f"{tsuuden_cvr:.1f}% / {kaden_cvr:.1f}%")
            c5.metric("獲得資料数", f"{total_docs:,} 件")

            st.markdown("---")
            
            # 日別 / 月別の詳細集計テーブル
            st.subheader("📅 日別・月別 パフォーマンス集計表")
            if selected_month == "全期間":
                df_summary = create_summary_table(df_filtered, "年月")
                st.write("【月単位 集計表】")
            else:
                df_summary = create_summary_table(df_filtered, "日付")
                st.write(f"【{selected_month} 日単位 集計表】")
            
            st.dataframe(df_summary, use_container_width=True)

            # 🔒 管理者専用 エリア（売上・コスト ＆ 担当者別集計表）
            st.markdown("---")
            with st.expander("🔒 【管理者専用】収益確認 ＆ 担当者別集計表"):
                input_pass = st.text_input("管理者パスワードを入力してください", type="password", key="admin_pass")
                if input_pass == ADMIN_PASSWORD:
                    # コスト計算
                    total_cost = 0
                    if "work_minutes" in st.session_state:
                        total_cost = sum(mins * MINUTE_WAGE for staff, mins in st.session_state["work_minutes"].items() if staff.lower() != 'k')
                    
                    total_revenue = total_docs * DOCUMENT_UNIT_PRICE
                    profit = total_revenue - total_cost

                    m1, m2, m3 = st.columns(3)
                    m1.metric("確実な獲得売上 (資料数×4,500円)", f"¥{total_revenue:,}")
                    m2.metric("概算人件費 (kさん除外)", f"¥{int(total_cost):,}")
                    m3.metric("推定粗利益", f"¥{int(profit):,}")

                    st.markdown("---")
                    st.subheader("👥 担当者別 集計表（管理者閲覧）")
                    df_staff_summary = create_summary_table(df_filtered, "担当者")
                    st.dataframe(df_staff_summary, use_container_width=True)

                elif input_pass != "":
                    st.error("パスワードが正しくありません")

        # TAB 2: 時間帯・巡目別
        with tab2:
            st.subheader("🔄 巡目別 パフォーマンス分析")
            if not df_filtered.empty:
                df_junmu = create_summary_table(df_filtered, "巡目")
                st.dataframe(df_junmu, use_container_width=True)
                
                st.bar_chart(df_junmu.set_index("巡目")[["通電率(%)", "通電CVR(%)", "架電CVR(%)"]])

            st.markdown("---")
            st.subheader("⏰ 時間帯別 パフォーマンス分析")
            if not df_filtered.empty:
                df_hour = create_summary_table(df_filtered, "時間帯")
                st.dataframe(df_hour, use_container_width=True)

        # TAB 3: 個人レポート
        with tab3:
            st.subheader("👤 個人成績 ＆ 稼働時間入力")
            selected_staff = st.selectbox("担当者を選択してください", all_staffs)
            
            if selected_staff:
                st.info(f"🔒 **{selected_staff}** さんの詳細データを表示するにはパスワードが必要です。")
                input_user_pass = st.text_input(f"{selected_staff} さんのパスワードを入力", type="password", key=f"pass_{selected_staff}")
                
                correct_pass = USER_PASSWORDS.get(selected_staff, "")
                
                if input_user_pass != "" and (input_user_pass == correct_pass or input_user_pass == ADMIN_PASSWORD):
                    st.success("認証に成功しました！")
                    
                    # 稼働時間入力
                    if "work_minutes" not in st.session_state:
                        st.session_state["work_minutes"] = {}
                    
                    default_mins = st.session_state["work_minutes"].get(selected_staff, 0 if selected_staff.lower() == 'k' else 240)
                    user_mins = st.number_input(f"✍️ 本日の稼働時間（分）", min_value=0, value=default_mins, step=15, key=f"mins_{selected_staff}")
                    st.session_state["work_minutes"][selected_staff] = user_mins

                    # データ抽出
                    df_person = df_filtered[df_filtered["担当者"] == selected_staff]
                    
                    # 本日（当日分）の成績抽出
                    today_str = datetime.now().strftime('%Y-%m-%d')
                    df_today = df_person[df_person["日付"] == today_str]
                    
                    today_calls = len(df_today)
                    today_cv = df_today["CVフラグ"].sum() if not df_today.empty else 0
                    today_docs = df_today["資料数"].sum() if not df_today.empty else 0

                    st.markdown(f"### 📌 本日 ({today_str}) の成果")
                    p1, p2, p3 = st.columns(3)
                    p1.metric("本日 架電数", f"{today_calls} 件")
                    p2.metric("本日 CV(許諾)数", f"{today_cv} 件")
                    p3.metric("本日 獲得資料数", f"{today_docs} 件")

                    st.markdown("---")
                    st.markdown("#### 📋 Slack日報用テンプレート")
                    slack_text = f"""お疲れ様です！本日の架電報告です。

【担当】{selected_staff}
【稼働時間】{user_mins}分
【架電数】{today_calls}件
【CV(許諾)数】{today_cv}件
【獲得資料数】{today_docs}件

【所感】
（ここに本日の所感を記入）
"""
                    st.code(slack_text, language="markdown")

                elif input_user_pass != "":
                    st.error("パスワードが正しくありません。")

    except Exception as e:
        st.error(f"スプレッドシートの読み込み・処理に失敗しました: {e}")
