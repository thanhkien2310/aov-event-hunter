import requests
import os
import time
import subprocess
import shutil
import random
import json
import sys
import hashlib
import re
import io
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

# ================= SYSTEM SETUP =================
# Ép Terminal hiển thị Tiếng Việt chuẩn trên Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ================= CONFIG =================
URL_RAW = os.getenv('EVENT_URL', '')
URL_LIST = [u.strip() for u in URL_RAW.split(',') if u.strip()]
TG_TOKEN = os.getenv('TELEGRAM_TOKEN')
TG_ID = os.getenv('TELEGRAM_CHAT_ID')
RUN_ID = os.getenv('GITHUB_RUN_NUMBER', '0')
CURRENT_RUN_ID = os.getenv('GITHUB_RUN_ID', '0')
LOG_FILE = "history.json"
GH_TOKEN = os.getenv('GH_TOKEN')

MAINTENANCE_KEYWORDS = [
    "under maintenance", "system maintenance", "bảo trì", 
    "tạm đóng", "đang cập nhật", "come back later", "quay lại sau"
]

REAL_OPEN_HINTS = [
    "webpack", "bundle.js", "vue", "react", "vite", 
    "nuxt", "astro", "window.__INITIAL_STATE__", "__NUXT__", "__NEXT__"
]

# ================= UTILS =================
def get_vn_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)

def get_event_id(url):
    parsed = urlparse(url)
    domain = parsed.netloc.split('.')[0]
    path_code = ([p for p in parsed.path.split('/') if p] or ['event'])[0]
    return f"{domain}-{path_code}"

def get_url_hash(url_string):
    return hashlib.sha256(url_string.encode()).hexdigest()

def safe_get(url, retry=3):
    """Thực hiện request với cơ chế thử lại (Retry)"""
    headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"}
    for i in range(retry):
        try:
            res = requests.get(url, timeout=20, allow_redirects=True, headers=headers)
            return res
        except Exception as e:
            print(f"[*] Lỗi kết nối lần {i+1}: {e}")
            time.sleep(3)
    return None

def is_fake_200(html_content):
    """Xác minh thông minh sau khi Render"""
    if not html_content: return True
    content_lower = html_content.lower()

    # 1. Kiểm tra từ khóa bảo trì chắc chắn
    if any(key in content_lower for key in MAINTENANCE_KEYWORDS):
        return True
    
    # 2. Kiểm tra dấu hiệu Framework (SPA) -> Ưu tiên là trang thật
    if any(hint in content_lower for hint in REAL_OPEN_HINTS):
        return False

    # 3. Ngưỡng độ dài tối thiểu (120 byte theo review)
    if len(html_content) < 120:
        return True

    return False

