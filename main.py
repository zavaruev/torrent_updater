"""Torrent auto-updater: Rutracker + Transmission, FastAPI UI on :6050.

UNFINISHED (Cloudflare wall, Sep 2026) — read before touching login/search:
  1. Bot Chrome gets an interactive Turnstile checkbox on topic/tracker pages
     and the backend SILENTLY rejects the click (box stays empty). Clicks land
     pixel-perfect (verified via screenshots), mouse moves human-like — still
     rejected. Score factors: datacenter egress IP + automation fingerprint.
     Fixed so far: WebGL (SwiftShader), chrome.runtime stub, deviceMemory->8,
     headed 1920x1080, human mouse. See _cf_point_click() for the full story.
  2. Session cookies (bb_session/bb_t) are VALID (proven over plain HTTP with
     the same egress IP: index returns authed content). But the bot browser is
     treated as guest on strict paths even with them. Session-only requests
     work for index.php; viewtopic/tracker/search/dl.php need CF clearance.
  3. cf_clearance is SHORT-LIVED (rotates <~1h). It must be fresh-minutes AND
     a singleton in the jar: duplicates (site re-issues its own copy when it
     rejects ours) make the server read the wrong one. See _try_cookie_login().
  4. search_tracker() + query-word filter + tracker table parser are CODED
     (see movie-recommender/) but have NEVER passed E2E — blocked by (1).
     First green signal to watch for: 'Login form found' / tracker results.
  5. What WORKED and must keep working: cookie session restore on index.php
     ('Cookie session restored'), date-check parsing, season-complete removal,
     download-before-delete update order, UI/API/recommendations.
  6. Do NOT hammer: bursts escalate CF strictness. One session at a time,
     20-30s pacing between navigations (already in check cycle).
"""

import os
import time
import logging
import datetime
import signal
import sys
sys.path.insert(0, "/opt/data/scripts/movie-recommender")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "movie-recommender"))
import threading
import concurrent.futures
import random
import re
import schedule
import uvicorn
import base64
from typing import Optional, List, Dict
from collections import deque

from dotenv import load_dotenv
from seleniumbase import SB
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from transmission_rpc import Client
from dateparser import parse
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# Recommender is optional: daily recommendations are scheduled only if it imports.
# A hard import here would kill the whole updater (incl. Transmission updates)
# on systems without the recommender on sys.path.
try:
    from recommender import run_daily_recommendation as run_daily_recommendations
    RECOMMENDER_AVAILABLE = True
except Exception as _rec_err:
    run_daily_recommendations = None
    RECOMMENDER_AVAILABLE = False
    _rec_import_error = str(_rec_err)
# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
if not RECOMMENDER_AVAILABLE:
    logger.warning(f"Movie recommender unavailable, daily recommendations disabled: {_rec_import_error}")

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

    def record_torrent_check(self, torrent: str, local_date: str, tracker_date: str, state: str, error_msg: str = '', torrent_url: str = ''):
        """Records a torrent check result in history.
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
                "torrent_url": torrent_url,
                "local_date": local_date,
                "tracker_date": tracker_date,
                "state": state,  # 'ok', 'updated', 'error'
                "error_msg": error_msg,
            })
            # Keep only last 100
            self.history = self.history[:100]

    def record_update(self, torrent: str, local_date: str, tracker_date: str, success: bool, error_msg: str = '', torrent_url: str = ''):
        """Legacy wrapper — kept for compatibility."""
        self.record_torrent_check(torrent, local_date, tracker_date, 'updated' if success else 'error', error_msg=error_msg, torrent_url=torrent_url)

status_manager = StatusManager()

# Configuration
LOGIN_RUTRACKER = os.getenv('LOGIN_RUTRACKER')
PASSWORD_RUTRACKER = os.getenv('PASSWORD_RUTRACKER')
TR_HOST = os.getenv('TR_HOST')
TR_PORT = int(os.getenv('TR_PORT', 9091))
TR_USER = os.getenv('TR_USER')
TR_PASSWORD = os.getenv('TR_PASSWORD')
DOWNLOAD_DIR = os.getenv('DOWNLOAD_DIR', '/downloads/')
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

@app.get("/api/recommendations")
async def get_recommendations():
    """Get cached movie/series recommendations.

    Entries already downloaded (added=true) are dropped entirely — they are
    visible in Jellyfin as fresh arrivals, no need to show them here.
    """
    import json
    from pathlib import Path

    cache_file = Path("/opt/data/cache/movie_recommendations.json")
    if cache_file.exists():
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data['movies'] = [m for m in data.get('movies', []) if not m.get('added')]
            data['series'] = [s for s in data.get('series', []) if not s.get('added')]
            return data
        except Exception as e:
            logger.error("Failed to read recommendations cache: " + str(e))

    return {"movies": [], "series": [], "timestamp": None}

@app.post("/api/add-torrent")
async def add_torrent(request: Request):
    """Add torrent from Rutracker URL to Transmission."""
    from transmission_rpc import Client
    import requests
    import re
    
    try:
        data = await request.json()
        rutracker_url = data.get('url')
        content_type = data.get('type', 'movie')
        
        if not rutracker_url:
            return {"success": False, "error": "URL is required"}
        
        download_dir = "/movies" if content_type == 'movie' else "/series"
        labels = ["movie", "auto"] if content_type == 'movie' else ["series", "auto"]
        
        match = re.search(r't=(\d+)', rutracker_url)
        if not match:
            return {"success": False, "error": "Invalid Rutracker URL"}
        
        topic_id = match.group(1)
        download_url = "https://rutracker.org/forum/dl.php?t=" + topic_id
        
        login_len = len(LOGIN_RUTRACKER)
        pass_len = len(PASSWORD_RUTRACKER)
        cookies = {
            'bb_data': 'a%3A2%3A%7Bs%3A11%3A%22login_username%22%3Bs%3A' + str(login_len) + '%3A%22' + LOGIN_RUTRACKER + '%22%3Bs%3A11%3A%22login_password%22%3Bs%3A' + str(pass_len) + '%3A%22' + PASSWORD_RUTRACKER + '%22%3B%7D'
        }
        
        resp = requests.get(
            download_url,
            cookies=cookies,
            headers={'Referer': rutracker_url},
            timeout=30
        )
        
        if resp.status_code != 200 or not resp.headers.get('Content-Type', '').startswith('application/x-bittorrent'):
            return {"success": False, "error": "Failed to download torrent: " + str(resp.status_code)}
        
        tr = Client(host=TR_HOST, port=TR_PORT, username=TR_USER, password=TR_PASSWORD)
        try:
            torrent = tr.add_torrent(
                torrent=resp.content,
                download_dir=download_dir,
                paused=False,
                labels=labels
            )
        except TypeError:
            # Older transmission-rpc without `labels` support
            torrent = tr.add_torrent(
                torrent=resp.content,
                download_dir=download_dir,
                paused=False
            )
        
        logger.info("Added " + content_type + " torrent: " + torrent.name + " (ID: " + str(torrent.id) + ") to " + download_dir)
        return {"success": True, "torrent_id": torrent.id, "name": torrent.name}
        
    except Exception as e:
        logger.error("Failed to add torrent: " + str(e))
        return {"success": False, "error": str(e)}


def _search_and_download_blocking(query: str, content_type: str, season, imdb_id, verify: bool) -> dict:
    """Blocking browser work for /api/search-and-download. Runs in a thread,
    never directly in the FastAPI event loop (SeleniumBase breaks otherwise)."""
    from seleniumbase import SB
    from search_and_download import (
        search_best_torrent,
        download_url_and_add_to_transmission,
        verify_jellyfin,
        detect_content_kind,
    )
    from rutracker_scraper import RutrackerScraper

    login_username = os.environ.get('LOGIN_RUTRACKER')
    login_password = os.environ.get('PASSWORD_RUTRACKER')
    tr_host = os.environ.get('TR_HOST')
    tr_port = int(os.environ.get('TR_PORT', 9091))
    tr_user = os.environ.get('TR_USER')
    tr_password = os.environ.get('TR_PASSWORD')

    with SB(uc=True, chromium_arg="--enable-unsafe-swiftshader") as sb:
        driver = create_session(sb, login_username, login_password)
        if not driver:
            return {"success": False, "error": "Failed to login to Rutracker"}

        # Single shared browser session (a second concurrent login triggers
        # Cloudflare strictness). Settle before searching (human pace).
        time.sleep(15)
        scraper = RutrackerScraper.attach(sb, login_username, login_password)
        try:
            best = search_best_torrent(query, content_type == 'series', season, imdb_id, scraper=scraper)
        except Exception as e:
            logger.warning(f"Search failed: {e}")
            return {"success": False, "error": f"No results found: {e}"}

        # Route by detected content kind (filter passes everything now):
        # series -> /series, movies -> /movies, with labels for Jellyfin.
        kind = detect_content_kind(best)
        download_dir = '/series' if kind == 'series' else '/movies'
        labels = ["series", "auto"] if kind == 'series' else ["movie", "auto"]
        if kind == 'series' and season:
            labels.append(f"S{season:02d}")
        if imdb_id:
            labels.append(f"imdb_{imdb_id}")
        # TODO(imdb-resolve): if the caller did not pass imdb_id (e.g. manual
        # search from the UI), the imdb_* label is not set -> post_process_downloads.py
        # skips the torrent (it requires an imdb_* label) and no NFO is created for
        # Jellyfin. For movies this is critical: without NFO Jellyfin identifies the
        # file from built-in MKV tags, and spartanec releases contain garbage there
        # ("Release by spartanec", year from creation_time) -> the movie shows up
        # with a wrong name. Fix: when kind=='movie' and imdb_id is empty, resolve
        # the id via the IMDb suggestion API
        # (https://v2.sg.media-imdb.com/suggestion/<first letter>/<urlencoded title>.json,
        # response {"d":[{"id":"tt0462538","l":"The Simpsons Movie","y":2007,"qid":"movie"}]})
        # and do labels.append(f"imdb_{resolved_id}") BEFORE add_torrent.
        # TODO(movie-nfo): or extend post_process_downloads.py so it resolves
        # imdb from the title_ label itself when imdb_* is missing.
        logger.info(f"Detected kind={kind}, dir={download_dir} for: {best.title[:60]}")
        success = download_url_and_add_to_transmission(sb, driver, best.url, download_dir, tr_host, tr_port, tr_user, tr_password, labels=labels)

        if not success:
            return {"success": False, "error": "Failed to download and add torrent"}

        result = {"success": True, "torrent": {
            "title": best.title, "url": best.url, "quality": best.quality,
            "dub_studio": best.dub_studio, "seeders": best.seeders,
            "size_gb": round(best.size_bytes / 1024 ** 3, 2),
            "kind": kind, "download_dir": download_dir,
        }}

        # Verify in Jellyfin if requested
        if verify and imdb_id:
            jellyfin_result = verify_jellyfin(imdb_id, content_type, season)
            result["jellyfin_verified"] = jellyfin_result

        return result


@app.post("/api/search-and-download")
async def search_and_download(request: Request):
    """Search Rutracker and download best match via Transmission, optionally verify in Jellyfin."""
    import asyncio

    try:
        data = await request.json()
        query = data.get('query')
        content_type = data.get('type', 'movie')
        season = data.get('season')
        imdb_id = data.get('imdb_id')
        verify = data.get('verify', False)

        if not query:
            return {"success": False, "error": "Query is required"}

        if not RECOMMENDER_AVAILABLE:
            return {"success": False, "error": "Recommender module unavailable: " + _rec_import_error}

        return await asyncio.to_thread(_search_and_download_blocking, query, content_type, season, imdb_id, verify)

    except Exception as e:
        logger.error(f"Search and download failed: {e}")
        return {"success": False, "error": str(e)}


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
        status_emoji = "\u2705"
        status_text = custom_message
    elif success:
        status_emoji = "\u2705"
        status_text = "\u0423\u0441\u043f\u0435\u0448\u043d\u043e \u043e\u0431\u043d\u043e\u0432\u043b\u0451\u043d"
    else:
        status_emoji = "\u274c"
        status_text = "\u041e\u0448\u0438\u0431\u043a\u0430 \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f"
    
    # Always log to console
    log_message = f"""
{'=' * 50}
{status_emoji} {status_text}
\U0001f4e5 \u0422\u043e\u0440\u0440\u0435\u043d\u0442: {torrent_name}
\U0001f4c5 \u041b\u043e\u043a\u0430\u043b\u044c\u043d\u0430\u044f \u0434\u0430\u0442\u0430: {local_date}
\U0001f4c5 \u0414\u0430\u0442\u0430 \u043d\u0430 \u0442\u0440\u0435\u043a\u0435\u0440\u0435: {tracker_date}
\U0001f517 URL: {torrent_url}
{'=' * 50}"""
    logger.info(log_message)
    
    # Check Telegram credentials
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    message = f"""
{status_emoji} <b>{status_text}</b>

\U0001f4e5 <b>\u0422\u043e\u0440\u0440\u0435\u043d\u0442:</b>
<code>{torrent_name}</code>

