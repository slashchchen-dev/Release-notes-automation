import os
import re
import argparse
import requests
from datetime import datetime
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ==========================================
# 0. 環境變數與設定
# ==========================================
CLICKUP_API_TOKEN = os.getenv("CLICKUP_API_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

# ClickUp 與 Google 專案設定
TEAM_ID = "9018311976"
TARGET_STATUSES = ["ready for deployment", "ready for deploy", "待部署", "已解決", "verified"]

# 解決 Variable should be lowercase: 將 SCOPES 移至全域變數
GOOGLE_SCOPES = [
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/drive.file'
]

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


# ==========================================
# 1. 讀取 ClickUp 任務
# ==========================================
def format_date(timestamp):
    if not timestamp:
        return "N/A"
    return datetime.fromtimestamp(int(timestamp) / 1000).strftime('%Y-%m-%d %H:%M')


def fetch_status_history(task_id, headers):
    url = f"https://api.clickup.com/api/v2/task/{task_id}/time_in_status"
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        history = res.json().get('status_history', [])
        audit_lines = []
        for history_item in history:
            s_name = str(history_item.get('status', '')).upper()
            # 這裡的 orderindex 是 ClickUp 官方欄位，若 PyCharm 報 typo 請手動加入字典
            c_time = format_date(history_item.get('orderindex_date'))
            audit_lines.append(f"   ↳ [{s_name}] 變更時間: {c_time}")
        return "\n".join(audit_lines)
    return "   ⚠️ 無法獲取審查紀錄"


def fetch_tasks_content(master_task_id):
    headers = {"Authorization": CLICKUP_API_TOKEN}
    params = {"custom_task_ids": "true", "team_id": TEAM_ID, "include_markdown_description": "true"}

    url_master = f"https://api.clickup.com/api/v2/task/{master_task_id}"
    res = requests.get(url_master, headers=headers, params=params)
    if res.status_code != 200:
        print(f"❌ 讀取母任務失敗: {res.text}")
        return ""

    md_text = res.json().get('markdown_description', '') or ""
    task_ids = re.findall(rf'/t/{TEAM_ID}/([a-zA-Z0-9\-]+)', md_text)
    task_ids = list(dict.fromkeys(task_ids))

    target_list_lower = [s.lower() for s in TARGET_STATUSES]

    print(f"📡 正在掃描母任務: {master_task_id}")
    print(f"✅ 發現 {len(task_ids)} 個工單連結，開始執行過濾與深度審查...\n")

    all_content = ""
    skipped_count = 0
    match_count = 0

    for tid in task_ids:
        if tid == master_task_id:
            continue

        d_url = f"https://api.clickup.com/api/v2/task/{tid}"
        d_res = requests.get(d_url, headers=headers, params=params)

        if d_res.status_code == 200:
            task_data = d_res.json()
            current_status = str(task_data.get('status', {}).get('status', '')).lower()
            name = task_data.get('name', '未命名')

            if current_status not in target_list_lower:
                print(f"⏭️ 跳過 - [{current_status.upper()}] {name}")
                skipped_count += 1
                continue

            match_count += 1
            tags = ", ".join([tag['name'] for tag in task_data.get('tags', [])]) or "未分類"
            task_url = task_data.get('url', 'N/A')
            audit_log = fetch_status_history(task_data.get('id'), headers)
            raw_desc = task_data.get('description', '') or ""
            clean_desc = raw_desc[:300].replace('\n', ' ').strip()

            item = f"【工單號: {tid}】 {name}\n"
            item += f"🔗 任務網址: {task_url}\n"
            item += f"🚥 當前狀態: {current_status.upper()} | 🏷️ 標籤: {tags}\n"
            item += f"📅 狀態審查流:\n{audit_log}\n"
            item += f"📝 開發摘要: {clean_desc}...\n"
            item += "-" * 75 + "\n"

            all_content += item
            print(f"🎯 命中 - [{current_status.upper()}] {name}")
        else:
            print(f"❌ 無法讀取工單: {tid}")

    print("\n" + "=" * 80)
    print(f"📊 統計: 命中 {match_count} 件, 跳過 {skipped_count} 件。")
    return all_content


# ==========================================
# 2. 調用 Gemini 產生英文摘要
# ==========================================
def generate_summary_with_gemini(text_content):
    if not text_content:
        return "No updates in this release."

    print("\n🧠 Generating AI Summary with Gemini...")
    model = genai.GenerativeModel('gemini-2.5-flash')

    prompt = f"""
            Please read the following task descriptions and summarize them into a professional English Release Note.
            Categorize the updates into three strict sections with the specified emojis. 

            STRICT RULES:
            1. LANGUAGE: Use ONLY English. Translate everything.
            2. BULLET POINTS: EVERY task entry must start with the bullet point symbol '●'.
            3. FORMAT: 
               ● [Task Name]: [Summary]
               🔗 URL: [Task URL]
            4. NO MARKDOWN: DO NOT use asterisks (*) or bold text (**).

            Categories:
            🚀 New Features:
            🔧 Bug Fixes:
            📈 Optimizations:

            Task Descriptions:
            {text_content}
            """

    response = model.generate_content(prompt)

    # --- 強效過濾處理 ---
    lines = response.text.split('\n')
    final_output = []

    for line in lines:
        # 1. 移除所有星號
        clean_line = line.replace('*', '').strip()

        if clean_line:
            # 2. 如果 AI 還是頑皮用了橫槓開頭，強制換成圓點
            if clean_line.startswith('-'):
                clean_line = '●' + clean_line[1:]

            # 3. 針對 URL 行做特殊處理：加兩個空格縮排，並確保有 🔗
            if 'url:' in clean_line.lower() or '🔗' in clean_line:
                # 提取網址部分
                url_part = clean_line.lower().replace('url:', '').replace('🔗', '').strip()
                clean_line = f"  🔗 URL: {url_part}"

            # 4. 如果這行既不是標題 (Emoji)，也不是圓點開點，也不是 URL，就補上圓點
            elif not any(emoji in clean_line for emoji in ['🚀', '🔧', '📈']) and not clean_line.startswith('●'):
                clean_line = f"● {clean_line}"

            final_output.append(clean_line)
        else:
            final_output.append("")  # 保留空行

    return '\n'.join(final_output)


# ==========================================
# 3. 建立 Google Doc 並放入指定資料夾
# ==========================================
def create_doc_in_folder(version_str, summary_text, raw_data_text):
    if not raw_data_text:
        print("⚠️ 沒有內容可以寫入 Google Doc，跳過建立。")
        return None

    print(f"\n📄 Creating Google Doc for {version_str} in specific folder...")
    creds = service_account.Credentials.from_service_account_file('credentials.json', scopes=GOOGLE_SCOPES)

    drive_service = build('drive', 'v3', credentials=creds)
    docs_service = build('docs', 'v1', credentials=creds)

    file_metadata = {
        'name': f'Release Note - {version_str}',
        'mimeType': 'application/vnd.google-apps.document',
        'parents': [GOOGLE_DRIVE_FOLDER_ID]
    }
    file = drive_service.files().create(
        body=file_metadata,
        fields='id',
        supportsAllDrives=True
    ).execute()
    new_doc_id = file.get('id')
    new_doc_url = f"https://docs.google.com/document/d/{new_doc_id}/edit"

    text_to_insert = f"🤖 AI Generated Release Note Summary\n\n{summary_text}\n\n{'=' * 50}\n📦 Raw Task Data\n\n{raw_data_text}"

    requests_body = [{'insertText': {'location': {'index': 1}, 'text': text_to_insert}}]
    docs_service.documents().batchUpdate(documentId=new_doc_id, body={'requests': requests_body}).execute()

    print(f"✅ Google Doc Successfully Created: {new_doc_url}")
    return new_doc_url


# ==========================================
# 主程式入口
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automate Release Notes")
    parser.add_argument("--version", required=True, help="Release version (e.g., v1.2.0)")
    parser.add_argument("--master_task", required=True, help="The ClickUp Master Task ID")

    args = parser.parse_args()

    # 解決 Shadows name 警告：變數名稱加上 main_ 前綴，避免與上方函式參數撞名
    main_raw_content = fetch_tasks_content(args.master_task)

    if main_raw_content:
        main_ai_summary = generate_summary_with_gemini(main_raw_content)
        main_doc_url = create_doc_in_folder(args.version, main_ai_summary, main_raw_content)
        print("\n🎉 --- Process Completed Successfully! ---")
    else:
        print("\n⚠️ --- Process Aborted: No relevant tasks found. ---")