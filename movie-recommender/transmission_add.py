#!/usr/bin/env python3
"""
Transmission torrent addition module.
Adds torrents to Transmission with correct download directories.

All addresses/credentials come from the environment (.env) only: no hardcoding,
so internal IPs and passwords never end up in git.
"""

import logging
import os
import requests
from typing import Optional, Dict, Any
from transmission_rpc import Client
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Download directories (inside Transmission container)
MOVIES_DOWNLOAD_DIR = "/movies"      # Maps to /mnt/media/movies on host
SERIES_DOWNLOAD_DIR = "/series"      # Maps to /mnt/media/series on host

# Fallback defaults point to localhost: both the container (network_mode: host)
# and a local run talk to Transmission on the same host. Real values live in .env
# (TR_HOST/TR_PORT/TR_USER/TR_PASSWORD), passed via env_file.
TRANSMISSION_HOST = os.getenv('TR_HOST', 'localhost')
TRANSMISSION_PORT = int(os.getenv('TR_PORT', '9091'))
TRANSMISSION_USER = os.getenv('TR_USER', '')
TRANSMISSION_PASSWORD = os.getenv('TR_PASSWORD', '')


@dataclass
class TorrentAddResult:
    """Result of adding a torrent."""
    success: bool
    torrent_id: Optional[int] = None
    name: Optional[str] = None
    error: Optional[str] = None


class TransmissionManager:
    """Manages Transmission torrent additions."""

    def __init__(self, host: str = None, port: int = None,
                 username: str = None, password: str = None):
        # Lazy env read at object creation time: the module may be imported
        # before load_dotenv(), while recommendations/search construct
        # TransmissionManager() only after .env is loaded.
        self.host = host or os.getenv('TR_HOST', TRANSMISSION_HOST)
        self.port = port or int(os.getenv('TR_PORT', str(TRANSMISSION_PORT)))
        self.username = username if username is not None else os.getenv('TR_USER', TRANSMISSION_USER)
        self.password = password if password is not None else os.getenv('TR_PASSWORD', TRANSMISSION_PASSWORD)
        self._client = None

    def connect(self) -> bool:
        """Connect to Transmission RPC."""
        try:
            self._client = Client(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=30
            )
            # Test connection
            self._client.get_session()
            logger.info("Connected to Transmission RPC")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Transmission: {e}")
            return False

    def add_torrent(self, torrent_data: bytes, download_dir: str, 
                    paused: bool = False, labels: Optional[list] = None) -> TorrentAddResult:
        """Add torrent from raw data (magnet or .torrent file)."""
        if not self._client:
            if not self.connect():
                return TorrentAddResult(success=False, error="Not connected to Transmission")

        try:
            torrent = self._client.add_torrent(
                torrent=torrent_data,
                download_dir=download_dir,
                paused=paused,
                labels=labels or []
            )
            logger.info(f"Added torrent: {torrent.name} (ID: {torrent.id}) to {download_dir}")
            return TorrentAddResult(success=True, torrent_id=torrent.id, name=torrent.name)
        except Exception as e:
            logger.error(f"Failed to add torrent: {e}")
            return TorrentAddResult(success=False, error=str(e))

    def set_comment(self, torrent_id: int, comment: str) -> bool:
        """Set torrent comment (used to store the Rutracker topic URL so the
        updater can track this torrent for updates)."""
        if not self._client:
            if not self.connect():
                return False
        try:
            self._client.change_torrent(torrent_id, comment=comment)
            return True
        except Exception as e:
            logger.error(f"Failed to set comment: {e}")
            return False

    def add_torrent_from_url(self, url: str, download_dir: str,
                             paused: bool = False, labels: Optional[list] = None) -> TorrentAddResult:
        """Add torrent from magnet link or torrent URL."""
        if not self._client:
            if not self.connect():
                return TorrentAddResult(success=False, error="Not connected to Transmission")

        try:
            torrent = self._client.add_torrent(
                torrent=url,
                download_dir=download_dir,
                paused=paused,
                labels=labels or []
            )
            logger.info(f"Added torrent from URL: {torrent.name} (ID: {torrent.id}) to {download_dir}")
            return TorrentAddResult(success=True, torrent_id=torrent.id, name=torrent.name)
        except Exception as e:
            logger.error(f"Failed to add torrent from URL: {e}")
            return TorrentAddResult(success=False, error=str(e))

    def download_torrent_file(self, rutracker_url: str, cookies: Dict[str, str] = None, login: str = None, password: str = None) -> Optional[bytes]:
        """Download .torrent file from Rutracker using session cookies or credentials."""
        # Extract topic ID
        import re
        match = re.search(r't=(\d+)', rutracker_url)
        if not match:
            return None
        
        topic_id = match.group(1)
        download_url = f"https://rutracker.org/forum/dl.php?t={topic_id}"
        
        # Use bb_data cookie format (like torrent_updater) if credentials provided
        if login and password:
            login_len = len(login)
            pass_len = len(password)
            # PHP serialized array: a:2:{s:11:"login_username";s:X:"USER";s:11:"login_password";s:Y:"PASS";}
            # URL-encode the structure but NOT the credentials (match torrent_updater exactly)
            bb_data = (
                'a%3A2%3A%7Bs%3A11%3A%22login_username%22%3Bs%3A' + str(login_len) + 
                '%3A%22' + login + '%22%3Bs%3A11%3A%22login_password%22%3Bs%3A' + 
                str(pass_len) + '%3A%22' + password + '%22%3B%7D'
            )
            cookies = {'bb_data': bb_data}
        elif not cookies:
            return None
        
        try:
            resp = requests.get(
                download_url,
                cookies=cookies,
                headers={'Referer': rutracker_url},
                timeout=30
            )
            if resp.status_code == 200 and resp.headers.get('Content-Type', '').startswith('application/x-bittorrent'):
                return resp.content
            else:
                logger.warning(f"Failed to download torrent file: {resp.status_code} - {resp.headers.get('Content-Type')}")
        except Exception as e:
            logger.error(f"Error downloading torrent file: {e}")
        
        return None

    def get_torrents(self) -> list:
        """Get all torrents."""
        if not self._client:
            if not self.connect():
                return []
        try:
            return self._client.get_torrents()
        except Exception as e:
            logger.error(f"Failed to get torrents: {e}")
            return []

    def remove_torrent(self, torrent_id: int, delete_data: bool = False) -> bool:
        """Remove torrent from Transmission."""
        if not self._client:
            if not self.connect():
                return False
        try:
            self._client.remove_torrent(torrent_id, delete_data=delete_data)
            return True
        except Exception as e:
            logger.error(f"Failed to remove torrent: {e}")
            return False


