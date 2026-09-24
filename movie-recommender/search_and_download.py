#!/usr/bin/env python3
"""
On-demand Rutracker search and download with Jellyfin verification.
Usage:
  python search_and_download.py "The Simpsons" --series --season 15 --imdb-id tt0096697
  python search_and_download.py "Inception" --movie --imdb-id tt1375666
"""

import argparse
import logging
import os
import re
import sys
import time
import random
from pathlib import Path

# Add the movie-recommender directory to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from seleniumbase import SB
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException


def _is_authed_page(src_lower: str, username: str) -> bool:
    """Ground-truth auth markers: no IS_GUEST flag + username or JS logout hook."""
    if not src_lower:
        return False
    if "is_guest: !!'1'" in src_lower:
        return False
    ul = (username or '').lower()
    return bool(ul and ul in src_lower) or 'logout: 1' in src_lower

from rutracker_scraper import (
    RutrackerScraper,
    RutrackerTorrent,
    RUTRACKER_MOVIES,
    RUTRACKER_TV,
    scrape_rutracker_movies,
    scrape_rutracker_tv,
)
from transmission_add import TransmissionManager, SERIES_DOWNLOAD_DIR, MOVIES_DOWNLOAD_DIR
from jellyfin_sync import JellyfinSync

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load env - try multiple locations for container compatibility
env_paths = [
    '/app/.env',
    '/opt/data/scripts/movie-recommender/.env',
    '/mnt/media/docker-compose/torrent_updater/.env',
]
for env_path in env_paths:
    if os.path.exists(env_path):
        load_dotenv(env_path)
        logger.info(f"Loaded env from {env_path}")
        break
else:
    load_dotenv()  # fallback

LOGIN_RUTRACKER = os.getenv('LOGIN_RUTRACKER')
PASSWORD_RUTRACKER = os.getenv('PASSWORD_RUTRACKER')
TR_HOST = os.getenv('TR_HOST')
TR_PORT = int(os.getenv('TR_PORT', 9091))
TR_USER = os.getenv('TR_USER')
TR_PASSWORD = os.getenv('TR_PASSWORD')


