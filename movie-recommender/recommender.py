#!/usr/bin/env python3
"""
Main recommender logic.
Coordinates IMDB scraping, Rutracker scraping, Jellyfin sync, and Transmission addition.
"""

import logging
import os
import json
import requests
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from jellyfin_sync import JellyfinSync
from imdb_scraper import IMDBScraper, IMDBItem
from rutracker_scraper import RutrackerScraper, RutrackerTorrent, scrape_rutracker_movies, scrape_rutracker_tv, RUTRACKER_MOVIES, RUTRACKER_TV
from transmission_add import TransmissionManager, add_movie_torrent_from_url, add_series_torrent_from_url, MOVIES_DOWNLOAD_DIR, SERIES_DOWNLOAD_DIR

logger = logging.getLogger(__name__)

# Configuration
MOVIE_MAX_SIZE_GB = 3
SERIES_MAX_EPISODE_GB = 2
MIN_IMDB_RATING = 7.0
MIN_SEEDERS = 5
MAX_MOVIES_PER_RUN = 5
MAX_SERIES_PER_RUN = 3

RECOMMENDATIONS_CACHE = "/opt/data/cache/movie_recommendations.json"

# Telegram config (loaded from env in run_daily_recommendation)
TELEGRAM_BOT_TOKEN = None
TELEGRAM_CHAT_ID = None


@dataclass
class MovieRecommendation:
    """Movie recommendation with all metadata."""
    title: str
    year: Optional[int]
    imdb_rating: float
    imdb_id: str
    rutracker_title: str
    rutracker_url: str
    seeders: int
    size_gb: float
    dub_studio: Optional[str]
    quality: Optional[str]
    reason: str  # Why this was recommended
    added: bool = False


@dataclass
class SeriesRecommendation:
    """Series recommendation with all metadata."""
    title: str
    year: Optional[int]
    imdb_rating: float
    imdb_id: str
    rutracker_title: str
    rutracker_url: str
    seeders: int
    size_gb: float
    dub_studio: Optional[str]
    quality: Optional[str]
    season: int
    episode: Optional[int]
    reason: str
    added: bool = False


