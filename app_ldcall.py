import streamlit as st
import pandas as pd
import re
import gspread
import calendar
from datetime import datetime, date, timezone, timedelta
from google.oauth2.service_account import Credentials

# ページ基本設定
st.set_page_config(page_title="社内コルセンダッシュボード", layout="wide")

# --- 1. 定数設定 ---
HOURLY_WAGE = 2000              # 時給2,000円
MINUTE_WAGE = HOURLY_WAGE / 60  # 分単価
JST = timezone(timedelta(hours=9)) # 日本時間(JST)の設定

# 承認率90%・オープンハウス20%混ざりを考慮した実効想定単価
UNIT_PRICE_SINGLE_TL_EST = 4050  # (5,000*0.8 + 2,500*0.2) * 0.9 = 4,050円
UNIT_PRICE_GODOU_EST = 4410      # (5,500*0.8 + 2,500*0.2) * 0.9 = 4,410円

USER_PASSWORDS = st.secrets.get("passwords", {"admin": "admin123"})
ADMIN_PASSWORD = USER_PASSWORDS.get("admin", "admin123")

# パスワード設定されているメンバー一覧（adminは除く）
REGISTERED_MEMBERS = [k for k in USER_PASSWORDS.keys() if k.lower() != "admin"]

st.title("📞 社内コルセンダッシュボード")

# --- 営業日数計算用の補助関数 ---
def get_business_days_info(year, month, target_cutoff_date, adjust_days=0):
    _, last_day = calendar.monthrange(year, month)
    
    def is_holiday(d):
        if d.weekday() >= 5: # 土日
            return True
        m, day = d.month, d.day
        fixed_holidays = [
            (1,1), (1,2), (1,3), (2,11), (2,23), (4,29), (5,3), (5,4), (5,5),
            (8,11), (11,3), (11,23), (12,23), (12,30), (12,31)
        ]
        if (m, day) in fixed_holidays:
            return True
        if m == 1 and d.weekday() == 0 and 8 <= day <= 14: return True
        if m == 7 and d.weekday() == 0 and 15 <= day <= 21: return True
        if m == 9 and d.weekday() == 0 and 15 <= day <= 21: return True
        if m == 10 and d.weekday() == 0 and 8 <= day <= 14: return True
        return False

    total_b_days = 0
    passed_b_days = 0

    for day_num in range(1, last_day + 1):
        cur_d = date(year, month, day_num)
        if not is_holiday(cur_d):
            total_b_days += 1
            if cur_d <= target_cutoff_date:
                passed_b_days += 1

    final_total_b_days = max(1, total_b_days + adjust_days)
    final_passed_b_days = max(1, min(passed_b_days, final_total_b_days))
    
    return final_passed_b_days, final_total_b_days

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

# --- LP分類＆想定売上計算ヘルパー関数（承認率90%・OH20%考慮） ---
def classify_lp_and_calc_revenue(df_month):
    counts = {
        "単独LP": 0,
        "合同LP3": 0,
        "合同LP4": 0,
        "不動産合同": 0,
        "TLデッド": 0,
        "その他": 0
    }
    
    if df_month.empty:
        return counts, 0, 0

    for _, row in df_month.iterrows():
        lp = str(row.get("LP", "")).strip()
        docs = int(row.get("資料数", 0))
        
        if "TLデッド" in lp or "TL" in lp:
            counts["TLデッド"] += docs
        elif "合同3" in lp or ("3" in lp and "合同" in lp):
            counts["合同LP3"] += docs
        elif "合同4" in lp or ("4" in lp and "合同" in lp):
            counts["合同LP4"] += docs
        elif "不動産合同" in lp or "不動産" in lp:
            counts["不動産合同"] += docs
        elif "合同" in lp:
            counts["合同LP3"] += docs
        elif "単独" in lp:
            counts["単独LP"] += docs
        else:
            counts["単独LP"] += docs

    total_docs = sum(counts.values())
    
    # 想定売上計算
    # 単独/TLデッド: 送客1件あたり 4,050円 ((5000*0.8 + 2500*0.2) * 0.9)
    # 合同/不動産合同: 送客1件あたり 4,410円 ((5500*0.8 + 2500*0.2) * 0.9)
    est_rev = int(
        (counts["単独LP"] + counts["TLデッド"]) * UNIT_PRICE_SINGLE_TL_EST +
        (counts["合同LP3"] + counts["合同LP4"] + counts["不動産合同"] + counts["その他"]) * UNIT_PRICE_GODOU_EST
    )
    
    return counts, total_docs, est_rev

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
        if lp_name in ["稼働時間", "システム費用", "月次収益記録"]:
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

                if staff_name.lower() == 'k':
                    staff_name = "黒川"

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