def create_rutracker_session(login: str, password: str):
    """Create authenticated Rutracker session using SeleniumBase UC mode.
    Returns (driver, sb) tuple. Caller must call sb.__exit__() when done."""
    _sb_cm = SB(uc=True, chromium_arg="--enable-unsafe-swiftshader")
    sb = _sb_cm.__enter__()

    driver = sb.driver
    for attempt in range(1, 4):
        try:
            # 0) Cookie session restore first — no captcha needed.
            _cookies = {}
            _sess = (os.environ.get('RUTRACKER_BB_SESSION') or '').strip().strip('"').strip("'")
            _data = (os.environ.get('RUTRACKER_BB_DATA') or '').strip().strip('"').strip("'")
            _bt = (os.environ.get('RUTRACKER_BB_T') or '').strip().strip('"').strip("'")
            _ssl = (os.environ.get('RUTRACKER_BB_SSL') or '').strip().strip('"').strip("'")
            if _sess:
                _cookies['bb_session'] = _sess
            if _data:
                _cookies['bb_data'] = _data
            if _bt:
                _cookies['bb_t'] = _bt
            if _ssl:
                _cookies['bb_ssl'] = _ssl
            if _cookies:
                try:
                    driver.execute_script("window.location.href = 'https://rutracker.org/forum/index.php'")
                except Exception:
                    pass
                time.sleep(4)
                for _n, _v in _cookies.items():
                    try:
                        driver.add_cookie({'name': _n, 'value': _v,
                                           'domain': '.rutracker.org', 'path': '/forum/'})
                    except Exception:
                        pass
                try:
                    driver.execute_script("window.location.href = 'https://rutracker.org/forum/index.php'")
                except Exception:
                    pass
                time.sleep(4)
                try:
                    _src = driver.page_source.lower()
                except Exception:
                    _src = ''
                if _is_authed_page(_src, login):
                    logger.info("Cookie session restored (auth markers present)")
                    driver.set_page_load_timeout(120)
                    return driver, sb
                for _cn in ('bb_session', 'bb_data'):
                    try:
                        driver.delete_cookie(_cn)
                    except Exception:
                        pass

            logger.info(f"Logging into Rutracker (attempt {attempt}/3, plain UC, no CDP)...")
            try:
                driver.execute_script("window.location.href = 'https://rutracker.org/forum/login.php'")
            except Exception as exc:
                logger.info(f"Nav exec: {type(exc).__name__}")

            # Wait for the form; click the Turnstile checkbox if challenged.
            # (Manual iframe click — sb.solve_captcha exists only in CDP mode,
            # and CDP attach crashes this Chrome build.)
            def _click_cf_checkbox() -> bool:
                try:
                    from selenium.webdriver.common.action_chains import ActionChains
                    for frame in driver.find_elements(By.TAG_NAME, 'iframe'):
                        try:
                            src = (frame.get_attribute('src') or '') + ' ' + (frame.get_attribute('title') or '')
                        except Exception:
                            continue
                        if 'challenge' not in src.lower() and 'turnstile' not in src.lower():
                            continue
                        try:
                            driver.switch_to.frame(frame)
                        except Exception:
                            continue
                        try:
                            boxes = driver.find_elements(By.XPATH, "//input[@type='checkbox']")
                            if not boxes:
                                boxes = driver.find_elements(By.TAG_NAME, 'input')
                            for box in boxes:
                                try:
                                    if box.is_displayed():
                                        ActionChains(driver).move_to_element(box).click().perform()
                                        logger.info("CF checkbox clicked")
                                        time.sleep(3)
                                        driver.switch_to.default_content()
                                        return True
                                except Exception:
                                    continue
                        finally:
                            try:
                                driver.switch_to.default_content()
                            except Exception:
                                pass
                except Exception as e:
                    logger.info(f"CF clicker issue: {e}")
                return False

            deadline = time.time() + 90
            fields = []
            title = ''
            _clicked = False
            while time.time() < deadline:
                try:
                    current_url = driver.current_url
                    title = driver.title
                    if '521' in title or '520' in title or '522' in title or '503' in title:
                        raise RuntimeError(f"Server error page: '{title}'")
                    if 'login.php' not in current_url:
                        break
                    fields = driver.find_elements(By.NAME, 'login_username')
                    if fields:
                        logger.info(f"Login form found | title: '{title}' | URL: {current_url}")
                        break
                    if not _clicked:
                        _clicked = _click_cf_checkbox()
                except Exception:
                    pass
                time.sleep(3)
            else:
                raise RuntimeError(f"Login form not found | title: '{title}'")

            if not fields and 'login.php' in driver.current_url:
                raise RuntimeError(f"Login form not found | title: '{driver.title}'")

            if not fields:
                # Redirected away from login.php — check for an active session.
                try:
                    _src = driver.page_source.lower()
                except Exception:
                    _src = ''
                if _is_authed_page(_src, login):
                    logger.info(f"Already logged in via existing session | URL: {driver.current_url}")
                    driver.set_page_load_timeout(120)
                    return driver, sb
                raise RuntimeError("Redirected without auth markers and no form")

            page_src = driver.page_source
            if 'rutracker.org' not in page_src.lower() or 'login_username' not in page_src:
                raise RuntimeError("Form found but page doesn't look like Rutracker login")

            def _visible(name):
                els = driver.find_elements(By.NAME, name)
                vis = [e for e in els if e.is_displayed()]
                return vis[0] if vis else (els[0] if els else None)

            username_field = _visible('login_username')
            password_field = _visible('login_password')
            login_btn = _visible('login')
            if not username_field or not password_field or not login_btn:
                raise RuntimeError("Login form elements not found (visible)")
            logger.info(f"Login controls: user tag={username_field.tag_name}, "
                        f"btn tag={login_btn.tag_name} type={login_btn.get_attribute('type')}")

            try:
                sb.clear('input[name="login_username"]')
                sb.type('input[name="login_username"]', login)
                sb.clear('input[name="login_password"]')
                sb.type('input[name="login_password"]', password)
            except Exception as e:
                logger.info(f"sb.type failed ({e}), using JS fill")
                driver.execute_script("arguments[0].value = arguments[1]", username_field, login)
                driver.execute_script("arguments[0].value = arguments[1]", password_field, password)
            try:
                got_u = username_field.get_attribute('value') or ''
                got_p = password_field.get_attribute('value') or ''
                logger.info(f"Fill check: user len={len(got_u)} pass len={len(got_p)}")
                if len(got_u) != len(login) or len(got_p) != len(password):
                    raise RuntimeError("Filled values did not stick")
            except RuntimeError:
                raise
            except Exception as e:
                logger.info(f"Fill readback failed: {e}")

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
                    raise RuntimeError(f"All submit attempts failed: {e}")

            # IMPORTANT: do NOT force-navigate — it aborts the login POST
            # and leaves a guest session. Just wait for the redirect below.
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
                try:
                    driver.execute_script("window.stop()")
                except Exception:
                    pass
                raise RuntimeError(f"Still on login page | URL: {current_url}")

            try:
                verify_src = driver.page_source.lower()
            except Exception:
                verify_src = ''
            if not _is_authed_page(verify_src, login):
                raise RuntimeError("Reached index as guest (no auth markers)")

            logger.info(f"Logged into Rutracker | URL: {current_url}")
            driver.set_page_load_timeout(120)
            return driver, sb

        except Exception as e:
            logger.warning(f"Login attempt {attempt} failed: {e}")

        if attempt < 3:
            time.sleep(random.uniform(10, 30))

    _sb_cm.__exit__(None, None, None)
    raise RuntimeError("Failed to login to Rutracker after 3 attempts")


