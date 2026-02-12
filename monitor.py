import requests
import os
import time
import subprocess
import shutil
import random
import json
import sys
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

# --- CẤU HÌNH ---
URL_RAW = os.getenv('EVENT_URL', '')
URL_LIST = [u.strip() for u in URL_RAW.split(',') if u.strip()]
TG_TOKEN = os.getenv('TELEGRAM_TOKEN')
TG_ID = os.getenv('TELEGRAM_CHAT_ID')
RUN_ID = os.getenv('GITHUB_RUN_NUMBER', '0')
CURRENT_RUN_ID = os.getenv('GITHUB_RUN_ID', '0')
LOG_FILE = "history.json"
GH_TOKEN = os.getenv('GH_TOKEN')

# Danh sách từ khóa báo hiệu trang bảo trì (Maintenance)
MAINTENANCE_KEYWORDS = [
    "under maintainance", "maintainance", "maintenance", 
    "bảo trì", "come back later", "quay lại sau", 
    "chưa bắt đầu", "hệ thống đang nâng cấp"
]

def get_vn_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)

def get_event_id(url):
    parsed = urlparse(url)
    domain = parsed.netloc.split('.')[0]
    path_code = ([p for p in parsed.path.split('/') if p] or ['event'])[0]
    return f"{domain}-{path_code}"

def get_url_hash(url_string):
    return hashlib.sha256(url_string.encode()).hexdigest()

def is_fake_200(html_content):
    """Kiểm tra xem trang web có phải là trang bảo trì giả mạo mã 200 không"""
    if not html_content: return True
    content_lower = html_content.lower()
    
    # 1. Kiểm tra từ khóa bảo trì
    for key in MAINTENANCE_KEYWORDS:
        if key in content_lower:
            return True
            
    # 2. Kiểm tra độ phức tạp của trang (Trang bảo trì thường rất ngắn)
    if len(html_content) < 3000: # Ngưỡng 3KB thường là trang tĩnh đơn giản
        return True
        
    return False

def git_sync_general(data, message):
    try:
        subprocess.run(["git", "config", "user.name", "AOV-Hunter-Bot"], check=False)
        subprocess.run(["git", "config", "user.email", "bot@github.com"], check=False)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        subprocess.run(["git", "add", LOG_FILE], check=False)
        subprocess.run(["git", "commit", "-m", message], check=False)
        push = subprocess.run(["git", "push"], capture_output=True)
        return push.returncode == 0
    except: return False

def kill_entire_fleet():
    print(f"[!!!] NHIỆM VỤ HOÀN TẤT. ĐANG GIẢI TÁN HẠM ĐỘI...")
    try:
        subprocess.run(["gh", "workflow", "disable", "AOV Event Monitor"], env={**os.environ, "GH_TOKEN": GH_TOKEN}, check=False)
        cmd = ["gh", "run", "list", "--workflow", "AOV Event Monitor", "--status", "in_progress", "--json", "databaseId"]
        result = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "GH_TOKEN": GH_TOKEN})
        if result.returncode == 0:
            runs = json.loads(result.stdout)
            for r in runs:
                other_id = str(r['databaseId'])
                if other_id != CURRENT_RUN_ID:
                    subprocess.run(["gh", "run", "cancel", other_id], env={**os.environ, "GH_TOKEN": GH_TOKEN}, check=False)
        os._exit(0)
    except: os._exit(0)

def git_lock_and_check(ev_id):
    history = {}
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r") as f: history = json.load(f)
        except: pass
    if history.get(ev_id, {}).get("archived"): return False, history
    history[ev_id] = {"status": 200, "archived": True, "time": get_vn_now().strftime('%Y-%m-%d %H:%M:%S'), "by_run": RUN_ID}
    return git_sync_general(history, f"Run #{RUN_ID}: Lock {ev_id}"), history

