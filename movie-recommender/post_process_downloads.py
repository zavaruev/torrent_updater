#!/usr/bin/env python3
"""
Post-processing for completed downloads: creates NFO files with IMDB ID
so Jellyfin can properly identify media and fetch metadata.

Jellyfin reads NFO files during library scan:
  - Movie NFO: same name as video file, .nfo extension, in same folder
  - Series NFO: in series folder, same name as show
  - Episode NFO: same name as episode file, .nfo extension

Run via cron or as part of the daily recommendation cycle.
"""

import logging
import os
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# load_dotenv('/mnt/media/docker-compose/torrent_updater/.env')  # load_dotenv() will find .env in working dir

# Host paths (actual mount points)
MOVIES_DIR = Path(os.getenv('MOVIES_HOST_DIR', '/mnt/media/movies'))
SERIES_DIR = Path(os.getenv('SERIES_HOST_DIR', '/mnt/media/series'))

# Transmission config
# Addresses come from .env only; localhost also works inside the container (network_mode: host)
TRANSMISSION_HOST = os.getenv('TR_HOST', 'localhost')
TRANSMISSION_PORT = int(os.getenv('TR_PORT', '9091'))
TRANSMISSION_USER = os.getenv('TR_USER', 'transmission')
TRANSMISSION_PASSWORD = os.getenv('TR_PASSWORD', '')

# Video file extensions
VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.wmv', '.mpg', '.mpeg', '.m4v', '.mov', '.ts', '.webm'}


def connect_transmission():
    """Connect to Transmission RPC."""
    from transmission_rpc import Client
    try:
        client = Client(
            host=TRANSMISSION_HOST,
            port=TRANSMISSION_PORT,
            username=TRANSMISSION_USER,
            password=TRANSMISSION_PASSWORD,
            timeout=30
        )
        client.get_session()
        return client
    except Exception as e:
        logger.error(f"Failed to connect to Transmission: {e}")
        return None


def extract_imdb_id(labels: List[str]) -> Optional[str]:
    """Extract IMDB ID from labels like 'imdb_tt12345678'."""
    for label in labels:
        m = re.search(r'imdb_(tt\d+)', label)
        if m:
            return m.group(1)
    return None


def extract_title(labels: List[str]) -> Optional[str]:
    """Extract title from labels like 'title_The Odyssey'."""
    for label in labels:
        if label.startswith('title_'):
            return label[6:]
    return None


def extract_year(labels: List[str]) -> Optional[str]:
    """Try to extract year from labels or torrent name."""
    for label in labels:
        m = re.search(r'\((\d{4})\)', label)
        if m:
            return m.group(1)
    return None


def find_completed_downloads(client) -> List[Dict]:
    """Find completed torrents that need NFO files."""
    results = []
    torrents = client.get_torrents(
        arguments=['id', 'name', 'status', 'labels', 'download_dir', 'totalSize', 'sizeWhenDone']
    )

    for t in torrents:
        # Completed torrents have status "stopped" or "seeding"
        if t.status not in ('stopped', 'seeding'):
            continue

        labels = t.labels or []
        imdb_id = extract_imdb_id(labels)
        if not imdb_id:
            continue

        # Map container path to host path
        download_dir = t.download_dir or ''
        if '/movies' in download_dir:
            host_dir = MOVIES_DIR
            is_series = False
        elif '/series' in download_dir:
            host_dir = SERIES_DIR
            is_series = True
        else:
            continue

        results.append({
            'id': t.id,
            'name': t.name,
            'imdb_id': imdb_id,
            'title': extract_title(labels),
            'labels': labels,
            'download_dir': download_dir,
            'host_dir': str(host_dir),
            'is_series': is_series,
        })

    return results


def create_movie_nfo(host_dir: Path, torrent_name: str,
                     imdb_id: str, title: Optional[str], year: Optional[str]) -> bool:
    """Create NFO file for movie by scanning the filesystem."""
    # Find video files in host_dir matching the torrent name
    video_files = []

    torrent_base = os.path.basename(torrent_name)
    torrent_stem = os.path.splitext(torrent_base)[0]

    # Check host_dir directly - match by torrent name (with or without extension)
    for f in host_dir.iterdir():
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
            # Match if filename starts with torrent stem or torrent name
            if torrent_stem in f.stem or f.name.startswith(torrent_stem[:50]):
                video_files.append(f)

    # Check subfolder named after torrent
    subfolder = host_dir / torrent_base
    if subfolder.exists() and subfolder.is_dir():
        for f in subfolder.iterdir():
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                video_files.append(f)

    # Also check any subfolder that matches torrent stem
    for subdir in host_dir.iterdir():
        if subdir.is_dir() and torrent_stem in subdir.name:
            for f in subdir.iterdir():
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    video_files.append(f)

    if not video_files:
        logger.warning(f"  No video file found in {host_dir} matching '{torrent_stem}'")
        return False

    # Find the video file that best matches the torrent name
    # Prefer exact stem match, then prefix match, then largest
    def match_score(f):
        stem = f.stem
        if stem == torrent_stem:
            return (0, -f.stat().st_size)
        if stem.startswith(torrent_stem):
            return (1, -f.stat().st_size)
        if torrent_stem in stem:
            return (2, -f.stat().st_size)
        return (3, -f.stat().st_size)

    main_file = min(video_files, key=match_score)

    nfo_path = main_file.with_suffix('.nfo')

    # Check if NFO already exists with correct IMDB ID
    if nfo_path.exists():
        existing = nfo_path.read_text(encoding='utf-8', errors='ignore')
        if f'<imdbid>{imdb_id}</imdbid>' in existing:
            logger.info(f"  NFO already exists with correct IMDB ID: {nfo_path.name}")
            return True

    # Build NFO XML
    clean_title = title or os.path.splitext(main_file.name)[0]
    clean_title = clean_title.replace('&', '&').replace('<', '<').replace('>', '>')

    nfo_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<movie>
    <title>{clean_title}</title>
    <imdbid>{imdb_id}</imdbid>
    <year>{year or ''}</year>
