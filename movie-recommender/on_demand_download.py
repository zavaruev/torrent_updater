#!/usr/bin/env python3
"""
On-demand download for specific movies/series from Rutracker.
Adds to Transmission with IMDB ID labels for Jellyfin identification.

Usage:
    python on_demand_download.py "The Simpsons" --season 15
    python on_demand_download.py --imdb-id tt0096697 --season 15
    python on_demand_download.py "Inception" --movie
"""

import sys
import os
import argparse
import logging
import re
from pathlib import Path
from typing import Optional, List, Dict
from dataclasses import dataclass

# Add project root to path
sys.path.insert(0, '/opt/data/scripts/movie-recommender')

from dotenv import load_dotenv
# load_dotenv('/mnt/media/docker-compose/torrent_updater/.env')  # load_dotenv() will find .env in working dir

from rutracker_scraper import RutrackerScraper, RUTRACKER_MOVIES, RUTRACKER_TV, _kw_start_re
from transmission_add import TransmissionManager, MOVIES_DOWNLOAD_DIR, SERIES_DOWNLOAD_DIR

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Quality ranking (higher = better)
# NB (сент. 2026): 2160p/4K/HDR/DV убраны — LE-zal их не воспроизводит,
# такие раздачи отсекает EXCLUDE_KEYWORDS (rutracker_scraper.py) до скоринга.
QUALITY_RANK = {
    'WEB-DL 1080p': 100,
    'BDRip 1080p': 95,
    'Remux 1080p': 90,
    'WEBRip 1080p': 85,
    'WEB-DL 720p': 75,
    'BDRip 720p': 65,
    'WEBRip 720p': 60,
    'WEB-DL': 50,
    'BDRip': 40,
    'Remux': 35,
    'WEBRip': 30,
    'AVC': 20, 'x264': 20,
    'TS': 10,
}

PREFERRED_STUDIOS = [
    'LostFilm', 'TVShows', 'BaibaKo', 'Octopus', 'NewStudio', 
    'Jaskier', 'AlexFilm', 'Red Head Sound', 'HamsterStudio',
    'DreamTeam', 'Edelweiss', 'Amedia', 'KuboKube', 'Kinozal',
    'Novice', 'HDRezka', 'West Video', 'MobilStudia', 'Vozrozhdenie'
]

EXCLUDE_KEYWORDS = [
    'Камрип', 'CAMRip', 'TS', 'TC', 'Scr', 'Screener', 
    'DVDRip', 'HDRip', 'одноголос', 'закадров', 
    'One Voice', 'Single Voice', 'одноголосый',
    'Перевод: Одноголосый', 'Перевод: Закадровый',
    'AMZN', 'iTunes', 'MOD', 'VHS', 'DVD5', 'DVD9',
    # LE-zal не воспроизводит: HEVC/x265, 2160p/4K/UHD, HDR, DV
    # (см. EXCLUDE_KEYWORDS в rutracker_scraper.py — список синхронизировать!)
    'HEVC', 'x265', 'H265', 'H.265',
    '2160p', '4K', 'UHD',
    'HDR', 'DV',
]

MIN_SEEDERS = 5
MOVIE_SIZE_MIN = 1.5 * 1024**3  # 1.5 GB
MOVIE_SIZE_MAX = 50 * 1024**3   # 50 GB
EPISODE_SIZE_MIN = 0.5 * 1024**3  # 500 MB
EPISODE_SIZE_MAX = 15 * 1024**3   # 15 GB


@dataclass
class TorrentMatch:
    title: str
    url: str
    seeders: int
    size_bytes: int
    dub_studio: Optional[str]
    quality: str
    score: int
    season: Optional[int] = None
    episode: Optional[int] = None


def score_torrent(torrent) -> int:
    """Score torrent based on quality, dub studio, seeders, size."""
    score = 0
    
    # Quality
    score += QUALITY_RANK.get(torrent.quality, 0)
    
    # Dub studio preference
    if torrent.dub_studio:
        for i, studio in enumerate(PREFERRED_STUDIOS):
            if studio.lower() in torrent.dub_studio.lower():
                score += 50 - i
                break
    
    # Seeders (capped)
    score += min(torrent.seeders, 500) // 10
    
    # Size preference (sweet spot)
    size_gb = torrent.size_bytes / 1024**3
    if 2 <= size_gb <= 15:
        score += 20
    elif 1.5 <= size_gb <= 30:
        score += 10
    
    return score