_QUERY_STOPWORDS = {'the', 'a', 'an', 'and', 'of', 'и', 'в', 'на', 'с', 'со', 'из', 'от', 'для', 'по'}


def _query_words(query: str) -> list:
    """Significant search tokens: alphanumerics len>=3 (not stopwords) + any numbers."""
    toks = re.findall(r'[a-zа-яё0-9]+', (query or '').lower())
    return [t for t in toks if (len(t) >= 3 and t not in _QUERY_STOPWORDS) or t.isdigit()]


def _title_matches(title: str, words: list) -> bool:
    tl = (title or '').lower()
    return all(w in tl for w in words)


def search_best_torrent(query: str, is_series: bool, season: int = None, imdb_id: str = None) -> RutrackerTorrent:
    """Search Rutracker and return best matching torrent.
    Primary: tracker.php?nm=<query> (real site search, members only).
    Fallback: latest forum topics scan (old behavior).

    STATUS Sep 2026 — the old code NEVER matched the query at all (it just
    picked the best-scoring recent topic!). Fixed here: tracker-first +
    _query_words/_title_matches filter (stopwords dropped, numbers kept, e.g.
    'The Simpsons 15' -> ['simpsons', '15']). Whole flow is E2E-blocked by the
    Cloudflare wall (needs authed browser session). If tracker is empty AND
    forum scan is empty, the caller gets 'No suitable torrents' — check
    whether the session was really authed before blaming filters.
    """
    forum_url = RUTRACKER_TV if is_series else RUTRACKER_MOVIES
    logger.info(f"Searching Rutracker for: {query!r} (series={is_series}, season={season})")

    scraper = RutrackerScraper(LOGIN_RUTRACKER, PASSWORD_RUTRACKER)
    scraper.__enter__()

    try:
        torrents = []
        try:
            torrents = scraper.search_tracker(query, max_pages=3)
            logger.info(f"Tracker search returned {len(torrents)} torrents")
        except Exception as e:
            logger.warning(f"Tracker search failed, falling back to forum scan: {e}")

        if not torrents:
            forum_id = 252 if not is_series else 1803
            torrents = scraper.scrape_forum(forum_url, forum_id, max_pages=5)
            logger.info(f"Forum scan found {len(torrents)} torrents on Rutracker")

        # Query-word filter (the old forum scan never matched the query at all)
        words = _query_words(query)
        if words:
            before = len(torrents)
            torrents = [t for t in torrents if _title_matches(t.title, words)]
            logger.info(f"Query filter {words}: {before} -> {len(torrents)}")

        # Filter and score
        filtered = []
        for t in torrents:
            if t.is_excluded:
                continue
            if not t.has_dubbing:
                continue
            if t.seeders < 5:
                continue
            if not t.quality:
                continue

            # For series, check if season matches
            if is_series and season:
                t_season, _ = extract_season_episode(t.title)
                if t_season and t_season != season:
                    continue

            # Try to match with IMDB if provided
            if imdb_id:
                # Could add IMDB matching here
                pass

            filtered.append(t)

        if not filtered:
            raise RuntimeError(f"No suitable torrents found for: {query}")

        # Score and pick best
        best = max(filtered, key=score_torrent)
        logger.info(f"Best torrent: {best.title} | {best.quality} | {best.dub_studio} | {best.seeders} seeders | {best.size_bytes/1024**3:.2f} GB")
        return best
    finally:
        scraper.__exit__(None, None, None)


