import streamlit as st
import pandas as pd
import re
import gspread
from datetime import datetime, timezone, timedelta
from google.oauth2.service_account import Credentials

# ページ基本設定
st.set_page_config(page_title="社内コルセン　ダッシュボード", layout="wide")

# --- 1. 定数設定 ---
HOURLY_WAGE = 2000              # 時給2,000円
MINUTE_WAGE = HOURLY_WAGE / 60  # 分単価
DOCUMENT_UNIT_PRICE = 4500      # 資料1件あたりの売上単価 (4,500円)
JST = timezone(timedelta(hours=9)) # 日本時間(JST)の設定

USER_PASSWORDS = st.secrets.get("passwords", {"admin": "admin123"})
ADMIN_PASSWORD = USER_PASSWORDS.get("admin", "admin123")

# パスワード設定されているメンバー一覧（adminは除く）
REGISTERED_MEMBERS = [k for k in USER_PASSWORDS.keys() if k.lower() != "admin"]

st.title("📞 社内コルセンダッシュボード")

# --- 日付パース用の補助関数 ---
def parse_custom_date(date_str):
    if not date_str:
        return None, "不明"
    parsed = pd.to_datetime(date_str, errors='coerce')
    if pd.notnull(parsed) and parsed.year > 2000:
        return parsed.strftime('%Y-%m-%d'), parsed.strftime('%Y-%m')
    
    match = re.search(r'(\d{1,2})/(\d{1,2})', str(date_str))
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        year = 2025 if month >= 9 else 2026
        try:
            dt = datetime(year, month, day)
            return dt.strftime('%Y-%m-%d'), dt.strftime('%Y-%m')
        except ValueError:
            return None, "不明"
    return None, "不明"

# --- GSpread認証クライアント取得関数 ---
def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

# --- 2. スプレッドシート読み込み＆前処理 ---
@st.cache_data(ttl=300, show_spinner="スプレッドシートから高速データ取得中...")
def load_and_process_all_data(spreadsheet_id):
    client = get_gspread_client()
    sh = client.open_by_key(spreadsheet_id)
    worksheets = sh.worksheets()
    
    all_records = []
    call_pairs = [(2, 3, 4, 7), (8, 9, 10, 13), (14, 15, 16, 19), (20, 21, 22, 25)]
    circle_num_map = {'⑨': 9, '⑩': 10, '⑪': 11, '⑫': 12, '⑬': 13, '⑭': 14, '⑮': 15, '⑯': 16, '⑰': 17, '⑱': 18}
    result_keywords = ["NG", "ng", "Ng", "許諾", "不通", "留守", "着拒", "繋がらない", "折TEL", "折tel", "結果", "キャンセル"]

    for ws in worksheets:
        lp_name = ws.title
        if lp_name == "稼働時間":
            continue
            
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

                if not date_val or not res_val or res_val == "結果" or not staff_name:
                    continue

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
                
                doc_count = 0
                clean_note = note_val.strip()
                if clean_note.isdigit():
                    val = int(clean_note)
                    if 1 <= val <= 15:
                        doc_count = val
                
                is_connected = 1 if any(kw in res_val for kw in ["許諾", "NG", "ng", "Ng", "再"]) else 0
                is_cv = 1 if "許諾" in res_val else 0
                
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

# --- 3. 稼働時間シートの読み込み・書き込み関数 ---
@st.cache_data(ttl=60, show_spinner="稼働時間データを読み込み中...")
def load_work_hours(spreadsheet_id):
    try:
        client = get_gspread_client()
        sh = client.open_by_key(spreadsheet_id)
        ws = sh.worksheet("稼働時間")
        records = ws.get_all_records()
        df_wh = pd.DataFrame(records)
        if not df_wh.empty and "日付" in df_wh.columns and "担当者" in df_wh.columns and "稼働時間" in df_wh.columns:
            df_wh["日付"] = df_wh["日付"].astype(str)
            df_wh["担当者"] = df_wh["担当者"].astype(str)
            df_wh["稼働時間"] = pd.to_numeric(df_wh["稼働時間"], errors='coerce').fillna(0).astype(int)
            return df_wh
    except Exception as e:
        pass
    return pd.DataFrame(columns=["日付", "担当者", "稼働時間"])

def save_work_hour(spreadsheet_id, date_str, staff_name, mins):
    client = get_gspread_client()
    sh = client.open_by_key(spreadsheet_id)
    ws = sh.worksheet("稼働時間")
    
    records = ws.get_all_values()
    if not records:
        ws.append_row(["日付", "担当者", "稼働時間"])
        records = [["日付", "担当者", "稼働時間"]]
        
    row_to_update = None
    for idx, row in enumerate(records[1:], start=2):
        if len(row) >= 2 and row[0] == date_str and row[1] == staff_name:
            row_to_update = idx
            break
            
    if row_to_update:
        ws.update_cell(row_to_update, 3, mins)
    else:
        ws.append_row([date_str, staff_name, mins])
        
    st.cache_data.clear()

