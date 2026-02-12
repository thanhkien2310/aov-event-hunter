import requests
import os
import time
import subprocess
import shutil
import random
import json
import hashlib
import re
import sys
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

def get_vn_now():
    return datetime.now(timezone.utc) + timedelta(hours=7)

def get_event_id(url):
    parsed = urlparse(url)
    domain = parsed.netloc.split('.')[0]
    path_code = ([p for p in parsed.path.split('/') if p] or ['event'])[0]
    # Rút gọn ID để tránh lỗi thư mục quá dài
    return f"{domain}-{path_code}"

def get_url_hash(url_string):
    return hashlib.sha256(url_string.encode()).hexdigest()

def is_actually_open(res):
    """Kiểm tra xem trang web có thực sự mở hay là trang bảo trì giả"""
    # 1. Nếu bị redirect về domain bảo trì hoặc path bảo trì
    if any(x in res.url.lower() for x in ["maintenance", "bảo-trì", "error"]):
        return False
    
    # 2. Kiểm tra nội dung html (chỉ các từ khóa chắc chắn là trang lỗi hệ thống)
    content = res.text.lower()
    system_errors = ["system is under maintainance", "403 forbidden", "access denied"]
    if any(err in content for err in system_errors):
        return False
        
    # 3. Trang 200 OK và không thuộc các trường hợp trên là trang tiềm năng
    return True

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
    """Vô hiệu hóa toàn bộ hạm đội ngay lập tức"""
    print(f"[!!!] HOÀN TẤT NHIỆM VỤ. ĐANG GIẢI TÁN MÁY ẢO...")
    gh_env = {**os.environ, "GH_TOKEN": GH_TOKEN}
    try:
        subprocess.run(["gh", "workflow", "disable", "AOV Event Monitor"], env=gh_env, check=False)
        # Hủy các run đang in_progress khác
        cmd = ["gh", "run", "list", "--workflow", "AOV Event Monitor", "--status", "in_progress", "--json", "databaseId"]
        result = subprocess.run(cmd, capture_output=True, text=True, env=gh_env)
        if result.returncode == 0:
            for r in json.loads(result.stdout):
                if str(r['databaseId']) != CURRENT_RUN_ID:
                    subprocess.run(["gh", "run", "cancel", str(r['databaseId'])], env=gh_env, check=False)
    except: pass
    os._exit(0)

def archive_event(url, ev_id):
    """Bóc tách dữ liệu bằng trình duyệt thật"""
    try:
        from playwright.sync_api import sync_playwright
        os.makedirs(ev_id, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 375, 'height': 812}, is_mobile=True)
            page = context.new_page()
            res_counter = [0]

            def handle_res(res):
                try:
                    u = res.url
                    if any(x in u for x in ["google", "analytics", "facebook", "doubleclick"]): return
                    res_counter[0] += 1
                    ct = res.headers.get("content-type", "").lower()
                    
                    # Đặt tên file dựa trên API hoặc Tài nguyên
                    ext = ".json" if "json" in ct else ".js" if "javascript" in ct else ".css" if "css" in ct else ""
                    fname = f"{res_counter[0]:03d}_{u.split('/')[-1].split('?')[0] or 'index'}{ext}"
                    
                    path = os.path.join(ev_id, fname)
                    if "json" in ct:
                        with open(path, "w", encoding="utf-8") as f:
                            json.dump(res.json(), f, indent=4, ensure_ascii=False)
                    else:
                        with open(path, "wb") as f: f.write(res.body())
                except: pass

            page.on("response", handle_res)
            page.goto(url, wait_until="networkidle", timeout=60000)
            time.sleep(15) # Đợi React đổ dữ liệu

            # Chụp ảnh và lưu DOM
            page.screenshot(path=f"{ev_id}.png", full_page=True)
            with open(os.path.join(ev_id, "rendered_view.html"), "w", encoding="utf-8") as f:
                f.write(page.content())
            browser.close()

        zip_file = shutil.make_archive(ev_id, 'zip', ev_id)
        caption = f" Ding Dong✨! {ev_id} ĐÃ MỞ\n⏰ {get_vn_now().strftime('%H:%M:%S %d/%m')}\n🔍 Run #{RUN_ID}"
        
        # Gửi Telegram
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data={"chat_id": TG_ID, "caption": caption}, files={'photo': open(f"{ev_id}.png", 'rb')})
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument", data={"chat_id": TG_ID}, files={'document': open(f"{ev_id}.zip", 'rb')})
        
        shutil.rmtree(ev_id, ignore_errors=True)
        return True
    except Exception as e:
        print(f"Lỗi Archive: {e}"); return False

def run():
    print(f"[*] Fleet Commander #{RUN_ID} khởi chạy múi giờ VN...")
    
    # CÀI ĐẶT 1 LẦN DUY NHẤT NGOÀI VÒNG LẶP
    print("[*] Đang chuẩn bị môi trường trình duyệt...")
    subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
    subprocess.run(["npx", "playwright", "install-deps"], check=False)

    start_ts = time.time()
    session = requests.Session() # Dùng Session để tăng tốc request
    session.headers.update({"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"})

    while time.time() - start_ts < 19800:
        history = {}
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r", encoding="utf-8") as f: history = json.load(f)
            except: pass

        # Kiểm tra Reset
        current_hash = get_url_hash(URL_RAW)
        if history.get("__metadata__", {}).get("url_hash") != current_hash:
            history = {"__metadata__": {"url_hash": current_hash}}
            git_sync_general(history, "Config Change: Resetting Hunter fleet")

        pending = [u for u in URL_LIST if not history.get(get_event_id(u), {}).get("archived")]
        if not pending and len(URL_LIST) > 0:
            kill_entire_fleet()
            return

        for url in pending:
            ev_id = get_event_id(url)
            try:
                # Kiểm tra nhạy bén (không chặn bừa bãi)
                res = session.get(url, timeout=20, allow_redirects=True)
                
                if res.status_code == 200 and is_actually_open(res):
                    print(f"[!] MỤC TIÊU XÁC NHẬN MỞ: {ev_id}")
                    
                    # Cập nhật lịch sử để khóa các phiên chạy khác
                    history[ev_id] = {"status": 200, "archived": True, "time": get_vn_now().strftime('%Y-%m-%d %H:%M:%S')}
                    if git_sync_general(history, f"Success: Captured {ev_id}"):
                        if archive_event(url, ev_id):
                            if all(history.get(get_event_id(u), {}).get("archived") for u in URL_LIST):
                                kill_entire_fleet()
                                return
            except Exception as e:
                print(f"Lỗi kết nối {ev_id}: {e}")

        # Nghỉ thông minh
        wait = random.randint(300, 600)
        print(f"[{get_vn_now().strftime('%H:%M:%S')}] Đang rình rập... Nghỉ {wait}s")
        time.sleep(wait)

if __name__ == "__main__":
    run()