class MovieRecommender:
    """Main recommendation engine."""

    def __init__(self, rutracker_login: str, rutracker_password: str):
        self.rutracker_login = rutracker_login
        self.rutracker_password = rutracker_password
        self.jellyfin = JellyfinSync()
        self.imdb = IMDBScraper(min_rating=MIN_IMDB_RATING)
        self.transmission = TransmissionManager()

        # Cache for recommendations
        self.movie_recommendations: List[MovieRecommendation] = []
        self.series_recommendations: List[SeriesRecommendation] = []

    def run_daily_recommendation(self) -> Dict:
        """Run full daily recommendation cycle."""
        logger.info("=== Starting daily recommendation cycle ===")
        start_time = datetime.now()

        results = {
            'timestamp': start_time.isoformat(),
            'movies_found': 0,
            'movies_added': 0,
            'series_found': 0,
            'series_added': 0,
            'movie_recommendations': [],
            'series_recommendations': [],
            'errors': []
        }

        try:
            # 1. Refresh Jellyfin DB
            logger.info("Refreshing Jellyfin DB...")
            self.jellyfin.refresh_db_copy()
            self.jellyfin.connect()

            # 2. Get IMDB popular movies and TV
            logger.info("Fetching IMDB popular charts...")
            imdb_data = self.imdb.get_all_popular(limit_per_chart=30)

            # 3. Scrape Rutracker (reuse single session for both forums)
            logger.info("Scraping Rutracker movies and TV...")
            scraper = RutrackerScraper(self.rutracker_login, self.rutracker_password)
            scraper.__enter__()
            try:
                movie_torrents = scraper.scrape_forum(RUTRACKER_MOVIES, 252, max_pages=3)
                tv_torrents = scraper.scrape_forum(RUTRACKER_TV, 1803, max_pages=3)

                # 4. Match and filter movies
                logger.info("Matching movies...")
                movie_recs = self._match_and_filter_movies(imdb_data, movie_torrents)
                results['movies_found'] = len(movie_recs)
                self.movie_recommendations = movie_recs

                # 5. Match and filter series
                logger.info("Matching series...")
                series_recs = self._match_and_filter_series(imdb_data, tv_torrents)
                results['series_found'] = len(series_recs)
                self.series_recommendations = series_recs

                # 6. Add top movies to Transmission
                logger.info("Adding movies to Transmission...")
                added_movies = self._add_top_movies(movie_recs[:MAX_MOVIES_PER_RUN], scraper)
                results['movies_added'] = added_movies

                # 7. Series: only recommend, DO NOT auto-add (require manual approval)
                logger.info("Series recommendations ready (manual approval required)")
                results['series_added'] = 0

            finally:
                scraper.__exit__(None, None, None)

            # 8. Save recommendations cache for web UI
            self._save_recommendations_cache()
            results['movie_recommendations'] = [asdict(r) for r in movie_recs[:10]]
            results['series_recommendations'] = [asdict(r) for r in series_recs[:10]]

        except Exception as e:
            logger.error(f"Error in recommendation cycle: {e}")
            results['errors'].append(str(e))

        finally:
            self.jellyfin.close()

        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"=== Recommendation cycle complete in {elapsed:.1f}s ===")
        logger.info(f"Movies: {results['movies_found']} found, {results['movies_added']} added")
        logger.info(f"Series: {results['series_found']} found, {results['series_added']} added")

        # Send Telegram notification
        self._send_telegram_summary(results)

        return results

    def _send_telegram_summary(self, results: Dict):
        """Send Telegram notification with daily recommendation summary."""
        global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.debug("Telegram not configured, skipping notification")
            return

        try:
            # Build message
            msg_lines = [
                f"🎬 <b>Daily Recommendations — {datetime.now().strftime('%d.%m.%Y')}</b>",
                ""
            ]

            if results['movies_added'] > 0:
                msg_lines.append(f"🎥 <b>Фильмы добавлено:</b> {results['movies_added']}")
                for rec in results['movie_recommendations'][:results['movies_added']]:
                    size = f"{rec['size_gb']}GB" if rec.get('size_gb') else ""
                    imdb = f"IMDb {rec['imdb_rating']}" if rec.get('imdb_rating') else ""
                    seeders = f"{rec['seeders']}s" if rec.get('seeders') else ""
                    meta = " • ".join(filter(None, [imdb, seeders, size]))
                    msg_lines.append(f"  • {rec['title']} ({meta})")
                msg_lines.append("")

            if results['movies_found'] > results['movies_added']:
                msg_lines.append(f"📋 <b>Фильмы в рекомендациях:</b> {results['movies_found'] - results['movies_added']} шт.")

            if results['series_added'] > 0:
                msg_lines.append(f"📺 <b>Сериалы добавлено:</b> {results['series_added']}")
                for rec in results['series_recommendations'][:results['series_added']]:
                    imdb = f"IMDb {rec['imdb_rating']}" if rec.get('imdb_rating') else ""
                    seeders = f"{rec['seeders']}s" if rec.get('seeders') else ""
                    season = f"S{rec['season']:02d}" if rec.get('season') else ""
                    meta = " • ".join(filter(None, [imdb, seeders, season]))
                    msg_lines.append(f"  • {rec['title']} {season} ({meta})")
                msg_lines.append("")

            if results['series_found'] > results['series_added']:
                msg_lines.append(f"📋 <b>Сериалы в рекомендациях:</b> {results['series_found'] - results['series_added']} шт.")

            if results['errors']:
                msg_lines.append(f"⚠️ <b>Ошибки:</b> {len(results['errors'])}")
                for err in results['errors'][:3]:
                    msg_lines.append(f"  • {err[:100]}")

            message = "\n".join(msg_lines)

            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info("Telegram summary sent successfully")
            else:
                logger.warning(f"Telegram send failed: {resp.status_code} - {resp.text}")

        except Exception as e:
            logger.error(f"Failed to send Telegram summary: {e}")

    def _score_torrent(self, torrent: RutrackerTorrent) -> int:
        """Score torrent based on quality, dub studio, seeders, size. Higher = better."""
        score = 0

        # Quality ranking (higher = better) - matches new format "SOURCE RESOLUTION HDR"
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

        # Dub studio preference (preferred studios get bonus)
        preferred_studios = [
            'LostFilm', 'TVShows', 'BaibaKo', 'Octopus', 'NewStudio', 
            'Jaskier', 'AlexFilm', 'Red Head Sound', 'HamsterStudio',
            'DreamTeam', 'Edelweiss', 'Amedia', 'KuboKube', 'Kinozal',
            'Novice', 'HDRezka', 'West Video', 'MobilStudia', 'Vozrozhdenie'
        ]
        if torrent.dub_studio:
            for i, studio in enumerate(preferred_studios):
                if studio.lower() in torrent.dub_studio.lower():
                    score += 50 - i  # Higher score for more preferred
                    break
        
        # Seeders (capped at 500 for scoring)
        score += min(torrent.seeders, 500) // 10
        
        # Size bonus for reasonable sizes (not too small, not too large)
        size_gb = torrent.size_bytes / 1024**3
        if 1.5 <= size_gb <= 30:
            score += 10
        
        return score

    def _match_and_filter_movies(self, imdb_data: Dict, rutracker_torrents: List[RutrackerTorrent]) -> List[MovieRecommendation]:
        """Match IMDB movies with Rutracker torrents and filter."""
        # Build IMDB movie lookup (by normalized title)
        imdb_movies = {}
        for chart_name, items in imdb_data.items():
            if 'movie' in chart_name:
                for item in items:
                    key = self._normalize_title(item.title)
                    if key not in imdb_movies or item.rating > imdb_movies[key].rating:
                        imdb_movies[key] = item

        logger.info(f"IMDB movies loaded: {len(imdb_movies)}")

        # Group torrents by IMDB match
        matches = {}  # imdb_id -> list of torrents
        for torrent in rutracker_torrents:
            # Skip excluded, no dubbing, too large, too few seeders
            if torrent.is_excluded:
                continue
            if not torrent.has_dubbing:
                continue
            if torrent.size_bytes > MOVIE_MAX_SIZE_GB * 1024**3:
                continue
            if torrent.seeders < MIN_SEEDERS:
                continue
            if not torrent.quality:
                continue

            # Try to match with IMDB
            norm_title = self._normalize_title(torrent.title)
            imdb_item = self._find_imdb_match(norm_title, imdb_movies)

            if imdb_item:
                # Check if already watched or in library
                if self.jellyfin.is_movie_watched_or_owned(imdb_item.title, imdb_item.year):
                    logger.debug(f"Skipping already watched/owned: {imdb_item.title}")
                    continue
                
                if imdb_item.imdb_id not in matches:
                    matches[imdb_item.imdb_id] = []
                matches[imdb_item.imdb_id].append((imdb_item, torrent))

        # For each IMDB match, pick the best torrent
        recommendations = []
        for imdb_id, torrents in matches.items():
            # Sort by score, pick best
            best_imdb, best_torrent = max(torrents, key=lambda x: self._score_torrent(x[1]))
            
            reason = f"IMDB {best_imdb.rating}/10, {best_torrent.seeders} seeders, {best_torrent.quality}, {best_torrent.dub_studio or 'Dub'}"
            rec = MovieRecommendation(
                title=best_imdb.title,
                year=best_imdb.year,
                imdb_rating=best_imdb.rating,
                imdb_id=best_imdb.imdb_id,
                rutracker_title=best_torrent.title,
                rutracker_url=best_torrent.url,
                seeders=best_torrent.seeders,
                size_gb=round(best_torrent.size_bytes / 1024**3, 2),
                dub_studio=best_torrent.dub_studio,
                quality=best_torrent.quality,
                reason=reason
            )
            recommendations.append(rec)

        # Sort by IMDB rating * seeders (popularity + quality)
        recommendations.sort(key=lambda r: r.imdb_rating * (1 + r.seeders / 100), reverse=True)

        logger.info(f"Movie recommendations after filtering: {len(recommendations)}")
        return recommendations

    def _match_and_filter_series(self, imdb_data: Dict, rutracker_torrents: List[RutrackerTorrent]) -> List[SeriesRecommendation]:
        """Match IMDB TV shows with Rutracker torrents and filter."""
        recommendations = []

        # Build IMDB TV lookup
        imdb_tv = {}
        for chart_name, items in imdb_data.items():
            if 'tv' in chart_name:
                for item in items:
                    key = self._normalize_title(item.title)
                    if key not in imdb_tv or item.rating > imdb_tv[key].rating:
                        imdb_tv[key] = item

        logger.info(f"IMDB TV shows loaded: {len(imdb_tv)}")

        # Get watched series progress
        watched_progress = self.jellyfin.get_watched_series_progress()
        library_series = self.jellyfin.get_library_series()

        # Group torrents by IMDB match + season
        matches = {}  # (imdb_id, season) -> list of (imdb_item, torrent, reason)

        for torrent in rutracker_torrents:
            # Skip excluded, no dubbing, too few seeders
            if torrent.is_excluded:
                continue
            if not torrent.has_dubbing:
                continue
            if torrent.seeders < MIN_SEEDERS:
                continue
            if not torrent.quality:
                continue

            # Try to match with IMDB
            norm_title = self._normalize_title(torrent.title)
            imdb_item = self._find_imdb_match(norm_title, imdb_tv)

            if imdb_item:
                series_name = imdb_item.title.lower()

                # Extract season/episode from torrent title
                season, episode = self._extract_season_episode(torrent.title)

                if season is None:
                    continue

                # Determine if this is a new season of watched show
                watched_seasons = watched_progress.get(series_name, {}).get('seasons', set())
                in_library = series_name in library_series

                # Only recommend if user has watched previous seasons of this show
                if not in_library or len(watched_seasons) == 0:
                    # New show or never watched - skip
                    continue

                is_new_season = season not in watched_seasons

                if is_new_season:
                    reason = f"New season of watched show, IMDB {imdb_item.rating}/10, S{season}"
                else:
                    reason = f"Continuation of watched show (watched S{sorted(watched_seasons)}), IMDB {imdb_item.rating}/10, S{season}"

                key = (imdb_item.imdb_id, season)
                if key not in matches:
                    matches[key] = []
                matches[key].append((imdb_item, torrent, reason))

        # For each IMDB match + season, pick the best torrent
        recommendations = []
        for (imdb_id, season), torrents in matches.items():
            # Sort by score, pick best
            best_imdb, best_torrent, reason = max(torrents, key=lambda x: self._score_torrent(x[1]))

            rec = SeriesRecommendation(
                title=best_imdb.title,
                year=best_imdb.year,
                imdb_rating=best_imdb.rating,
                imdb_id=best_imdb.imdb_id,
                rutracker_title=best_torrent.title,
                rutracker_url=best_torrent.url,
                seeders=best_torrent.seeders,
                size_gb=round(best_torrent.size_bytes / 1024**3, 2),
                dub_studio=best_torrent.dub_studio,
                quality=best_torrent.quality,
                season=season,
                episode=episode,
                reason=reason
            )
            recommendations.append(rec)

        # Sort: continuations first, then high-rated new shows
        recommendations.sort(key=lambda r: (
            0 if "Continuation" in r.reason or "New season" in r.reason else 1,
            -r.imdb_rating,
            -r.seeders
        ))

        logger.info(f"Series recommendations after filtering: {len(recommendations)}")
        return recommendations

    def _find_imdb_match(self, norm_title: str, imdb_dict: Dict) -> Optional[IMDBItem]:
        """Find best IMDB match for normalized title."""
        # Direct match
        if norm_title in imdb_dict:
            return imdb_dict[norm_title]

        # Fuzzy match - check if IMDB title contains our title or vice versa
        for key, item in imdb_dict.items():
            if norm_title in key or key in norm_title:
                return item

        return None

    def _normalize_title(self, title: str) -> str:
        """Normalize title for matching."""
        import re
        title = title.lower()
        title = re.sub(r'\[.*?\]', '', title)
        title = re.sub(r'\(.*?\)', '', title)
        title = re.sub(r'\b(1080p|720p|4K|2160p|HDR|DV|WEB-DL|BDRip|BluRay|Remux|WEB|DL|H264|H265|x264|x265|HEVC|AVC|AAC|AC3|DTS|5\.1|7\.1|Atmos)\b', '', title)
        title = re.sub(r'\b(DUB|Дублированный|Dubbing|Multi|Мульти|2xDVD|BDRip-DUB|WEB-DL-DUB|WEB-DUB|Dual|Дубляж|Дублирован|Мультиголос|Профессиональный|Amedia|LostFilm|Novice|HDRezka|West Video|Кубик в кубе|Дубль|TVShow|NewStudio|Jaskier|AlexFilm|BaibaKo|MobilStudia|Vozrozhdenie|Kinozal|HamsterStudio|DreamTeam|Edelweiss)\b', '', title, flags=re.IGNORECASE)
        title = re.sub(r'\s+', ' ', title).strip()
        return title

    def _extract_season_episode(self, title: str) -> tuple:
        """Extract season and episode from torrent title."""
        import re

        # Patterns: S01E01, S01, Season 1, 1 сезон, 1 серия, Сезон: 4, Сезон 4
        patterns = [
            r'S(\d{1,2})[Ee](\d{1,2})',           # S01E01
            r'S(\d{1,2})\b',                       # S01
            r'Season\s+(\d{1,2})',                 # Season 1
            r'Сезон\s*[:]?\s*(\d{1,2})',           # Сезон: 4, Сезон 4
            r'(\d{1,2})\s*сезон',                  # 1 сезон
            r'(\d{1,2})\s*сер(ия|\.|$)',           # 1 серия
        ]

        for pattern in patterns:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                season = int(match.group(1))
                episode = int(match.group(2)) if len(match.groups()) > 1 and match.group(2) else None
                return season, episode

        return None, None

    def _add_top_movies(self, recommendations: List[MovieRecommendation], scraper) -> int:
        """Add top movie recommendations to Transmission."""
        added = 0

        if not self.transmission.connect():
            logger.error("Failed to connect to Transmission for movies")
            return 0

        for rec in recommendations:
            if rec.added:
                continue

            # Download torrent file from Rutracker using browser session
            magnet_url = scraper.download_torrent_file(rec.rutracker_url)

            if magnet_url:
                result = self.transmission.add_torrent_from_url(
                    magnet_url,
                    MOVIES_DOWNLOAD_DIR,
                    labels=["movie", "auto", f"imdb_{rec.imdb_id}", f"title_{rec.title}"]
                )
                if result.success:
                    rec.added = True
                    added += 1
                    logger.info(f"Added movie: {rec.title} ({rec.year})")
                else:
                    logger.error(f"Failed to add movie {rec.title}: {result.error}")
            else:
                logger.warning(f"Could not download torrent for: {rec.rutracker_title}")

        return added

    def _add_top_series(self, recommendations: List[SeriesRecommendation], scraper) -> int:
        """Add top series recommendations to Transmission."""
        added = 0

        if not self.transmission.connect():
            logger.error("Failed to connect to Transmission for series")
            return 0

        for rec in recommendations:
            if rec.added:
                continue

            magnet_url = scraper.download_torrent_file(rec.rutracker_url)

            if magnet_url:
                result = self.transmission.add_torrent_from_url(
                    magnet_url,
                    SERIES_DOWNLOAD_DIR,
                    labels=["series", "auto", f"imdb_{rec.imdb_rating}", f"S{rec.season:02d}"]
                )
                if result.success:
                    rec.added = True
                    added += 1
                    logger.info(f"Added series: {rec.title} S{rec.season:02d}")
                else:
                    logger.error(f"Failed to add series {rec.title} S{rec.season:02d}: {result.error}")
            else:
                logger.warning(f"Could not download torrent for: {rec.rutracker_title}")

        return added

    def _save_recommendations_cache(self):
        """Save recommendations to JSON cache for web UI."""
        cache_data = {
            'timestamp': datetime.now().isoformat(),
            'movies': [asdict(r) for r in self.movie_recommendations[:10]],
            'series': [asdict(r) for r in self.series_recommendations[:10]]
        }

        Path(RECOMMENDATIONS_CACHE).parent.mkdir(parents=True, exist_ok=True)
        with open(RECOMMENDATIONS_CACHE, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)

        logger.info(f"Saved recommendations cache to {RECOMMENDATIONS_CACHE}")

    def get_cached_recommendations(self) -> Dict:
        """Get cached recommendations for web UI."""
        if Path(RECOMMENDATIONS_CACHE).exists():
            with open(RECOMMENDATIONS_CACHE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {'movies': [], 'series': [], 'timestamp': None}


def run_daily_recommendation():
    """Main entry point for cron job."""
    import os
    from dotenv import load_dotenv

    # load_dotenv('/mnt/media/docker-compose/torrent_updater/.env')  # load_dotenv() will find .env in working dir

    login = os.getenv('LOGIN_RUTRACKER')
    password = os.getenv('PASSWORD_RUTRACKER')

    if not login or not password:
        logger.error("Rutracker credentials not found in .env")
        return

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    recommender = MovieRecommender(login, password)
    results = recommender.run_daily_recommendation()

    # Print summary
    print(json.dumps(results, ensure_ascii=False, indent=2))

    return results


if __name__ == "__main__":
    run_daily_recommendation()