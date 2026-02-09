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

MAINTENANCE_KEYWORDS = ["under maintainance", "maintainance", "maintenance", "bảo trì", "quay lại sau", "nâng cấp", "đang cập nhật"]

def get_vn_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)

def get_event_id(url):
    parsed = urlparse(url)
    domain = parsed.netloc.split('.')[0]
    path_code = ([p for p in parsed.path.split('/') if p] or ['event'])[0]
    suffix = hashlib.md5(url.encode()).hexdigest()[:4]
    return f"{domain}-{path_code}-{suffix}"

def get_url_hash(url_string):
    return hashlib.sha256(url_string.encode()).hexdigest()

def is_fake_200(html_content):
    if not html_content or len(html_content) < 800: return True
    content_lower = html_content.lower()
    for key in MAINTENANCE_KEYWORDS:
        if key in content_lower: return True
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

def cleanup_older_runs():
    """Cưỡng chế hủy tất cả các Run khác đang chạy để ưu tiên Run mới nhất"""
    print(f"[*] ĐANG DỌN DẸP CHIẾN TRƯỜNG: Hủy các tiến trình cũ...")
    gh_env = {**os.environ, "GH_TOKEN": GH_TOKEN}
    try:
        cmd = ["gh", "run", "list", "--workflow", "AOV Event Monitor", "--status", "in_progress", "--json", "databaseId"]
        result = subprocess.run(cmd, capture_output=True, text=True, env=gh_env)
        if result.returncode == 0:
            runs = json.loads(result.stdout)
            for r in runs:
                rid = str(r['databaseId'])
                if rid != CURRENT_RUN_ID:
                    print(f"[!] Cưỡng chế dừng Run: {rid}")
                    subprocess.run(["gh", "run", "cancel", rid], env=gh_env, check=False)
    except Exception as e:
        print(f"Lỗi Cleanup: {e}")

def kill_entire_fleet():
    """Nhiệm vụ hoàn tất: Vô hiệu hóa hạm đội vĩnh viễn"""
    print(f"[!!!] NHIỆM VỤ HOÀN TẤT. ĐANG GIẢI TÁN HẠM ĐỘI...")
    gh_env = {**os.environ, "GH_TOKEN": GH_TOKEN}
    try:
        cleanup_older_runs()
        subprocess.run(["gh", "workflow", "disable", "AOV Event Monitor"], env=gh_env, check=False)
        print("[*] Hạm đội đã giải tán.")
        os._exit(0)
    except: os._exit(0)

def git_lock_and_check(ev_id):
    history = {}
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f: history = json.load(f)
        except: pass
    if history.get(ev_id, {}).get("archived"): return False, history
    history[ev_id] = {"status": 200, "archived": True, "time": get_vn_now().strftime('%Y-%m-%d %H:%M:%S'), "by_run": RUN_ID}
    return git_sync_general(history, f"Run #{RUN_ID}: Lock {ev_id}"), history

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
                    u = res.url
                    if any(x in u for x in ["google", "analytics", "facebook"]): return
                    res_counter[0] += 1
                    ct = res.headers.get("content-type", "").lower()
                    clean_fname = (urlparse(u).path.split('/')[-1] or "index").split('?')[0]
                    save_name = f"{res_counter[0]:03d}_{clean_fname}"
                    if "javascript" in ct and not save_name.endswith(".js"): save_name += ".js"
                    elif "css" in ct and not save_name.endswith(".css"): save_name += ".css"
                    elif "json" in ct and not save_name.endswith(".json"): save_name += ".json"

                    raw_data = res.body()
                    file_path = os.path.join(ev_id, save_name)
                    if "json" in ct:
                        try:
                            with open(file_path, "w", encoding="utf-8") as f:
                                json.dump(json.loads(raw_data.decode('utf-8')), f, indent=4, ensure_ascii=False)
                        except:
                            with open(file_path, "wb") as f: f.write(raw_data)
                    else:
                        with open(file_path, "wb") as f: f.write(raw_data)
                except: pass

            page.on("response", handle_res)
            page.goto(url, wait_until="networkidle", timeout=90000)
            time.sleep(12)
            if is_fake_200(page.content()):
                browser.close()
                return "MAINTENANCE"

            page.screenshot(path=f"{ev_id}.png", full_page=True)
            with open(os.path.join(ev_id, "000_DOM.html"), "w", encoding="utf-8") as f: f.write(page.content())
            browser.close()

        zip_file = shutil.make_archive(ev_id, 'zip', root_dir=ev_id)
        caption = f"✅ ĐÓNG GÓI: {ev_id}\n⏰ {get_vn_now().strftime('%H:%M:%S %d/%m')}\n🔍 Node: Run #{RUN_ID}"
        with open(f"{ev_id}.png", 'rb') as f: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data={"chat_id": TG_ID, "caption": caption}, files={'photo': f})
        with open(f"{ev_id}.zip", 'rb') as f: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument", data={"chat_id": TG_ID}, files={'document': f})
        shutil.rmtree(ev_id, ignore_errors=True)
        for f in [f"{ev_id}.png", f"{ev_id}.zip"]:
            if os.path.exists(f): os.remove(f)
        return True
    except Exception as e:
        print(f"Archive Error {ev_id}: {e}"); return False

