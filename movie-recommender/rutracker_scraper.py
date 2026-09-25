#!/usr/bin/env python3
"""
Rutracker forum scraper for movies (f=252) and TV shows (f=1803).
Uses SeleniumBase with CDP mode to bypass Cloudflare.
"""

import logging
import re
import time
from typing import List, Dict, Optional
from dataclasses import dataclass
from seleniumbase import SB

logger = logging.getLogger(__name__)

RUTRACKER_LOGIN = "https://rutracker.org/forum/login.php"
RUTRACKER_MOVIES = "https://rutracker.org/forum/viewforum.php?f=252"
RUTRACKER_TV = "https://rutracker.org/forum/viewforum.php?f=1803"

def _is_authed_page(src_lower: str, username: str) -> bool:
    """Ground-truth auth markers: no IS_GUEST flag + username or JS logout hook."""
    if not src_lower:
        return False
    if "is_guest: !!'1'" in src_lower:
        return False
    ul = (username or '').lower()
    return bool(ul and ul in src_lower) or 'logout: 1' in src_lower

# Dubbing detection keywords (series voiceovers included: MVO/LVO/VO/ПМ/ПД
# are the standard Rutracker translation tags for series packs).
DUB_KEYWORDS = [
    'DUB', 'Дублированный', 'Dubbing', 'Multi', 'Мульти',
    '2xDVD', 'BDRip-DUB', 'WEB-DL-DUB', 'WEB-DUB', 'Dual',
    'Дубляж', 'Дублирован', 'Мультиголос', 'Профессиональный',
    'MVO', 'LVO', 'VO', 'ПМ', 'ПД', 'Original', 'Оригинал',
    'Amedia', 'LostFilm', 'Novice', 'HDRezka', 'West Video',
    'Кубик в кубе', 'Дубль', 'TVShow', 'NewStudio', 'Jaskier',
    'AlexFilm', 'BaibaKo', 'MobilStudia', 'Vozrozhdenie',
    'Kinozal', 'HamsterStudio', 'DreamTeam', 'Edelweiss'
]

# Preferred dubbing studios
PREFERRED_DUB_STUDIOS = [
    'Кубик в кубе', 'West Video', 'Novice', 'HDRezka', 
    'LostFilm', 'Amedia', 'Дубль', 'NewStudio', 'TVShow',
    'Jaskier', 'AlexFilm', 'BaibaKo', 'MobilStudia'
]

# Exclude keywords (bad quality, single voice, cams)
EXCLUDE_KEYWORDS = [
    'Камрип', 'CAMRip', 'TS', 'TC', 'Scr', 'Screener', 
    'DVDRip', 'HDRip', 'одноголос', 'закадров', 
    'One Voice', 'Single Voice', 'одноголосый',
    'Перевод: Одноголосый', 'Перевод: Закадровый',
    'AMZN', 'iTunes', 'MOD', 'VHS', 'DVD5', 'DVD9',
    # LE-zal (Kodi 21.3, 192.0.2.164) НЕ воспроизводит эти форматы —
    # не скачивать раздачи с ними (требование пользователя, сент. 2026):
    # HEVC/x265/H265 — кодек, 2160p/4K/UHD — разрешение, HDR/HDR10 и
    # DV (Dolby Vision) — HDR-семейство цвета (без поддержки HDR даёт
    # зелёно-фиолетовую картинку). Синонимы перечислены все, т.к. релизы
    # подписаны по-разному ("HEVC", "x265", "H.265", "UHD-BD" и т.д.).
    # _kw_start_re() матчит по началу слова: "Adventure"/"Advance" НЕ
    # заденет 'DV' (lookbehind), а "HDRip" и так исключён выше.
    'HEVC', 'x265', 'H265', 'H.265',
    '2160p', '4K', 'UHD',
    'HDR', 'DV',
]

# Quality keywords (good quality for LE-Zal/Kodi)
# NB: 2160p/4K/HDR/DV/HEVC/x265 СУЩЕСТВЕННО убраны — они в EXCLUDE_KEYWORDS
# (LE-zal их не играет), здесь только то, что приставка точно воспроизводит.
QUALITY_KEYWORDS = [
    'WEB-DL', 'WEBRip', 'BDRip', 'BluRay', 'Remux',
    '1080p', '720p',
    'x264', 'AVC'
]