</movie>
"""
    nfo_path.write_text(nfo_xml, encoding='utf-8')
    logger.info(f"  Created movie NFO: {nfo_path.name}")
    return True
    return True


def create_series_nfo(host_dir: Path, torrent_name: str,
                      imdb_id: str, title: Optional[str], year: Optional[str]) -> bool:
    """Create NFO files for series (show-level + episode-level) by scanning filesystem."""
    # Find video files recursively in host_dir
    video_files = []

    # Check subfolders
    for subdir in host_dir.iterdir():
        if subdir.is_dir():
            for f in subdir.rglob('*'):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    video_files.append(f)

    if not video_files:
        # Try the torrent name as subfolder
        subfolder = host_dir / os.path.basename(torrent_name)
        if subfolder.exists() and subfolder.is_dir():
            for f in subfolder.rglob('*'):
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                    video_files.append(f)

    if not video_files:
        logger.warning(f"  No video files found in {host_dir}")
        return False

    clean_title = title or 'Unknown Show'
    clean_title = clean_title.replace('&', '&').replace('<', '<').replace('>', '>')

    # Determine show directory from first video file
    first_file = video_files[0]
    # Find the show folder (parent of season folder)
    show_dir = first_file.parent
    while show_dir.parent != host_dir and show_dir.parent != host_dir.parent:
        show_dir = show_dir.parent
    # If we went too far up, use the first file's parent
    if show_dir == host_dir:
        show_dir = first_file.parent

    if not show_dir.exists():
        logger.warning(f"  Show directory not found: {show_dir}")
        return False

    # --- Series-level NFO ---
    series_nfo_path = show_dir / f"{clean_title}.nfo"

    if series_nfo_path.exists():
        existing = series_nfo_path.read_text(encoding='utf-8', errors='ignore')
        if f'<imdbid>{imdb_id}</imdbid>' in existing:
            logger.info(f"  Series NFO already exists: {series_nfo_path.name}")
            series_nfo_created = False
        else:
            series_nfo_created = True
    else:
        series_nfo_created = True

    if series_nfo_created:
        series_nfo_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<tvshow>
    <title>{clean_title}</title>
    <imdbid>{imdb_id}</imdbid>
    <year>{year or ''}</year>
</tvshow>
"""
        series_nfo_path.write_text(series_nfo_xml, encoding='utf-8')
        logger.info(f"  Created series NFO: {series_nfo_path.name}")

    # --- Episode-level NFO files ---
    ep_created = 0
    for f in video_files:
        ep_nfo_path = f.with_suffix('.nfo')

        if ep_nfo_path.exists():
            existing = ep_nfo_path.read_text(encoding='utf-8', errors='ignore')
            if '<imdbid>' in existing:
                continue

        # Try to extract season/episode from filename
        basename = f.stem

        # Pattern: Show.Name.S01E01
        m = re.search(r'S(\d{1,2})[Ee](\d{1,2})', basename)
        if m:
            season = int(m.group(1))
            episode = int(m.group(2))
        else:
            # Pattern: "Сезон: 1" / "Серии: 1-6"
            m_s = re.search(r'Сезон:\s*(\d+)', basename, re.IGNORECASE)
            m_e = re.search(r'[Сс]ер(ии|ия):\s*(\d+)', basename, re.IGNORECASE)
            season = int(m_s.group(1)) if m_s else 0
            episode = int(m_e.group(1)) if m_e else 0

        if season == 0:
            # Try to guess from folder structure (Season 01/)
            parent = f.parent.name
            m_dir = re.search(r'[Ss](?:eason)?\s*(\d+)', parent)
            if m_dir:
                season = int(m_dir.group(1))

        ep_nfo_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<episodedetails>
    <showtitle>{clean_title}</showtitle>
    <season>{season}</season>
    <episode>{episode}</episode>
    <imdbid>{imdb_id}</imdbid>
</episodedetails>
"""
        ep_nfo_path.write_text(ep_nfo_xml, encoding='utf-8')
        ep_created += 1

    if ep_created:
        logger.info(f"  Created {ep_created} episode NFO(s)")

    return True


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    logger.info("=== Starting NFO post-processing for completed downloads ===")

    client = connect_transmission()
    if not client:
        logger.error("Cannot connect to Transmission, aborting")
        return 1

    downloads = find_completed_downloads(client)
    logger.info(f"Found {len(downloads)} completed downloads to process")

    processed = 0
    for info in downloads:
        logger.info(f"Processing: {info['name'][:60]}...")
        try:
            host_dir = Path(info['host_dir'])
            year = extract_year(info['labels'])
            # Also try to get year from torrent name
            if not year:
                m = re.search(r'\((\d{4})\)', info['name'])
                if m:
                    year = m.group(1)

            if info['is_series']:
                success = create_series_nfo(
                    host_dir, info['name'],
                    info['imdb_id'], info['title'], year
                )
            else:
                success = create_movie_nfo(
                    host_dir, info['name'],
                    info['imdb_id'], info['title'], year
                )
            if success:
                processed += 1
        except Exception as e:
            logger.error(f"  Error processing {info['name']}: {e}")

    logger.info(f"=== NFO post-processing complete: {processed}/{len(downloads)} processed ===")
    return 0


if __name__ == '__main__':
    sys.exit(main())
