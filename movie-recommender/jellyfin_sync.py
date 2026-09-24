#!/usr/bin/env python3
"""
Jellyfin library sync module.
Reads Jellyfin SQLite database to get watched history and full library inventory.
"""

import sqlite3
import logging
import os
from dataclasses import dataclass
from typing import Set, List, Dict, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# User 'alex' ID from Jellyfin
ALEX_USER_ID = "F74D127E-0EB4-4591-A04C-494C33DF4C5E"
# Read-only mount of the live Jellyfin data dir (WAL: db + -wal + -shm).
JELLYFIN_DATA_DIR = "/jellyfin-data"
JELLYFIN_DB_SOURCE = os.path.join(JELLYFIN_DATA_DIR, "jellyfin.db")
# Local working copy: refresh_db_copy() snapshots the live DB here via the
# sqlite backup API, so the recommender never locks Jellyfin's live files.
JELLYFIN_DB_PATH = "/tmp/jellyfin.db"


@dataclass
class MediaItem:
    """Represents a media item from Jellyfin."""
    id: str
    name: str
    type: str  # "Movie", "Series", "Season", "Episode"
    series_name: Optional[str] = None
    season_number: Optional[int] = None
    episode_number: Optional[int] = None
    production_year: Optional[int] = None
    imdb_id: Optional[str] = None
    path: Optional[str] = None


