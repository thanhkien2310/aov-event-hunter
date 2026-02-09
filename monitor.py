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

MAINTENANCE_KEYWORDS = [
    "under maintainance", "maintainance", "maintenance", 
    "bảo trì", "come back later", "quay lại sau", 
    "chưa bắt đầu", "hệ thống đang nâng cấp", "đang cập nhật",
    "vui lòng quay lại", "sẽ sớm bắt đầu"
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
    """Kiểm tra trang bảo trì - Đã nới lỏng để không bỏ sót"""
    if not html_content or len(html_content) < 500: return True # Quá nhỏ thì chắc chắn lỗi
    
    content_lower = html_content.lower()
    
    # 1. Ưu tiên kiểm tra từ khóa trong thẻ Title
    title_match = re.search(r'<title>(.*?)</title>', content_lower)
    if title_match:
        t_text = title_match.group(1)
        if any(key in t_text for key in MAINTENANCE_KEYWORDS):
            return True

    # 2. Kiểm tra từ khóa trong toàn bộ nội dung
    # Chỉ đánh dấu bảo trì nếu chứa từ khóa, KHÔNG chỉ dựa vào độ dài
    for key in MAINTENANCE_KEYWORDS:
        if key in content_lower:
            return True
            
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
    except Exception as e:
        print(f"Git Sync Error: {e}")
        return False

def kill_entire_fleet():
    print(f"[!!!] HOÀN TẤT NHIỆM VỤ. ĐANG DỪNG HẠM ĐỘI...")
    try:
        subprocess.run(["gh", "workflow", "disable", "AOV Event Monitor"], env={**os.environ, "GH_TOKEN": GH_TOKEN}, check=False)
        cmd = ["gh", "run", "list", "--workflow", "AOV Event Monitor", "--status", "in_progress", "--json", "databaseId"]
        result = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "GH_TOKEN": GH_TOKEN})
        if result.returncode == 0:
            runs = json.loads(result.stdout)
            for r in runs:
                oid = str(r['databaseId'])
                if oid != CURRENT_RUN_ID:
                    subprocess.run(["gh", "run", "cancel", oid], env={**os.environ, "GH_TOKEN": GH_TOKEN}, check=False)
    except: pass
    os._exit(0)

def git_lock_and_check(ev_id):
    history = {}
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f: history = json.load(f)
        except: pass
    
    if history.get(ev_id, {}).get("archived"): return False, history
    
    history[ev_id] = {"status": 200, "archived": True, "time": get_vn_now().strftime('%Y-%m-%d %H:%M:%S'), "by_run": RUN_ID}
    success = git_sync_general(history, f"Run #{RUN_ID}: Lock {ev_id}")
    return success, history

def archive_event(url, ev_id):
    try:
        from playwright.sync_api import sync_playwright
        if os.path.exists(ev_id): shutil.rmtree(ev_id)
        os.makedirs(ev_id)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 375, 'height': 812}, is_mobile=True)
            page = context.new_page()
            res_list = []

            def handle_res(res):
                try:
                    u = res.url
                    ct = res.headers.get("content-type", "").lower()
                    if any(x in u.lower() for x in ['api', 'graphql', 'ajax']) or "json" in ct:
                        data = res.json()
                        fname = f"API_{hashlib.md5(u.encode()).hexdigest()[:6]}.json"
                        with open(os.path.join(ev_id, fname), "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=4, ensure_ascii=False)
                except: pass

            page.on("response", handle_res)
            page.goto(url, wait_until="load", timeout=90000)
            time.sleep(12) # Chờ render

            # Kiểm tra bảo trì sau render
            if is_fake_200(page.content()):
                browser.close()
                return "MAINTENANCE"

            page.screenshot(path=f"{ev_id}.png", full_page=True)
            with open(os.path.join(ev_id, "source.html"), "w", encoding="utf-8") as f:
                f.write(page.content())
            browser.close()

        zip_path = shutil.make_archive(ev_id, 'zip', ev_id)
        caption = f"Ding Dong✨! Sự kiện đã mở: {ev_id}\n⏰ {get_vn_now().strftime('%H:%M:%S %d/%m')}\n🔍 Run #{RUN_ID}"
        
        # Gửi Telegram và log kết quả
        r1 = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data={"chat_id": TG_ID, "caption": caption}, files={'photo': open(f"{ev_id}.png", 'rb')})
        r2 = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument", data={"chat_id": TG_ID}, files={'document': open(zip_path, 'rb')})
        print(f"Telegram status: Photo({r1.status_code}), Doc({r2.status_code})")
        
        shutil.rmtree(ev_id)
        if os.path.exists(f"{ev_id}.png"): os.remove(f"{ev_id}.png")
        if os.path.exists(zip_path): os.remove(zip_path)
        return True
    except Exception as e:
        print(f"Lỗi đóng gói {ev_id}: {e}")
        return False

def run():
    print(f"[*] Fleet Commander #{RUN_ID} xuất kích...")
    
    # Cài đặt Playwright ngay khi bắt đầu (để đảm bảo sẵn sàng)
    subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
    
    start_ts = time.time()
    while time.time() - start_ts < 19800:
        history = {}
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f: history = json.load(f)
            except: pass

        current_hash = get_url_hash(URL_RAW)
        is_url_changed = history.get("__metadata__", {}).get("url_hash") != current_hash

        if is_url_changed:
            print("[!] PHÁT HIỆN THAY ĐỔI URL -> QUÉT NGAY LẬP TỨC.")
            history = {"__metadata__": {"url_hash": current_hash}}
            git_sync_general(history, f"Run #{RUN_ID}: New URL detected")

        pending = [u for u in URL_LIST if not history.get(get_event_id(u), {}).get("archived")]
        
        if not pending and len(URL_LIST) > 0:
            kill_entire_fleet()
            return

        for url in pending:
            ev_id = get_event_id(url)
            print(f"[{get_vn_now().strftime('%H:%M:%S')}] Kiểm tra: {ev_id}")
            try:
                res = requests.get(url, timeout=20, allow_redirects=True)
                if res.status_code == 200:
                    if is_fake_200(res.text):
                        print(f"   -> {ev_id}: Đang bảo trì (200 giả).")
                        continue
                    
                    print(f"   -> {ev_id}: ĐÃ MỞ! Tiến hành xử lý...")
                    is_winner, history = git_lock_and_check(ev_id)
                    if is_winner:
                        result = archive_event(url, ev_id)
                        if result == "MAINTENANCE":
                            history[ev_id]["archived"] = False
                            git_sync_general(history, f"Run #{RUN_ID}: Unlock {ev_id} (False Positive)")
                        elif result:
                            # Sau khi xong, kiểm tra xem còn gì pending không
                            if all(history.get(get_event_id(u), {}).get("archived") for u in URL_LIST):
                                kill_entire_fleet()
            except Exception as e:
                print(f"   -> Lỗi truy cập {ev_id}: {e}")

        # Nếu vừa đổi URL, không nghỉ, quét tiếp vòng 2 ngay
        if is_url_changed:
            is_url_changed = False
            continue
            
        wait = random.randint(300, 600)
        print(f"[*] Nghỉ {wait}s...")
        time.sleep(wait)

if __name__ == "__main__":
    run()