# --- 3. データ入出力関数群 ---
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
            df_wh["担当者"] = df_wh["担当者"].apply(lambda x: "黒川" if str(x).lower() == 'k' else x)
            df_wh["稼働時間"] = pd.to_numeric(df_wh["稼働時間"], errors='coerce').fillna(0).astype(int)
            return df_wh
    except Exception:
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
        if len(row) >= 2 and row[0] == date_str and (row[1] == staff_name or (staff_name == "黒川" and row[1].lower() == "k")):
            row_to_update = idx
            break
            
    if row_to_update:
        ws.update_cell(row_to_update, 3, mins)
    else:
        ws.append_row([date_str, staff_name, mins])
        
    st.cache_data.clear()

@st.cache_data(ttl=60, show_spinner="システム費用データを読み込み中...")
def load_system_costs(spreadsheet_id):
    try:
        client = get_gspread_client()
        sh = client.open_by_key(spreadsheet_id)
        ws = sh.worksheet("システム費用")
        records = ws.get_all_records()
        df_sc = pd.DataFrame(records)
        if not df_sc.empty and "年月" in df_sc.columns and "システム費用" in df_sc.columns:
            df_sc["年月"] = df_sc["年月"].astype(str)
            df_sc["システム費用"] = pd.to_numeric(df_sc["システム費用"], errors='coerce').fillna(0).astype(int)
            return df_sc
    except Exception:
        pass
    return pd.DataFrame(columns=["年月", "システム費用"])

def save_system_cost(spreadsheet_id, month_str, cost_val):
    client = get_gspread_client()
    sh = client.open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet("システム費用")
    except Exception:
        ws = sh.add_worksheet(title="システム費用", rows="100", cols="2")
        ws.append_row(["年月", "システム費用"])
        
    records = ws.get_all_values()
    if not records:
        ws.append_row(["年月", "システム費用"])
        records = [["年月", "システム費用"]]
        
    row_to_update = None
    for idx, row in enumerate(records[1:], start=2):
        if len(row) >= 1 and row[0] == month_str:
            row_to_update = idx
            break
            
    if row_to_update:
        ws.update_cell(row_to_update, 2, cost_val)
    else:
        ws.append_row([month_str, cost_val])
        
    st.cache_data.clear()

@st.cache_data(ttl=60, show_spinner="月次収益記録を読み込み中...")
def load_monthly_revenue_records(spreadsheet_id):
    cols = [
        "年月", "送客_単独LP", "送客_合同LP3", "送客_合同LP4", "送客_不動産合同", "送客_TLデッド", "送客_合計", "想定売上",
        "承認_単独TL", "承認_合同", "承認_オープンハウス", "承認_合計", "確定売上", "更新日時"
    ]
    try:
        client = get_gspread_client()
        sh = client.open_by_key(spreadsheet_id)
        try:
            ws = sh.worksheet("月次収益記録")
        except Exception:
            ws = sh.add_worksheet(title="月次収益記録", rows="500", cols=len(cols))
            ws.append_row(cols)
            return pd.DataFrame(columns=cols)

        records = ws.get_all_records()
        df_rec = pd.DataFrame(records)
        if not df_rec.empty:
            df_rec["年月"] = df_rec["年月"].astype(str)
            return df_rec
    except Exception:
        pass
    return pd.DataFrame(columns=cols)