def extract_season_episode(title: str):
    """Extract season and episode from title."""
    import re
    patterns = [
        r'S(\d{1,2})[Ee](\d{1,2})',
        r'S(\d{1,2})\b',
        r'Season\s+(\d{1,2})',
        r'Сезон\s*[:]?\s*(\d{1,2})',
        r'(\d{1,2})\s*сезон',
        r'(\d{1,2})\s*сер(ия|\.|$)',
    ]
    for pattern in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            season = int(match.group(1))
            episode = int(match.group(2)) if len(match.groups()) > 1 and match.group(2) else None
            return season, episode
    return None, None


def score_torrent(torrent: RutrackerTorrent) -> int:
    """Score torrent for quality. Higher = better."""
    score = 0

    quality_rank = {
        'WEB-DL 2160p': 105, 'WEB-DL 4K': 105, 'WEB-DL 4K HDR': 105, 'WEB-DL 4K DV': 105,
        'WEB-DL 1080p': 100, 'WEB-DL 1080p HDR': 100, 'WEB-DL 1080p DV': 100,
        'BDRip 1080p': 95, 'BDRip 1080p HDR': 95,
        'Remux 1080p': 90, 'Remux 1080p HDR': 90,
        'WEBRip 1080p': 85,
        'WEB-DL 720p': 75,
        'BDRip 720p': 65,
        'WEBRip 720p': 60,
        'WEB-DL': 50,
        'BDRip': 40,
        'Remux': 35,
        'WEBRip': 30,
        'AVC': 20, 'HEVC': 20, 'x265': 20, 'x264': 20,
        'TS': 10,
    }
    score += quality_rank.get(torrent.quality, 0)

    preferred_studios = [
        'LostFilm', 'TVShows', 'BaibaKo', 'Octopus', 'NewStudio',
        'Jaskier', 'AlexFilm', 'Red Head Sound', 'HamsterStudio',
        'DreamTeam', 'Edelweiss', 'Amedia', 'KuboKube', 'Kinozal',
        'Novice', 'HDRezka', 'West Video', 'MobilStudia', 'Vozrozhdenie'
    ]
    if torrent.dub_studio:
        for i, studio in enumerate(preferred_studios):
            if studio.lower() in torrent.dub_studio.lower():
                score += 50 - i
                break

    score += min(torrent.seeders, 500) // 10

    size_gb = torrent.size_bytes / 1024**3
    if 1.5 <= size_gb <= 30:
        score += 10

    return score


def download_and_add_to_transmission(torrent: RutrackerTorrent, driver: WebDriver, is_series: bool, imdb_id: str = None, title: str = None, season: int = None) -> str:
    """Download torrent file and add to Transmission."""
    import base64
    import requests
    import re

    # Get download URL
    match = re.search(r't=(\d+)', torrent.url)
    if not match:
        raise RuntimeError("Could not extract topic ID from URL")
    topic_id = match.group(1)
    download_url = f"https://rutracker.org/forum/dl.php?t={topic_id}"

    # Use browser fetch to download
    logger.info(f"Downloading torrent from {download_url}")
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

    if result is None or (isinstance(result, str) and result.startswith(('HTTP_', 'FETCH_ERR'))):
        raise RuntimeError(f"Failed to download torrent: {result}")

    torrent_data = base64.b64decode(result)
    logger.info(f"Downloaded {len(torrent_data)} bytes")

    # Add to Transmission
    tr = TransmissionManager()
    if not tr.connect():
        raise RuntimeError("Failed to connect to Transmission")

    download_dir = SERIES_DOWNLOAD_DIR if is_series else MOVIES_DOWNLOAD_DIR
    labels = ["series", "auto"] if is_series else ["movie", "auto"]

    if imdb_id:
        labels.append(f"imdb_{imdb_id}")
    if title:
        labels.append(f"title_{title.replace(' ', '_')}")
    if is_series and season:
        labels.append(f"S{season:02d}")

    result = tr.add_torrent(torrent_data, download_dir, labels=labels)
    if not result.success:
        raise RuntimeError(f"Failed to add to Transmission: {result.error}")

    logger.info(f"Added to Transmission: {result.name} (ID: {result.torrent_id})")
    return result.torrent_id