class JellyfinSync:
    """Syncs Jellyfin library data for recommendation filtering."""

    def __init__(self, db_path: str = JELLYFIN_DB_PATH, user_id: str = ALEX_USER_ID):
        self.db_path = db_path
        self.user_id = user_id
        self._conn = None

    def connect(self):
        """Connect to Jellyfin SQLite database."""
        if not Path(self.db_path).exists():
            raise FileNotFoundError(f"Jellyfin DB not found at {self.db_path}")
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        logger.info(f"Connected to Jellyfin DB: {self.db_path}")

    def close(self):
        if self._conn:
            self._conn.close()

    def get_watched_movies(self) -> Set[str]:
        """Get set of watched movie names (normalized)."""
        cursor = self._conn.cursor()
        cursor.execute("""
            SELECT DISTINCT bi.Name, bi.ProductionYear
            FROM UserData ud
            JOIN BaseItems bi ON ud.ItemId = bi.Id
            WHERE ud.UserId = ? AND ud.Played = 1 AND bi.Type = 'MediaBrowser.Controller.Entities.Movies.Movie'
        """, (self.user_id,))
        
        watched = set()
        for row in cursor.fetchall():
            name = row['Name'].strip()
            year = row['ProductionYear']
            if year:
                watched.add(f"{name} ({year})".lower())
            else:
                watched.add(name.lower())
        return watched

    def get_watched_episodes(self) -> Set[str]:
        """Get set of watched episode keys (series SxxExx)."""
        cursor = self._conn.cursor()
        cursor.execute("""
            SELECT DISTINCT bi.Name, bi.SeriesName, bi.ParentIndexNumber, bi.IndexNumber
            FROM UserData ud
            JOIN BaseItems bi ON ud.ItemId = bi.Id
            WHERE ud.UserId = ? AND ud.Played = 1 AND bi.Type = 'MediaBrowser.Controller.Entities.TV.Episode'
        """, (self.user_id,))
        
        watched = set()
        for row in cursor.fetchall():
            series = row['SeriesName'].strip()
            season = row['ParentIndexNumber']
            episode = row['IndexNumber']
            if season and episode:
                watched.add(f"{series.lower()} s{season:02d}e{episode:02d}")
        return watched

    def get_library_movies(self) -> Set[str]:
        """Get all movies in library (owned)."""
        cursor = self._conn.cursor()
        cursor.execute("""
            SELECT Name, ProductionYear FROM BaseItems WHERE Type = 'MediaBrowser.Controller.Entities.Movies.Movie'
        """)
        
        movies = set()
        for row in cursor.fetchall():
            name = row['Name'].strip()
            year = row['ProductionYear']
            if year:
                movies.add(f"{name} ({year})".lower())
            else:
                movies.add(name.lower())
        return movies

    def get_library_series(self) -> Dict[str, Dict]:
        """Get all series in library with season/episode info."""
        cursor = self._conn.cursor()
        cursor.execute("""
            SELECT bi.Id, bi.Name, bi.Type, bi.SeriesName, bi.SeriesId, bi.ParentIndexNumber, bi.IndexNumber, bi.ProductionYear
            FROM BaseItems bi
            WHERE bi.Type IN ('MediaBrowser.Controller.Entities.TV.Series', 'MediaBrowser.Controller.Entities.TV.Season', 'MediaBrowser.Controller.Entities.TV.Episode')
        """)
        
        # First pass: collect all Series by their Id
        series_by_id = {}
        for row in cursor.fetchall():
            if row['Type'] == 'MediaBrowser.Controller.Entities.TV.Series':
                series_by_id[row['Id']] = {
                    'name': row['Name'].strip(),
                    'year': row['ProductionYear'],
                    'seasons': set(),
                    'episodes': set()
                }
        
        # Second pass: link Seasons and Episodes to their Series via SeriesId
        cursor.execute("""
            SELECT bi.Id, bi.Name, bi.Type, bi.SeriesName, bi.SeriesId, bi.ParentIndexNumber, bi.IndexNumber
            FROM BaseItems bi
            WHERE bi.Type IN ('MediaBrowser.Controller.Entities.TV.Season', 'MediaBrowser.Controller.Entities.TV.Episode')
        """)
        
        for row in cursor.fetchall():
            series_id = row['SeriesId']
            if series_id and series_id in series_by_id:
                if row['Type'] == 'MediaBrowser.Controller.Entities.TV.Season':
                    # Season number is in IndexNumber
                    if row['IndexNumber']:
                        series_by_id[series_id]['seasons'].add(row['IndexNumber'])
                elif row['Type'] == 'MediaBrowser.Controller.Entities.TV.Episode':
                    s = row['ParentIndexNumber']
                    e = row['IndexNumber']
                    if s and e:
                        series_by_id[series_id]['episodes'].add((s, e))
        
        # Convert to name-keyed dict (deduplicate by name, merge data)
        series_data = {}
        for series_info in series_by_id.values():
            name = series_info['name'].lower()
            if name in series_data:
                series_data[name]['seasons'].update(series_info['seasons'])
                series_data[name]['episodes'].update(series_info['episodes'])
            else:
                series_data[name] = series_info
        
        return series_data

    def get_watched_series_progress(self) -> Dict[str, Dict]:
        """Get watched progress for series (which seasons/episodes watched)."""
        cursor = self._conn.cursor()
        cursor.execute("""
            SELECT bi.SeriesName, bi.ParentIndexNumber, bi.IndexNumber
            FROM UserData ud
            JOIN BaseItems bi ON ud.ItemId = bi.Id
            WHERE ud.UserId = ? AND ud.Played = 1 AND bi.Type = 'MediaBrowser.Controller.Entities.TV.Episode'
        """, (self.user_id,))
        
        progress = {}
        for row in cursor.fetchall():
            series = row['SeriesName'].strip().lower()
            season = row['ParentIndexNumber']
            episode = row['IndexNumber']
            if series not in progress:
                progress[series] = {'seasons': set(), 'episodes': set()}
            if season:
                progress[series]['seasons'].add(season)
            if season and episode:
                progress[series]['episodes'].add((season, episode))
        
        return progress

    def is_movie_watched_or_owned(self, title: str, year: Optional[int] = None) -> bool:
        """Check if movie is watched or in library."""
        watched = self.get_watched_movies()
        owned = self.get_library_movies()
        
        search = title.lower()
        if year:
            search_with_year = f"{title} ({year})".lower()
            return search_with_year in watched or search_with_year in owned
        return search in watched or search in owned

    def is_series_watched(self, series_name: str, season: int, episode: int) -> bool:
        """Check if specific episode was watched."""
        watched = self.get_watched_episodes()
        return f"{series_name.lower()} s{season:02d}e{episode:02d}" in watched

    def get_series_watched_seasons(self, series_name: str) -> Set[int]:
        """Get set of watched seasons for a series."""
        progress = self.get_watched_series_progress()
        return progress.get(series_name.lower(), {}).get('seasons', set())

    def refresh_db_copy(self):
        """Snapshot the live Jellyfin DB to the local working copy.

        Uses the sqlite3 backup API (consistent snapshot, does not lock the
        live DB). Keeps the previous copy if the source is unavailable.
        MUST be called before connect().
        """
        if not Path(JELLYFIN_DB_SOURCE).exists():
            if Path(self.db_path).exists():
                logger.warning(f"Jellyfin live DB missing at {JELLYFIN_DB_SOURCE}, reusing previous copy")
            else:
                logger.error(f"Jellyfin live DB not found at {JELLYFIN_DB_SOURCE} and no previous copy")
            return
        try:
            # /tmp/jellyfin.db may be a stale directory left by the old broken
            # bind-mount (docker creates a dir when the source file is missing).
            if Path(self.db_path).is_dir() and not Path(self.db_path).is_symlink():
                import shutil
                shutil.rmtree(self.db_path)
                logger.warning(f"Removed stale directory at {self.db_path} (leftover of broken mount)")
            src = sqlite3.connect(f"file:{JELLYFIN_DB_SOURCE}?mode=ro", uri=True, timeout=30)
            try:
                dst = sqlite3.connect(self.db_path, timeout=30)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            logger.info(f"Jellyfin DB snapshot ready at {self.db_path}")
        except Exception as e:
            logger.error(f"Jellyfin DB snapshot failed ({e}), reusing previous copy if any")


def normalize_title(title: str) -> str:
    """Normalize title for comparison."""
    import re
    # Remove quality tags, release info, etc.
    title = re.sub(r'\[.*?\]', '', title)
    title = re.sub(r'\(.*?\)', '', title)
    title = re.sub(r'\b(1080p|720p|4K|2160p|HDR|DV|WEB-DL|BDRip|BluRay|Remux)\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\s+', ' ', title).strip()
    return title.lower()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    sync = JellyfinSync()
    sync.connect()
    
    print("=== Watched Movies ===")
    for m in sorted(sync.get_watched_movies()):
        print(f"  {m}")
    
    print("\n=== Library Movies ===")
    for m in sorted(sync.get_library_movies()):
        print(f"  {m}")
    
    print("\n=== Library Series ===")
    for name, data in sorted(sync.get_library_series().items()):
        print(f"  {data['name']} ({data['year']}): Seasons {sorted(data['seasons'])}, Episodes {len(data['episodes'])}")
    
    print("\n=== Watched Series Progress ===")
    for name, data in sorted(sync.get_watched_series_progress().items()):
        print(f"  {name}: Seasons {sorted(data['seasons'])}, Episodes {len(data['episodes'])}")
    
    sync.close()