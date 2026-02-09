import requests
import os
import time
import subprocess
import shutil
import random
import json
import hashlib
import re
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

# Từ khóa bảo trì Unicode
MAINTENANCE_KEYWORDS = [
    "under maintainance", "maintainance", "maintenance", 
    "bảo trì", "come back later", "quay lại sau", 
    "chưa bắt đầu", "hệ thống đang nâng cấp", "đang cập nhật"
]

def get_vn_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)

def get_event_id(url):
    parsed = urlparse(url)
    domain = parsed.netloc.split('.')[0]
    path_parts = [p for p in parsed.path.split('/') if p]
    path_code = path_parts[0] if path_parts else 'event'
    suffix = hashlib.md5(url.encode()).hexdigest()[:4]
    return f"{domain}-{path_code}-{suffix}"

def get_url_hash(url_string):
    return hashlib.sha256(url_string.encode()).hexdigest()

def is_fake_200(html_content):
    """Kiểm tra trang bảo trì giả mạo mã 200"""
    if not html_content: return True
    content_lower = html_content.lower()
    
    # Kiểm tra thẻ Title
    title_match = re.search(r'<title>(.*?)</title>', content_lower)
    if title_match:
        if any(key in title_match.group(1) for key in MAINTENANCE_KEYWORDS):
            return True

    # Kiểm tra Body và độ dài
    for key in MAINTENANCE_KEYWORDS:
        if key in content_lower: return True
    if len(html_content) < 5000: return True # Trang bảo trì VN thường rất nhẹ
    return False

def git_sync_general(data, message):
    try:
        subprocess.run(["git", "config", "user.name", "AOV-Hunter-Bot"], check=False)
        subprocess.run(["git", "config", "user.email", "bot@github.com"], check=False)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        subprocess.run(["git", "add", LOG_FILE], check=False)
        subprocess.run(["git", "commit", "-m", message], check=False)
        push = subprocess.run(["git", "push"], capture_output=True)
        return push.returncode == 0
    except: return False

def kill_entire_fleet():
    """Vô hiệu hóa workflow và hủy toàn bộ run đang chạy để tiết kiệm tài nguyên"""
    print(f"[!!!] TẤT CẢ SỰ KIỆN ĐÃ MỞ. ĐANG GIẢI TÁN HẠM ĐỘI...")
    try:
        # 1. Tắt Workflow
        subprocess.run(["gh", "workflow", "disable", "AOV Event Monitor"], 
                       env={**os.environ, "GH_TOKEN": GH_TOKEN}, check=False)
        # 2. Tìm và hủy các Run khác đang chạy
        cmd = ["gh", "run", "list", "--workflow", "AOV Event Monitor", "--status", "in_progress", "--json", "databaseId"]
        result = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "GH_TOKEN": GH_TOKEN})
        if result.returncode == 0:
            runs = json.loads(result.stdout)
            for r in runs:
                other_id = str(r['databaseId'])
                if other_id != CURRENT_RUN_ID:
                    subprocess.run(["gh", "run", "cancel", other_id], 
                                   env={**os.environ, "GH_TOKEN": GH_TOKEN}, check=False)
        os._exit(0)
    except: os._exit(0)

def git_lock_and_check(ev_id):
    """Cơ chế khóa nguyên tử: Ngăn nhiều máy ảo xử lý cùng 1 sự kiện"""
    history = {}
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f: history = json.load(f)
        except: pass
    
    if history.get(ev_id, {}).get("archived"):
        return False, history
    
    # Ghi đè trạng thái khóa
    history[ev_id] = {
        "status": 200, 
        "archived": True, 
        "time": get_vn_now().strftime('%Y-%m-%d %H:%M:%S'),
        "by_run": RUN_ID
    }
    success = git_sync_general(history, f"Run #{RUN_ID}: Locking {ev_id}")
    return success, history

