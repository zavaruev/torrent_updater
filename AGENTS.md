# AGENTS.md

## Quick start
- **Local**: `python main.py` (requires `.env`)
- **Docker**: `docker-compose up -d --build`
- **Web UI**: http://localhost:6050

## ⚠️ Repository is PUBLIC (since Sep 2026)
- **All IPs/addresses and passwords live only in `.env`** (`.env` and any `*.env`,
  `config.env`, `hosts.env` are in `.gitignore`; the `.env.example` template is committed).
- **Hardcoding is forbidden**: private IPs, passwords, usernames in code, compose
  files or docs. `docker-compose.yml` takes DNS from `${HOME_DNS:?}` (`.env`).
- Pre-push check: `git grep -E "192\.168\.|10\.[0-9]+\." -- .` must be empty.
- WARNING: the Transmission password once leaked into commit history — the history
  was rewritten with `git filter-repo` (Sep 2026); treat the old password as
  compromised and never put it back into the code.

## Required variables (`.env`)
```
LOGIN_RUTRACKER=<login>
PASSWORD_RUTRACKER=<password>
TR_HOST=<transmission host>
TR_PORT=9091
TR_USER=<transmission user>
TR_PASSWORD=<transmission password>
DOWNLOAD_DIR=/path/to/downloads
```

## Optional: cookie login (captcha bypass)
If Rutracker requires a captcha on the login form, copy cookies from your browser
(F12 → Application → Cookies → `https://rutracker.org`: `bb_session`, `bb_data`):
```
RUTRACKER_BB_SESSION=<bb_session value>
RUTRACKER_BB_DATA=<bb_data value>
```
After adding them — `docker compose up -d`. Cookie login is tried first, the login
form is the fallback.

## Known limitation (Sep 2026)
Cloudflare shows an interactive check on torrent/tracker pages for automated
Chrome (datacenter IP + automation flags): the checkbox click is rejected server-side.
Date checks/search/download on these pages currently run into it; the index,
statuses, history and recommendations work. Search by query (`tracker.php?nm=`) is
implemented (`RutrackerScraper.search_tracker` + query-word filter) but awaits
passing the check for an E2E test.

## Jellyfin: media identification (Sep 2026)
- **DNS**: in `docker-compose.yml` Jellyfin has `dns: [<HOME_DNS>]` (the router IP
  from `.env`) — without it `api.themoviedb.org` is blocked (DNS returns
  127.0.0.1, even 8.8.8.8 is hijacked), all online providers fail with
  "Connection refused" and content is not identified. The router DNS (fake-ip
  tunnel) returns a working address. In this repository: `dns: ${HOME_DNS:?}`
  comes from `.env` — internal IPs are never stored in git.
- **NFO for movies**: `post_process_downloads.py` creates an NFO only when the
  torrent has an `imdb_*` label. Without an NFO Jellyfin uses the built-in MKV
  Title tag (in spartanec releases it is garbage: "Release by spartanec"). See
  TODO(imdb-resolve) in `main.py` — imdb_id resolution for manual search.
- **Correct imdbid**: The Simpsons Movie (2007) = `tt0462538`
  (NOT tt0449088 — that is Pirates of the Caribbean). Check via
  `https://v2.sg.media-imdb.com/suggestion/<letter>/<title>.json`.
- **DANGER: `DELETE /Items/{id}?deleteFile=false` in Jellyfin deletes files!**
  The parameter did not work (July API version), the log showed "Deleting item
  path ... .mkv" — a 25 GB file and the NFO were deleted and had to be
  re-downloaded. Never delete library items via the API — only re-identify
  (Refresh) or edit the NFO.
- **Refresh API**: `POST /Items/{id}/Refresh?MetadataRefreshMode=2&ImageRefreshMode=2`
  (the enum takes a number only: 2=Full/DownloadAll), auth:
  `Authorization: MediaBrowser Token=<key>`. The key is in the `ApiKeys` table
  (name `hermes`) in `jellyfin.db`.

## Hardware compatibility filter (LE-zal, Sep 2026)
- **LE-zal = Kodi 21.3**, in HomeAssistant the `kodi` entry → `<LE_ZAL_IP>:8080`
  (the box IP is stored in `.env` as `LE_ZAL_HOST`, never committed; there are
  also LE-Kitchen/LE-spalnya/LE-vlada — do not mix them up). User requirement:
  download only releases the box plays without problems.
- **Excluded: HEVC/x265/H265/H.265, 2160p/4K/UHD, HDR/HDR10, DV (Dolby Vision).**
  The only source is `EXCLUDE_KEYWORDS` in `movie-recommender/rutracker_scraper.py`;
  `on_demand_download.py` imports it (no local copy anymore).
- Matching — shared entry `is_excluded_title()`: word-start matching (`_kw_start_re`,
  lookbehind) for the main list + whole-word matching (`_whole_word_re`) for the
  short keys `EXCLUDE_WHOLE_WORDS` (`TS`, `TC`, `MOD`, `Scr`) — otherwise `'MOD'`
  hits "Modern Family", `'Scr'` — "Scrubs", `'TS'` — "Tsunami". Do not use
  substring matching: `'DV'` would catch "Adventure".
- Scoring tables (`quality_rank` in `search_and_download.py`/`recommender.py`,
  `QUALITY_RANK` in `on_demand_download.py`) have been cleared of 4K/HDR/DV/HEVC
  bonuses.
- Suitable for LE-zal: h264/AVC/x264, 1080p/720p, WEB-DL/BDRip/Remux.
  DTS audio: if the box has no receiver — Kodi downmixes (OK).

## Architecture
- **Entry point**: `main.py` — `check_and_update_torrents()` runs on schedule
- **Web server**: FastAPI on port 6050, template `templates/index.html`
- **Chrome**: headless undetected-chromedriver for login and Rutracker parsing
- **Transmission RPC**: `transmission-rpc` client for torrent management

## Key features
- Update checks every `CHECK_INTERVAL` (`hours`/`minutes`/`days`)
- Completed-season detection (marker `Серии: 1-X из X`) — auto-removal from Transmission
- The new torrent is downloaded before the old one is removed (fault tolerance)
- The last 100 log entries are buffered for the web UI

## Important
- Chrome is installed in Docker (Dockerfile lines 9-19)
- `set_page_load_timeout(120)` — bounds each page load so a Cloudflare challenge cannot stall the driver indefinitely (set in `create_uc_driver` and `_try_login`)
- Navigation via `execute_script("window.location.href=...")` in `_try_check_torrent` and `download_and_add_torrent` — avoids page_load_timeout blocking
- After clicking Login: `driver.get("index.php")` is wrapped in `try/except Exception` with `window.stop()` — Cloudflare may delay rendering even after a successful login; the renderer timeout does not block continuing
- Telegram notifications are commented out (require `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`)
