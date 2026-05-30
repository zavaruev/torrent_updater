import os
import time
import logging
import datetime
import signal
import sys
import threading
import concurrent.futures
import random
import subprocess
import re
import schedule
import uvicorn
from typing import Optional, List, Dict
from collections import deque

from dotenv import load_dotenv
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from transmission_rpc import Client
from dateparser import parse
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Memory buffer for UI logs
class LogBufferHandler(logging.Handler):
    def __init__(self, buffer):
        super().__init__()
        self.buffer = buffer
    
    def emit(self, record):
        msg = self.format(record)
        self.buffer.append(msg)

log_buffer = deque(maxlen=100)
logger.addHandler(LogBufferHandler(log_buffer))

# Status Management
class StatusManager:
    def __init__(self):
        self.status = "idle"
        self.last_check = None
        self.next_check = None
        self.total_updates = 0
        self.total_checks = 0
        self.history: List[Dict] = []
        self._current_cycle_torrents: set = set()  # tracks which torrents were seen in current cycle
        self._lock = threading.Lock()

    def update_status(self, status: str):
        with self._lock:
            self.status = status

    def try_start_check(self) -> bool:
        """Atomically checks if a check is already in progress and sets status to 'checking' if not."""
        with self._lock:
            if self.status == "checking" or self.status == "updating":
                return False
            self.status = "checking"
            self._current_cycle_torrents = set()  # reset per-cycle tracking
            return True

    def record_check(self):
        with self._lock:
            self.last_check = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def record_torrent_check(self, torrent: str, local_date: str, tracker_date: str, state: str, error_msg: str = ''):
        """Record result of a single torrent check.
        state: 'ok' (up-to-date), 'updated' (new version downloaded), 'error' (failed)
        Replaces any previous entry for the same torrent so history never has duplicates.
        """
        with self._lock:
            self.total_checks += 1
            if state == 'updated':
                self.total_updates += 1
            # Remove ALL previous entries for this torrent to avoid cross-cycle duplicates
            self.history = [h for h in self.history if h['torrent'] != torrent]
            self._current_cycle_torrents.add(torrent)
            self.history.insert(0, {
                "time": datetime.datetime.now().strftime("%H:%M:%S"),
                "torrent": torrent,
                "local_date": local_date,
                "tracker_date": tracker_date,
                "state": state,  # 'ok', 'updated', 'error'
                "error_msg": error_msg,
            })
            # Keep only last 100
            self.history = self.history[:100]

    def record_update(self, torrent: str, local_date: str, tracker_date: str, success: bool, error_msg: str = ''):
        """Legacy wrapper — kept for compatibility."""
        self.record_torrent_check(torrent, local_date, tracker_date, 'updated' if success else 'error', error_msg=error_msg)

status_manager = StatusManager()

# Configuration
LOGIN_RUTRACKER = os.getenv('LOGIN_RUTRACKER')
PASSWORD_RUTRACKER = os.getenv('PASSWORD_RUTRACKER')
TR_HOST = os.getenv('TR_HOST')
TR_PORT = int(os.getenv('TR_PORT', 9091))
TR_USER = os.getenv('TR_USER')
TR_PASSWORD = os.getenv('TR_PASSWORD')
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', '/home/user/Downloads/')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# Scheduling
CHECK_INTERVAL = int(os.getenv('CHECK_INTERVAL', '1'))
CHECK_INTERVAL_UNIT = os.getenv('CHECK_INTERVAL_UNIT', 'hours').lower() # 'minutes', 'hours', 'days'
RUN_ON_STARTUP = os.getenv('RUN_ON_STARTUP', 'true').lower() == 'true'