def archive_event(url, ev_id):
    try:
        from playwright.sync_api import sync_playwright
        if not os.path.exists(ev_id): os.makedirs(ev_id)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 375, 'height': 812}, is_mobile=True)
            page = context.new_page()
            res_counter = [0]

            def handle_res(res):
                try:
                    u, ct = res.url, res.headers.get("content-type", "").lower()
                    res_counter[0] += 1
                    if any(x in u.lower() for x in ['api', 'graphql', 'config']) or "json" in ct:
                        data = res.json()
                        fname = f"{res_counter[0]:02d}_api_{u.split('/')[-1].split('?')[0] or 'gql'}.json"
                        with open(os.path.join(ev_id, fname), "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=4, ensure_ascii=False)
                    elif any(ext in u.lower() for ext in ['.js', '.css', '.png', '.jpg', '.html']):
                        fname = u.split('/')[-1].split('?')[0] or f"file_{res_counter[0]}"
                        with open(os.path.join(ev_id, fname), "wb") as f: f.write(res.body())
                except: pass

            page.on("response", handle_res)
            page.goto(url, wait_until="networkidle", timeout=90000)
            time.sleep(15)

            # --- KIỂM TRA LẦN CUỐI SAU KHI RENDER ---
            rendered_content = page.content()
            if is_fake_200(rendered_content):
                print(f"[!] Hủy bỏ: {ev_id} vẫn báo bảo trì sau khi render.")
                browser.close()
                return "MAINTENANCE"

            with open(os.path.join(ev_id, "00_rendered_view.html"), "w", encoding="utf-8") as f:
                f.write(rendered_content)
            page.screenshot(path=f"{ev_id}.png", full_page=True)
            browser.close()

        zip_path = shutil.make_archive(ev_id, 'zip', ev_id)
        vn_time = get_vn_now().strftime('%H:%M:%S %d/%m')
        caption = f"Ding Dong✨, Sự kiện đã bắt đầu! {ev_id}\n⏰ Lúc: {vn_time}\n🔍 Bởi: Run #{RUN_ID}"
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data={"chat_id": TG_ID, "caption": caption}, files={'photo': open(f"{ev_id}.png", 'rb')})
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument", data={"chat_id": TG_ID}, files={'document': open(zip_path, 'rb')})
        return True
    except Exception as e:
        print(f"Lỗi đóng gói: {e}")
        return False

def run():
    print(f"[*] Fleet Commander #{RUN_ID} (Content Verifier Mode) trực chiến...")
    start_ts = time.time()
    while time.time() - start_ts < 19800:
        history = {}
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r") as f: history = json.load(f)
            except: pass

        current_hash = get_url_hash(URL_RAW)
        if history.get("__metadata__", {}).get("url_hash") != current_hash:
            history = {"__metadata__": {"url_hash": current_hash}}
            git_sync_general(history, f"Run #{RUN_ID}: Reset log for new URLs")

        pending = [u for u in URL_LIST if not history.get(get_event_id(u), {}).get("archived")]
        if not pending and len(URL_LIST) > 0:
            kill_entire_fleet()
            return

        for url in pending:
            ev_id = get_event_id(url)
            try:
                headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}
                res = requests.get(url, timeout=15, allow_redirects=True, headers=headers)
                
                # CHỐNG 200 GIẢ
                if res.status_code == 200:
                    if is_fake_200(res.text):
                        print(f"[{get_vn_now().strftime('%H:%M:%S')}] {ev_id} | Status: 200 (Maintenance detected)")
                        continue
                    
                    print(f"[{get_vn_now().strftime('%H:%M:%S')}] {ev_id} | Status: 200 (Real Open!)")
                    is_winner, history = git_lock_and_check(ev_id)
                    if is_winner:
                        subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
                        result = archive_event(url, ev_id)
                        if result == "MAINTENANCE":
                            # Reset lại log nếu lỡ khóa nhầm trang bảo trì
                            history[ev_id]["archived"] = False
                            git_sync_general(history, f"Run #{RUN_ID}: Unlock {ev_id} due to maintenance")
                        elif result:
                            if all(history.get(get_event_id(u), {}).get("archived") for u in URL_LIST):
                                kill_entire_fleet()
                                return
            except: pass
        time.sleep(random.randint(300, 600))

if __name__ == "__main__":
    run()
