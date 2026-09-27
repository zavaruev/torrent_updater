# Hermes Agent Guide — torrent_updater

Instructions for the Hermes agent operating this service. **Guardrails** is the
mandatory section; everything else is reference material.

## What this service is

- Automated Rutracker update checker for a home media stack
  (Transmission + Jellyfin): compares local vs tracker dates, re-downloads
  updated torrents, auto-removes completed seasons.
- Recommendations engine: IMDb popular/top charts × newest Rutracker topics ×
  Jellyfin watched/owned state × Transmission contents. Movies are added to
  Transmission automatically; series are offered for manual approval.
- Web UI + JSON API on **port 6050**.

## Ground rules

- This repository is **PUBLIC**. Secrets and private IPs live only in `.env`
  (gitignored). Never write IPs, passwords or usernames into the repo, commit
  messages, docs or anything you share.
- Management is `docker compose` only, from the repo directory:
  `up -d --build`, `restart`, `logs -f`. Never `docker run`.
- If Hermes runs on another host, address the service as `http://<host>:6050`
  using values from your own configuration — do not persist them in this repo.
- Prefer the HTTP API below. Use `docker compose exec` only for the rare
  force-refresh recipe.

## Server-side schedule (no action needed)

| Job | When | Notes |
|---|---|---|
| Update check | every `CHECK_INTERVAL` × `CHECK_INTERVAL_UNIT` (`.env`, default 1 hour) | history/status via `/api/status` |
| Recommendations | daily **03:00** + catch-up if cache older than 24 h (30 min after boot) | full browser cycle ≈ 3 min |

## API reference

All endpoints: `http://localhost:6050` (or your host), plain JSON.
**All POST endpoints return HTTP 200 even on failure** — always check the
`success` field in the body.

### `GET /api/status`

```json
{"status": "idle|checking|updating", "last_check": "2026-09-27 11:52:05",
 "next_check": "2026-09-27 13:02:30", "total_updates": 0, "total_checks": 106,
 "history": [{"time": "11:58:27", "torrent": "...", "torrent_url": "...",
              "local_date": "2026-09-22", "tracker_date": "2026-09-22",
              "state": "ok|updated|error|season_complete", "error_msg": ""}],
 "logs": ["...last 100 log lines..."]}
```

- `status`: `idle` is the only quiet state; `checking`/`updating` mean a cycle
  is running right now.
- `history` state meanings: `ok` = up to date, `updated` = new version pulled,
  `season_complete` = finished season removed from Transmission,
  `error` = check failed (see `error_msg`).

### `GET /api/recommendations`

```json
{"timestamp": "2026-09-27T03:02:51.413131", "movies": [...], "series": [...]}
```

- Entries with `added=true` are dropped by the API (they are already in
  Transmission / visible in Jellyfin).