# FastAPI App
app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def read_item(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/api/status")
async def get_status():
    jobs = schedule.get_jobs()
    next_run = "N/A"
    if jobs:
        next_run = jobs[0].next_run.strftime("%Y-%m-%d %H:%M:%S")
    
    return {
        "status": status_manager.status,
        "last_check": status_manager.last_check,
        "next_check": next_run,
        "total_updates": status_manager.total_updates,
        "total_checks": status_manager.total_checks,
        "history": status_manager.history,
        "logs": list(log_buffer)
    }

@app.post("/api/check")
async def trigger_check():
    threading.Thread(target=check_and_update_torrents).start()
    return {"message": "Check triggered"}

def send_telegram_notification(
    torrent_name: str,
    torrent_url: str,
    local_date: str,
    tracker_date: str,
    success: bool,
    custom_message: str = ''
) -> None:
    """Sends a formatted Telegram notification about torrent update."""
    
    if custom_message:
        status_emoji = "✅"
        status_text = custom_message
    elif success:
        status_emoji = "✅"
        status_text = "Успешно обновлён"
    else:
        status_emoji = "❌"
        status_text = "Ошибка обновления"
    
    # Always log to console
    log_message = f"""
{'=' * 50}
{status_emoji} {status_text}
📥 Торрент: {torrent_name}
📅 Локальная дата: {local_date}
📅 Дата на трекере: {tracker_date}
🔗 URL: {torrent_url}
{'=' * 50}"""
    logger.info(log_message)
    
    # Check Telegram credentials
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    message = f"""
{status_emoji} <b>{status_text}</b>

📥 <b>Торрент:</b>
<code>{torrent_name}</code>

📅 <b>Даты:</b>
• Локальная: <code>{local_date}</code>
• Трекер: <code>{tracker_date}</code>

🔗 <a href="{torrent_url}">Открыть на трекере</a>
"""
    
    # TODO: раскомментировать когда настроишь Telegram
    # if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    #     return
    # try:
    #     url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    #     payload = {
    #         "chat_id": TELEGRAM_CHAT_ID,
    #         "text": message.strip(),
    #         "parse_mode": "HTML",
    #         "disable_web_page_preview": True
    #     }
    #     response = requests.post(url, json=payload, timeout=10)
    #     if response.status_code == 200:
    #         logger.info("Telegram notification sent successfully")
    #     else:
    #         logger.error(f"Failed to send Telegram notification: {response.text}")
    # except Exception as e:
    #     logger.error(f"Error sending Telegram notification: {e}")

def get_chrome_version() -> Optional[int]:
    """Detect installed Chrome major version."""
    try:
        result = subprocess.run(
            ['google-chrome', '--version'],
            capture_output=True, text=True, timeout=10
        )
        match = re.search(r'(\d+)\.', result.stdout)
        if match:
            ver = int(match.group(1))
            logger.info(f"Detected Chrome version: {ver}")
            return ver
    except Exception as e:
        logger.warning(f"Could not detect Chrome version: {e}")
    return None

def create_driver() -> Optional[uc.Chrome]:
    """Create an undetected Chrome WebDriver instance."""
    try:
        chrome_version = get_chrome_version()
        options = uc.ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--lang=ru-RU')
        kwargs = {'options': options, 'use_subprocess': True}
        if chrome_version:
            kwargs['version_main'] = chrome_version
        return uc.Chrome(**kwargs)
    except Exception as e:
        logger.error(f"Failed to create Chrome driver: {e}")
        return None

def create_session(login_username: str, login_password: str, max_retries: int = 3) -> Optional[uc.Chrome]:
    """Opens Chrome, logs into Rutracker, returns the authenticated driver."""
    for attempt in range(1, max_retries + 1):
        result = _try_login(login_username, login_password)
        if result is not None:
            return result
        if attempt < max_retries:
            wait_sec = random.uniform(10, 30)
            logger.warning(f"Login attempt {attempt}/{max_retries} failed. Retrying in {wait_sec:.0f}s...")
            time.sleep(wait_sec)
    logger.error(f"All {max_retries} login attempts failed.")
    return None

def _try_login(login_username: str, login_password: str) -> Optional[uc.Chrome]:
    """Single Chrome login attempt."""
    driver = None
    try:
        driver = create_driver()
        if not driver:
            return None

        logger.info("Opening Rutracker login page...")
        driver.get("https://rutracker.org/forum/login.php")

        # Step 1: Wait up to 90s for Cloudflare challenge to pass
        # (undetected-chromedriver handles JS challenge automatically, just needs time)
        deadline = time.time() + 90
        while time.time() < deadline:
            title = driver.title
            url = driver.current_url
            # 521 / 522 / 503 = server down, no point waiting
            if '521' in title or '522' in title or '503' in title:
                logger.warning(f"Server error page: '{title}'")
                return None
            # Login form appeared = challenge passed
            fields = driver.find_elements(By.NAME, 'login_username')
            if fields:
                logger.info(f"Login form found after {90 - int(deadline - time.time())}s | title: '{title}'")
                break
            time.sleep(2)
        else:
            logger.warning(f"Timed out waiting for login form | title: '{driver.title}' | URL: {driver.current_url}")
            return None

        username_field = driver.find_element(By.NAME, 'login_username')
        password_field = driver.find_element(By.NAME, 'login_password')
        login_btn = driver.find_element(By.NAME, 'login')

        # Verify this is the real Rutracker form, not a Cloudflare honeypot
        page_src = driver.page_source
        if 'rutracker.org' not in page_src.lower() or 'login_username' not in page_src:
            logger.warning("Form found but page doesn't look like Rutracker login")
            return None

        # JavaScript fill — bypasses Cloudflare overlays that block send_keys
        driver.execute_script("arguments[0].value = arguments[1]", username_field, login_username)
        driver.execute_script("arguments[0].value = arguments[1]", password_field, login_password)
        driver.execute_script("arguments[0].click()", login_btn)

        # Wait for redirect away from login page
        WebDriverWait(driver, 30).until(lambda d: 'login.php' not in d.current_url)
        logger.info(f"Logged into Rutracker | URL: {driver.current_url}")
        return driver

    except TimeoutException:
        logger.warning(f"Redirect timed out after login | URL: {driver.current_url if driver else '?'}")
        if driver:
            driver.quit()
        return None
    except Exception as e:
        logger.warning(f"Login attempt error: {e}")
        if driver:
            driver.quit()
        return None

def is_series_complete(text: str) -> bool:
    """Returns True if the text indicates a completed TV series season.
    Checks patterns like 'Серии: 1-8 из 8' or 'Episodes: 1-10 of 10'.
    Pass the rutracker page title for reliable detection — the local torrent
    name (file/folder) never contains episode counts.
    """
    # Russian pattern: Серии/Серия: X-Y из Y (case-insensitive, spaces around dash allowed)
    m = re.search(r'[Сс]ери[ия]:\s*\d+\s*-\s*(\d+)\s+из\s+(\d+)', text, re.IGNORECASE)
    if m and m.group(1) == m.group(2):
        return True
    # English pattern: Episodes: X-Y of Y  /  Ep. X-Y of Y (case-insensitive)
    m = re.search(r'[Ee]p(?:isodes?)?[.:]?\s*\d+\s*-\s*(\d+)\s+of\s+(\d+)', text, re.IGNORECASE)
    if m and m.group(1) == m.group(2):
        return True
    return False

def is_torrent_updated(url: str, torrent_date: datetime.datetime, session: uc.Chrome, max_retries: int = 3) -> tuple[bool, str, str, str, bool]:
    """Checks if the torrent on the tracker is newer than the local one. Retries on failure.
    Returns: (is_updated, local_date_str, tracker_date_str, error_msg, is_season_complete)
    """
    last_error = 'Неизвестная ошибка'
    for attempt in range(1, max_retries + 1):
        result = _try_check_torrent(url, torrent_date, session)
        is_updated, local_date_str, tracker_date_str, error_msg, is_season_complete = result
        last_error = error_msg
        if local_date_str or tracker_date_str:  # got a real result
            return result
        if attempt < max_retries:
            wait_sec = random.uniform(2, 5)
            logger.warning(f"Date fetch attempt {attempt}/{max_retries} failed for {url}. Retrying in {wait_sec:.0f}s...")
            time.sleep(wait_sec)
    return (False, "", "", last_error, False)

def _try_check_torrent(url: str, torrent_date: datetime.datetime, session: uc.Chrome) -> tuple[bool, str, str, str, bool]:
    """Returns: (is_updated, local_date_str, tracker_date_str, error_msg, is_season_complete)
    is_season_complete: True when the rutracker page title contains e.g. 'Серии: 1-8 из 8'
    """
    try:
        driver = session  # session IS the Chrome driver
        driver.get(url)

        # Wait for readyState then a bit more for JS rendering
        try:
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script('return document.readyState') == 'complete'
            )
        except TimeoutException:
            pass
        time.sleep(0.5)  # reduced extra wait for Rutracker JS

        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'lxml')
        title_text = soup.find('title')
        title_text = title_text.get_text(strip=True) if title_text else '?'

        # Quick check: are we still on the page and logged in?
        if 'login.php' in driver.current_url:
            logger.error(f"Session expired for {url}")
            return (False, "", "", "Сессия истекла", False)

        # If Cloudflare blocked the page
        if '521' in title_text or '503' in title_text or '522' in title_text:
            logger.warning(f"CF error on torrent page: {title_text} | {url}")
            return (False, "", "", f"CF {title_text[:30]}", False)

        # Detect season completion from the rutracker page title
        # The title contains the full torrent name from rutracker, e.g.
        # "The Boys (Season 5) WEB-DL 1080p [Серии: 1-8 из 8]"
        season_complete = is_series_complete(title_text)

        date_str = None

        # Strategy 1: span.posted_since (most common)
        el = soup.find('span', {'class': 'posted_since hide-for-print'})
        if el and el.get_text(strip=True):
            date_str = el.get_text(strip=True)

        # Strategy 2: any span containing 'posted_since'
        if not date_str:
            for span in soup.find_all('span', class_=True):
                if 'posted_since' in ' '.join(span.get('class', [])):
                    text = span.get_text(strip=True)
                    if text:
                        date_str = text
                        break

        # Strategy 3: p-link small anchor text
        if not date_str:
            date_link = soup.find('a', {'class': 'p-link small'})
            if date_link and date_link.get_text(strip=True):
                date_str = date_link.get_text(strip=True)

        # Strategy 4: look for any element with a date-looking pattern near the title
        if not date_str:
            # Look for Russian month names or year patterns in the page
            date_pattern = re.compile(
                r'\d{1,2}[- ](Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec'
                r'|янв|фев|мар|апр|мая|апр|июн|июл|авг|сен|окт|ноя|дек)[- ]\d{2,4}',
                re.IGNORECASE
            )
            for tag in soup.find_all(string=date_pattern):
                m = date_pattern.search(tag)
                if m:
                    date_str = m.group(0)
                    break

        if not date_str:
            snippet = soup.get_text()[:200].replace('\n', ' ').strip()
            logger.warning(f"Дата не найдена | title: '{title_text}' | {url} | snippet: {snippet[:150]}")
            return (False, "", "", f"Дата не найдена | {title_text[:40]}", season_complete)

        date_str = date_str.replace(')', '').replace('(', '').strip()
        if 'ред' in date_str:
            date_str = date_str.split('ред. ')[-1].strip()
        # Remove extra qualifiers like 'скачан' etc.
        date_str = date_str.split(',')[0].strip()
            
        tracker_date_obj = parse(date_str, settings={'TIMEZONE': 'UTC', 'RETURN_AS_TIMEZONE_AWARE': True})
        
        if not tracker_date_obj:
            logger.warning(f"Could not parse date string: '{date_str}'")
            return (False, "", "", f"Не удалось разобрать дату: {date_str[:30]}", season_complete)

        local_date_str = torrent_date.date().strftime('%Y-%m-%d')
        tracker_date_str = tracker_date_obj.date().strftime('%Y-%m-%d')

        is_updated = tracker_date_obj.date() > torrent_date.date()
        if season_complete:
            logger.info(f"Season complete detected from rutracker title: '{title_text[:80]}'")
        return (is_updated, local_date_str, tracker_date_str, '', season_complete)

    except Exception as e:
        logger.error(f"Error checking update for {url}: {e}")
        return (False, "", "", f"Исключение: {str(e)[:60]}", False)