def wait_for_download(torrent_id: int, timeout_minutes: int = 60) -> bool:
    """Wait for torrent to finish downloading."""
    tr = TransmissionManager()
    if not tr.connect():
        return False

    start = time.time()
    timeout = timeout_minutes * 60

    while time.time() - start < timeout:
        torrents = tr.get_torrents()
        for t in torrents:
            if t.id == torrent_id:
                if t.percent_done == 1.0 or t.status == 'seeding':
                    logger.info(f"Torrent {torrent_id} finished downloading")
                    return True
                progress = t.percent_done * 100
                logger.info(f"Torrent {torrent_id}: {progress:.1f}% - {t.status}")
                break
        time.sleep(30)

    logger.warning(f"Timeout waiting for torrent {torrent_id}")
    return False


def _verify_jellyfin_cli(imdb_id: str, is_series: bool, season: int = None, title: str = None) -> bool:
    """Verify Jellyfin has identified the content correctly."""
    import sqlite3
    from jellyfin_sync import JellyfinSync

    jellyfin = JellyfinSync()
    try:
        jellyfin.connect()

        # Trigger library scan
        import requests
        try:
            resp = requests.post("http://192.0.2.10:8096/Library/Refresh", timeout=10)
            logger.info(f"Jellyfin scan triggered: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Could not trigger Jellyfin scan: {e}")

        time.sleep(5)  # Wait for scan

        # Check if item exists in library
        cursor = jellyfin._conn.cursor()

        if is_series:
            # Check for series with matching IMDB ID
            cursor.execute("""
                SELECT Name, SeriesId FROM BaseItems
                WHERE Type = 'MediaBrowser.Controller.Entities.TV.Series'
                AND ProviderIds LIKE ?
            """, (f'%\"Imdb\":\"{imdb_id}\"%',))
            result = cursor.fetchone()
            if result:
                logger.info(f"Jellyfin identified series: {result['Name']}")

                # Check for season
                if season:
                    cursor.execute("""
                        SELECT Name, IndexNumber FROM BaseItems
                        WHERE SeriesId = ? AND Type = 'MediaBrowser.Controller.Entities.TV.Season' AND IndexNumber = ?
                    """, (result['SeriesId'], season))
                    season_result = cursor.fetchone()
                    if season_result:
                        logger.info(f"Jellyfin identified season {season}: {season_result['Name']}")
                        return True
                    else:
                        logger.warning(f"Season {season} not found in Jellyfin yet")
                return True
        else:
            # Check for movie
            cursor.execute("""
                SELECT Name FROM BaseItems
                WHERE Type = 'MediaBrowser.Controller.Entities.Movies.Movie'
                AND ProviderIds LIKE ?
            """, (f'%\"Imdb\":\"{imdb_id}\"%',))
            result = cursor.fetchone()
            if result:
                logger.info(f"Jellyfin identified movie: {result['Name']}")
                return True

        logger.warning("Content not yet identified by Jellyfin")
        return False
    finally:
        jellyfin.close()


# =============================================================================
# API Wrapper Functions (matching main.py expectations)
# =============================================================================

def search_rutracker(sb, driver, query: str, content_type: str, season: int = None, imdb_id: str = None):
    """Wrapper for on-demand search used by API.
    Returns list of dicts with torrent info.
    """
    is_series = content_type == 'series'
    try:
        # Use existing search_best_torrent which does full search + filtering
        best = search_best_torrent(query, is_series, season, imdb_id)
        return [{
            'title': best.title,
            'url': best.url,
            'quality': best.quality,
            'dub_studio': best.dub_studio,
            'seeders': best.seeders,
            'size_bytes': best.size_bytes,
            'size_gb': round(best.size_bytes / 1024**3, 2),
        }]
    except Exception as e:
        logger.warning(f'Search failed: {e}')
        return []