# --- 集計テーブル作成ヘルパー関数 ---
def create_summary_table(df, group_col, raw_mode=False):
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
    
    if raw_mode:
        return summary

    formatted = summary.copy()
    formatted["架電数"] = formatted["架電数"].apply(lambda x: f"{x:,}件")
    formatted["通電数"] = formatted["通電数"].apply(lambda x: f"{x:,}件")
    formatted["CV数"] = formatted["CV数"].apply(lambda x: f"{x:,}件")
    formatted["獲得資料数"] = formatted["獲得資料数"].apply(lambda x: f"{x:,}件")
    
    formatted["通電率"] = formatted["通電率(%)"].apply(lambda x: f"{x:.2f}%")
    formatted["通電CVR"] = formatted["通電CVR(%)"].apply(lambda x: f"{x:.2f}%")
    formatted["架電CVR"] = formatted["架電CVR(%)"].apply(lambda x: f"{x:.2f}%")
    
    formatted = formatted.drop(columns=["通電率(%)", "通電CVR(%)", "架電CVR(%)"])
    cols = [group_col, "架電数", "通電数", "通電率", "CV数", "通電CVR", "架電CVR", "獲得資料数"]
    
    return formatted[[c for c in cols if c in formatted.columns]]

# --- 4. メイン処理 ---
spreadsheet_id = st.secrets.get("SPREADSHEET_ID", "")

st.sidebar.title("⚙️ 設定")
if st.sidebar.button("🔄 データを最新に更新"):
    st.cache_data.clear()
    st.rerun()