def download_and_add_torrent(url: str, session: uc.Chrome, to_dir: str, tr: Client, max_retries: int = 3) -> bool:
    """Downloads the torrent file using the authenticated Chrome driver and adds it to Transmission."""
    import tempfile, glob
    driver = session

    for attempt in range(1, max_retries + 1):
        try:
            driver.get(url)
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script('return document.readyState') == 'complete'
            )

            if 'login.php' in driver.current_url:
                logger.error("Session expired during download attempt")
                return False

            soup = BeautifulSoup(driver.page_source, 'lxml')
            download_link = soup.find('a', href=lambda h: h and 'dl.php?t=' in h)
            if not download_link:
                logger.warning(f"Could not find download link for {url} (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(random.uniform(5, 15))
                    continue
                return False

            download_url = download_link['href']
            if not download_url.startswith('http'):
                download_url = 'https://rutracker.org/forum/' + download_url.lstrip('/')

            logger.info(f"Downloading .torrent from {download_url}")

            # Use requests with cookies from the driver to download the file
            cookies = {c['name']: c['value'] for c in driver.get_cookies()}
            headers = {'User-Agent': driver.execute_script('return navigator.userAgent')}
            torrent_resp = requests.get(download_url, cookies=cookies, headers=headers, timeout=30)

            if torrent_resp.status_code != 200:
                logger.warning(f"Torrent download returned {torrent_resp.status_code} (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(random.uniform(5, 15))
                    continue
                return False

        except Exception as e:
            logger.warning(f"Download attempt {attempt}/{max_retries} error: {e}")
            if attempt < max_retries:
                time.sleep(random.uniform(5, 15))
                continue
            return False

    logger.error(f"All {max_retries} download attempts failed for {url}")
    return False

def main():
    logger.info("Starting torrent-updater service...")
    
    # Handle termination signals
    def signal_handler(sig, frame):
        logger.info("Termination signal received. Exiting...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Schedule the job
    if CHECK_INTERVAL_UNIT == 'minutes':
        schedule.every(CHECK_INTERVAL).minutes.do(check_and_update_torrents)
    elif CHECK_INTERVAL_UNIT == 'days':
        schedule.every(CHECK_INTERVAL).days.do(check_and_update_torrents)
    else: # Default to hours
        schedule.every(CHECK_INTERVAL).hours.do(check_and_update_torrents)

    logger.info(f"Scheduled check every {CHECK_INTERVAL} {CHECK_INTERVAL_UNIT}")

    # Start Web Server in a separate thread
    def run_web_server():
        logger.info("Starting web server on port 6050...")
        uvicorn.run(app, host="0.0.0.0", port=6050, log_level="warning")

    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    # Run once on startup if enabled
    if RUN_ON_STARTUP:
        logger.info("Running initial check on startup...")
        # Run in thread to not block scheduler startup
        threading.Thread(target=check_and_update_torrents).start()

    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    # Rename original main to check_and_update_torrents for better clarity in scheduling
    def check_and_update_torrents():
        if not status_manager.try_start_check():
            logger.warning("Check already in progress, skipping...")
            return

        logger.info("--- Starting Check Cycle ---")
        status_manager.record_check()

        if not all([LOGIN_RUTRACKER, PASSWORD_RUTRACKER, TR_HOST, TR_PORT, TR_USER, TR_PASSWORD]):
            logger.error("Missing configuration. Please check .env file.")
            status_manager.update_status("idle")
            return

        try:
            tr = Client(host=TR_HOST, port=TR_PORT, username=TR_USER, password=TR_PASSWORD)
            logger.info(f"Connected to Transmission at {TR_HOST}:{TR_PORT}")
        except Exception as e:
            logger.error(f"Failed to connect to Transmission: {e}")
            status_manager.update_status("idle")
            return

        session = create_session(LOGIN_RUTRACKER, PASSWORD_RUTRACKER)
        if not session:
            # All attempts exhausted — retry in 15 min instead of waiting the full scheduled cycle
            logger.warning("Login failed completely. Will retry in 15 minutes.")
            status_manager.update_status("idle")
            def _retry_soon():
                time.sleep(15 * 60)
                check_and_update_torrents()
            threading.Thread(target=_retry_soon, daemon=True).start()
            return

        try:
            torrents = tr.get_torrents()
            rutracker_torrents = [
                t for t in torrents
                if t.percent_complete == 1
                and t.comment
                and 'rutracker.org' in t.comment
            ]
            logger.info(f"Checking {len(rutracker_torrents)} torrents...")

            # Note: season completion is now detected from the rutracker page title
            # during process_torrent(), so no pre-filtering by local name needed here.

            def process_torrent(torrent):
                torrent_url = torrent.comment
                torrent_date_ts = getattr(torrent, 'date_created', getattr(torrent, 'dateCreated', 0))
                if not torrent_date_ts:
                    torrent_date_ts = getattr(torrent, 'added_date', 0)
                if isinstance(torrent_date_ts, datetime.datetime):
                    torrent_date = torrent_date_ts
                else:
                    torrent_date = datetime.datetime.fromtimestamp(int(torrent_date_ts))

                is_updated, local_date_str, tracker_date_str, error_msg, is_season_complete = is_torrent_updated(torrent_url, torrent_date, session)

                if not local_date_str and not tracker_date_str:
                    status_manager.record_torrent_check(torrent.name, '?', '?', 'error', error_msg=error_msg)
                    return None

                if is_updated:
                    if is_season_complete:
                        # Final episode just dropped — download it first, THEN remove on next cycle
                        logger.info(f"Season complete + update available: will download final episode first: {torrent.name}")
                        return ('update', torrent, torrent_url, local_date_str, tracker_date_str)
                    else:
                        return ('update', torrent, torrent_url, local_date_str, tracker_date_str)
                else:
                    if is_season_complete:
                        # Already have all episodes locally — safe to stop tracking
                        return ('season_complete', torrent, torrent_url, local_date_str, tracker_date_str)
                    logger.info(f"Up-to-date: {torrent.name} (local: {local_date_str}, tracker: {tracker_date_str})")
                    status_manager.record_torrent_check(torrent.name, local_date_str, tracker_date_str, 'ok')
                    return None

            results = []
            for torrent in rutracker_torrents:
                results.append(process_torrent(torrent))

            # Handle completed seasons — remove from Transmission without re-downloading
            for r in results:
                if r is not None and r[0] == 'season_complete':
                    _, torrent, torrent_url, local_date_str, tracker_date_str = r
                    try:
                        tr.remove_torrent(torrent.id)
                        status_manager.record_torrent_check(torrent.name, local_date_str, tracker_date_str, 'ok',
                            error_msg='Сезон завершён, удалён из Transmission')
                        send_telegram_notification(
                            torrent_name=torrent.name,
                            torrent_url=torrent_url,
                            local_date=local_date_str,
                            tracker_date=tracker_date_str,
                            success=True,
                            custom_message='🏁 Сезон завершён, сериал скачан полностью'
                        )
                    except Exception as e:
                        logger.error(f"Failed to remove completed season {torrent.name}: {e}")

            # Filter results that need updating
            updates_to_perform = [r[1:] for r in results if r is not None and r[0] == 'update']
            
            for torrent, torrent_url, local_date_str, tracker_date_str in updates_to_perform:
                logger.info(f"Update available for {torrent.name}. Updating...")
                status_manager.update_status("updating")

                try:
                    # Download new torrent FIRST — only remove old one if download succeeds
                    if download_and_add_torrent(torrent_url, session, torrent.download_dir, tr):
                        tr.remove_torrent(torrent.id)
                        logger.info("Update successful.")
                        status_manager.record_update(torrent.name, local_date_str, tracker_date_str, True)
                        send_telegram_notification(
                            torrent_name=torrent.name,
                            torrent_url=torrent_url,
                            local_date=local_date_str,
                            tracker_date=tracker_date_str,
                            success=True
                        )
                    else:
                        logger.error("Failed to download new torrent — old torrent kept intact.")
                        status_manager.record_update(torrent.name, local_date_str, tracker_date_str, False)
                        send_telegram_notification(
                            torrent_name=torrent.name,
                            torrent_url=torrent_url,
                            local_date=local_date_str,
                            tracker_date=tracker_date_str,
                            success=False
                        )
                except Exception as e:
                    logger.error(f"Error during update process: {e}")
                    status_manager.record_update(torrent.name, local_date_str, tracker_date_str, False)
                    send_telegram_notification(
                        torrent_name=torrent.name,
                        torrent_url=torrent_url,
                        local_date=local_date_str,
                        tracker_date=tracker_date_str,
                        success=False
                    )

                status_manager.update_status("checking")

        except Exception as e:
            logger.error(f"Error in check loop: {e}")
        finally:
            # Always close Chrome driver to free resources
            try:
                session.quit()
            except Exception:
                pass
            status_manager.update_status("idle")
            logger.info("--- Check Cycle Finished ---")

    main()