def archive_event(url, ev_id):
    try:
        from playwright.sync_api import sync_playwright
        if os.path.exists(ev_id): shutil.rmtree(ev_id)
        if os.path.exists(f"{ev_id}.zip"): os.remove(f"{ev_id}.zip")
        os.makedirs(ev_id)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 375, 'height': 812}, is_mobile=True)
            page = context.new_page()
            res_counter = [0]

            def handle_res(res):
                try:
                    u = res.url
                    ct = res.headers.get("content-type", "").lower()
                    res_counter[0] += 1
                    u_path = urlparse(u).path.split('/')[-1] or "api_data"
                    
                    if any(x in u.lower() for x in ['api', 'graphql', 'ajax']) or "json" in ct:
                        data = res.json()
                        fname = f"{res_counter[0]:02d}_API_{u_path[:20]}.json"
                        with open(os.path.join(ev_id, fname), "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=4, ensure_ascii=False)
                    elif any(ext in u.lower() for ext in ['.js', '.css', '.html']):
                        fname = f"{res_counter[0]:02d}_{u_path[:20]}"
                        with open(os.path.join(ev_id, fname), "wb") as f: f.write(res.body())
                except: pass

            page.on("response", handle_res)
            page.goto(url, wait_until="networkidle", timeout=60000)
            time.sleep(10)

            rendered_content = page.content()
            if is_fake_200(rendered_content):
                browser.close()
                return "MAINTENANCE"

            with open(os.path.join(ev_id, "view_source.html"), "w", encoding="utf-8") as f:
                f.write(rendered_content)
            page.screenshot(path=f"{ev_id}.png", full_page=True)
            browser.close()

        zip_path = shutil.make_archive(ev_id, 'zip', ev_id)
        caption = f"✨ SỰ KIỆN ĐÃ MỞ: {ev_id}\n⏰ Lúc: {get_vn_now().strftime('%H:%M:%S %d/%m')}\n🔍 Run: #{RUN_ID}"
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data={"chat_id": TG_ID, "caption": caption}, files={'photo': open(f"{ev_id}.png", 'rb')})
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument", data={"chat_id": TG_ID}, files={'document': open(zip_path, 'rb')})
        
        # Dọn dẹp tránh lỗi lặp file
        shutil.rmtree(ev_id)
        return True
    except Exception as e:
        print(f"Lỗi đóng gói: {e}")
        return False

def run():
    print(f"[*] Fleet Commander #{RUN_ID} xuất kích...")
    start_ts = time.time()
    
    while time.time() - start_ts < 19800:
        history = {}
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f: history = json.load(f)
            except: pass

        # --- KIỂM TRA THAY ĐỔI URL LẬP TỨC ---
        current_hash = get_url_hash(URL_RAW)
        force_immediate = False
        if history.get("__metadata__", {}).get("url_hash") != current_hash:
            print("[!] URL THAY ĐỔI -> QUÉT ƯU TIÊN KHÔNG DELAY.")
            history = {"__metadata__": {"url_hash": current_hash}}
            git_sync_general(history, f"Run #{RUN_ID}: New URL list detected")
            force_immediate = True

        pending = [u for u in URL_LIST if not history.get(get_event_id(u), {}).get("archived")]
        
        if not pending and len(URL_LIST) > 0:
            kill_entire_fleet()
            return

        for url in pending:
            ev_id = get_event_id(url)
            try:
                headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"}
                res = requests.get(url, timeout=15, allow_redirects=True, headers=headers)
                
                if res.status_code == 200:
                    if is_fake_200(res.text):
                        print(f"[{get_vn_now().strftime('%H:%M:%S')}] {ev_id} (Bảo trì/Giả 200)")
                        continue
                    
                    print(f"[+] {ev_id} ĐÃ MỞ! Đang khóa mục tiêu...")
                    is_winner, history = git_lock_and_check(ev_id)
                    if is_winner:
                        subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
                        result = archive_event(url, ev_id)
                        if result == "MAINTENANCE":
                            history[ev_id]["archived"] = False
                            git_sync_general(history, f"Run #{RUN_ID}: Unlock {ev_id} (False Positive)")
                        elif result:
                            # Kiểm tra lại nếu hết sự kiện thì kill luôn
                            if all(history.get(get_event_id(u), {}).get("archived") for u in URL_LIST):
                                kill_entire_fleet()
            except Exception as e:
                print(f"Error checking {ev_id}: {e}")

        if force_immediate:
            force_immediate = False
            continue
            
        wait_time = random.randint(300, 600)
        print(f"[*] Chờ {wait_time}s...")
        time.sleep(wait_time)

if __name__ == "__main__":
    run()