if spreadsheet_id:
    try:
        df_all = load_and_process_all_data(spreadsheet_id)
        df_work_hours = load_work_hours(spreadsheet_id)

        if not df_all.empty:
            available_months = sorted([m for m in df_all["年月"].unique() if m != "不明"], reverse=True)
            lp_list = ["全LP合計"] + sorted([str(x) for x in df_all["LP"].unique()])
            
            recent_2_months = available_months[:2] if len(available_months) >= 2 else available_months
            df_recent = df_all[df_all["年月"].isin(recent_2_months)]
            recent_active_staffs = df_recent["担当者"].unique()
            
            if REGISTERED_MEMBERS:
                all_staffs = sorted([s for s in REGISTERED_MEMBERS if s in recent_active_staffs])
            else:
                all_staffs = sorted([s for s in recent_active_staffs if s != "不明"])
        else:
            available_months, lp_list, all_staffs = [], ["全LP合計"], []

        # --- 5. 画面表示 ---
        tab1, tab2, tab3 = st.tabs(["📊 全体パフォーマンス", "📈 巡目・時間帯別分析", "👤 個人レポート＆日報"])

        # ==========================================
        # TAB 1: 全体パフォーマンス
        # ==========================================
        with tab1:
            st.subheader("🔍 全体集計フィルター")
            f_col1, f_col2 = st.columns(2)
            with f_col1:
                sel_month = st.selectbox("📅 対象月を選択", available_months + ["全期間"], index=0, key="t1_month")
            with f_col2:
                sel_lp = st.selectbox("📄 対象LPを選択", lp_list, index=0, key="t1_lp")

            df_t1 = df_all.copy()
            if sel_month != "全期間":
                df_t1 = df_t1[df_t1["年月"] == sel_month]
            if sel_lp != "全LP合計":
                df_t1 = df_t1[df_t1["LP"] == sel_lp]

            total_calls = len(df_t1)
            total_connects = df_t1["通電フラグ"].sum() if not df_t1.empty else 0
            total_cv = df_t1["CVフラグ"].sum() if not df_t1.empty else 0
            total_docs = df_t1["資料数"].sum() if not df_t1.empty else 0
            
            tsuuden_cvr = (total_cv / total_connects * 100) if total_connects > 0 else 0
            kaden_cvr = (total_cv / total_calls * 100) if total_calls > 0 else 0
            tsuuden_rate = (total_connects / total_calls * 100) if total_calls > 0 else 0

            st.markdown("---")
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("総架電数", f"{total_calls:,}件")
            c2.metric("通電数 (通電率)", f"{total_connects:,}件 ({tsuuden_rate:.2f}%)")
            c3.metric("CV(許諾)数", f"{total_cv:,}件")
            c4.metric("通電CVR / 架電CVR", f"{tsuuden_cvr:.2f}% / {kaden_cvr:.2f}%")
            c5.metric("獲得資料数", f"{total_docs:,}件")

            st.markdown("---")
            st.subheader("📅 日別・月別 パフォーマンス集計表")
            df_summary = create_summary_table(df_t1, "年月" if sel_month == "全期間" else "日付")
            st.dataframe(df_summary, use_container_width=True)

            st.markdown("---")
            st.subheader(f"🔄 【{sel_lp}】 巡目別パフォーマンス集計表")
            df_lp_junmu = create_summary_table(df_t1, "巡目")
            st.dataframe(df_lp_junmu, use_container_width=True)

            # 🔒 管理者専用エリア
            st.markdown("---")
            with st.expander("🔒 【管理者専用】収益確認 ＆ 担当者別集計表"):
                input_pass = st.text_input("管理者パスワードを入力してください", type="password", key="admin_pass")
                if input_pass == ADMIN_PASSWORD:
                    if not df_work_hours.empty:
                        # 全員の総稼働時間（kさん含む）
                        total_mins_all = int(df_work_hours["稼働時間"].sum())
                        
                        # 人件費計算用の総稼働時間（kさん除外）
                        df_wh_cost_target = df_work_hours[df_work_hours["担当者"].str.lower() != 'k']
                        total_mins_cost_target = int(df_wh_cost_target["稼働時間"].sum())
                    else:
                        total_mins_all = 0
                        total_mins_cost_target = 0
                        
                    total_hours_all = round(total_mins_all / 60, 2)
                    total_cost = total_mins_cost_target * MINUTE_WAGE
                    
                    total_revenue = total_docs * DOCUMENT_UNIT_PRICE
                    profit = total_revenue - total_cost

                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("確定売上 (資料数×4,500円)", f"¥{total_revenue:,}")
                    m2.metric("総稼働時間 (全員分)", f"{total_mins_all:,}分 ({total_hours_all}時間)")
                    m3.metric("概算人件費 (kさん除外)", f"¥{int(total_cost):,}")
                    m4.metric("推定粗利益", f"¥{int(profit):,}")

                    st.markdown("---")
                    st.subheader("👥 担当者別 集計表")
                    df_staff_summary = create_summary_table(df_t1, "担当者")
                    st.dataframe(df_staff_summary, use_container_width=True)
                elif input_pass != "":
                    st.error("パスワードが正しくありません")

        # ==========================================
        # TAB 2: 巡目・時間帯別分析
        # ==========================================
        with tab2:
            st.subheader("🔍 分析フィルター")
            f2_col1, f2_col2 = st.columns(2)
            with f2_col1:
                sel_month_t2 = st.selectbox("📅 対象月を選択", available_months + ["全期間"], index=0, key="t2_month")
            with f2_col2:
                sel_lp_t2 = st.selectbox("📄 対象LPを選択", lp_list, index=0, key="t2_lp")

            df_t2 = df_all.copy()
            if sel_month_t2 != "全期間":
                df_t2 = df_t2[df_t2["年月"] == sel_month_t2]
            if sel_lp_t2 != "全LP合計":
                df_t2 = df_t2[df_t2["LP"] == sel_lp_t2]

            st.subheader(f"🔄 【{sel_lp_t2}】 巡目別パフォーマンス（折れ線グラフ）")
            if not df_t2.empty:
                df_junmu_raw = create_summary_table(df_t2, "巡目", raw_mode=True)
                df_junmu_fmt = create_summary_table(df_t2, "巡目", raw_mode=False)
                st.dataframe(df_junmu_fmt, use_container_width=True)
                
                chart_data = df_junmu_raw.set_index("巡目")[["通電率(%)", "通電CVR(%)", "架電CVR(%)"]]
                st.line_chart(chart_data)

            st.markdown("---")
            st.subheader(f"⏰ 【{sel_lp_t2}】 時間帯別パフォーマンス")
            if not df_t2.empty:
                df_hour_fmt = create_summary_table(df_t2, "時間帯", raw_mode=False)
                st.dataframe(df_hour_fmt, use_container_width=True)

        # ==========================================
        # TAB 3: 個人レポート ＆ 稼働時間入力
        # ==========================================
        with tab3:
            st.subheader("👤 個人成績 ＆ 本日の日報提出")
            
            if all_staffs:
                selected_staff = st.selectbox("担当者を選択してください", all_staffs)
            else:
                selected_staff = None
                st.warning("対象となるアクティブな担当者が見つかりません。")

            if selected_staff:
                st.info(f"🔒 **{selected_staff}** さんのパスワードを入力してください。")
                input_user_pass = st.text_input(f"{selected_staff} さんのパスワード", type="password", key=f"pass_{selected_staff}")
                
                correct_pass = USER_PASSWORDS.get(selected_staff, "")
                
                if input_user_pass != "" and (input_user_pass == correct_pass or input_user_pass == ADMIN_PASSWORD):
                    st.success("認証されました！")
                    
                    # --- A. 稼働時間入力 ＆ 確定登録 (JST対応) ---
                    st.markdown("---")
                    st.markdown("#### ✍️ 本日の稼働時間 提出")
                    
                    today_str = datetime.now(JST).strftime('%Y-%m-%d')
                    
                    current_mins = 0
                    if not df_work_hours.empty:
                        match_row = df_work_hours[(df_work_hours["日付"] == today_str) & (df_work_hours["担当者"] == selected_staff)]
                        if not match_row.empty:
                            current_mins = int(match_row.iloc[0]["稼働時間"])
                    
                    c_work1, c_work2 = st.columns([2, 1])
                    with c_work1:
                        input_mins = st.number_input("本日の稼働時間（分）を入力してください", min_value=0, max_value=600, value=current_mins, step=15)
                    with c_work2:
                        st.write("")
                        st.write("")
                        if st.button("✅ 稼働時間を確定・提出する", key=f"btn_confirm_{selected_staff}"):
                            try:
                                save_work_hour(spreadsheet_id, today_str, selected_staff, input_mins)
                                st.success(f"スプレッドシートに保存完了！{selected_staff} さんの本日({today_str})の稼働時間（{input_mins}分）を提出しました。")
                                st.rerun()
                            except Exception as save_err:
                                st.error(f"スプレッドシートへの保存に失敗しました: {save_err}")

                    # --- B. 当日（本日）の全LP合計 成績表示 ---
                    df_person_today = df_all[(df_all["担当者"] == selected_staff) & (df_all["日付"] == today_str)]
                    
                    today_cv = df_person_today["CVフラグ"].sum() if not df_person_today.empty else 0
                    today_docs = df_person_today["資料数"].sum() if not df_person_today.empty else 0

                    st.markdown("---")
                    st.markdown(f"### 📌 本日 ({today_str}) の全LP合計成果")
                    p1, p2 = st.columns(2)
                    p1.metric("本日 CV(許諾)数", f"{today_cv}件")
                    p2.metric("本日 獲得資料請求数", f"{today_docs}件")

                    # --- C. 日報用テンプレート ---
                    st.markdown("---")
                    st.markdown("#### 📋 Slack報告用メッセージ")
                    slack_text = f"""お疲れ様です。本日の架電業務終了いたします。
結果：{today_cv}CV、{today_docs}資料請求

（所感）"""
                    st.code(slack_text, language="markdown")
                    st.caption("💡 右上のアイコンでテキストをコピーし、自分のSlackにペーストして投稿してください。")

                    # --- D. 個人用：日別パフォーマンス表 ---
                    st.markdown("---")
                    st.subheader(f"📅 {selected_staff} さんの日別パフォーマンス表")
                    
                    p_sel_month = st.selectbox("📅 対象月を選択", available_months + ["全期間"], index=0, key="p_month")
                    
                    df_person = df_all[df_all["担当者"] == selected_staff]
                    if p_sel_month != "全期間":
                        df_person = df_person[df_person["年月"] == p_sel_month]

                    if not df_person.empty:
                        df_p_daily = create_summary_table(df_person, "日付")
                        
                        mins_list = []
                        for _, row in df_p_daily.iterrows():
                            d_str = str(row["日付"])
                            if not df_work_hours.empty:
                                m_row = df_work_hours[(df_work_hours["日付"] == d_str) & (df_work_hours["担当者"] == selected_staff)]
                                if not m_row.empty:
                                    m_val = m_row.iloc[0]["稼働時間"]
                                    mins_list.append(f"{m_val}分")
                                else:
                                    mins_list.append("-")
                            else:
                                mins_list.append("-")
                        
                        df_p_daily.insert(0, "稼働時間", mins_list)
                        st.dataframe(df_p_daily, use_container_width=True)

                    st.markdown("---")
                    st.subheader(f"📊 {selected_staff} さんのLP別・巡目別集計")
                    p_sel_lp = st.selectbox("📄 対象LPを選択", lp_list, index=0, key="p_lp")

                    df_person_lp = df_person if p_sel_lp == "全LP合計" else df_person[df_person["LP"] == p_sel_lp]

                    if not df_person_lp.empty:
                        df_p_junmu = create_summary_table(df_person_lp, "巡目")
                        st.dataframe(df_p_junmu, use_container_width=True)
                    else:
                        st.info("該当するデータの組み合わせはありません。")

                elif input_user_pass != "":
                    st.error("パスワードが正しくありません。")

    except Exception as e:
        st.error(f"スプレッドシートの読み込み・処理に失敗しました: {e}")
