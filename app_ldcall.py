import streamlit as st
import pandas as pd
import re
import gspread
from google.oauth2.service_account import Credentials

# ページ基本設定
st.set_page_config(page_title="コール分析＆収益管理ダッシュボード", layout="wide")

# --- 1. 定数設定 ---
HOURLY_WAGE = 2000              # 時給2,000円
MINUTE_WAGE = HOURLY_WAGE / 60  # 分単価（約33.33円）
DOCUMENT_UNIT_PRICE = 4500      # 資料1件あたりの売上単価 (4,500円)

USER_PASSWORDS = st.secrets.get("passwords", {"admin": "admin123"})
ADMIN_PASSWORD = USER_PASSWORDS.get("admin", "admin123")

st.title("📞 コール分析＆収益管理ダッシュボード")

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
    
    # 0始まりの列インデックス
    # 1巡目: C(2), D(3), E(4), H(7)
    # 2巡目: I(8), J(9), K(10), N(13)
    # 3巡目: O(14), P(15), Q(16), T(19)
    # 4巡目: U(20), V(21), W(22), Z(25)
    call_pairs = [
        (2, 3, 4, 7),
        (8, 9, 10, 13),
        (14, 15, 16, 19),
        (20, 21, 22, 25)
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

                # 💡 必須判定：「日付」「結果」「担当者」の3つがすべて揃っていない場合はスキップ
                if not date_val or not res_val or res_val == "結果" or not staff_name:
                    continue

                # --- B. 時間帯 (1つ目の数字を適用) ---
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
                
                # --- C. 資料数 ---
                doc_count = 0
                if note_val.isdigit():
                    doc_count = int(note_val)
                else:
                    doc_digits = re.findall(r'\d+', note_val)
                    if doc_digits:
                        doc_count = int(doc_digits[0])
                
                # --- D. フラグ判定 ---
                is_cv = 1 if "許諾" in res_val else 0
                is_connected = 0 if any(ng in res_val for ng in ["繋がらない", "NG", "不通", "留守", "着拒"]) else 1
                
                # --- E. 月情報 ---
                month_str = "不明"
                if date_val:
                    parsed_date = pd.to_datetime(date_val, errors='coerce')
                    if pd.notnull(parsed_date):
                        month_str = parsed_date.strftime('%Y-%m')
                
                all_records.append({
                    "年月": month_str,
                    "日付": date_val,
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

# --- 3. メイン処理 ---
spreadsheet_id = st.secrets.get("SPREADSHEET_ID", "")

if st.sidebar.button("🔄 データを最新に更新"):
    st.cache_data.clear()
    st.rerun()

if spreadsheet_id:
    try:
        df_all = load_and_process_all_data(spreadsheet_id)

        if not df_all.empty:
            lp_list = ["全LP合計"] + sorted([str(x) for x in df_all["LP"].unique()])
            selected_lp = st.sidebar.selectbox("対象LP（タブ）を選択", lp_list)
            
            if selected_lp != "全LP合計":
                df_lp = df_all[df_all["LP"] == selected_lp]
            else:
                df_lp = df_all

            available_months = sorted([m for m in df_lp["年月"].unique() if m != "不明"], reverse=True)
            selected_month = st.sidebar.selectbox("対象月を選択", ["全期間"] + available_months)
            
            if selected_month != "全期間":
                df_filtered = df_lp[df_lp["年月"] == selected_month]
            else:
                df_filtered = df_lp
        else:
            df_filtered = pd.DataFrame()

        all_staffs = sorted([s for s in df_filtered["担当者"].unique() if s != "不明"]) if not df_filtered.empty else []

        # --- 4. 画面表示 ---
        tab1, tab2, tab3 = st.tabs(["📊 全体パフォーマンス", "⏰ 時間帯別分析", "👤 個人レポート＆稼働時間"])

        # TAB 1: 全体集計
        with tab1:
            st.subheader(f"📌 集計対象: {selected_lp} ({selected_month if 'selected_month' in locals() else '全期間'})")
            
            st.markdown("##### ✍️ 本日の稼働時間入力（分）")
            col_inputs = st.columns(len(all_staffs) if all_staffs else 1)
            work_minutes = {}
            for i, staff in enumerate(all_staffs):
                with col_inputs[i % len(col_inputs)]:
                    default_mins = 0 if staff.lower() == 'k' else 240
                    work_minutes[staff] = st.number_input(f"{staff} (分)", min_value=0, value=default_mins, step=15)

            total_calls = len(df_filtered)
            total_connects = df_filtered["通電フラグ"].sum() if not df_filtered.empty else 0
            total_cv = df_filtered["CVフラグ"].sum() if not df_filtered.empty else 0
            total_docs = df_filtered["資料数"].sum() if not df_filtered.empty else 0
            
            st.markdown("---")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("総架電数", f"{total_calls:,} 件")
            c2.metric("通電率", f"{(total_connects/total_calls*100):.1f}%" if total_calls > 0 else "0%")
            c3.metric("CV(許諾)数", f"{total_cv:,} 件")
            c4.metric("獲得資料数", f"{total_docs:,} 件")

            # 🔒 管理者専用 収益エリア
            st.markdown("---")
            with st.expander("🔒 【管理者専用】売上・人件費・利益の確認"):
                input_pass = st.text_input("管理者パスワードを入力してください", type="password", key="admin_pass")
                if input_pass == ADMIN_PASSWORD:
                    total_cost = sum(mins * MINUTE_WAGE for staff, mins in work_minutes.items() if staff.lower() != 'k')
                    total_revenue = total_docs * DOCUMENT_UNIT_PRICE
                    profit = total_revenue - total_cost

                    m1, m2, m3 = st.columns(3)
                    m1.metric("概算売上", f"¥{total_revenue:,}")
                    m2.metric("概算人件費 (kさん除外)", f"¥{int(total_cost):,}")
                    m3.metric("推定粗利益", f"¥{int(profit):,}")
                elif input_pass != "":
                    st.error("パスワードが正しくありません")

        # TAB 2: 時間帯別
        with tab2:
            st.subheader("⏰ 時間帯ごとのパフォーマンス分析")
            if not df_filtered.empty:
                hour_summary = df_filtered.groupby("時間帯").agg(
                    架電数=("結果", "count"),
                    通電数=("通電フラグ", "sum"),
                    CV数=("CVフラグ", "sum"),
                    資料数=("資料数", "sum")
                ).reset_index()
                
                hour_summary["通電率(%)"] = (hour_summary["通電数"] / hour_summary["架電数"] * 100).round(1)
                hour_summary["CV率(%)"] = (hour_summary["CV数"] / hour_summary["架電数"] * 100).round(1)

                st.bar_chart(hour_summary.set_index("時間帯")[["通電率(%)", "CV率(%)"]])
                st.dataframe(hour_summary, use_container_width=True)

        # TAB 3: 個人レポート
        with tab3:
            st.subheader("👤 個人成績 ＆ 日報出力")
            selected_staff = st.selectbox("担当者を選択してください", all_staffs)
            
            if selected_staff:
                st.info(f"🔒 **{selected_staff}** さんの詳細データを表示するにはパスワードが必要です。")
                input_user_pass = st.text_input(f"{selected_staff} さんのパスワードを入力", type="password", key=f"pass_{selected_staff}")
                
                correct_pass = USER_PASSWORDS.get(selected_staff, "")
                
                if input_user_pass != "" and (input_user_pass == correct_pass or input_user_pass == ADMIN_PASSWORD):
                    st.success("認証に成功しました！")
                    
                    df_person = df_filtered[df_filtered["担当者"] == selected_staff]
                    
                    p_calls = len(df_person)
                    p_cv = df_person["CVフラグ"].sum()
                    p_docs = df_person["資料数"].sum()
                    p_mins = work_minutes.get(selected_staff, 0)
                    
                    st.markdown("---")
                    p1, p2, p3 = st.columns(3)
                    p1.metric("架電数", f"{p_calls} 件")
                    p2.metric("CV(許諾)数", f"{p_cv} 件")
                    p3.metric("資料数", f"{p_docs} 件")

                    st.markdown("#### 📋 Slack日報用テンプレート")
                    slack_text = f"""お疲れ様です！本日の架電報告です。

【担当】{selected_staff}
【稼働時間】{p_mins}分
【架電数】{p_calls}件
【CV(許諾)数】{p_cv}件
【獲得資料数】{p_docs}件

【所感】
（ここに本日の所感を記入）
"""
                    st.code(slack_text, language="markdown")
                    
                    st.markdown("#### 📄 架電履歴")
                    st.dataframe(df_person, use_container_width=True)

                elif input_user_pass != "":
                    st.error("パスワードが正しくありません。")

    except Exception as e:
        st.error(f"スプレッドシートの読み込み・処理に失敗しました: {e}")
