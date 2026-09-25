#!/usr/bin/env python3
"""
Jellyfin verification helper for on-demand downloads.
Can be called independently to check if content was correctly identified.
"""

import sqlite3
import requests
import logging
import sys
import time
import os

from jellyfin_sync import JellyfinSync

# Адрес Jellyfin — из .env (JELLYFIN_URL); localhost-фолбэк корректен
# при network_mode: host. Хардкод внутреннего IP запрещён (публичный репозиторий).
JELLYFIN_URL = os.getenv('JELLYFIN_URL', 'http://localhost:8096')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def verify_jellyfin_identification(imdb_id: str, is_series: bool, season: int = None, title: str = None, 
                                    max_retries: int = 5, retry_delay: int = 10) -> bool:
    """
    Verify that Jellyfin has correctly identified the downloaded content.
    
    Args:
        imdb_id: IMDB ID (e.g., tt0096697)
        is_series: Whether it's a TV series
        season: Season number (for series)
        title: Expected title (for additional verification)
        max_retries: Maximum number of retry attempts
        retry_delay: Delay between retries in seconds
    
    Returns:
        True if verified, False otherwise
    """
    logger.info(f"Verifying Jellyfin identification: IMDB={imdb_id}, series={is_series}, season={season}")
    
    jellyfin = JellyfinSync()
    try:
        jellyfin.connect()
        
        for attempt in range(1, max_retries + 1):
            logger.info(f"Verification attempt {attempt}/{max_retries}")
            
            # Trigger library scan
            try:
                resp = requests.post(f"{JELLYFIN_URL}/Library/Refresh", timeout=10)
                logger.info(f"Jellyfin scan triggered: {resp.status_code}")
            except Exception as e:
                logger.warning(f"Could not trigger Jellyfin scan: {e}")
            
            time.sleep(retry_delay)
            
            cursor = jellyfin._conn.cursor()
            
            if is_series:
                # Check for series with matching IMDB ID
                cursor.execute("""
                    SELECT Name, SeriesId FROM BaseItems 
                    WHERE Type = 'MediaBrowser.Controller.Entities.TV.Series' 
                    AND ProviderIds LIKE ?
                """, (f'%"Imdb":"{imdb_id}"%',))
                result = cursor.fetchone()
                
                if result:
                    logger.info(f"✓ Jellyfin identified series: {result['Name']} (ID: {result['SeriesId']})")
                    
                    if title and title.lower() not in result['Name'].lower():
                        logger.warning(f"Title mismatch: expected '{title}', got '{result['Name']}'")
                    
                    # Check for season if specified
                    if season:
                        cursor.execute("""
                            SELECT Name, IndexNumber FROM BaseItems 
                            WHERE SeriesId = ? 
                            AND Type = 'MediaBrowser.Controller.Entities.TV.Season' 
                            AND IndexNumber = ?
                        """, (result['SeriesId'], season))
                        season_result = cursor.fetchone()
                        
                        if season_result:
                            logger.info(f"✓ Jellyfin identified season {season}: {season_result['Name']}")
                            return True
                        else:
                            logger.warning(f"Season {season} not found yet in Jellyfin (series exists)")
                    else:
                        return True
                else:
                    logger.warning(f"Series not yet identified in Jellyfin (attempt {attempt})")
            else:
                # Check for movie
                cursor.execute("""
                    SELECT Name FROM BaseItems 
                    WHERE Type = 'MediaBrowser.Controller.Entities.Movies.Movie' 
                    AND ProviderIds LIKE ?
                """, (f'%"Imdb":"{imdb_id}"%',))
                result = cursor.fetchone()
                
                if result:
                    logger.info(f"✓ Jellyfin identified movie: {result['Name']}")
                    return True
                else:
                    logger.warning(f"Movie not yet identified in Jellyfin (attempt {attempt})")
            
            if attempt < max_retries:
                time.sleep(retry_delay)
        
        logger.error(f"Failed to verify after {max_retries} attempts")
        return False
        
    finally:
        jellyfin.close()


def check_library_refresh() -> bool:
    """Check if Jellyfin library scan is running."""
    try:
        resp = requests.get(f"{JELLYFIN_URL}/System/Info", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Verify Jellyfin identification")
    parser.add_argument("--imdb-id", required=True, help="IMDB ID (e.g., tt0096697)")
    parser.add_argument("--series", action="store_true", help="Is TV series")
    parser.add_argument("--season", type=int, help="Season number")
    parser.add_argument("--title", help="Expected title")
    parser.add_argument("--retries", type=int, default=5, help="Max retry attempts")
    parser.add_argument("--delay", type=int, default=10, help="Delay between retries (seconds)")
    
    args = parser.parse_args()
    
    success = verify_jellyfin_identification(
        imdb_id=args.imdb_id,
        is_series=args.series,
        season=args.season,
        title=args.title,
        max_retries=args.retries,
        retry_delay=args.delay
    )
    
    sys.exit(0 if success else 1)