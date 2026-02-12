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
    "under maintenance",
    "system maintenance",
    "bảo trì hệ thống",
    "đang nâng cấp"
]

REAL_OPEN_HINTS = [
    "webpack",
    "bundle.js",
    "root",
    "vue",
    "react",
    "app"
]

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

# ================= FAKE 200 DETECTOR =================
def is_fake_200(html_content):
    if not html_content:
        return True

    content_lower = html_content.lower()

    # Keyword maintenance chắc chắn
    for key in MAINTENANCE_KEYWORDS:
        if key in content_lower:
            return True

    # SPA hint -> coi là mở
    for hint in REAL_OPEN_HINTS:
        if hint in content_lower:
            return False

    # HTML quá ngắn -> nghi ngờ
    if len(html_content) < 600:
        return True

    return False

# ================= GIT =================
def git_sync_general(data, message):
    try:
        subprocess.run(["git", "config", "user.name", "AOV-Hunter-Bot"])
        subprocess.run(["git", "config", "user.email", "bot@github.com"])
        subprocess.run(["git", "pull", "--rebase", "origin", "main"])
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        subprocess.run(["git", "add", LOG_FILE])
        subprocess.run(["git", "commit", "-m", message])
        push = subprocess.run(["git", "push"], capture_output=True)
        return push.returncode == 0
    except:
        return False

# ================= LOCK =================
def git_lock_and_check(ev_id):
    history = {}
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            history = json.load(f)

    if history.get(ev_id, {}).get("archived"):
        return False, history

    history[ev_id] = {
        "status": 200,
        "archived": True,
        "time": get_vn_now().strftime('%Y-%m-%d %H:%M:%S'),
        "by_run": RUN_ID
    }

    return git_sync_general(history, f"Run #{RUN_ID}: Lock {ev_id}"), history

# ================= ARCHIVE =================
def archive_event(url, ev_id):
    try:
        from playwright.sync_api import sync_playwright

        if not os.path.exists(ev_id):
            os.makedirs(ev_id)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 375, 'height': 812}, is_mobile=True)
            page = context.new_page()

            page.goto(url, wait_until="networkidle", timeout=90000)
            time.sleep(8)
            page.mouse.wheel(0, 3000)
            time.sleep(5)

            rendered_content = page.content()

            if is_fake_200(rendered_content):
                print(f"[!] {ev_id} vẫn maintenance sau render.")
                browser.close()
                return "MAINTENANCE"

            dom_text = page.inner_text("body")[:2000].lower()

            if any(x in dom_text for x in ["tham gia", "nhận quà", "đăng nhập"]):
                print("[✓] DOM xác nhận trang mở")
            else:
                print("[?] DOM chưa chắc chắn nhưng không phải maintenance")

            with open(os.path.join(ev_id, "view.html"), "w", encoding="utf-8") as f:
                f.write(rendered_content)

            page.screenshot(path=f"{ev_id}.png", full_page=True)
            browser.close()

        zip_path = shutil.make_archive(ev_id, 'zip', ev_id)

        caption = f"Sự kiện đã mở: {ev_id}"
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            data={"chat_id": TG_ID, "caption": caption},
            files={'photo': open(f"{ev_id}.png", 'rb')}
        )

        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
            data={"chat_id": TG_ID},
            files={'document': open(zip_path, 'rb')}
        )

        return True

    except Exception as e:
        print("Archive error:", e)
        return False

# ================= MAIN =================
def run():
    print("Bot start...")

    while True:
        for url in URL_LIST:
            ev_id = get_event_id(url)
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                res = requests.get(url, timeout=15, allow_redirects=True, headers=headers)

                if res.status_code == 200:
                    print(f"{ev_id} -> 200 detected -> render")

                    is_winner, history = git_lock_and_check(ev_id)
                    if is_winner:
                        subprocess.run(["python", "-m", "playwright", "install", "chromium"])
                        result = archive_event(url, ev_id)

                        if result == "MAINTENANCE":
                            history[ev_id]["archived"] = False
                            git_sync_general(history, f"Unlock {ev_id}")

                else:
                    print(f"{ev_id} -> status {res.status_code}")

            except Exception as e:
                print("Error:", e)

        time.sleep(random.randint(300, 600))

if __name__ == "__main__":
    run()