def select_best_torrent(results, content_type: str, season: int = None):
    """Wrapper - already filtered in search_rutracker."""
    if not results:
        return None
    return results[0]


def download_url_and_add_to_transmission(sb, driver, torrent_url: str, download_dir: str,
                                      tr_host: str, tr_port: int, tr_user: str, tr_password: str) -> bool:
    """Wrapper for on-demand download used by API."""
    from seleniumbase import SB
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import re
    import base64
    import requests
    from transmission_add import TransmissionManager
    from rutracker_scraper import RutrackerTorrent

    # Create torrent object from URL
    match = re.search(r't=(\d+)', torrent_url)
    if not match:
        raise RuntimeError('Could not extract topic ID from URL')
    topic_id = match.group(1)
    torrent = RutrackerTorrent(
        title='', url=torrent_url, size_bytes=0, size_str='', seeders=0, leechers=0,
        quality='', dub_studio='', is_excluded=False, has_dubbing=True,
        forum_id=0, topic_id=topic_id
    )

    # Use the existing driver (passed from main.py)
    try:
        # Download torrent file
        download_url = f'https://rutracker.org/forum/dl.php?t={topic_id}'
        logger.info(f'Downloading torrent from {download_url}')
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

        if result is None or (isinstance(result, str) and result.startswith(('HTTP_', 'FETCH_ERR'))):
            raise RuntimeError(f'Failed to download torrent: {result}')

        torrent_data = base64.b64decode(result)
        logger.info(f'Downloaded {len(torrent_data)} bytes')

        # Add to Transmission
        tr = TransmissionManager(host=tr_host, port=tr_port, username=tr_user, password=tr_password)
        if not tr.connect():
            raise RuntimeError('Failed to connect to Transmission')

        result = tr.add_torrent(torrent_data, download_dir)
        if not result.success:
            raise RuntimeError(f'Failed to add to Transmission: {result.error}')

        logger.info(f'Added to Transmission: {result.name} (ID: {result.torrent_id})')
        return True

    except Exception as e:
        logger.error(f'Download failed: {e}')
        return False


def verify_jellyfin(imdb_id: str, content_type: str, season: int = None) -> bool:
    """Verify Jellyfin identification for API."""
    is_series = content_type == 'series'
    return _verify_jellyfin_cli(imdb_id, is_series, season)


def main():
    parser = argparse.ArgumentParser(description="On-demand Rutracker search and download")
    parser.add_argument("query", help="Search query (e.g., 'The Simpsons')")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--movie", action="store_true", help="Search for movie")
    group.add_argument("--series", action="store_true", help="Search for TV series")
    parser.add_argument("--season", type=int, help="Season number (for series)")
    parser.add_argument("--imdb-id", help="IMDB ID for verification (e.g., tt0096697)")
    parser.add_argument("--wait", action="store_true", help="Wait for download to complete")
    parser.add_argument("--verify", action="store_true", help="Verify Jellyfin identification")

    args = parser.parse_args()

    if not all([LOGIN_RUTRACKER, PASSWORD_RUTRACKER, TR_HOST, TR_USER, TR_PASSWORD]):
        logger.error("Missing required environment variables")
        sys.exit(1)

    is_series = args.series
    if is_series and not args.season:
        logger.warning("No season specified for series, will search for any season")

    try:
        # Create session
        driver, sb = create_rutracker_session(LOGIN_RUTRACKER, PASSWORD_RUTRACKER)

        # Search for best torrent
        best_torrent = search_best_torrent(args.query, is_series, args.season, args.imdb_id)

        # Download and add to Transmission
        torrent_id = download_and_add_to_transmission(
            best_torrent, driver, is_series,
            imdb_id=args.imdb_id, title=args.query, season=args.season
        )

        # Wait for download if requested
        if args.wait:
            wait_for_download(torrent_id)

        # Verify Jellyfin if requested
        if args.verify and args.imdb_id:
            _verify_jellyfin_cli(args.imdb_id, is_series, args.season, args.query)

        sb.__exit__(None, None, None)
        logger.info("Done!")

    except Exception as e:
        logger.error(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
