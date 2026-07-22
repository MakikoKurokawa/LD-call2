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

st.title("📞 コール分析＆収益管理ダッシュボード")

# --- 2. Googleスプレッドシート自動取得関数 ---
@st.cache_data(ttl=600)  # 10分間キャッシュ（「データ更新」ボタンで即時更新可能）
def load_data_from_gsheets(spreadsheet_id):
    """Googleスプレッドシートから全ワークシート（LP別タブ）のデータを取得"""
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    
    # st.secrets からサービスアカウント情報を取得
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    sh = client.open_by_key(spreadsheet_id)
    worksheets = sh.worksheets()
    
    sheets_data = {}
    for ws in worksheets:
        # ヘッダーなしの生のリスト形式で取得
        raw_values = ws.get_all_values()
        if raw_values:
            sheets_data[ws.title] = pd.DataFrame(raw_values)
            
    return sheets_data

# --- 3. データ前処理ロジック ---
def process_call_data(df_raw, lp_name="全体"):
    records = []
    
    for idx, row in df_raw.iterrows():
        # ヘッダー行や空行のスキップ
        if idx < 2:  # 1〜2行目がタイトルの場合を考慮
            continue
            
        current_lp = str(row.iloc[0]) if pd.notnull(row.iloc[0]) and str(row.iloc[0]).strip() != "" else lp_name
        
        # 1〜4コール目の列インデックスペア (1コール目=B,C,D / 2コール目=E,F,G / 3コール目=H,I,J / 4コール目=K,L,M)
        call_pairs = [(1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12)]
        
        for idx_call, (col_date, col_res, col_staff) in enumerate(call_pairs, 1):
            if len(row) <= col_staff:
                continue
            
            date_val = row.iloc[col_date]
            res_val = row.iloc[col_res]
            staff_val = row.iloc[col_staff]
            
            if pd.isna(res_val) or str(res_val).strip() == "":
                continue
                
            staff_str = str(staff_val) if pd.notnull(staff_val) else ""
            
            # --- A. 担当者名の抽出 (留守電を表す小文字の 'r' を除外) ---
            # 例: "坂本⑪⑫" -> "坂本", "kr⑭" -> "k"
            match_name = re.match(r'^([^\d①-⑳]+)', staff_str)
            if match_name:
                raw_name = match_name.group(1)
                staff_name = raw_name.replace('r', '').strip()  # 'r'を除去
            else:
                staff_name = "不明"
            
            # --- B. 時間帯の抽出 (1つ目の数字を優先取得) ---
            hours = []
            circle_num_map = {'⑨': 9, '⑩': 10, '⑪': 11, '⑫': 12, '⑬': 13, '⑭': 14, '⑮': 15, '⑯': 16, '⑰': 17, '⑱': 18}
            for char in staff_str:
                if char in circle_num_map:
                    hours.append(circle_num_map[char])
            if not hours:
                digits = re.findall(r'\d+', staff_str)
                hours = [int(d) for d in digits if 8 <= int(d) <= 20]
            
            # 「坂本⑪⑫」のように複数ある場合は 1つ目(⑪ -> 11時台) を適用
            primary_hour = hours[0] if hours else None
            
            # --- C. 備考欄（兼資料数）からの数値抽出 ---
            doc_count = 0
            for cell in row.iloc[13:]:
                if pd.notnull(cell) and str(cell).strip().isdigit():
                    doc_count += int(str(cell).strip())
            
            # --- D. 通電・CV判定 ---
            is_cv = 1 if "許諾" in str(res_val) else 0
            is_connected = 0 if any(ng in str(res_val) for ng in ["繋がらない", "NG", "不通"]) else 1
            
            records.append({
                "LP": current_lp,
                "日付": date_val,
                "架電回数": f"{idx_call}コール目",
                "結果": res_val,
                "担当者": staff_name,
                "時間帯": f"{primary_hour}時台" if primary_hour else "不明",
                "通電フラグ": is_connected,
                "CVフラグ": is_cv,
                "資料数": doc_count
            })
            
    return pd.DataFrame(records)

# --- 4. サイドバー設定 ＆ スプレッドシート読み込み ---
st.sidebar.header("⚙️ スプレッドシート連携")

# スプレッドシートID設定（secretsから取得、または画面入力）
spreadsheet_id = st.secrets.get("SPREADSHEET_ID", "")
if not spreadsheet_id:
    spreadsheet_id = st.sidebar.text_input("スプレッドシートIDを入力", value="")

if st.sidebar.button("🔄 データを最新に更新"):
    st.cache_data.clear()
    st.rerun()