def _kw_start_re(kw: str):
    """Keyword must start at a word boundary (avoids 'TC' matching 'Match',
    'MOD' matching 'Modern', 'Scr' matching 'Description')."""
    return re.compile(r'(?<![a-zа-яё0-9])' + re.escape(kw.lower()))


@dataclass
class RutrackerTorrent:
    """Rutracker torrent entry."""
    topic_id: int
    title: str
    seeders: int
    leechers: int
    size_bytes: int
    size_str: str
    url: str
    forum_id: int  # 252=movies, 1803=tv
    has_dubbing: bool = False
    dub_studio: Optional[str] = None
    quality: Optional[str] = None
    is_excluded: bool = False


class RutrackerScraper:
    """Scrapes Rutracker forums for movies and TV shows."""

    def __init__(self, login: str, password: str):
        self.login = login
        self.password = password
        self.sb = None

    @classmethod
    def attach(cls, sb, login: str = '', password: str = ''):
        """Attach an already-logged-in SB session (no extra browser/login).
        Use this from API flows to avoid a second concurrent session — bursts
        of parallel sessions trigger Cloudflare strictness."""
        inst = cls(login, password)
        inst.sb = sb
        inst._sb_cm = None
        return inst

    def __enter__(self):
        self._sb_cm = SB(uc=True, chromium_arg="--enable-unsafe-swiftshader")
        self.sb = self._sb_cm.__enter__()
        self._login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._sb_cm:
            self._sb_cm.__exit__(exc_type, exc_val, exc_tb)

    def _harden_browser(self) -> None:
        try:
            self.sb.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': (
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

    def _try_cookie_login(self) -> bool:
        """Restores session from RUTRACKER_BB_SESSION / RUTRACKER_BB_DATA env cookies."""
        import os
        driver = self.sb.driver
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
        self._harden_browser()
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
                except Exception:
                    continue
                if _is_authed_page(src, self.login):
                    logger.info("Cookie session restored (auth markers present)")
                    driver.set_page_load_timeout(120)
                    return True
            logger.warning("Cookie session restore failed (still guest)")
            return False
        except Exception as e:
            logger.warning(f"Cookie login error: {e}")
            return False

    def _click_cf_checkbox(self) -> bool:
        """Clicks the Cloudflare Turnstile checkbox inside its iframe (no CDP)."""
        from selenium.webdriver.common.by import By
        driver = self.sb.driver
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

    def _login(self):
        """Login to Rutracker using UC + CDP mode to bypass Cloudflare Turnstile."""
        from selenium.webdriver.common.by import By
        import random

        sb = self.sb
        driver = sb.driver

        # 0) Cookie session restore first — no captcha needed.
        try:
            if self._try_cookie_login():
                return
        except Exception as e:
            logger.info(f"Cookie login skipped: {e}")

        for attempt in range(1, 4):
            try:
                logger.info(f"Logging into Rutracker (attempt {attempt}/3, plain UC, no CDP)...")
                try:
                    driver.execute_script("window.location.href = arguments[0]", RUTRACKER_LOGIN)
                except Exception as exc:
                    logger.info(f"Nav exec: {type(exc).__name__}")

                # Wait for the form; click the Turnstile checkbox if challenged.
                # (Manual iframe click — sb.solve_captcha exists only in CDP mode,
                # and CDP attach crashes this Chrome build.)
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
                            break  # redirected (session?) — handled below
                        fields = driver.find_elements(By.NAME, 'login_username')
                        if fields:
                            logger.info(f"Login form found | title: '{title}' | URL: {current_url}")
                            break
                        if not _clicked:
                            _clicked = self._click_cf_checkbox()
                    except Exception:
                        pass
                    time.sleep(3)
                else:
                    raise RuntimeError(f"Login form not found | title: '{title}'")

                if fields:
                    pass  # form path continues below
                elif 'login.php' not in driver.current_url:
                    # Redirected away from login.php with no form = active session
                    # (Rutracker sends logged-in users from login.php to index.php).
                    try:
                        src = driver.page_source.lower()
                    except Exception:
                        src = ''
                    if _is_authed_page(src, self.login):
                        logger.info(f"Already logged in via existing session | URL: {driver.current_url}")
                        driver.set_page_load_timeout(120)
                        return
                    logger.info("On index without auth markers yet, continuing to form check...")
                    fields = driver.find_elements(By.NAME, 'login_username')
                    if not fields:
                        raise RuntimeError(f"Login form not found | title: '{driver.title}' | URL: {driver.current_url}")
                else:
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        title = driver.title
                        if '521' in title or '520' in title or '522' in title or '503' in title:
                            raise RuntimeError(f"Server error: '{title}'")
                        fields = driver.find_elements(By.NAME, 'login_username')
                        if fields:
                            logger.info(f"Login form appeared after extra wait | title: '{title}'")
                            break
                        time.sleep(2)
                    else:
                        raise RuntimeError(f"Login form not found | title: '{driver.title}'")

                def _visible(name):
                    els = driver.find_elements(By.NAME, name)
                    vis = [e for e in els if e.is_displayed()]
                    return vis[0] if vis else (els[0] if els else None)

                username_field = _visible('login_username')
                password_field = _visible('login_password')
                login_btn = _visible('login')
                if not username_field or not password_field or not login_btn:
                    raise RuntimeError("Login form elements not found (visible)")

                page_src = driver.page_source
                if 'rutracker.org' not in page_src.lower() or 'login_username' not in page_src:
                    raise RuntimeError("Form found but page doesn't look like Rutracker login")

                try:
                    sb.clear('input[name="login_username"]')
                    sb.type('input[name="login_username"]', self.login)
                    sb.clear('input[name="login_password"]')
                    sb.type('input[name="login_password"]', self.password)
                except Exception as e:
                    logger.info(f"sb.type failed ({e}), using JS fill")
                    driver.execute_script("arguments[0].value = arguments[1]", username_field, self.login)
                    driver.execute_script("arguments[0].value = arguments[1]", password_field, self.password)
                try:
                    got_u = username_field.get_attribute('value') or ''
                    got_p = password_field.get_attribute('value') or ''
                    if len(got_u) != len(self.login) or len(got_p) != len(self.password):
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
                if not _is_authed_page(verify_src, self.login):
                    raise RuntimeError("Reached index as guest (no auth markers)")

                logger.info(f"Logged into Rutracker | URL: {current_url}")
                driver.set_page_load_timeout(120)
                return

            except Exception as e:
                logger.warning(f"Login attempt {attempt} failed: {e}")
                if attempt < 3:
                    time.sleep(random.uniform(10, 30))
                else:
                    raise

    def search_tracker(self, query: str, max_pages: int = 3) -> List[RutrackerTorrent]:
        """Search via tracker.php?nm=<query> (members only) and parse result rows.

        STATUS Sep 2026 — CODED but NEVER passed E2E (blocked by the Cloudflare
        wall, see module docstring in main.py). Parser targets the LIVE table
        layout (verified against a real screenshot: columns
        [dl] [status] ФОРУМ | ТЕМА | АВТОР | РАЗМЕР | S | L | C | ДОБАВЛЕН):
        header lookup by 'РАЗМЕР'+'ТЕМА', size via GB/MB regex, S/L = integer
        cells after size, forum_id from viewforum.php?f= link, plus a generic
        viewtopic-link fallback scan. Query-word/season filtering happens in
        search_best_torrent(). To verify when auth works: search 'simpsons'
        must return 300+ topics incl. season packs; first green signal.
        Raises on guest redirect (login.php?redirect=tracker).
        """
        from bs4 import BeautifulSoup
        from urllib.parse import quote_plus

        driver = self.sb.driver
        results: List[RutrackerTorrent] = []
        seen = set()
        for page in range(max_pages):
            url = f"https://rutracker.org/forum/tracker.php?nm={quote_plus(query)}"
            if page:
                url += f"&start={page * 50}"
                time.sleep(10)  # human pace between pages; bursts trigger CF
            logger.info(f"  Tracker search p{page + 1}: {query!r}")
            page_torrents = []
            # Up to 3 attempts per page: challenge may clear after CDP solve.
            for attempt in range(1, 4):
                try:
                    driver.execute_script("window.location.href = arguments[0]", url)
                    time.sleep(6)
                except Exception as e:
                    logger.warning(f"Tracker nav issue: {e}")
                # Wait out a possible CF challenge (managed sometimes auto-clears).
                src, current = '', ''
                deadline = time.time() + 30
                while time.time() < deadline:
                    try:
                        current = driver.current_url
                        src = driver.page_source
                    except Exception:
                        time.sleep(3)
                        continue
                    if 'login.php' in current or "IS_GUEST: !!'1'" in src:
                        raise RuntimeError("Tracker search bounced to login (no auth session)")
                    low = src.lower()
                    if 'just a moment' not in low and 'challenge-platform' not in low:
                        break
                    time.sleep(3)
                page_torrents = self._parse_tracker_page(src)
                if page_torrents:
                    break
                logger.info(f"  Tracker p{page + 1} attempt {attempt}: empty, {'CDP-solving' if attempt < 3 else 'giving up'}")
                if attempt < 3:
                    self._cdp_solve_page(url)
                    time.sleep(5)
            if not page_torrents:
                logger.info("  No (more) tracker results, stopping")
                break
            for t in page_torrents:
                if t.topic_id not in seen:
                    seen.add(t.topic_id)
                    results.append(t)
            logger.info(f"  Tracker page {page + 1}: {len(page_torrents)} results")
        return results

    def _cdp_solve_page(self, url: str) -> str:
        """Full CDP reload+solve for a challenged page (duplicated from main.py
        to avoid a circular import; keep the two in sync). Attach goes blank
        (normal), then CDP-navigate + AWAITED Turnstile solve + reconnect."""
        import asyncio
        sb = self.sb
        driver = sb.driver
        try:
            sb.activate_cdp_mode()
        except Exception as e:
            return f"attach-fail {type(e).__name__}"
        try:
            sb.goto(url)
        except Exception as e:
            return f"cdp-goto-fail {type(e).__name__}"
        time.sleep(8)
        try:
            res = sb.solve_captcha()
            if asyncio.iscoroutine(res):
                res = asyncio.run(res)
        except Exception as e:
            return f"solve-fail {type(e).__name__}"
        time.sleep(5)
        try:
            sb.connect()
        except Exception:
            pass
        try:
            ok = 'just a moment' not in driver.page_source.lower()
            return f"solved={res} clean={ok}"
        except Exception:
            return f"solved={res} clean=?"

    def _parse_tracker_page(self, page_source: str) -> List[RutrackerTorrent]:
        """Parse tracker.php search results. Single pass over EVERY table row
        with a topic link (header detection proved brittle — Rutracker uses
        td-based headers). Layout per live screenshot:
        [dl] [status] ФОРУМ | ТЕМА | АВТОР | РАЗМЕР | S | L | C | ДОБАВЛЕН.
        S/L = plain integer cells right after the size cell.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(page_source, 'lxml')
        torrents: List[RutrackerTorrent] = []
        seen = set()
        n_rows = 0

        for row in soup.find_all('tr'):
            try:
                links = row.find_all('a', href=re.compile(r'viewtopic\.php\?t=\d+'))
                if not links:
                    continue
                title_link = max(links, key=lambda a: len(a.get_text(strip=True)))
                title = title_link.get_text(strip=True)
                if len(title) < 5:
                    continue
                m = re.search(r't=(\d+)', title_link.get('href', ''))
                if not m:
                    continue
                topic_id = int(m.group(1))
                if topic_id in seen:
                    continue
                n_rows += 1

                cells = row.find_all(['td', 'th'])
                cell_texts = [c.get_text(' ', strip=True) for c in cells]
                size_bytes, size_str = 0, ''
                seeders, leechers = 0, 0
                size_idx = next(
                    (i for i, t in enumerate(cell_texts)
                     if re.search(r'\d[\d\s.,]*\s*(?:TB|GB|MB|KB)\b', t, re.IGNORECASE)),
                    None,
                )
                if size_idx is not None:
                    sm = re.search(r'(\d[\d\s.,]*\s*(?:TB|GB|MB|KB))\b', cell_texts[size_idx], re.IGNORECASE)
                    if sm:
                        size_str = sm.group(1)
                        size_bytes = self._parse_size(size_str)
                    # S/L: first two bare-integer cells after size (also
                    # handles combined "S L" cells via split fallback).
                    nums = []
                    # Whole-cell integers only (S, then L): thousand separators
                    # inside ONE cell ("1 234") must not merge with neighbours;
                    # a combined "10 0" cell still splits sanely.
                    for t in cell_texts[size_idx + 1:]:
                        tn = re.sub(r'\s+', ' ', t).strip()
                        if not re.fullmatch(r'[\d\s,]+', tn):
                            if nums:
                                break
                            continue
                        for g in re.findall(r'\d[\d,]*', tn):
                            try:
                                nums.append(int(g.replace(',', '')))
                            except Exception:
                                pass
                            if len(nums) >= 2:
                                break
                        if len(nums) >= 2:
                            break
                    if nums:
                        seeders = nums[0]
                    if len(nums) > 1:
                        leechers = nums[1]
                else:
                    # No size cell: try forum-style "seeders|leechers" text.
                    row_text = row.get_text(' ', strip=True)
                    pm = re.search(r'(\d[\d\s,]*)\s*\|\s*(\d[\d\s,]*)', row_text)
                    if pm:
                        try:
                            seeders = int(re.sub(r'[^\d]', '', pm.group(1)))
                        except Exception:
                            pass
                        try:
                            leechers = int(re.sub(r'[^\d]', '', pm.group(2)))
                        except Exception:
                            pass
                    sm = re.search(r'(\d[\d\s.,]*\s*(?:TB|GB|MB|KB))\b', row_text, re.IGNORECASE)
                    if sm:
                        size_str = sm.group(1)
                        size_bytes = self._parse_size(size_str)

                forum_id = 0
                fm = row.find('a', href=re.compile(r'viewforum\.php\?f=\d+'))
                if fm:
                    fmm = re.search(r'f=(\d+)', fm.get('href', ''))
                    if fmm:
                        forum_id = int(fmm.group(1))

                has_dubbing, dub_studio = self._check_dubbing(title)
                quality = self._check_quality(title)
                is_excluded = self._check_excluded(title)
                seen.add(topic_id)
                torrents.append(RutrackerTorrent(
                    topic_id=topic_id, title=title, seeders=seeders, leechers=leechers,
                    size_bytes=size_bytes, size_str=size_str,
                    url=f"https://rutracker.org/forum/viewtopic.php?t={topic_id}",
                    forum_id=forum_id, has_dubbing=has_dubbing, dub_studio=dub_studio,
                    quality=quality, is_excluded=is_excluded,
                ))
            except Exception:
                continue
        logger.info(f"  Tracker parse: {n_rows} topic rows -> {len(torrents)} torrents")
        return torrents

    def get_session_cookies(self) -> Dict[str, str]:
        """Get authenticated session cookies from the browser."""
        if not self.sb or not self.sb.driver:
            return {}
        cookies = {}
        for cookie in self.sb.driver.get_cookies():
            cookies[cookie['name']] = cookie['value']
        return cookies

    def scrape_forum(self, forum_url: str, forum_id: int, max_pages: int = 3) -> List[RutrackerTorrent]:
        """Scrape a forum for torrents."""
        from bs4 import BeautifulSoup
        
        logger.info(f"Scraping forum {forum_id}...")
        torrents = []
        driver = self.sb.driver
        
        for page in range(max_pages):
            url = f"{forum_url}&start={page * 50}" if page > 0 else forum_url
            logger.info(f"  Page {page + 1}: {url}")
            
            try:
                # Navigate via JS like main.py does
                driver.execute_script("window.location.href = arguments[0]", url)
                time.sleep(8)  # human pace; bursts trigger CF challenges
                
                # Try to solve captcha if present
                try:
                    self.sb.solve_captcha()
                    time.sleep(3)
                except:
                    pass
                
                page_source = driver.page_source
                page_torrents = self._parse_forum_page(page_source, forum_id)
                if not page_torrents:
                    logger.info("  No more torrents found, stopping")
                    break
                
                torrents.extend(page_torrents)
                logger.info(f"  Found {len(page_torrents)} torrents on page {page + 1}")
                
            except Exception as e:
                logger.error(f"Error scraping page {page + 1}: {e}")
                break
        
        return torrents

    def _parse_forum_page(self, page_source: str, forum_id: int) -> List[RutrackerTorrent]:
        """Parse torrents from forum page HTML."""
        from bs4 import BeautifulSoup
        
        torrents = []
        soup = BeautifulSoup(page_source, 'lxml')
        
        # Find torrent rows - Rutracker uses table with class 'forumline' or similar
        rows = soup.find_all('tr', class_='hl-tr') or \
               soup.find_all('tr', class_=re.compile(r'.*t.*')) or \
               soup.find_all('tr')
        
        for row in rows:
            try:
                torrent = self._parse_torrent_row(row, forum_id)
                if torrent:
                    torrents.append(torrent)
            except Exception as e:
                logger.debug(f"Failed to parse row: {e}")
                continue
        
        return torrents

    def _parse_torrent_row(self, row, forum_id: int) -> Optional[RutrackerTorrent]:
        """Parse a single torrent row."""
        # Get all cells
        cells = row.find_all('td')
        if len(cells) < 5:
            return None
        
        # Cell 1: title
        title_cell = cells[1]
        title_link = title_cell.find('a', href=re.compile(r'viewtopic\.php\?t=\d+'))
        if not title_link:
            return None
        
        topic_match = re.search(r't=(\d+)', title_link.get('href', ''))
        if not topic_match:
            return None
        topic_id = int(topic_match.group(1))
        
        title = title_link.get_text(strip=True)
        
        # Cell 2: seeders/leechers and size
        tor_cell = cells[2]
        tor_text = tor_cell.get_text(strip=True)
        
        # Parse seeders and leechers (format: "seeders|leechers")
        seeders = 0
        leechers = 0
        if '|' in tor_text:
            parts = tor_text.split('|')
            try:
                seeders = int(re.sub(r'[^\d]', '', parts[0] or '0') or 0)
            except:
                pass
            try:
                leechers = int(re.sub(r'[^\d]', '', parts[1] or '0') or 0)
            except:
                pass
        
        # Parse size from download link
        size_str = ""
        size_bytes = 0
        size_link = tor_cell.find('a', href=re.compile(r'dl\.php\?t=\d+'))
        if size_link:
            size_str = size_link.get_text(strip=True)
            size_bytes = self._parse_size(size_str)
        
        # Analyze title for dubbing, quality, exclusions
        has_dubbing, dub_studio = self._check_dubbing(title)
        quality = self._check_quality(title)
        is_excluded = self._check_excluded(title)
        
        return RutrackerTorrent(
            topic_id=topic_id,
            title=title,
            seeders=seeders,
            leechers=leechers,
            size_bytes=size_bytes,
            size_str=size_str,
            url=f"https://rutracker.org/forum/viewtopic.php?t={topic_id}",
            forum_id=forum_id,
            has_dubbing=has_dubbing,
            dub_studio=dub_studio,
            quality=quality,
            is_excluded=is_excluded
        )

    def _check_dubbing(self, title: str) -> tuple:
        """Check if title indicates dubbing (not single voice)."""
        title_lower = title.lower()

        # Check for exclusion keywords first
        for ex in EXCLUDE_KEYWORDS:
            if _kw_start_re(ex).search(title_lower):
                return False, None

        # Check for preferred studios
        for studio in PREFERRED_DUB_STUDIOS:
            if _kw_start_re(studio).search(title_lower):
                return True, studio

        # Check for general dubbing keywords
        for kw in DUB_KEYWORDS:
            if _kw_start_re(kw).search(title_lower):
                return True, "Unknown"

        return False, None

    def _check_quality(self, title: str) -> Optional[str]:
        """Extract quality from title - combine source + resolution."""
        title_lower = title.lower()
        
        # Source types (prefer order)
        source = None
        for s in ['Remux', 'BluRay', 'BDRip', 'WEBRip', 'WEB-DL']:
            if s.lower() in title_lower:
                source = s
                break
        
        # Resolution
        resolution = None
        for r in ['2160p', '4K', '1080p', '720p']:
            if r.lower() in title_lower:
                resolution = r
                break
        
        # HDR/DV
        hdr = None
        for h in ['DV', 'HDR']:
            if h.lower() in title_lower:
                hdr = h
                break
        
        # Codec (only if no source/resolution found)
        codec = None
        if not source and not resolution:
            for c in ['HEVC', 'x265', 'x264', 'AVC']:
                if c.lower() in title_lower:
                    codec = c
                    break
        
        # Build combined quality string
        parts = []
        if source:
            parts.append(source)
        if resolution:
            parts.append(resolution)
        if hdr:
            parts.append(hdr)
        if not parts and codec:
            parts.append(codec)
        
        return ' '.join(parts) if parts else None

    def _check_excluded(self, title: str) -> bool:
        """Check if title has exclusion keywords."""
        title_lower = title.lower()
        for ex in EXCLUDE_KEYWORDS:
            if _kw_start_re(ex).search(title_lower):
                return True
        return False

    def _parse_size(self, size_str: str) -> int:
        """Parse size string to bytes."""
        size_str = re.sub(r'\s+', '', size_str.upper().replace(',', ''))
        multipliers = {'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}

        for unit, mult in multipliers.items():
            if size_str.endswith(unit):
                try:
                    return int(float(size_str[:-len(unit)]) * mult)
                except:
                    pass
        return 0

    def download_torrent_file(self, rutracker_url: str) -> Optional[str]:
        """Get magnet link using the authenticated browser session."""
        import re

        if not self.sb or not self.sb.driver:
            logger.error("No browser session available for torrent download")
            return None

        # Extract topic ID
        match = re.search(r't=(\d+)', rutracker_url)
        if not match:
            return None

        driver = self.sb.driver

        try:
            # Navigate to topic page
            driver.execute_script("window.location.href = arguments[0]", rutracker_url)
            time.sleep(3)

            # Poll for page load
            deadline = time.time() + 30
            current_url = ''
            while time.time() < deadline:
                try:
                    current_url = driver.current_url
                    if current_url and ('login.php' in current_url or 'viewtopic.php' in current_url):
                        break
                except Exception:
                    pass
                time.sleep(1)

            if 'login.php' in current_url:
                logger.error("Session expired during torrent download")
                return None

            # Get page HTML and find magnet link
            page_html = driver.page_source
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(page_html, 'lxml')
            
            # Find magnet link (class='magnet-link')
            magnet_link = soup.find('a', class_='magnet-link')
            if not magnet_link:
                logger.warning(f"Could not find magnet link for {rutracker_url}")
                return None

            magnet_url = magnet_link['href']
            if magnet_url.startswith('magnet:'):
                logger.info(f"Found magnet link for {rutracker_url}")
                return magnet_url
            else:
                logger.warning(f"Magnet link has unexpected format: {magnet_url}")
                return None

        except Exception as e:
            logger.error(f"Error getting magnet link: {e}")
            return None


def scrape_rutracker_movies(login: str, password: str, max_pages: int = 3) -> List[RutrackerTorrent]:
    """Convenience function to scrape movies forum."""
    with RutrackerScraper(login, password) as scraper:
        return scraper.scrape_forum(RUTRACKER_MOVIES, 252, max_pages)


def scrape_rutracker_tv(login: str, password: str, max_pages: int = 3) -> List[RutrackerTorrent]:
    """Convenience function to scrape TV forum."""
    with RutrackerScraper(login, password) as scraper:
        return scraper.scrape_forum(RUTRACKER_TV, 1803, max_pages)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Test with env vars
    import os
    from dotenv import load_dotenv
    # load_dotenv('/mnt/media/docker-compose/torrent_updater/.env')  # load_dotenv() will find .env in working dir
    
    login = os.getenv('LOGIN_RUTRACKER')
    password = os.getenv('PASSWORD_RUTRACKER')
    
    if login and password:
        print("Testing movie scrape...")
        movies = scrape_rutracker_movies(login, password, max_pages=1)
        print(f"Found {len(movies)} movies")
        for m in movies[:5]:
            print(f"  {m.title[:80]} | Seeds: {m.seeders} | Size: {m.size_str} | Dub: {m.has_dubbing} ({m.dub_studio}) | Quality: {m.quality} | Excl: {m.is_excluded}")
        
        print("\nTesting TV scrape...")
        tv = scrape_rutracker_tv(login, password, max_pages=1)
        print(f"Found {len(tv)} TV shows")
        for t in tv[:5]:
            print(f"  {t.title[:80]} | Seeds: {t.seeders} | Size: {t.size_str} | Dub: {t.has_dubbing} ({t.dub_studio}) | Quality: {t.quality} | Excl: {t.is_excluded}")
    else:
        print("No credentials in .env")