def extract_season_episode(title: str) -> tuple:
    """Extract season and episode from title."""
    patterns = [
        r'S(\d{1,2})[Ee](\d{1,2})',           # S01E01
        r'S(\d{1,2})\b',                       # S01
        r'Season\s+(\d{1,2})',                  # Season 1
        r'(\d{1,2})\s*сезон',                  # 1 сезон
        r'(\d{1,2})\s*сер(ия|\.|$)',           # 1 серия
        r'Сезон:\s*(\d{1,2})',                 # Сезон: 4
        r'Серия:\s*(\d{1,2})',                 # Серия: 12
    ]
    
    for pattern in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            groups = match.groups()
            if len(groups) >= 2 and groups[1]:
                return int(groups[0]), int(groups[1])
            return int(groups[0]), None
    
    return None, None


def has_dubbing(title: str) -> tuple:
    """Check for dubbing studios in title."""
    title_lower = title.lower()
    for studio in PREFERRED_STUDIOS:
        if studio.lower() in title_lower:
            return True, studio
    return False, None


def is_excluded(title: str) -> bool:
    """Check if title has exclusion keywords.

    Match by word start (same as rutracker_scraper._check_excluded), NOT by
    plain substring: with substrings short keys like 'DV' or '4K' would hit
    innocent titles ("Adventure", "1/4Kg") and 'TS' — any "...ts..." word.
    """
    title_lower = title.lower()
    for ex in EXCLUDE_KEYWORDS:
        if _kw_start_re(ex).search(title_lower):
            return True
    return False


def check_quality(title: str) -> Optional[str]:
    """Extract quality from title - combine source + resolution."""
    title_lower = title.lower()
    
    source = None
    for s in ['Remux', 'BluRay', 'BDRip', 'WEBRip', 'WEB-DL']:
        if s.lower() in title_lower:
            source = s
            break
    
    resolution = None
    for r in ['2160p', '4K', '1080p', '720p']:
        if r.lower() in title_lower:
            resolution = r
            break
    
    hdr = None
    for h in ['DV', 'HDR']:
        if h.lower() in title_lower:
            hdr = h
            break
    
    codec = None
    if not source and not resolution:
        for c in ['HEVC', 'x265', 'x264', 'AVC']:
            if c.lower() in title_lower:
                codec = c
                break
    
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


def normalize_title(title: str) -> str:
    """Normalize title for matching."""
    title = title.lower()
    title = re.sub(r'\[.*?\]', '', title)
    title = re.sub(r'\(.*?\)', '', title)
    title = re.sub(r'\s+', ' ', title)
    return title.strip()


def search_imdb_id(title: str, year: Optional[int] = None) -> Optional[str]:
    """Search IMDB ID via imdb_scraper if available."""
    try:
        from imdb_scraper import IMDBScraper
        scraper = IMDBScraper()
        results = scraper.search(title)
        for r in results:
            if year and r.year != year:
                continue
            return r.imdb_id
    except Exception as e:
        logger.warning(f"IMDB search failed: {e}")
    return None


def find_best_torrent(scraper: RutrackerScraper, query: str, is_series: bool,
                       season: Optional[int] = None, episode: Optional[int] = None) -> Optional[TorrentMatch]:
    """Search Rutracker and return best matching torrent."""
    # FIX: scrape_forum takes (url_string, forum_id_int) — NOT (forum_id, forum_id)
    forum_url = RUTRACKER_TV if is_series else RUTRACKER_MOVIES
    forum_int = 1803 if is_series else 252
    torrents = scraper.scrape_forum(forum_url, forum_int, max_pages=5)
    
    norm_query = normalize_title(query)
    matches = []
    
    for t in torrents:
        if t.seeders < MIN_SEEDERS:
            continue
        if is_excluded(t.title):
            continue
        
        has_dub, dub_studio = has_dubbing(t.title)
        if not has_dub:
            continue
        
        quality = check_quality(t.title)
        if not quality:
            continue
        
        # Size check
        if is_series:
            if not (EPISODE_SIZE_MIN <= t.size_bytes <= EPISODE_SIZE_MAX):
                continue
        else:
            if not (MOVIE_SIZE_MIN <= t.size_bytes <= MOVIE_SIZE_MAX):
                continue
        
        # Season/episode match for series
        if is_series:
            t_season, t_episode = extract_season_episode(t.title)
            if season and t_season != season:
                continue
            if episode and t_episode != episode:
                continue
        
        # Title match
        norm_title = normalize_title(t.title)
        if norm_query not in norm_title and norm_title not in norm_query:
            continue
        
        score = score_torrent(t)
        matches.append(TorrentMatch(
            title=t.title,
            url=t.url,
            seeders=t.seeders,
            size_bytes=t.size_bytes,
            dub_studio=dub_studio,
            quality=quality,
            score=score,
            season=t_season if is_series else None,
            episode=t_episode if is_series else None
        ))
    
    if not matches:
        return None
    
    # Sort by score, pick best
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches[0]