if spreadsheet_id:
    try:
        # スプレッドシートからの全タブ自動取得
        sheets_dict = load_data_from_gsheets(spreadsheet_id)
        sheet_names = list(sheets_dict.keys())
        
        selected_lp = st.sidebar.selectbox("対象LP（タブ）を選択", ["全LP合計"] + sheet_names)
        
        if selected_lp == "全LP合計":
            df_list = [process_call_data(sheets_dict[s], lp_name=s) for s in sheet_names]
            df_processed = pd.concat(df_list, ignore_index=True) if df_list else pd.DataFrame()
        else:
            df_processed = process_call_data(sheets_dict[selected_lp], lp_name=selected_lp)

        all_staffs = [s for s in df_processed["担当者"].unique() if s != "不明"] if not df_processed.empty else []

        # --- 5. メイン画面レイアウト ---
        tab1, tab2, tab3 = st.tabs(["📊 全体収益＆KPIレポート", "⏰ 時間帯別パフォーマンス", "👤 個人レポート＆稼働時間入力"])

        # ---------------------------------------------------------
        # TAB 1: 全体収益＆KPIレポート
        # ---------------------------------------------------------
        with tab1:
            st.subheader(f"📌 集計対象: {selected_lp}")
            
            st.markdown("##### ✍️ 本日の稼働時間入力（分）")
            col_inputs = st.columns(len(all_staffs) if all_staffs else 1)
            
            work_minutes = {}
            for i, staff in enumerate(all_staffs):
                with col_inputs[i % len(col_inputs)]:
                    default_mins = 0 if staff.lower() == 'k' else 240
                    work_minutes[staff] = st.number_input(f"{staff} (分)", min_value=0, value=default_mins, step=15)

            total_calls = len(df_processed)
            total_connects = df_processed["通電フラグ"].sum() if not df_processed.empty else 0
            total_cv = df_processed["CVフラグ"].sum() if not df_processed.empty else 0
            total_docs = df_processed["資料数"].sum() if not df_processed.empty else 0
            
            # 人件費（kさん除外）
            total_cost = sum(mins * MINUTE_WAGE for staff, mins in work_minutes.items() if staff.lower() != 'k')
            total_revenue = total_docs * DOCUMENT_UNIT_PRICE
            profit = total_revenue - total_cost

            st.markdown("---")
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("総架電数", f"{total_calls:,} 件")
            c2.metric("通電率", f"{(total_connects/total_calls*100):.1f}%" if total_calls > 0 else "0%")
            c3.metric("CV(許諾)数", f"{total_cv:,} 件")
            c4.metric("獲得資料数", f"{total_docs:,} 件")
            c5.metric("推定粗利益", f"¥{int(profit):,}", delta=f"売上: ¥{total_revenue:,} / コスト: ¥{int(total_cost):,}")

            st.markdown("---")
            st.subheader("📋 架電データ一覧")
            st.dataframe(df_processed, use_container_width=True)

        # ---------------------------------------------------------
        # TAB 2: 時間帯別パフォーマンス
        # ---------------------------------------------------------
        with tab2:
            st.subheader("⏰ 時間帯ごとの通電率・CV率分析")
            if not df_processed.empty:
                hour_summary = df_processed.groupby("時間帯").agg(
                    架電数=("結果", "count"),
                    通電数=("通電フラグ", "sum"),
                    CV数=("CVフラグ", "sum"),
                    資料数=("資料数", "sum")
                ).reset_index()
                
                hour_summary["通電率(%)"] = (hour_summary["通電数"] / hour_summary["架電数"] * 100).round(1)
                hour_summary["CV率(%)"] = (hour_summary["CV数"] / hour_summary["架電数"] * 100).round(1)

                st.bar_chart(hour_summary.set_index("時間帯")[["通電率(%)", "CV率(%)"]])
                st.dataframe(hour_summary, use_container_width=True)

        # ---------------------------------------------------------
        # TAB 3: 個人別レポート ＆ Slack報告文章生成
        # ---------------------------------------------------------
        with tab3:
            st.subheader("👤 個人成績 ＆ Slack報告文章の生成")
            selected_staff = st.selectbox("担当者を選択してください", all_staffs)
            
            if selected_staff:
                df_person = df_processed[df_processed["担当者"] == selected_staff]
                
                p_calls = len(df_person)
                p_connects = df_person["通電フラグ"].sum()
                p_cv = df_person["CVフラグ"].sum()
                p_docs = df_person["資料数"].sum()
                p_mins = work_minutes.get(selected_staff, 0)
                p_cost = 0 if selected_staff.lower() == 'k' else p_mins * MINUTE_WAGE
                
                p1, p2, p3, p4 = st.columns(4)
                p1.metric("架電数", f"{p_calls} 件")
                p2.metric("CV(許諾)数", f"{p_cv} 件")
                p3.metric("資料数", f"{p_docs} 件")
                p4.metric("発生人件費", f"¥{int(p_cost):,}" if selected_staff.lower() != 'k' else "人件費対象外")

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

    except Exception as e:
        st.error(f"スプレッドシートの読み込みに失敗しました。認証情報またはIDを確認してください: {e}")
else:
    st.info("👈 左側のサイドバーに「スプレッドシートID」を入力してください。")