def run():
    print(f"[*] Fleet Commander #{RUN_ID} (High Authority Mode) khởi chạy...")
    subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
    
    start_ts = time.time()
    while time.time() - start_ts < 19800:
        history = {}
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f: history = json.load(f)
            except: pass

        # --- KIỂM TRA THAY ĐỔI CẤU HÌNH NGAY LẬP TỨC ---
        current_hash = get_url_hash(URL_RAW)
        last_hash = history.get("__metadata__", {}).get("url_hash")
        
        is_urgent = False
        if last_hash != current_hash:
            print("[!!!] PHÁT HIỆN THAY ĐỔI EVENT_URL. THIẾT LẬP QUYỀN ƯU TIÊN CAO NHẤT.")
            # Hủy các run khác ngay lập tức khi phát hiện đổi URL
            cleanup_older_runs()
            history = {"__metadata__": {"url_hash": current_hash}}
            git_sync_general(history, f"Run #{RUN_ID}: Global Configuration Change Detected")
            is_urgent = True

        pending = [u for u in URL_LIST if not history.get(get_event_id(u), {}).get("archived")]
        
        if not pending and len(URL_LIST) > 0:
            kill_entire_fleet()
            return

        # Thực thi quét danh sách
        for url in pending:
            ev_id = get_event_id(url)
            print(f"[{get_vn_now().strftime('%H:%M:%S')}] Đang quét: {ev_id}")
            try:
                res = requests.get(url, timeout=20, allow_redirects=True)
                if res.status_code == 200 and not is_fake_200(res.text):
                    is_winner, history = git_lock_and_check(ev_id)
                    if is_winner:
                        result = archive_event(url, ev_id)
                        if result == "MAINTENANCE":
                            history[ev_id]["archived"] = False
                            git_sync_general(history, f"Run #{RUN_ID}: Unlock {ev_id} (Maint)")
            except: pass

        # CƠ CHẾ REFRESH NHẠY BÉN:
        # Nếu đang ở chế độ khẩn cấp (vừa đổi URL), bỏ qua nghỉ ngẫu nhiên để quét tiếp đợt 2 ngay
        if is_urgent:
            print("[*] Đã xử lý đợt quét ưu tiên. Tiếp tục kiểm tra sát sao...")
            is_urgent = False
            continue 

        # Nếu không có gì thay đổi mới nghỉ ngẫu nhiên
        wait = random.randint(300, 600)
        print(f"[*] Chế độ theo dõi định kỳ. Nghỉ {wait}s...")
        time.sleep(wait)

if __name__ == "__main__":
    run()