def send_tg_safe(method, payload, files=None):
    """Gửi Telegram an toàn, không làm sập tiến trình chính"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    try:
        r = requests.post(url, data=payload, files=files, timeout=30)
        return r.status_code == 200
    except Exception as e:
        print(f"[!] Telegram Error: {e}")
        return False

# ================= FLEET & GIT CONTROL =================
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

def cleanup_older_runs():
    print(f"[*] Đang dọn dẹp hạm đội máy ảo song song...")
    gh_env = {**os.environ, "GH_TOKEN": GH_TOKEN}
    try:
        cmd = ["gh", "run", "list", "--workflow", "AOV Event Monitor", "--status", "in_progress", "--json", "databaseId"]
        res = subprocess.run(cmd, capture_output=True, text=True, env=gh_env)
        if res.returncode == 0:
            for r in json.loads(res.stdout):
                rid = str(r['databaseId'])
                if rid != CURRENT_RUN_ID:
                    subprocess.run(["gh", "run", "cancel", rid], env=gh_env, check=False)
    except: pass

def kill_entire_fleet():
    print(f"[!!!] NHIỆM VỤ HOÀN TẤT. TẮT HỆ THỐNG.")
    gh_env = {**os.environ, "GH_TOKEN": GH_TOKEN}
    try:
        subprocess.run(["gh", "workflow", "disable", "AOV Event Monitor"], env=gh_env, check=False)
        cleanup_older_runs()
    except: pass
    sys.exit(0)

def git_lock_and_check(ev_id):
    """KHÓA NGUYÊN TỬ: Đảm bảo duy nhất 1 phiên xử lý"""
    try:
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        history = {}
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r") as f: history = json.load(f)
        if history.get(ev_id, {}).get("archived"): return False, history

        history[ev_id] = {"status": 200, "archived": True, "time": get_vn_now().strftime('%Y-%m-%d %H:%M:%S'), "run": RUN_ID}
        success = git_sync_general(history, f"Lock {ev_id}")
        return success, history
    except: return False, {}

# ================= ARCHIVE CORE =================
def archive_event(url, ev_id):
    try:
        from playwright.sync_api import sync_playwright
        if os.path.exists(ev_id): shutil.rmtree(ev_id, ignore_errors=True)
        os.makedirs(ev_id, exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            # Random Viewport theo review
            v_port = random.choice([{'width': 375, 'height': 812}, {'width': 1366, 'height': 768}])
            context = browser.new_context(viewport=v_port, is_mobile=(v_port['width'] < 500))
            page = context.new_page()
            res_counter = [0]

            def handle_res(res):
                try:
                    u, ct = res.url, res.headers.get("content-type", "").lower()
                    if any(x in u for x in ["google", "analytics", "facebook"]): return
                    res_counter[0] += 1
                    name = u.split('/')[-1].split('?')[0] or "index"
                    
                    if "json" in ct or any(x in u.lower() for x in ['api', 'graphql', 'config']):
                        try: data = res.json()
                        except: data = {"raw": res.text()[:5000]} # Sửa lỗi crash JSON
                        fname = f"{res_counter[0]:02d}_api_{name}.json"
                        with open(os.path.join(ev_id, fname), "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=4, ensure_ascii=False)
                    elif any(ext in u.lower() for ext in ['.js', '.css', '.png', '.jpg', '.html']):
                        fname = f"{res_counter[0]:02d}_{name}"
                        with open(os.path.join(ev_id, fname), "wb") as f: f.write(res.body())
                except: pass

            page.on("response", handle_res)
            page.goto(url, wait_until="networkidle", timeout=90000)
            page.wait_for_timeout(10000) # Tối ưu render theo review
            
            final_content = page.content()
            if is_fake_200(final_content):
                print(f"[!] {ev_id} vẫn là bảo trì.")
                browser.close()
                return "MAINTENANCE"

            with open(os.path.join(ev_id, "00_rendered.html"), "w", encoding="utf-8") as f: f.write(final_content)
            page.screenshot(path=f"{ev_id}.png", full_page=True)
            browser.close()

        zip_path = shutil.make_archive(ev_id, 'zip', ev_id)
        caption = f"✅ SỰ KIỆN MỞ THẬT: {ev_id}\n⏰ {get_vn_now().strftime('%H:%M:%S %d/%m')}\n🔍 Node #{RUN_ID}"
        
        # Gửi Telegram an toàn
        send_tg_safe("sendPhoto", {"chat_id": TG_ID, "caption": caption}, {"photo": open(f"{ev_id}.png", 'rb')})
        send_tg_safe("sendDocument", {"chat_id": TG_ID}, {"document": open(f"{ev_id}.zip", 'rb')})
        
        return True
    except Exception as e:
        print(f"[!] Archive Error: {e}"); return False
    finally:
        # Cleanup file tạm theo review
        shutil.rmtree(ev_id, ignore_errors=True)
        for ext in [".png", ".zip"]:
            fpath = f"{ev_id}{ext}"
            if os.path.exists(fpath): os.remove(fpath)

# ================= RUN LOOP =================
def run():
    print(f"[*] Fleet Predator #{RUN_ID} Start...")
    # Cài đặt Playwright
    subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
    if shutil.which("npx"): # Kiểm tra npx theo review
        subprocess.run(["npx", "playwright", "install-deps", "chromium"], check=False)

    start_ts = time.time()
    while time.time() - start_ts < 19800:
        history = {}
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r") as f: history = json.load(f)
            except: pass

        # --- URGENT REFRESH LOGIC ---
        current_hash = get_url_hash(URL_RAW)
        if history.get("__metadata__", {}).get("url_hash") != current_hash:
            print("[!!!] CONFIG CHANGE -> RESET LOG")
            cleanup_older_runs()
            history = {"__metadata__": {"url_hash": current_hash}}
            git_sync_general(history, "Config Reset")
            continue 

        pending = [u for u in URL_LIST if not history.get(get_event_id(u), {}).get("archived")]
        if not pending and len(URL_LIST) > 0:
            kill_entire_fleet()
            return

        for url in pending:
            ev_id = get_event_id(url)
            res = safe_get(url) # Dùng safe_get có retry
            if res and res.status_code == 200:
                print(f"[*] {ev_id} -> 200 OK. Kiểm tra quyền...")
                is_winner, history = git_lock_and_check(ev_id)
                if is_winner:
                    result = archive_event(url, ev_id)
                    if result == "MAINTENANCE":
                        history[ev_id]["archived"] = False
                        git_sync_general(history, f"Unlock {ev_id} (Fake 200)")
                    elif result:
                        if all(history.get(get_event_id(u), {}).get("archived") for u in URL_LIST):
                            kill_entire_fleet()
                            return
        
        wait = random.randint(300, 600)
        print(f"[{get_vn_now().strftime('%H:%M:%S')}] Rình rập... Nghỉ {wait}s")
        time.sleep(wait)

if __name__ == "__main__":
    run()
