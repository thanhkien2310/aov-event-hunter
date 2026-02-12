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
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

# ================= CONFIG =================
URL_RAW = os.getenv('EVENT_URL', '')
URL_LIST = [u.strip() for u in URL_RAW.split(',') if u.strip()]
TG_TOKEN = os.getenv('TELEGRAM_TOKEN')
TG_ID = os.getenv('TELEGRAM_CHAT_ID')
RUN_ID = os.getenv('GITHUB_RUN_NUMBER', '0')
CURRENT_RUN_ID = os.getenv('GITHUB_RUN_ID', '0')
LOG_FILE = "history.json"
GH_TOKEN = os.getenv('GH_TOKEN')

MAINTENANCE_KEYWORDS = ["under maintenance", "system maintenance", "bảo trì hệ thống", "đang nâng cấp", "come back later"]
REAL_OPEN_HINTS = ["webpack", "bundle.js", "root", "vue", "react", "app", "tham gia", "nhận quà"]

# ================= UTIL =================
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
    if not html_content or len(html_content) < 800: return True
    content_lower = html_content.lower()
    for key in MAINTENANCE_KEYWORDS:
        if key in content_lower: return True
    for hint in REAL_OPEN_HINTS:
        if hint in content_lower: return False
    return False

# ================= GIT & FLEET CONTROL =================
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
    """Hủy toàn bộ các phiên đang chạy khác"""
    print(f"[*] Đang dọn dẹp các máy ảo song song...")
    gh_env = {**os.environ, "GH_TOKEN": GH_TOKEN}
    try:
        cmd = ["gh", "run", "list", "--workflow", "AOV Event Monitor", "--status", "in_progress", "--json", "databaseId"]
        result = subprocess.run(cmd, capture_output=True, text=True, env=gh_env)
        if result.returncode == 0:
            for r in json.loads(result.stdout):
                rid = str(r['databaseId'])
                if rid != CURRENT_RUN_ID:
                    subprocess.run(["gh", "run", "cancel", rid], env=gh_env, check=False)
    except: pass

def kill_entire_fleet():
    """Nhiệm vụ hoàn tất: Tắt workflow và đóng máy ảo"""
    print(f"[!!!] GIẢI TÁN HẠM ĐỘI...")
    gh_env = {**os.environ, "GH_TOKEN": GH_TOKEN}
    try:
        subprocess.run(["gh", "workflow", "disable", "AOV Event Monitor"], env=gh_env, check=False)
        cleanup_older_runs()
    except: pass
    os._exit(0)

# ================= ARCHIVE CORE =================
def archive_event(url, ev_id):
    try:
        from playwright.sync_api import sync_playwright
        if os.path.exists(ev_id): shutil.rmtree(ev_id, ignore_errors=True)
        os.makedirs(ev_id, exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 375, 'height': 812}, is_mobile=True)
            page = context.new_page()
            res_counter = [0]

            def handle_res(res):
                try:
                    u, ct = res.url, res.headers.get("content-type", "").lower()
                    if any(x in u for x in ["google", "analytics", "facebook"]): return
                    res_counter[0] += 1
                    
                    name = u.split('/')[-1].split('?')[0] or "index"
                    if any(x in u.lower() for x in ['api', 'graphql', 'config']) or "json" in ct:
                        data = res.json()
                        fname = f"{res_counter[0]:02d}_api_{name or 'gql'}.json"
                        with open(os.path.join(ev_id, fname), "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=4, ensure_ascii=False)
                    elif any(ext in u.lower() for ext in ['.js', '.css', '.png', '.jpg', '.html']):
                        fname = f"{res_counter[0]:02d}_{name}"
                        with open(os.path.join(ev_id, fname), "wb") as f: f.write(res.body())
                except: pass

            page.on("response", handle_res)
            page.goto(url, wait_until="networkidle", timeout=90000)
            time.sleep(15)

            if is_fake_200(page.content()):
                browser.close()
                return "MAINTENANCE"

            with open(os.path.join(ev_id, "00_rendered_view.html"), "w", encoding="utf-8") as f:
                f.write(page.content())
            page.screenshot(path=f"{ev_id}.png", full_page=True)
            browser.close()

        zip_path = shutil.make_archive(ev_id, 'zip', ev_id)
        vn_time = get_vn_now().strftime('%H:%M:%S %d/%m')
        caption = f"✨ Sự kiện mới: {ev_id}\n⏰ {vn_time}\n🔍 Node #{RUN_ID}"
        with open(f"{ev_id}.png", 'rb') as f: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data={"chat_id": TG_ID, "caption": caption}, files={'photo': f})
        with open(f"{ev_id}.zip", 'rb') as f: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument", data={"chat_id": TG_ID}, files={'document': f})
        shutil.rmtree(ev_id, ignore_errors=True)
        return True
    except Exception as e:
        print(f"Archive error: {e}"); return False

# ================= RUN LOOP =================
def run():
    print(f"[*] Fleet Commander #{RUN_ID} khởi chạy...")
    start_ts = time.time()
    
    while time.time() - start_ts < 19800:
        history = {}
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r") as f: history = json.load(f)
            except: pass

        # --- CƠ CHẾ NHẬN DIỆN THAY ĐỔI CẤU HÌNH NHANH ---
        current_hash = get_url_hash(URL_RAW)
        last_hash = history.get("__metadata__", {}).get("url_hash")
        
        is_urgent = False
        if last_hash != current_hash:
            print("[!!!] PHÁT HIỆN THAY ĐỔI EVENT_URL. RESET NHẬT KÝ...")
            cleanup_older_runs()
            history = {"__metadata__": {"url_hash": current_hash}}
            git_sync_general(history, "Config Change: Hunter Reset")
            is_urgent = True

        pending = [u for u in URL_LIST if not history.get(get_event_id(u), {}).get("archived")]
        
        if not pending and len(URL_LIST) > 0:
            kill_entire_fleet()
            return

        for url in pending:
            ev_id = get_event_id(url)
            try:
                headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}
                res = requests.get(url, timeout=20, allow_redirects=True, headers=headers)
                
                if res.status_code == 200 and not is_fake_200(res.text):
                    print(f"[!] {ev_id} ĐÃ MỞ!")
                    # Ghi danh khóa sự kiện trước khi làm
                    history[ev_id] = {"status": 200, "archived": True, "time": get_vn_now().strftime('%Y-%m-%d %H:%M:%S')}
                    if git_sync_general(history, f"Lock {ev_id}"):
                        subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
                        result = archive_event(url, ev_id)
                        if result == "MAINTENANCE":
                            history[ev_id]["archived"] = False
                            git_sync_general(history, f"Unlock {ev_id} (False Positive)")
                        elif result:
                            # Kiểm tra xem đã hết danh sách chưa
                            if all(history.get(get_event_id(u), {}).get("archived") for u in URL_LIST):
                                kill_entire_fleet()
                                return
            except: pass

        if is_urgent:
            print("[*] Đã xử lý đợt quét ưu tiên. Tiếp tục kiểm tra sát sao...")
            is_urgent = False
            continue 

        wait = random.randint(300, 600)
        print(f"[{get_vn_now().strftime('%H:%M:%S')}] Đang rình rập... Nghỉ {wait}s")
        time.sleep(wait)

if __name__ == "__main__":
    run()