def save_monthly_revenue_detailed(spreadsheet_id, month_str, send_dict, est_rev, app_single, app_godou, app_oh, final_rev):
    cols = [
        "年月", "送客_単独LP", "送客_合同LP3", "送客_合同LP4", "送客_不動産合同", "送客_TLデッド", "送客_合計", "想定売上",
        "承認_単独TL", "承認_合同", "承認_オープンハウス", "承認_合計", "確定売上", "更新日時"
    ]
    client = get_gspread_client()
    sh = client.open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet("月次収益記録")
    except Exception:
        ws = sh.add_worksheet(title="月次収益記録", rows="500", cols=len(cols))
        ws.append_row(cols)

    existing_values = ws.get_all_values()
    if not existing_values:
        ws.append_row(cols)
        existing_values = [cols]

    send_total = sum(send_dict.values())
    app_total = app_single + app_godou + app_oh
    now_time_str = datetime.now(JST).strftime('%Y-%m-%d %H:%M')

    row_data = [
        month_str,
        send_dict.get("単独LP", 0), send_dict.get("合同LP3", 0), send_dict.get("合同LP4", 0),
        send_dict.get("不動産合同", 0), send_dict.get("TLデッド", 0), send_total, est_rev,
        app_single, app_godou, app_oh, app_total, final_rev, now_time_str
    ]

    row_to_update = None
    for idx, row in enumerate(existing_values[1:], start=2):
        if len(row) >= 1 and row[0] == month_str:
            row_to_update = idx
            break

    if row_to_update:
        ws.update(f"A{row_to_update}:N{row_to_update}", [row_data])
    else:
        ws.append_row(row_data)

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
        df_system_costs = load_system_costs(spreadsheet_id)
        df_revenue_records = load_monthly_revenue_records(spreadsheet_id)

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
        tab1, tab2, tab3, tab4 = st.tabs(["📊 全体パフォーマンス", "📈 巡目・時間帯別分析", "👤 個人レポート＆日報", "💰 売上管理"])

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

            st.markdown("---")
            st.subheader("👥 担当者別 集計表")
            df_staff_summary = create_summary_table(df_t1, "担当者")
            st.dataframe(df_staff_summary, use_container_width=True)

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
                
                correct_pass = USER_PASSWORDS.get(selected_staff, USER_PASSWORDS.get("k", ""))
                
                if input_user_pass != "" and (input_user_pass == correct_pass or input_user_pass == ADMIN_PASSWORD):
                    st.success("認証されました！")
                    
                    # --- A. 稼働時間入力 ＆ 確定登録 ---
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

        # ==========================================
        # TAB 4: 💰 売上管理 (管理者専用タブ)
        # ==========================================
        with tab4:
            st.subheader("🔒 管理者用 収益確認・着地予想 ＆ 月次承認売上管理")
            input_pass = st.text_input("管理者パスワードを入力してください", type="password", key="admin_tab4_pass")
            
            if input_pass == ADMIN_PASSWORD:
                target_month_for_cost = st.selectbox("📅 管理・記入対象の月を選択", available_months if available_months else ["2026-07"], index=0, key="tab4_target_month")
                
                # --- A. コスト・営業日数設定 ---
                current_sys_cost = 0
                if not df_system_costs.empty:
                    m_cost = df_system_costs[df_system_costs["年月"] == target_month_for_cost]
                    if not m_cost.empty:
                        current_sys_cost = int(m_cost.iloc[0]["システム費用"])
                        
                st.markdown("---")
                st.markdown(f"#### ⚙️ 【{target_month_for_cost}】 コスト・営業日数 設定")
                sc_col1, sc_col2 = st.columns(2)
                with sc_col1:
                    input_sys_cost = st.number_input(f"【{target_month_for_cost}】のシステム費用（円）を入力", min_value=0, value=current_sys_cost, step=1000)
                    if st.button("💾 システム費用を保存", key="btn_save_sys_cost_t4"):
                        try:
                            save_system_cost(spreadsheet_id, target_month_for_cost, input_sys_cost)
                            st.success(f"{target_month_for_cost}のシステム費用（¥{input_sys_cost:,}）を保存しました！")
                            st.rerun()
                        except Exception as sys_err:
                            st.error(f"保存に失敗しました: {sys_err}")

                with sc_col2:
                    adjust_b_days = st.number_input("営業日数 補正（日）※特別休業などはマイナス指定", value=0, step=1, key="tab4_adj_b_days")

                # --- B. 着地予想 ＆ 当月収益サマリー ---
                now_jst = datetime.now(JST)
                try:
                    y_val, m_val = map(int, target_month_for_cost.split('-'))
                except Exception:
                    y_val, m_val = now_jst.year, now_jst.month

                is_past_month = (y_val < now_jst.year) or (y_val == now_jst.year and m_val < now_jst.month)

                if is_past_month:
                    _, last_d = calendar.monthrange(y_val, m_val)
                    cutoff_date = date(y_val, m_val, last_d)
                else:
                    if now_jst.hour < 16:
                        cutoff_date = (now_jst - timedelta(days=1)).date()
                    else:
                        cutoff_date = now_jst.date()

                passed_days, total_b_days = get_business_days_info(y_val, m_val, cutoff_date, adjust_b_days)
                cutoff_date_str = cutoff_date.strftime('%Y-%m-%d')

                df_month_all = df_all[df_all["年月"] == target_month_for_cost]
                
                # LP別自動分類と想定売上算出（承認率90%・OH20%考慮済みの想定売上）
                send_counts, total_send_docs, est_total_revenue = classify_lp_and_calc_revenue(df_month_all)

                df_proj_t1 = df_month_all[df_month_all["日付"] <= cutoff_date_str] if not is_past_month else df_month_all
                _, _, proj_revenue = classify_lp_and_calc_revenue(df_proj_t1)

                if not df_work_hours.empty:
                    df_wh_m = df_work_hours[df_work_hours["日付"].str.startswith(target_month_for_cost)]
                    df_wh_cost_all = df_wh_m[~df_wh_m["担当者"].isin(['黒川', 'k', 'K'])]
                    total_labor_cost = int(df_wh_cost_all["稼働時間"].sum()) * MINUTE_WAGE

                    df_wh_filtered = df_wh_m if is_past_month else df_wh_m[df_wh_m["日付"] <= cutoff_date_str]
                    df_wh_cost_target = df_wh_filtered[~df_wh_filtered["担当者"].isin(['黒川', 'k', 'K'])]
                    proj_labor_cost = int(df_wh_cost_target["稼働時間"].sum()) * MINUTE_WAGE
                else:
                    total_labor_cost = 0
                    proj_labor_cost = 0

                daily_avg_revenue = proj_revenue / passed_days
                daily_avg_labor_cost = proj_labor_cost / passed_days

                projected_revenue = int(daily_avg_revenue * total_b_days)
                projected_labor_cost = int(daily_avg_labor_cost * total_b_days)
                projected_profit = projected_revenue - (projected_labor_cost + current_sys_cost)

                current_profit = est_total_revenue - (total_labor_cost + current_sys_cost)

                st.markdown("---")
                st.markdown(f"#### 📈 収益サマリー ＆ 月末着地予想（{target_month_for_cost}）")
                st.caption(f"⏰ 基準日: **{cutoff_date_str}** 時点（16:00更新）｜ 営業日数: **{passed_days} 日** / **{total_b_days} 日**（日別平均想定売上: **¥{int(daily_avg_revenue):,}**）")
                st.caption("※想定売上は「承認率90%・オープンハウス構成比20%（単独4,050円/合同4,410円）」をあらかじめ加味した硬めの推計値です。")

                m1, m2, m3, m4 = st.columns(4)
                m1.metric("想定売上 (送客×90%承認単価)", f"¥{est_total_revenue:,}", delta=f"着地予想: ¥{projected_revenue:,}")
                m2.metric("概算人件費 (黒川さん除外)", f"¥{int(total_labor_cost):,}", delta=f"着地予想: ¥{projected_labor_cost:,}", delta_color="inverse")
                m3.metric("システム費用", f"¥{current_sys_cost:,}")
                m4.metric("概算粗利益 (想定)", f"¥{int(current_profit):,}", delta=f"着地予想: ¥{projected_profit:,}")

                # --- C. 承認後件数・確定売上 入力フォーム ---
                st.markdown("---")
                st.markdown(f"#### 📝 承認後件数・確定売上 入力（対象月: {target_month_for_cost}）")
                
                st.info(
                    f"📊 **【{target_month_for_cost}】 送客数自動集計結果** ： "
                    f"単独LP: **{send_counts['単独LP']}**件 ｜ 合同LP3: **{send_counts['合同LP3']}**件 ｜ 合同LP4: **{send_counts['合同LP4']}**件 ｜ "
                    f"不動産合同: **{send_counts['不動産合同']}**件 ｜ TLデッド: **{send_counts['TLデッド']}**件 "
                    f"👉 **合計: {total_send_docs}件** （承認率90%考慮の想定売上: **¥{est_total_revenue:,}**）"
                )

                # 保存済みデータを初期値として呼び出し（承認数は送客数×0.9をデフォルト設定）
                init_app_single = int((send_counts['単独LP'] + send_counts['TLデッド']) * 0.9)
                init_app_godou = int((send_counts['合同LP3'] + send_counts['合同LP4'] + send_counts['不動産合同']) * 0.9)
                init_app_oh = 0
                init_final_rev = est_total_revenue

                if not df_revenue_records.empty:
                    ex_row = df_revenue_records[df_revenue_records["年月"] == target_month_for_cost]
                    if not ex_row.empty:
                        init_app_single = int(ex_row.iloc[0].get("承認_単独TL", init_app_single))
                        init_app_godou = int(ex_row.iloc[0].get("承認_合同", init_app_godou))
                        init_app_oh = int(ex_row.iloc[0].get("承認_オープンハウス", 0))
                        init_final_rev = int(ex_row.iloc[0].get("確定売上", est_total_revenue))

                st.write("##### 1. 承認後件数の入力")
                c_app1, c_app2, c_app3 = st.columns(3)
                with c_app1:
                    in_app_single = st.number_input("承認件数：単独LP・TLデッド (5,000円/件)", min_value=0, value=init_app_single, key="in_app_single")
                with c_app2:
                    in_app_godou = st.number_input("承認件数：合同LP (5,500円/件)", min_value=0, value=init_app_godou, key="in_app_godou")
                with c_app3:
                    in_app_oh = st.number_input("承認件数：オープンハウス (2,500円/件)", min_value=0, value=init_app_oh, key="in_app_oh")

                # 単価連動の確定売上自動推計（入力した確定数×定価単価）
                auto_calc_rev = (in_app_single * 5000) + (in_app_godou * 5500) + (in_app_oh * 2500)
                
                st.write("##### 2. 確定売上の調整・確定")
                st.caption("※入力した承認件数に基づく確定売上（定価計算）が表示されています。振込額や調整額がある場合は数値を書き換えてください。")
                in_final_rev = st.number_input("確定売上（合計金額 / 円）", min_value=0, value=auto_calc_rev if init_final_rev == est_total_revenue else init_final_rev, step=1000, key="in_final_rev")

                app_sum_docs = in_app_single + in_app_godou + in_app_oh
                rate_val = (app_sum_docs / total_send_docs * 100) if total_send_docs > 0 else 0
                calc_final_profit = in_final_rev - (total_labor_cost + current_sys_cost)

                st.markdown(
                    f"💡 **【判定サマリー】** 承認件数合計: **{app_sum_docs}件** / 送客数: **{total_send_docs}件** "
                    f"（実効承認率: **{rate_val:.1f}%**） ｜ **確定粗利益: ¥{int(calc_final_profit):,}**"
                )

                if st.button(f"💾 【{target_month_for_cost}】の確定データを保存・更新する", key="btn_save_detailed_rev"):
                    try:
                        save_monthly_revenue_detailed(spreadsheet_id, target_month_for_cost, send_counts, est_total_revenue, in_app_single, in_app_godou, in_app_oh, in_final_rev)
                        st.success(f"{target_month_for_cost}の確定データをスプレッドシートに保存しました！")
                        st.rerun()
                    except Exception as rev_err:
                        st.error(f"保存に失敗しました: {rev_err}")

                # --- D. 蓄積された月次収益記録の一覧テーブル ---
                st.markdown("---")
                st.subheader("📚 月次収益記録 一覧（過去蓄積データ）")
                if not df_revenue_records.empty:
                    df_disp = df_revenue_records.copy()

                    # 数値型に変換
                    num_cols = ["送客_単独LP", "送客_合同LP3", "送客_合同LP4", "送客_不動産合同", "送客_TLデッド", "送客_合計", "想定売上", "承認_単独TL", "承認_合同", "承認_オープンハウス", "承認_合計", "確定売上"]
                    for nc in num_cols:
                        if nc in df_disp.columns:
                            df_disp[nc] = pd.to_numeric(df_disp[nc], errors='coerce').fillna(0).astype(int)

                    # 承認率計算
                    df_disp["承認率(%)"] = (df_disp["承認_合計"] / df_disp["送客_合計"] * 100).fillna(0).round(1)

                    # 表示用フォーマット整理
                    df_disp["送客_単独LP"] = df_disp["送客_単独LP"].apply(lambda x: f"{x:,}件")
                    df_disp["送客_合同LP3"] = df_disp["送客_合同LP3"].apply(lambda x: f"{x:,}件")
                    df_disp["送客_合同LP4"] = df_disp["送客_合同LP4"].apply(lambda x: f"{x:,}件")
                    df_disp["送客_不動産合同"] = df_disp["送客_不動産合同"].apply(lambda x: f"{x:,}件")
                    df_disp["送客_TLデッド"] = df_disp["送客_TLデッド"].apply(lambda x: f"{x:,}件")
                    df_disp["送客_合計"] = df_disp["送客_合計"].apply(lambda x: f"{x:,}件")
                    df_disp["想定売上"] = df_disp["想定売上"].apply(lambda x: f"¥{x:,}")

                    df_disp["承認_単独TL"] = df_disp["承認_単独TL"].apply(lambda x: f"{x:,}件")
                    df_disp["承認_合同"] = df_disp["承認_合同"].apply(lambda x: f"{x:,}件")
                    df_disp["承認_オープンハウス"] = df_disp["承認_オープンハウス"].apply(lambda x: f"{x:,}件")
                    df_disp["承認_合計"] = df_disp["承認_合計"].apply(lambda x: f"{x:,}件")
                    df_disp["確定売上"] = df_disp["確定売上"].apply(lambda x: f"¥{x:,}")
                    df_disp["承認率"] = df_disp["承認率(%)"].apply(lambda x: f"{x:.1f}%")

                    cols_order = [
                        "年月",
                        "送客_単独LP", "送客_合同LP3", "送客_合同LP4", "送客_不動産合同", "送客_TLデッド", "送客_合計",
                        "想定売上",
                        "承認_単独TL", "承認_合同", "承認_オープンハウス", "承認_合計",
                        "確定売上",
                        "承認率",
                        "更新日時"
                    ]
                    
                    st.dataframe(df_disp[[c for c in cols_order if c in df_disp.columns]], use_container_width=True)
                else:
                    st.info("まだ保存された月次記録はありません。上記フォームから確定データを保存すると一覧に蓄積されます。")

            elif input_pass != "":
                st.error("パスワードが正しくありません")

    except Exception as e:
        st.error(f"スプレッドシートの読み込み・処理に失敗しました: {e}")