def add_to_transmission(match: TorrentMatch, scraper: RutrackerScraper, 
                        imdb_id: str, title: str, is_series: bool) -> bool:
    """Download torrent and add to Transmission."""
    logger.info(f"Downloading torrent: {match.title}")
    
    magnet_url = scraper.download_torrent_file(match.url)
    if not magnet_url:
        logger.error("Failed to get magnet link")
        return False
    
    client = TransmissionManager()
    if not client.connect():
        logger.error("Failed to connect to Transmission")
        return False
    
    download_dir = SERIES_DOWNLOAD_DIR if is_series else MOVIES_DOWNLOAD_DIR
    labels = ["series" if is_series else "movie", "auto", f"imdb_{imdb_id}", f"title_{title}"]
    
    if is_series and match.season:
        labels.append(f"S{match.season:02d}")
    
    result = client.add_torrent_from_url(magnet_url, download_dir, labels=labels)
    
    if result.success:
        logger.info(f"Successfully added to Transmission: {match.title}")
        return True
    else:
        logger.error(f"Failed to add to Transmission: {result.error}")
        return False


def main():
    parser = argparse.ArgumentParser(description="On-demand movie/series download from Rutracker")
    parser.add_argument("title", nargs="?", help="Movie/series title to search for")
    parser.add_argument("--imdb-id", help="IMDB ID (e.g., tt0096697)")
    parser.add_argument("--season", type=int, help="Season number (for series)")
    parser.add_argument("--episode", type=int, help="Episode number (for series)")
    parser.add_argument("--movie", action="store_true", help="Force movie mode")
    parser.add_argument("--series", action="store_true", help="Force series mode")
    args = parser.parse_args()
    
    if not args.title and not args.imdb_id:
        parser.error("Either title or --imdb-id is required")
    
    is_series = args.series or (args.season is not None)
    if args.movie:
        is_series = False
    
    # Determine IMDB ID
    imdb_id = args.imdb_id
    search_title = args.title
    
    if not imdb_id and search_title:
        logger.info(f"Searching IMDB ID for: {search_title}")
        imdb_id = search_imdb_id(search_title)
        if not imdb_id:
            logger.warning("Could not find IMDB ID, proceeding without")
            imdb_id = "unknown"
    
    # Load credentials
    rutracker_login = os.getenv('LOGIN_RUTRACKER')
    rutracker_password = os.getenv('PASSWORD_RUTRACKER')
    
    if not rutracker_login or not rutracker_password:
        logger.error("Rutracker credentials not found in .env")
        return 1
    
    logger.info(f"Searching Rutracker for: {search_title or imdb_id} (series={is_series})")
    
    # Search and download
    scraper = RutrackerScraper(rutracker_login, rutracker_password)
    scraper.__enter__()
    try:
        match = find_best_torrent(scraper, search_title or imdb_id, is_series, args.season, args.episode)
        
        if not match:
            logger.error("No matching torrent found")
            return 1
        
        logger.info(f"Best match: {match.title}")
        logger.info(f"  Quality: {match.quality}, Seeders: {match.seeders}, "
                    f"Size: {match.size_bytes/1024**3:.2f}GB, Dub: {match.dub_studio}")
        
        if is_series and match.season:
            logger.info(f"  Season: {match.season}, Episode: {match.episode}")
        
        success = add_to_transmission(match, scraper, imdb_id, search_title or "unknown", is_series)
        
        if success:
            logger.info("Download started. Run post_process_downloads.py after completion for NFO creation.")
            return 0
        else:
            return 1
            
    finally:
        scraper.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())