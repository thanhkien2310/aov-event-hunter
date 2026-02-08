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
CURRENT_RUN_ID = os.getenv('GITHUB_RUN_ID', '0') # ID định danh của máy ảo hiện tại
LOG_FILE = "history.json"
GH_TOKEN = os.getenv('GH_TOKEN')

def get_vn_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)

def get_event_id(url):
    parsed = urlparse(url)
    domain = parsed.netloc.split('.')[0]
    path_code = ([p for p in parsed.path.split('/') if p] or ['event'])[0]
    return f"{domain}-{path_code}"

def get_url_hash(url_string):
    return hashlib.sha256(url_string.encode()).hexdigest()

def kill_fleet():
    """Lệnh tối thượng: Vô hiệu hóa Workflow và Hủy toàn bộ các máy ảo đang chạy khác"""
    print(f"[!!!] SỰ KIỆN HOÀN TẤT. ĐANG THỰC THI LỆNH TRUY SÁT TOÀN HỆ THỐNG...")
    try:
        # 1. Tắt vĩnh viễn Workflow
        subprocess.run(["gh", "workflow", "disable", "AOV Event Monitor"], env={**os.environ, "GH_TOKEN": GH_TOKEN}, check=False)
        
        # 2. Lấy danh sách tất cả các phiên đang chạy (in_progress)
        cmd_list = ["gh", "run", "list", "--workflow", "AOV Event Monitor", "--status", "in_progress", "--json", "databaseId"]
        result = subprocess.run(cmd_list, capture_output=True, text=True, env={**os.environ, "GH_TOKEN": GH_TOKEN})
        
        if result.returncode == 0:
            runs = json.loads(result.stdout)
            for run in runs:
                other_run_id = str(run['databaseId'])
                if other_run_id != CURRENT_RUN_ID:
                    print(f"[*] Đang hủy máy ảo song song: {other_run_id}")
                    subprocess.run(["gh", "run", "cancel", other_run_id], env={**os.environ, "GH_TOKEN": GH_TOKEN}, check=False)
        
        print("[+] Đã dọn dẹp xong. Tạm biệt!")
        os._exit(0) # Đóng máy ảo hiện tại ngay lập tức
    except Exception as e:
        print(f"Lỗi khi thực thi lệnh hủy: {e}")
        os._exit(0)

def git_lock_and_check(ev_id):
    """Cơ chế khóa để giành quyền đóng gói"""
    try:
        subprocess.run(["git", "config", "user.name", "AOV-Hunter-Bot"], check=False)
        subprocess.run(["git", "config", "user.email", "bot@github.com"], check=False)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
        
        history = {}
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r") as f: history = json.load(f)
        
        if history.get(ev_id, {}).get("archived") is True:
            return False, history

        history[ev_id] = {"status": 200, "archived": True, "time": get_vn_now().strftime('%Y-%m-%d %H:%M:%S'), "locked_by": RUN_ID}
        with open(LOG_FILE, "w", encoding="utf-8") as f: json.dump(history, f, indent=4)
        
        subprocess.run(["git", "add", LOG_FILE], check=False)
        subprocess.run(["git", "commit", "-m", f"Run #{RUN_ID}: Lock {ev_id}"], check=False)
        push = subprocess.run(["git", "push"], capture_output=True)
        return (push.returncode == 0), history
    except: return False, {}

def archive_event(url, ev_id):
    """Đóng gói và gửi báo cáo"""
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
                        with open(os.path.join(ev_id, f"{res_counter[0]:02d}_api.json"), "w", encoding="utf-8") as f:
                            json.dump(res.json(), f, indent=4, ensure_ascii=False)
                    elif any(ext in u.lower() for ext in ['.js', '.css', '.png', '.jpg', '.html']):
                        fname = u.split('/')[-1].split('?')[0] or f"file_{res_counter[0]}"
                        with open(os.path.join(ev_id, fname), "wb") as f: f.write(res.body())
                except: pass
            page.on("response", handle_res)
            page.goto(url, wait_until="networkidle", timeout=60000)
            time.sleep(15)
            with open(os.path.join(ev_id, "rendered_view.html"), "w", encoding="utf-8") as f: f.write(page.content())
            page.screenshot(path=f"{ev_id}.png", full_page=True)
            browser.close()
        zip_path = shutil.make_archive(ev_id, 'zip', ev_id)
        vn_time = get_vn_now().strftime('%H:%M:%S %d/%m')
        caption = f"Ding Dong✨, Sự kiện đã bắt đầu! {ev_id}\n⏰ Phát hiện: {vn_time}\n🔍 Bởi: Run #{RUN_ID}"
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data={"chat_id": TG_ID, "caption": caption}, files={'photo': open(f"{ev_id}.png", 'rb')})
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument", data={"chat_id": TG_ID}, files={'document': open(zip_path, 'rb')})
        return True
    except: return False

def run():
    print(f"[*] Phiên làm việc #{RUN_ID} (System ID: {CURRENT_RUN_ID}) đang trực chiến...")
    start_ts = time.time()
    
    while time.time() - start_ts < 19800:
        history = {}
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r") as f: history = json.load(f)
            except: pass

        if history.get("__metadata__", {}).get("url_hash") != get_url_hash(URL_RAW):
            history = {"__metadata__": {"url_hash": get_url_hash(URL_RAW)}}
            with open(LOG_FILE, "w") as f: json.dump(history, f)

        pending = [u for u in URL_LIST if not history.get(get_event_id(u), {}).get("archived")]

        if not pending and len(URL_LIST) > 0:
            kill_fleet()
            return

        for url in pending:
            ev_id = get_event_id(url)
            try:
                headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}
                res = requests.get(url, timeout=15, allow_redirects=True, headers=headers)
                if res.status_code == 200 and "/maintenance" not in res.url.lower():
                    is_winner, history = git_lock_and_check(ev_id)
                    if is_winner:
                        print(f"[!] THẮNG CUỘC! Run #{RUN_ID} thực thi đóng gói.")
                        subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
                        archive_event(url, ev_id)
                        # Kiểm tra lại sau khi đóng gói
                        if all(history.get(get_event_id(u), {}).get("archived") for u in URL_LIST):
                            kill_fleet()
                            return
            except: pass
        
        time.sleep(random.randint(300, 600))

if __name__ == "__main__":
    run()