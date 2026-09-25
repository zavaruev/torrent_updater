# torrent_updater

Automatic updating of TV series and movies from RuTracker into Transmission, with
a web interface and Jellyfin integration.

The service periodically checks tracked torrents; when an update appears (including
season completion) it downloads the new torrent **before** removing the old one,
supports manual search by query and movie/series recommendations, filtering
releases for compatibility with the home media setup.

## Quick start

**Docker (recommended):**
```bash
docker compose up -d --build
```

**Local:**
```bash
python main.py          # requires .env in the directory
```

**Web interface:** http://localhost:6050

## Configuration (`.env`)

Template: copy `.env.example` to `.env` and fill in your own values.
The `.env` file (all addresses/passwords, including internal IPs) is listed in
`.gitignore` and never reaches the repository.

Required variables:

| Variable | Description |
|---|---|
| `LOGIN_RUTRACKER` / `PASSWORD_RUTRACKER` | RuTracker credentials |
| `TR_HOST`, `TR_PORT` | Transmission RPC address and port |
| `TR_USER`, `TR_PASSWORD` | Transmission credentials |
| `DOWNLOAD_DIR` | download directory |
| `HOME_DNS` | router DNS IP, used in `docker-compose.yml` (`dns: ${HOME_DNS}`) |
| `JELLYFIN_URL` | Jellyfin address (for library scan triggers) |

Optional:

| Variable | Description |
|---|---|
| `LE_ZAL_HOST` | address of the LE-zal set-top box (reference only, see AGENTS.md) |
| `RUTRACKER_BB_SESSION`, `RUTRACKER_BB_DATA` | browser cookies for login instead of the form (captcha bypass) |
| `CHECK_INTERVAL`, `CHECK_INTERVAL_UNIT` | check period (`minutes`/`hours`/`days`), default 1 hour |
| `RUN_ON_STARTUP` | run a check right on startup |
| `FORCE_FORM_LOGIN` | force form login (ignores cookies) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram notifications (disabled in code) |

Cookie login is tried first, the login form is the fallback. Cookies can be copied
from the browser: F12 → Application → Cookies → `https://rutracker.org`.

## Features

- **Scheduled auto-update** — checks every `CHECK_INTERVAL`; the last 100 log
  entries are available in the web interface.
- **Completed-season detection** — on the `Серии: 1-X из X` marker the torrent is
  automatically removed from Transmission.
- **Failure-safe updates** — the new torrent is downloaded first, and only then
  the old one is removed.
- **Manual search** (`POST /api/search-and-download`) — a RuTracker query, best
  result picked by scoring and added to Transmission; the content type
  (series/movie) is detected automatically and routed to `/series` or `/movies`.
- **Recommendations** (`GET /api/recommendations`) — movies and series based on
  IMDb data; already-downloaded content is fully hidden from the results.
- **Targeted add** (`POST /api/add-torrent`) — add by torrent URL.
- **Manual check** (`POST /api/check`) — run outside the schedule.

### Hardware compatibility filter

Releases that the home equipment (LE-zal / Kodi) cannot play are cut off at the
parsing stage. **Excluded:** HEVC/x265/H265, 2160p/4K/UHD, HDR/HDR10, DV (Dolby
Vision), as well as CAM/TS/Screener/single-voice and others. **Suitable:**
h264/AVC/x264, 1080p/720p, WEB-DL/BDRip/Remux.

The exclusion list is `EXCLUDE_KEYWORDS` in `movie-recommender/rutracker_scraper.py`
(single source of truth; the other modules import it).

## Architecture

- `main.py` — entry point: scheduling, FastAPI web server (port 6050), torrent
  check logic.
- `movie-recommender/` — RuTracker parsing (headless Chrome via SeleniumBase),
  scoring and filtering, recommendations, download post-processing.
- `templates/index.html` — web interface.
- Transmission is controlled via `transmission-rpc`.

## Known limitations

- **Cloudflare**: on torrent/tracker pages automated Chrome gets an interactive
  challenge that the server rejects. Index, statuses, history and recommendations
  work; the end-to-end search path awaits a solution (see `AGENTS.md`). Do not
  force these checks: bursts of requests raise Cloudflare's strictness — keep one
  session browser with 20–30 second pauses between navigations.
- **Telegram notifications** are commented out in the code.

## Jellyfin

Content is identified in Jellyfin by the NFO file, which is created when the
torrent has an `imdb_*` label. Note:

- Jellyfin needs a working outbound DNS (without it TMDB/IMDb are unreachable);
- **never delete library items via `DELETE /Items`** — in the current API version
  this also deletes the files themselves despite `deleteFile=false`; use Refresh
  or edit the NFO instead.

Details and the recipe are in [`AGENTS.md`](AGENTS.md).

## License

Personal project, no license specified.