def add_movie_torrent(torrent_data: bytes, labels: Optional[list] = None) -> TorrentAddResult:
    """Add movie torrent to /movies directory."""
    manager = TransmissionManager()
    if manager.connect():
        return manager.add_torrent(torrent_data, MOVIES_DOWNLOAD_DIR, labels=labels or ["movie", "auto"])
    return TorrentAddResult(success=False, error="Connection failed")


def add_series_torrent(torrent_data: bytes, labels: Optional[list] = None) -> TorrentAddResult:
    """Add series torrent to /series directory."""
    manager = TransmissionManager()
    if manager.connect():
        return manager.add_torrent(torrent_data, SERIES_DOWNLOAD_DIR, labels=labels or ["series", "auto"])
    return TorrentAddResult(success=False, error="Connection failed")


def add_movie_torrent_from_url(url: str, labels: Optional[list] = None) -> TorrentAddResult:
    """Add movie torrent from URL to /movies directory."""
    manager = TransmissionManager()
    if manager.connect():
        return manager.add_torrent_from_url(url, MOVIES_DOWNLOAD_DIR, labels=labels or ["movie", "auto"])
    return TorrentAddResult(success=False, error="Connection failed")


def add_series_torrent_from_url(url: str, labels: Optional[list] = None) -> TorrentAddResult:
    """Add series torrent from URL to /series directory."""
    manager = TransmissionManager()
    if manager.connect():
        return manager.add_torrent_from_url(url, SERIES_DOWNLOAD_DIR, labels=labels or ["series", "auto"])
    return TorrentAddResult(success=False, error="Connection failed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    manager = TransmissionManager()
    if manager.connect():
        print("Connected to Transmission")
        torrents = manager.get_torrents()
        print(f"Current torrents: {len(torrents)}")
        for t in torrents[:5]:
            print(f"  ID:{t.id} {t.name[:60]} | {t.download_dir} | {t.status}")
    else:
        print("Failed to connect")