\U0001f4c5 <b>\u0414\u0430\u0442\u044b:</b>
• \u041b\u043e\u043a\u0430\u043b\u044c\u043d\u0430\u044f: <code>{local_date}</code>
• \u0422\u0440\u0435\u043a\u0435\u0440: <code>{tracker_date}</code>

\U0001f517 <a href="{torrent_url}">\u041e\u0442\u043a\u0440\u044b\u0442\u044c \u043d\u0430 \u0442\u0440\u0435\u043a\u0435\u0440\u0435</a>
"""
    
    # TODO: uncomment once Telegram is configured
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

def create_uc_driver():
    """Raw undetected-chromedriver (no SeleniumBase): headed on :99, minimal flags."""
    import undetected_chromedriver as uc
    opts = uc.ChromeOptions()
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--window-size=1920,1080')
    driver = uc.Chrome(options=opts, headless=False, use_subprocess=False)
    try:
        driver.set_page_load_timeout(120)
    except Exception:
        pass
    return driver


def uc_login(driver: WebDriver, login_username: str, login_password: str, max_retries: int = 3) -> Optional[WebDriver]:
    """Login via raw UC driver: cookies first, then real form submit with
    human-like typing. Returns authed driver or None."""
    try:
        _geom = driver.execute_script(
            "return screen.width+'x'+screen.height+'x'+screen.colorDepth+' win:'+window.innerWidth+'x'+window.innerHeight;")
        logger.info(f"UC geometry: {_geom}")
    except Exception as e:
        logger.info(f"UC geometry check failed: {e}")
    if _try_cookie_login(driver):
        return driver
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"UC login attempt {attempt}/{max_retries}...")
            try:
                driver.get("https://rutracker.org/forum/login.php")
            except Exception as e:
                logger.info(f"UC goto issue: {e}")
            deadline = time.time() + 60
            found = False
            _cf_tried = False
            while time.time() < deadline:
                try:
                    curl = driver.current_url
                    if 'login.php' not in curl:
                        break
                    if driver.find_elements(By.NAME, 'login_username'):
                        found = True
                        logger.info(f"UC login form found | {driver.title} | {curl}")
                        break
                    if not _cf_tried:
                        _cf_tried = True
                        _cf_point_click(driver)
                except Exception:
                    pass
                time.sleep(3)
            if not found:
                try:
                    src = driver.page_source.lower()
                except Exception:
                    src = ''
                if _is_authed_page(src, login_username):
                    logger.info("UC already logged in via session")
                    return driver
                logger.warning(f"UC login form not found | {driver.title} | {driver.current_url}")
                continue
            u = driver.find_element(By.NAME, 'login_username')
            p = driver.find_element(By.NAME, 'login_password')
            b = driver.find_element(By.NAME, 'login')
            try:
                u.clear()
                time.sleep(1)
                for ch in login_username:
                    u.send_keys(ch)
                    time.sleep(random.uniform(0.02, 0.09))
                time.sleep(1)
                p.clear()
                time.sleep(1)
                for ch in login_password:
                    p.send_keys(ch)
                    time.sleep(random.uniform(0.02, 0.09))
                time.sleep(1)
            except Exception as e:
                logger.info(f"UC typing failed ({e}), JS fallback")
                driver.execute_script("arguments[0].value = arguments[1]", u, login_username)
                driver.execute_script("arguments[0].value = arguments[1]", p, login_password)
            try:
                b.click()
            except Exception:
                driver.execute_script("arguments[0].click()", b)
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    if 'login.php' not in driver.current_url:
                        break
                except Exception:
                    pass
                time.sleep(1)
            try:
                src = driver.page_source.lower()
            except Exception:
                src = ''
            if _is_authed_page(src, login_username):
                logger.info(f"UC logged in | {driver.current_url}")
                return driver
            logger.warning("UC login submit did not authenticate, retrying")
        except Exception as e:
            logger.warning(f"UC login error: {e}")
        if attempt < max_retries:
            time.sleep(random.uniform(10, 20))
    return None


def create_session(sb, login_username: str, login_password: str, max_retries: int = 3) -> Optional[WebDriver]:
    """Uses the SB context to log into Rutracker, returns the authenticated driver."""
    driver = sb.driver
    for attempt in range(1, max_retries + 1):
        result = _try_login(sb, driver, login_username, login_password)
        if result is not None:
            return result
        if attempt < max_retries:
            wait_sec = random.uniform(10, 30)
            logger.warning(f"Login attempt {attempt}/{max_retries} failed. Retrying in {wait_sec:.0f}s...")
            time.sleep(wait_sec)
    return None

def _is_authed_page(src_lower: str, username: str) -> bool:
    """Ground-truth auth markers from a proven logged-in page:
    guests carry IS_GUEST: !!'1', authed pages show the username and a
    JS logout Hook (post2url('login.php', {logout: 1})). The classic
    'logout=true' URL does NOT exist on Rutracker — never check for it."""
    if not src_lower:
        return False
    if "is_guest: !!'1'" in src_lower:
        return False
    ul = (username or '').lower()
    return bool(ul and ul in src_lower) or 'logout: 1' in src_lower

def _harden_browser(driver: WebDriver) -> None:
    """Reduces automation fingerprint: some Cloudflare checks key on
    window.chrome.runtime presence and navigator.deviceMemory plausibility
    (spec caps it at 8 — Chrome 154 reports raw RAM/1.5 here). Injects stubs
    on every new document via CDP (no CDP mode needed)."""
    try:
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': (
            "try {"
            "  var _cr = window.chrome || {};"
            "  if (!_cr.runtime || typeof _cr.runtime.sendMessage === 'undefined') {"
            "    _cr.runtime = {connect: function(){}, sendMessage: function(){}};"
            "  }"
            "  Object.defineProperty(window, 'chrome', {value: _cr, configurable: true});"
            "  try { Object.defineProperty(navigator, 'deviceMemory', {get: function() { return 8; }, configurable: true }); } catch (e) {}"
            "} catch (e) {}")})
        logger.info("Browser hardening injected (chrome.runtime + deviceMemory stubs)")
    except Exception as e:
        logger.info(f"Browser hardening failed: {e}")

def _human_move(driver, tox: int, toy: int) -> None:
    """Moves the mouse along a jittered curve with variable speed (human-like).
    Instant teleports scream automation to behavioral checks."""
    import random as _r
    import time as _t
    from selenium.webdriver.common.action_chains import ActionChains
    try:
        vw = driver.execute_script("return window.innerWidth;") or 1280
        vh = driver.execute_script("return window.innerHeight;") or 800
        body = driver.find_element(By.TAG_NAME, 'body')
    except Exception:
        return
    last = getattr(_human_move, 'pos', None)
    if last is None:
        last = (vw // 2, vh // 2)
    fx, fy = last
    # control point: midpoint + perpendicular jitter for a curve
    mx, my = (fx + tox) / 2, (fy + toy) / 2
    dx, dy = tox - fx, toy - fy
    dist = max(1, (dx * dx + dy * dy) ** 0.5)
    jx = -dy / dist * _r.uniform(-0.25, 0.25) * dist
    jy = dx / dist * _r.uniform(-0.25, 0.25) * dist
    cxp, cyp = mx + jx, my + jy
    steps = max(6, min(20, int(dist / 40)))
    prev_bx, prev_by = fx - vw / 2, fy - vh / 2
    for i in range(1, steps + 1):
        t = i / steps
        # quadratic bezier
        px = (1 - t) ** 2 * fx + 2 * (1 - t) * t * cxp + t * t * tox
        py = (1 - t) ** 2 * fy + 2 * (1 - t) * t * cyp + t * t * toy
        bx, by = px - vw / 2, py - vh / 2
        try:
            ActionChains(driver).move_to_element_with_offset(body, int(bx), int(by)).perform()
        except Exception:
            break
        _t.sleep(_r.uniform(0.02, 0.09))
    _human_move.pos = (tox, toy)


def _cf_point_click(driver) -> bool:
    """Clicks the Turnstile checkbox anchored on rendered text (resolution
    independent). The checkbox sits left of the 'Verify you are human'
    label; fallback: below the 'Performing security verification' heading.

    STATUS Sep 2026 — clicks LAND pixel-perfect (verified via screenshots)
    but the backend SILENTLY rejects them (box stays empty, no spinner, no
    error). Ruled out: missing the box (anchor+cluster+screenshots), instant
    teleports (now curved human mouse with pauses), dead widget (it renders).
    Hypothesis: Cloudflare scores this client (datacenter egress + automation
    tells) below the verify threshold. Next ideas: (a) exact User-Agent match
    to a passing browser; (b) pure undetected-chromedriver without SeleniumBase
    (fewer artifacts); (c) 2captcha/rucaptcha Turnstile task (sitekey is in
    page HTML) to mint cf_clearance, then plain HTTP + throttling (proven:
    session-only requests return authed index). DO NOT hammer.
    """
    try:
        from selenium.webdriver.common.action_chains import ActionChains

        def _rect(el):
            try:
                r = el.rect
                return (r['x'], r['y'], r['width'], r['height'])
            except Exception:
                return None

        target = None
        try:
            labels = driver.find_elements(By.XPATH, "//*[contains(text(), 'Verify you are human')]")
            for lb in labels:
                r = _rect(lb)
                if r and r[2] > 20:
                    target = (r[0] - 30, r[1] + 10, f"label@{int(r[0])},{int(r[1])}")
                    break
        except Exception:
            pass
        if target is None:
            try:
                heads = driver.find_elements(By.XPATH, "//*[contains(text(), 'Performing security verification')]")
                for h in heads:
                    r = _rect(h)
                    if r and r[2] > 50:
                        target = (r[0] + 19, r[1] + 126, f"head@{int(r[0])},{int(r[1])}")
                        break
            except Exception:
                pass
        if target is None:
            logger.info("CF anchor: no widget text rendered yet")
            return False
        cx, cy, how = target
        try:
            vw = driver.execute_script("return window.innerWidth;") or 1280
            vh = driver.execute_script("return window.innerHeight;") or 800
            body = driver.find_element(By.TAG_NAME, 'body')
            # Cluster around the box to cover anchor uncertainty, with
            # human-like curved movement (teleports fail behavioral checks).
            import random as _rr
            base_x, base_y = cx, cy
            for dx in (0, -12, 12):
                try:
                    _human_move(driver, int(base_x + dx), int(base_y + _rr.uniform(-4, 4)))
                    from selenium.webdriver.common.action_chains import ActionChains
                    body = driver.find_element(By.TAG_NAME, 'body')
                    vw = driver.execute_script("return window.innerWidth;") or 1280
                    vh = driver.execute_script("return window.innerHeight;") or 800
                    a = ActionChains(driver)
                    a.move_to_element_with_offset(
                        body, int(base_x + dx - vw / 2), int(base_y - vh / 2))
                    a.pause(_rr.uniform(0.15, 0.45))
                    a.click_and_hold()
                    a.pause(_rr.uniform(0.08, 0.22))
                    a.release()
                    a.perform()
                    _human_move.pos = (int(base_x + dx), int(base_y))
                    logger.info(f"CF widget human-clicked around ({int(base_x + dx)},{int(base_y)})")
                    time.sleep(5)
                except Exception as e:
                    logger.info(f"CF human click failed: {e}")
                    continue
            try:
                driver.save_screenshot('/tmp/last_click.png')
            except Exception:
                pass
            return True
        except Exception as e:
            logger.info(f"CF anchor click failed: {e}")
            return False
    except Exception as e:
        logger.info(f"CF clicker issue: {e}")
    return False


def _cdp_load_and_solve(sb, driver, url: str) -> str:
    """Full CDP sequence for a challenged page: attach (goes blank, that is
    normal) -> CDP-context navigate to url -> AWAITED Turnstile solve ->
    reconnect webdriver. Returns a short status string.
    NOTE: plain sb.solve_captcha() without await is a no-op (returns an
    un-awaited coroutine); it MUST be awaited. Never CDP-navigate blindly in
    a loop: on tunnel stall the driver can die -> caller must treat failure
    as a failed attempt and retry via the normal flow (SB recovers)."""
    import asyncio
    try:
        logger.info("CDP attaching...")
        sb.activate_cdp_mode()
        logger.info("CDP attached OK")
    except Exception as e:
        return f"attach-fail {type(e).__name__}: {str(e)[:120]}"
    try:
        sb.goto(url)
    except Exception as e:
        return f"cdp-goto-fail {type(e).__name__}: {str(e)[:120]}"
    time.sleep(8)
    try:
        logger.info("CDP solving Turnstile (awaited)...")
        res = sb.solve_captcha()
        if asyncio.iscoroutine(res):
            res = asyncio.run(res)
        logger.info(f"CDP solve returned: {res}")
    except Exception as e:
        return f"solve-fail {type(e).__name__}: {str(e)[:150]}"
    time.sleep(5)
    try:
        sb.connect()
    except Exception:
        pass
    return f"solved={res}"

def _try_cookie_login(driver: WebDriver) -> bool:
    """Restores Rutracker session from RUTRACKER_BB_SESSION / RUTRACKER_BB_DATA
    env cookies (copied once from a manually logged-in browser). Bypasses the
    login form and its captcha entirely.

    STATUS Sep 2026 — WORKS on index.php ('Cookie session restored' in logs:
    ground-truth markers are username + JS logout hook + no IS_GUEST flag;
    the classic 'logout=true' URL does NOT exist on Rutracker, never check it).
    Caveats learned the hard way:
    - bb_session/bb_t are LONG-LIVED (expiry 2027) and valid (proven: plain
      HTTP + same egress IP returns authed index). If restore fails, suspect
      values first (must be full-length, bb_session is 39 chars like
      0-<userid>-<hash>), not the code.
    - cf_clearance is SHORT-LIVED (rotates <~1h). A stale one is worse than
      none: the server re-issues its own copy -> duplicate cookies -> server
      reads the wrong one. Keep RUTRACKER_CF_CLEARANCE EMPTY unless testing
      with a fresh-minutes value (singleton required: exact domain+path match
      when injecting, else duplicates).
    - Strict paths (viewtopic/tracker/search/dl.php) need CF clearance even
      with a valid session; index.php does not.
    """
    if os.environ.get('FORCE_FORM_LOGIN') == '1':
        logger.info("FORCE_FORM_LOGIN=1, skipping cookie restore")
        return False
    cookies = {}
    sess = (os.environ.get('RUTRACKER_BB_SESSION') or '').strip().strip('"').strip("'")
    data = (os.environ.get('RUTRACKER_BB_DATA') or '').strip().strip('"').strip("'")
    bt = (os.environ.get('RUTRACKER_BB_T') or '').strip().strip('"').strip("'")
    ssl = (os.environ.get('RUTRACKER_BB_SSL') or '').strip().strip('"').strip("'")
    if sess:
        cookies['bb_session'] = sess
    if data:
        cookies['bb_data'] = data
    if bt:
        cookies['bb_t'] = bt
    if ssl:
        cookies['bb_ssl'] = ssl
    cf = (os.environ.get('RUTRACKER_CF_CLEARANCE') or '').strip().strip('"').strip("'")
    if cf:
        cookies['cf_clearance'] = cf
    if not cookies:
        return False
    _harden_browser(driver)
    try:
        logger.info("Trying cookie session restore...")
        # Clear site-issued copies on BOTH path contexts first. HttpOnly
        # cookies (cf_clearance) can't be touched via JS, and delete_cookie
        # only affects the current path — so visit each path explicitly.
        # Otherwise duplicates remain and the server may read the wrong one.
        for _path_url in ('https://rutracker.org/', 'https://rutracker.org/forum/index.php'):
            try:
                driver.execute_script(f"window.location.href = '{_path_url}'")
            except Exception:
                pass
            time.sleep(3)
            for _n in ('cf_clearance', 'bb_session', 'bb_data', 'bb_t', 'bb_ssl', 'bb_guid'):
                try:
                    driver.delete_cookie(_n)
                except Exception:
                    pass
        for name, value in cookies.items():
            # Paths/domains per live DevTools: cf_clearance on path=/,
            # forum cookies on /forum/, all on dotted .rutracker.org.
            # Exact match is required, otherwise duplicates are created and
            # the server may read the wrong copy.
            path = '/' if name == 'cf_clearance' else '/forum/'
            try:
                driver.add_cookie({'name': name, 'value': value,
                                   'domain': '.rutracker.org', 'path': path})
            except Exception as e:
                logger.warning(f"add_cookie {name} failed: {e}")
                return False
        try:
            driver.execute_script("window.location.href = 'https://rutracker.org/forum/index.php'")
        except Exception:
            pass
        # Poll for auth markers — the page may need a reload cycle (tunnel+CF).
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(3)
            try:
                src = driver.page_source.lower()
                allc = driver.get_cookies()
                names = [c.get('name', '') for c in allc]
                url = driver.current_url
            except Exception:
                continue
            logger.info(f"Cookie-check: url={url} title={driver.title} cookies={names} "
                        f"guest={'is_guest' in src} logout={'logout=true' in src}")
            try:
                _cf = [(c.get('domain'), c.get('path'), c.get('secure'), len(c.get('value', '')))
                       for c in allc if c.get('name') == 'cf_clearance']
                _bb = [(c.get('domain'), c.get('path'), len(c.get('value', '')))
                       for c in allc if c.get('name') == 'bb_session']
                logger.info(f"Cookie-attrs cf={_cf} bb_session={_bb}")
            except Exception as e:
                logger.info(f"Cookie-attrs failed: {e}")
            if _is_authed_page(src, LOGIN_RUTRACKER):
                logger.info("Cookie session restored (auth markers present)")
                return True
        logger.warning("Cookie session restore failed (still guest)")
        return False
    except Exception as e:
        logger.warning(f"Cookie login error: {e}")
        return False

def _try_login(sb, driver: WebDriver, login_username: str, login_password: str) -> Optional[WebDriver]:
    """Single login attempt: plain-UC navigation (CDP Page.navigate hangs and
    kills the driver on stall; CDP attach on challenge pages crashed Chrome
    154 natively — do NOT re-add either without re-testing), visible-form
    fill, ActionChains submit, strict auth verification.

    NOTE on site captcha: Rutracker shows an image captcha (cap_sid/cap_code)
    on the form after several failed attempts. The bot cannot solve it; EVERY
    failed submit extends the flag. If the form carries cap_* fields, stop
    trying (stop the container!) and let the counter decay, or log in manually
    once (resets it) and refresh RUTRACKER_BB_SESSION. Never hammer.
    """
    try:
        # 0) Cookie session restore first — no captcha needed.
        if _try_cookie_login(driver):
            driver.set_page_load_timeout(120)
            return driver

        # Dead session cookies make Rutracker bounce login.php -> index.php
        # (guest content, no form). Drop them so the real form appears.
        for _cn in ('bb_session', 'bb_data'):
            try:
                driver.delete_cookie(_cn)
            except Exception:
                pass

        # Hybrid: navigate in plain UC (CDP Page.navigate hangs and kills the
        # driver on stall). The login page may show an interactive Cloudflare
        # checkbox — click it manually inside its iframe (no CDP needed).
        logger.info("Opening Rutracker login page (plain UC mode)...")
        try:
            driver.execute_script("window.location.href = 'https://rutracker.org/forum/login.php'")
        except Exception as exc:
            logger.info(f"Nav exec: {type(exc).__name__}")
        time.sleep(6)

        def _click_cf_checkbox() -> bool:
            return _cf_point_click(driver)

        # Wait for the form; click the Turnstile checkbox if a challenge shows.
        deadline = time.time() + 120
        fields = []
        title = ''
        _clicked = False
        _cdp_tried = False
        while time.time() < deadline:
            try:
                current_url = driver.current_url
                title = driver.title
                if '521' in title or '520' in title or '522' in title or '503' in title:
                    logger.warning(f"Server error page: '{title}'")
                    return None
                if 'login.php' not in current_url:
                    break  # redirected (session?) — handled below
                fields = driver.find_elements(By.NAME, 'login_username')
                if fields:
                    logger.info(f"Login form found | title: '{title}' | URL: {current_url}")
                    break
                if not _clicked:
                    _clicked = _click_cf_checkbox()
                # If the widget won't click, one full CDP reload+solve.
                if not _cdp_tried and time.time() > deadline - 75:
                    _cdp_tried = True
                    logger.info(f"CDP solve: {_cdp_load_and_solve(sb, driver, 'https://rutracker.org/forum/login.php')}")
            except Exception:
                pass
            time.sleep(3)
        else:
            logger.warning(f"Login form not found | title: '{title}' | URL: {driver.current_url}")
            try:
                driver.save_screenshot('/tmp/last_login.png')
                logger.info("Saved failure screenshot to /tmp/last_login.png")
                h = driver.page_source
                logger.info(f"Failure HTML len={len(h)}")
                try:
                    with open('/tmp/last_login.html', 'w') as _f:
                        _f.write(h)
                    logger.info("Saved failure HTML to /tmp/last_login.html")
                except Exception as e:
                    logger.info(f"HTML save failed: {e}")
                try:
                    _blog = driver.get_log('browser')[-8:]
                    for _e in _blog:
                        logger.info(f"BrowserConsole: {_e.get('level')} {_e.get('message', '')[:250]}")
                except Exception as e:
                    logger.info(f"Console log unavailable: {e}")
            except Exception as e:
                logger.info(f"Failure dump failed: {e}")
            return None

        if fields:
            pass  # form path continues below
        elif 'login.php' not in driver.current_url:
            # Redirected away from login.php without a form = active session
            # (Rutracker sends logged-in users from login.php to index.php).
            try:
                src = driver.page_source.lower()
            except Exception:
                src = ''
            if _is_authed_page(src, login_username):
                logger.info(f"Already logged in via existing session | URL: {driver.current_url}")
                driver.set_page_load_timeout(120)
                return driver
            logger.info("On index without auth markers yet, continuing to form check...")
            fields = driver.find_elements(By.NAME, 'login_username')
            if not fields:
                logger.warning(f"Login form not found | title: '{driver.title}' | URL: {driver.current_url}")
                return None
        else:
            # Already waited 60s above while on login.php with no form.
            logger.warning(f"Login form not found | title: '{driver.title}' | URL: {driver.current_url}")
            return None

        # Fill the VISIBLE login form (the page can contain hidden quick-login
        # duplicates — filling those silently does nothing).
        def _visible(names):
            els = driver.find_elements(By.NAME, names)
            vis = [e for e in els if e.is_displayed()]
            return vis[0] if vis else (els[0] if els else None)

        username_field = _visible('login_username')
        password_field = _visible('login_password')
        login_btn = _visible('login')
        if not username_field or not password_field or not login_btn:
            logger.warning("Login form elements not found (visible)")
            return None
        logger.info(f"Login controls: user tag={username_field.tag_name}, "
                    f"btn tag={login_btn.tag_name} type={login_btn.get_attribute('type')}")

        page_src = driver.page_source
        if 'rutracker.org' not in page_src.lower() or 'login_username' not in page_src:
            logger.warning("Form found but page doesn't look like Rutracker login")
            return None

        # Fill via SeleniumBase (real input events) with JS fallback.
        try:
            sb.clear('input[name="login_username"]')
            sb.type('input[name="login_username"]', login_username)
            sb.clear('input[name="login_password"]')
            sb.type('input[name="login_password"]', login_password)
        except Exception as e:
            logger.info(f"sb.type failed ({e}), using JS fill")
            driver.execute_script("arguments[0].value = arguments[1]", username_field, login_username)
            driver.execute_script("arguments[0].value = arguments[1]", password_field, login_password)
        # Read back — if values didn't stick, the elements are detached.
        try:
            got_u = username_field.get_attribute('value') or ''
            got_p = password_field.get_attribute('value') or ''
            logger.info(f"Fill check: user len={len(got_u)} pass len={len(got_p)}")
            if len(got_u) != len(login_username) or len(got_p) != len(login_password):
                logger.warning("Filled values did not stick — elements likely detached")
                return None
        except Exception as e:
            logger.info(f"Fill readback failed: {e}")

        # Submit: real mouse click via ActionChains, with JS click +
        # form.submit() fallbacks.
        submitted = False
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(driver).move_to_element(login_btn).click().perform()
            submitted = True
        except Exception as e:
            logger.info(f"ActionChains click failed ({e}), trying JS click + form submit")
            try:
                driver.execute_script("arguments[0].click()", login_btn)
                submitted = True
            except Exception:
                pass
        if not submitted:
            try:
                driver.execute_script("arguments[0].form.submit()", login_btn)
                submitted = True
            except Exception as e:
                logger.warning(f"All submit attempts failed: {e}")
                return None

        # IMPORTANT: do NOT force-navigate here — the login POST needs time to
        # complete, and navigating away aborts it (leaving a guest session).
        logger.info("Login submitted, waiting for redirect...")
        deadline = time.time() + 30
        current_url = ''
        while time.time() < deadline:
            try:
                current_url = driver.current_url
                if 'login.php' not in current_url:
                    break
            except Exception:
                pass
            time.sleep(1)

        if 'login.php' in current_url:
            logger.warning(f"Still on login page after attempt | URL: {current_url}")
            try:
                driver.execute_script("window.stop()")
            except Exception:
                pass
            try:
                err_src = driver.page_source
                err_low = err_src.lower()
                for kw in ('неверн', 'ошибк', 'incorrect', 'error', 'забанен', 'banned', 'challenge', 'turnstile'):
                    if kw in err_low:
                        logger.warning(f"Login page contains marker: '{kw}'")
                        break
                logger.warning(f"Login form captcha present: {('cap_sid' in err_src or 'cap_code' in err_src)}")
                m = re.search(r'(неверн.{0,80}|ошибк.{0,80}|incorrect.{0,80})', err_src, re.IGNORECASE | re.DOTALL)
                if m:
                    logger.warning(f"Login error text: {m.group(1)[:120]}")
            except Exception:
                pass
            return None

        # Verify the session is really authenticated (guests can also open
        # index.php — URL alone proves nothing). Ground truth: no IS_GUEST
        # marker plus username or the JS logout hook. NOTE: mere presence of
        # a bb_session cookie proves NOTHING (it may be dead).
        try:
            verify_src = driver.page_source.lower()
        except Exception:
            verify_src = ''
        authed = _is_authed_page(verify_src, login_username)
        if not authed:
            logger.warning("Reached index but no auth markers (username/logout hook, no IS_GUEST) — treating as guest, retrying")
            return None

        logger.info(f"Logged into Rutracker | URL: {current_url}")
        driver.set_page_load_timeout(120)
        return driver

    except TimeoutException as e:
        logger.info(f"Login TimeoutException (may still have worked): {str(e)[:80]}")
        if driver:
            try:
                driver.execute_script("window.stop()")
            except Exception:
                pass
            try:
                current_url = driver.current_url
                if 'login.php' not in current_url:
                    logger.info(f"Recovered after timeout | URL: {current_url}")
                    return driver
            except Exception:
                pass
        return None
    except Exception as e:
        logger.warning(f"Login attempt error: {e}")
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

def _wait_for_clean_page(driver: WebDriver, timeout: int = 30, sb=None, url: str = '') -> tuple[str, str]:
    """Returns (page_source, title), waiting out a Cloudflare 'Just a moment'
    managed challenge (it often auto-clears in seconds). Moves the mouse and
    scrolls a little while waiting — human-presence signal for behavioral
    checks."""
    import time as _t
    import random as _r
    deadline = _t.time() + timeout
    src, title = '', ''
    _con_logged = False
    _moved = 0
    _clicks_done = 0
    _wait_for_clean_page._cdp_done = False  # one CDP solve attempt per page
    while True:
        try:
            src = driver.page_source
        except Exception:
            src = ''
        try:
            title = driver.title
        except Exception:
            title = ''
        low = (title + ' ' + src[:2000]).lower()
        if 'just a moment' not in low and 'challenge-platform' not in low:
            return src, title
        if _t.time() >= deadline:
            if not _con_logged:
                _con_logged = True
                try:
                    for _e in driver.get_log('browser')[-8:]:
                        logger.info(f"CFConsole: {_e.get('level')} {_e.get('message', '')[:250]}")
                except Exception as e:
                    logger.info(f"CF console unavailable: {e}")
            return src, title
        # One awaited CDP solve per page while challenged (needs sb context).
        if _clicks_done < 2:
            _clicks_done += 1
            try:
                _cf_point_click(driver)
            except Exception:
                pass
        if sb is not None and not getattr(_wait_for_clean_page, '_cdp_done', False):
            _wait_for_clean_page._cdp_done = True
            try:
                logger.info(f"CDP viewtopic solve: {_cdp_load_and_solve(sb, driver, url)}")
            except Exception as e:
                logger.info(f"CDP viewtopic solve crashed: {e}")
        # human-like presence: small mouse moves + scroll
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            vw = driver.execute_script("return window.innerWidth || 1280;") or 1280
            vh = driver.execute_script("return window.innerHeight || 800;") or 800
            body = driver.find_element(By.TAG_NAME, 'body')
            ox = _r.randint(-int(vw / 3), int(vw / 3))
            oy = _r.randint(-int(vh / 3), int(vh / 3))
            ActionChains(driver).move_to_element_with_offset(body, ox, oy).perform()
            if _moved % 2 == 1:
                driver.execute_script(f"window.scrollBy(0, {_r.randint(80, 240)});")
            _moved += 1
        except Exception:
            pass
        _t.sleep(3)

def is_torrent_updated(url: str, torrent_date: datetime.datetime, session: WebDriver, max_retries: int = 3, sb=None) -> tuple[bool, str, str, str, bool]:
    """Checks if the torrent on the tracker is newer than the local one. Retries on failure.
    Returns: (is_updated, local_date_str, tracker_date_str, error_msg, is_season_complete)
    """
    last_error = 'Неизвестная ошибка'
    for attempt in range(1, max_retries + 1):
        result = _try_check_torrent(url, torrent_date, session, sb)
        is_updated, local_date_str, tracker_date_str, error_msg, is_season_complete = result
        last_error = error_msg
        if local_date_str or tracker_date_str:  # got a real result
            return result
        if attempt < max_retries:
            wait_sec = random.uniform(2, 5)
            logger.warning(f"Date fetch attempt {attempt}/{max_retries} failed for {url}. Retrying in {wait_sec:.0f}s...")
            time.sleep(wait_sec)
    return (False, "", "", last_error, False)

def _try_check_torrent(url: str, torrent_date: datetime.datetime, session: WebDriver, sb=None) -> tuple[bool, str, str, str, bool]:
    """Returns: (is_updated, local_date_str, tracker_date_str, error_msg, is_season_complete)
    is_season_complete: True when the rutracker page title contains e.g. 'Серии: 1-8 из 8'
    """
    try:
        driver = session  # session IS the WebDriver from sb.connect()

        # Navigate via JS — returns immediately, page load async
        try:
            driver.execute_script("window.location.href = arguments[0]", url)
        except Exception as exc:
            logger.info(f"Nav exec error: {type(exc).__name__}")
        time.sleep(3)

        try:
            page_source, _ = _wait_for_clean_page(driver, timeout=30, sb=sb, url=url)
            if not page_source:
                raise RuntimeError("empty page")
        except Exception as exc:
            logger.info(f"page_source error: {type(exc).__name__}")
            try:
                driver.execute_script("window.stop()")
                time.sleep(1)
                page_source = driver.page_source
            except Exception:
                return (False, "", "", f"Не удалось загрузить страницу: {exc}", False)
        soup = BeautifulSoup(page_source, 'lxml')
        title_text = soup.find('title')
        title_text = title_text.get_text(strip=True) if title_text else '?'

        # Quick check: are we still on the page and logged in?
        if 'login.php' in driver.current_url:
            logger.error(f"Session expired for {url}")
            return (False, "", "", "Сессия истекла", False)

        # If Cloudflare blocked the page
        if '521' in title_text or '520' in title_text or '503' in title_text or '522' in title_text:
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

def download_and_add_torrent(url: str, session: WebDriver, to_dir: str, tr: Client, max_retries: int = 3) -> bool:
    """Downloads the torrent file using the authenticated Chrome driver and adds it to Transmission."""
    driver = session

    for attempt in range(1, max_retries + 1):
        try:
            try:
                driver.execute_script("window.location.href = arguments[0]", url)
            except Exception as exc:
                logger.info(f"Nav exec error: {type(exc).__name__}")

            # Poll current_url
            current_url = ''
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    current_url = driver.current_url
                    if current_url and ('login.php' in current_url or 'viewtopic.php' in current_url):
                        break
                except Exception:
                    pass
                time.sleep(1)

            if 'login.php' in current_url:
                logger.error("Session expired during download attempt")
                return False

            try:
                page_html = driver.page_source
            except Exception as exc:
                logger.info(f"page_source error: {type(exc).__name__}")
                try:
                    driver.execute_cdp_cmd("Page.stopLoading", {})
                    time.sleep(1)
                    page_html = driver.page_source
                except Exception:
                    page_html = ''

            soup = BeautifulSoup(page_html, 'lxml')
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

            # Use the browser's fetch API to download — bypasses Cloudflare via UC patches
            result = driver.execute_script("""
                return fetch(arguments[0], {credentials: 'include'})
                    .then(r => {
                        if (!r.ok) return 'HTTP_' + r.status;
                        return r.arrayBuffer().then(buf => {
                            var bytes = new Uint8Array(buf);
                            var binary = '';
                            for (var i = 0; i < bytes.length; i++) {
                                binary += String.fromCharCode(bytes[i]);
                            }
                            return btoa(binary);
                        });
                    })
                    .catch(e => 'FETCH_ERR: ' + e.message);
            """, download_url)

            if result is None:
                logger.warning("Fetch returned None")
                if attempt < max_retries:
                    time.sleep(random.uniform(5, 15))
                    continue
                return False

            if isinstance(result, str) and result.startswith('HTTP_'):
                status_code = int(result.split('_')[1])
                logger.warning(f"Torrent download returned {status_code} via fetch (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(random.uniform(5, 15))
                    continue
                return False

            if isinstance(result, str) and result.startswith('FETCH_ERR'):
                logger.warning(f"Fetch error: {result} (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    time.sleep(random.uniform(5, 15))
                    continue
                return False

            torrent_data = base64.b64decode(result)

            logger.info(f"Adding torrent: {len(torrent_data)} bytes | first 20: {torrent_data[:20]}")
            try:
                new_torrent = tr.add_torrent(torrent_data, download_dir=to_dir)
                tr.change_torrent(new_torrent.id, comment=url)
                logger.info(f"Torrent added to Transmission (id={new_torrent.id}, comment set)")
                return True
            except Exception as add_err:
                logger.warning(f"tr.add_torrent error: {type(add_err).__name__}: {add_err}")
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

    # Schedule daily recommendations at 03:00 MSK (only if recommender imports)
    if RECOMMENDER_AVAILABLE:
        schedule.every().day.at("03:00").do(run_daily_recommendations)
        logger.info("Scheduled daily recommendations at 03:00 MSK")
        # Catch-up: the 03:00 slot is often missed (container down/rebuilding),
        # so run the cycle now if the cache is missing or older than 24h.
        try:
            import json as _json
            from pathlib import Path as _Path
            _cache = _Path("/opt/data/cache/movie_recommendations.json")
            _stale = True
            if _cache.exists():
                try:
                    _ts = _json.loads(_cache.read_text(encoding='utf-8')).get('timestamp')
                    _age = (datetime.datetime.now(datetime.timezone.utc)
                            - datetime.datetime.fromisoformat(_ts)).total_seconds() if _ts else 1e9
                    _stale = _age > 24 * 3600
                except Exception:
                    _stale = True
            if _stale:
                logger.info("Recommendations cache missing/stale (>24h), catch-up cycle in 30 min (after startup check)...")

                def _delayed_recs():
                    time.sleep(30 * 60)
                    try:
                        run_daily_recommendations()
                    except Exception as e:
                        logger.warning(f"Catch-up recommendations cycle failed: {e}")

                threading.Thread(target=_delayed_recs, daemon=True).start()
        except Exception as e:
            logger.warning(f"Recommendations catch-up check failed: {e}")
    else:
        logger.warning("Daily recommendations NOT scheduled (recommender unavailable)")

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

    # SB-managed browser (plain UC, no CDP navigate): sb handle is threaded
    # through the check path so challenged pages can attempt one awaited
    # CDP Turnstile solve. Raw-UC fallback (create_uc_driver/uc_login) kept
    # below in this module if SB ever misbehaves.
    with SB(uc=True, chromium_arg="--enable-unsafe-swiftshader") as sb:
        session = create_session(sb, LOGIN_RUTRACKER, PASSWORD_RUTRACKER)
        if not session:
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
            time.sleep(20)  # let CF settle after the login burst; human pace

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

                is_updated, local_date_str, tracker_date_str, error_msg, is_season_complete = is_torrent_updated(torrent_url, torrent_date, session, sb=sb)

                if not local_date_str and not tracker_date_str:
                    status_manager.record_torrent_check(torrent.name, '?', '?', 'error', error_msg=error_msg, torrent_url=torrent_url)
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
                    status_manager.record_torrent_check(torrent.name, local_date_str, tracker_date_str, 'ok', torrent_url=torrent_url)
                    return None

            results = []
            for i, torrent in enumerate(rutracker_torrents):
                if i:
                    time.sleep(30)  # human pace: bursts trigger CF challenges
                results.append(process_torrent(torrent))

            # Handle completed seasons — remove from Transmission without re-downloading
            for r in results:
                if r is not None and r[0] == 'season_complete':
                    _, torrent, torrent_url, local_date_str, tracker_date_str = r
                    try:
                        tr.remove_torrent(torrent.id)
                        status_manager.record_torrent_check(torrent.name, local_date_str, tracker_date_str, 'season_complete',
                            error_msg='Сезон завершён, удалён из Transmission', torrent_url=torrent_url)
                        send_telegram_notification(
                            torrent_name=torrent.name,
                            torrent_url=torrent_url,
                            local_date=local_date_str,
                            tracker_date=tracker_date_str,
                            success=True,
                            custom_message='\U0001f3c1 Сезон завершён, сериал скачан полностью'
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
                        status_manager.record_update(torrent.name, local_date_str, tracker_date_str, True, torrent_url=torrent_url)
                        send_telegram_notification(
                            torrent_name=torrent.name,
                            torrent_url=torrent_url,
                            local_date=local_date_str,
                            tracker_date=tracker_date_str,
                            success=True
                        )
                    else:
                        logger.error("Failed to download new torrent — old torrent kept intact.")
                        status_manager.record_update(torrent.name, local_date_str, tracker_date_str, False, torrent_url=torrent_url)
                        send_telegram_notification(
                            torrent_name=torrent.name,
                            torrent_url=torrent_url,
                            local_date=local_date_str,
                            tracker_date=tracker_date_str,
                            success=False
                        )
                except Exception as e:
                    logger.error(f"Error during update process: {e}")
                    status_manager.record_update(torrent.name, local_date_str, tracker_date_str, False, torrent_url=torrent_url)
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
            status_manager.update_status("idle")
            logger.info("--- Check Cycle Finished ---")

if __name__ == "__main__":
    main()
