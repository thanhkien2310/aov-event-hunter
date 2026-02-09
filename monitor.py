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

MAINTENANCE_KEYWORDS = ["under maintainance", "maintainance", "maintenance", "bảo trì", "quay lại sau", "nâng cấp"]

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

def kill_entire_fleet():
    try:
        subprocess.run(["gh", "workflow", "disable", "AOV Event Monitor"], env={**os.environ, "GH_TOKEN": GH_TOKEN}, check=False)
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
    """Hàm thu thập tài nguyên chuẩn hóa (Fix lỗi định dạng file)"""
    try:
        from playwright.sync_api import sync_playwright
        # Dọn dẹp tuyệt đối thư mục cũ trước khi bắt đầu
        if os.path.exists(ev_id): shutil.rmtree(ev_id, ignore_errors=True)
        os.makedirs(ev_id, exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={'width': 375, 'height': 812}, 
                is_mobile=True,
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
            )
            page = context.new_page()
            res_counter = [0]

            def handle_res(res):
                try:
                    u = res.url
                    if "google-analytics" in u or "doubleclick" in u: return
                    
                    res_counter[0] += 1
                    ct = res.headers.get("content-type", "").lower()
                    
                    # Lấy tên tệp sạch sẽ
                    parsed_u = urlparse(u)
                    raw_fname = parsed_u.path.split('/')[-1] or "index"
                    clean_fname = raw_fname.split('?')[0].split('#')[0]
                    
                    # Đánh số thứ tự và đảm bảo tên tệp hợp lệ
                    save_name = f"{res_counter[0]:03d}_{clean_fname}"
                    
                    # Gán phần mở rộng nếu thiếu dựa trên content-type
                    if "javascript" in ct and not save_name.endswith(".js"): save_name += ".js"
                    elif "css" in ct and not save_name.endswith(".css"): save_name += ".css"
                    elif "json" in ct and not save_name.endswith(".json"): save_name += ".json"
                    elif "image" in ct and "." not in clean_fname: save_name += ".png"

                    # GIẢI PHÁP QUAN TRỌNG: Luôn lấy Body dạng Bytes để chống hỏng định dạng
                    raw_data = res.body()
                    
                    file_path = os.path.join(ev_id, save_name)
                    
                    # Xử lý đặc biệt cho JSON để dễ đọc (Pretty Print)
                    if "json" in ct:
                        try:
                            json_obj = json.loads(raw_data.decode('utf-8'))
                            with open(file_path, "w", encoding="utf-8") as f:
                                json.dump(json_obj, f, indent=4, ensure_ascii=False)
                        except:
                            # Nếu decode lỗi, lưu thô nhị phân luôn
                            with open(file_path, "wb") as f: f.write(raw_data)
                    else:
                        # CSS, JS, Ảnh... lưu dạng nhị phân nguyên bản (wb)
                        with open(file_path, "wb") as f:
                            f.write(raw_data)
                except:
                    pass

            page.on("response", handle_res)
            # Dùng wait_until="networkidle" để đảm bảo load hết CSS/JS ngoại vi
            page.goto(url, wait_until="networkidle", timeout=90000)
            time.sleep(10)

            final_html = page.content()
            if is_fake_200(final_html):
                browser.close()
                return "MAINTENANCE"

            # Lưu ảnh và file render cuối
            page.screenshot(path=f"{ev_id}.png", full_page=True)
            with open(os.path.join(ev_id, "000_DOM_RENDERED.html"), "w", encoding="utf-8") as f:
                f.write(final_html)
            
            browser.close()

        # Đóng gói ZIP: Chỉ đóng gói thư mục ev_id hiện tại
        # Sử dụng base_name và root_dir rõ ràng để tránh đóng gói thừa thư mục cha
        shutil.make_archive(ev_id, 'zip', root_dir=ev_id)
        zip_file = f"{ev_id}.zip"
        
        vn_time = get_vn_now().strftime('%H:%M:%S %d/%m')
        caption = f"✅ ĐÃ ĐÓNG GÓI SỰ KIỆN: {ev_id}\n⏰ {vn_time}\n🔍 Node: Run #{RUN_ID}"
        
        # Gửi Telegram
        with open(f"{ev_id}.png", 'rb') as photo:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto", data={"chat_id": TG_ID, "caption": caption}, files={'photo': photo})
        with open(zip_file, 'rb') as doc:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument", data={"chat_id": TG_ID}, files={'document': doc})
        
        # Dọn dẹp rác ngay sau khi gửi thành công
        shutil.rmtree(ev_id, ignore_errors=True)
        if os.path.exists(f"{ev_id}.png"): os.remove(f"{ev_id}.png")
        if os.path.exists(zip_file): os.remove(zip_file)
        return True
    except Exception as e:
        print(f"Archive Error {ev_id}: {e}")
        return False

def run():
    print(f"[*] Fleet Commander #{RUN_ID} (High Fidelity Mode) online.")
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
            print("[!] URL THAY ĐỔI -> RESET LOG.")
            history = {"__metadata__": {"url_hash": current_hash}}
            git_sync_general(history, f"Run #{RUN_ID}: Update URL hash")

        pending = [u for u in URL_LIST if not history.get(get_event_id(u), {}).get("archived")]
        if not pending and len(URL_LIST) > 0:
            kill_entire_fleet()
            return

        for url in pending:
            ev_id = get_event_id(url)
            print(f"[{get_vn_now().strftime('%H:%M:%S')}] Quét: {ev_id}")
            try:
                res = requests.get(url, timeout=20, allow_redirects=True)
                if res.status_code == 200 and not is_fake_200(res.text):
                    is_winner, history = git_lock_and_check(ev_id)
                    if is_winner:
                        result = archive_event(url, ev_id)
                        if result == "MAINTENANCE":
                            history[ev_id]["archived"] = False
                            git_sync_general(history, f"Run #{RUN_ID}: Unlock {ev_id} (Still Maint)")
                        elif result:
                            if all(history.get(get_event_id(u), {}).get("archived") for u in URL_LIST):
                                kill_entire_fleet()
            except Exception as e:
                print(f"Check Error {ev_id}: {e}")

        if is_url_changed:
            is_url_changed = False
            continue
            
        time.sleep(random.randint(300, 600))

if __name__ == "__main__":
    run()