- **An empty list is frequently the correct answer** — see
  [Interpreting results](#interpreting-results).
- Series entries carry `season`, `episode`, `reason`, `rutracker_url`,
  `imdb_id` — everything needed for `/api/add-torrent`.

### `POST /api/check`

Body: none. → `{"message": "Check triggered"}`.

Fire-and-forget: the check runs in a background thread (1–3 min). Poll
`GET /api/status` until `status` returns to `idle` and `last_check` changes.
Do not trigger again while `status` is `checking`/`updating`.

### `POST /api/add-torrent`

Body:

```json
{"url": "https://rutracker.org/forum/viewtopic.php?t=NNNNNN",
 "type": "movie|series", "season": 4, "imdb_id": "tt1234567"}
```

- `season` and `imdb_id` are optional but recommended (series get an `SNN`
  label, both get `imdb_*` so Jellyfin gets an NFO for movies).
- Response: `{"success": true, "message": "Added to /series"}` or
  `{"success": false, "error": "..."}`.
- Takes **20–60 s** (opens an authenticated browser session; Cloudflare blocks
  plain HTTP downloads). On success the matching recommendation is marked
  `added=true` and disappears from `/api/recommendations`.
- This is the **manual approval path for series recommendations** (movies
  auto-download, but the button/endpoint works for both).

### `POST /api/search-and-download`

Body:

```json
{"query": "The Simpsons", "type": "movie|series", "season": 3,
 "imdb_id": "tt0096697", "verify": false}
```

- Response: `{"success": true, "torrent": {"title", "url", "quality",
  "dub_studio", "seeders", "size_gb", "kind", "download_dir"},
  "jellyfin_verified": true}` or `{"success": false, "error": "..."}`.
- Takes **1–3 min** (browser: search → best match → download → add).
- `verify: true` additionally polls Jellyfin for the identification result.

### Timing / pacing for every browser endpoint

| Operation | Duration |
|---|---|
| `/api/add-torrent` | 20–60 s |
| `/api/search-and-download` | 1–3 min |
| Recommendations cycle | ≈ 3 min |
| `/api/check` cycle | 1–3 min |

Never retry in a tight loop. If an endpoint fails with a Cloudflare-ish error:
wait ≥ 20–30 s, retry at most once or twice, then stop and report to the user.

## Interpreting results

Recommendation pipeline (each cycle logs counters):

- **Movies** (auto-added, up to 5 per run): must be ≤ 3 GB, seeders ≥ 5,
  IMDb ≥ 7.0, not in the hardware-exclusion list (no HEVC/4K/HDR/DV),
  has dubbing, **not watched/owned in Jellyfin**, **not already in
  Transmission**.
- **Series** (manual via Add): must be a show already in your Jellyfin library
  with at least one watched episode; the torrent's season must be
  **unwatched** (a season you started or finished is never re-offered) and not
  already in Transmission. Only "next season of a show you watch" is offered.
- **Empty recommendations are normal** when everything matching is already
  watched, owned or downloading. Diagnose from logs (see below) before
  reporting a problem.

Diagnosing "why N recommendations" — in logs or `/api/status` → `logs`:

```
Movie filter gates: {'total': 150, 'excluded': 38, ..., 'passed': 110,
                     'imdb_matched': 8, 'watched_owned': 8, 'in_transmission': 0}
Series filter gates: {'total': 33, ..., 'imdb_matched': 8, 'not_followed': 6,
                      'watched_season': 2, 'in_transmission': 0}
```

Healthy scrape lines: `Found 50 torrents on page N (50 new)`,
`No next-page link for this forum, done`, `Cookie session restored`.
Sick scrape lines: `giving up (challenged)`, `empty (clean but 0 rows)`,
`errors: [...]` non-empty in a cycle result.

## Forcing a recommendations refresh (rare)

Only when the cache timestamp is stale (> 24 h) and the catch-up did not run,
or after a code change. This opens a browser and takes ≈ 3 minutes — make sure
`GET /api/status` shows `status: "idle"` first (do not overlap with a check):

```bash
docker compose exec -T torrent-updater python3 - <<'EOF'
import sys, logging
sys.path.insert(0, '/app/movie-recommender')
sys.path.insert(0, '/app')
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
from recommender import run_daily_recommendation
res = run_daily_recommendation()
print({k: res[k] for k in ('movies_found', 'movies_added', 'series_found', 'errors')})
EOF
```

## Guardrails (MUST / NEVER)

- **NEVER** hardcode or publish IPs/passwords/usernames — `.env` only
  (pre-push check: `git grep -E "192\.168\.|10\.[0-9]+\." -- .` must be empty).
- **NEVER** delete Jellyfin items via `DELETE /Items/{id}` — even with
  `deleteFile=false` it deletes media files (real incident: 25 GB movie + NFO
  were lost). Use Refresh/NFO edits only.
- **NEVER** hammer Rutracker: one browser session at a time, 20–30 s pauses
  between requests. Do not run a check and a recommendations cycle
  concurrently; do not loop retries on failure — bursts raise Cloudflare's
  strictness for everyone.
- **NEVER** start a second browser flow while `/api/status` shows
  `checking`/`updating`.
- Cloudflare: the service self-solves challenges (wait-out + CDP). If you see
  repeated `giving up (challenged)` or `403` in logs — stop, wait 5–10 min,
  report; do not re-trigger.
- Login captcha: if logs show `cap_*` fields or repeated login failures —
  stop triggering logins (each failed attempt extends the flag) and report:
  a manual login + cookie refresh (`RUTRACKER_BB_SESSION` in `.env`) is needed.
- **Do not remove torrents** from Transmission unless the user asked;
  the updater tracks its torrents by the topic URL stored as the torrent
  comment and may auto-remove completed seasons.
- Restarting/rebuilding is safe (`docker compose up -d --build`) — state
  lives in `.env`, the cache file and Transmission.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Recommendations timestamp older than 24 h | 03:00 slot missed (container down) | restart container (catch-up fires in 30 min) or force-refresh recipe |
| `movies`/`series` empty, `errors: []` | filters did their job | read gate counters, report them — usually nothing to fix |
| `errors: [...]` in cycle result | scrape/IMDb/Jellyfin failure | check full logs; if Cloudflare — wait and re-check later |
| `status` stuck at `checking` for > 10 min | cycle crashed mid-run | `docker compose logs --tail=200 torrent-updater`, then `docker compose restart torrent-updater` |
| `/api/add-torrent` returns login/403 error | dead session cookies or captcha | report to user; do not retry in a loop |
| UI shows stale recommendations | background tab froze its poll | reload the page — the API is the source of truth |
