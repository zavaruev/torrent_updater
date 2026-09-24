#!/usr/bin/env python3
"""
IMDB Most Popular (moviemeter) scraper using SeleniumBase.
Fetches trending/popular movies and TV shows from IMDB.
"""

import re
import logging
import time
from typing import List, Dict, Optional
from dataclasses import dataclass
from seleniumbase import SB
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

IMDB_MOST_POPULAR_MOVIES = "https://www.imdb.com/chart/moviemeter/"
IMDB_MOST_POPULAR_TV = "https://www.imdb.com/chart/tvmeter/"
IMDB_TOP_250_MOVIES = "https://www.imdb.com/chart/top/"
IMDB_TOP_250_TV = "https://www.imdb.com/chart/toptv/"


@dataclass
class IMDBItem:
    """IMDB movie/show entry."""
    rank: int
    title: str
    year: Optional[int]
    rating: float
    imdb_id: str
    type: str  # "movie" or "tv"
    votes: Optional[int] = None


class IMDBScraper:
    """Scrapes IMDB charts for popular movies and TV shows using SeleniumBase."""

    def __init__(self, min_rating: float = 7.0):
        self.min_rating = min_rating
        self._sb_cm = None
        self.sb = None
        self.driver = None

    def _ensure_driver(self):
        """Initialize driver if not already done."""
        if self.driver is None:
            self._sb_cm = SB(uc=True, xvfb=True)
            self.sb = self._sb_cm.__enter__()
            self.driver = self.sb.driver

    def __enter__(self):
        self._ensure_driver()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._sb_cm:
            self._sb_cm.__exit__(exc_type, exc_val, exc_tb)
            self._sb_cm = None
            self.sb = None
            self.driver = None

    def _fetch_page(self, url: str) -> Optional[str]:
        """Fetch a page and return HTML source."""
        self._ensure_driver()
        try:
            # Use execute_script for navigation - more stable with UC mode
            self.driver.execute_script("window.location.href = arguments[0]", url)
            time.sleep(8)  # IMDB needs more time to load
            # Verify driver is still alive
            _ = self.driver.title
            return self.driver.page_source
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            # Driver might be dead, reset and retry once
            self._reset_driver()
            try:
                self._ensure_driver()
                self.driver.execute_script("window.location.href = arguments[0]", url)
                time.sleep(8)
                return self.driver.page_source
            except Exception as e2:
                logger.error(f"Retry also failed: {e2}")
                return None

    def _reset_driver(self):
        """Reset the driver connection."""
        if self._sb_cm:
            try:
                self._sb_cm.__exit__(None, None, None)
            except:
                pass
            self._sb_cm = None
            self.sb = None
            self.driver = None

    def _parse_chart_page(self, html: str, item_type: str) -> List[IMDBItem]:
        """Parse an IMDB chart page HTML."""
        soup = BeautifulSoup(html, 'html.parser')
        items = []

        # Find the chart list - new IMDB layout uses ul.ipc-metadata-list
        chart_list = soup.find('ul', class_=re.compile(r'ipc-metadata-list'))
        
        if not chart_list:
            # Try alternative selectors
            chart_list = soup.find('div', {'data-testid': 'chart-layout-main-column'})
            if chart_list:
                chart_list = chart_list.find('ul', class_=re.compile(r'ipc-metadata-list'))

        if not chart_list:
            logger.warning("Could not find chart list in IMDB page")
            return items

        # Parse list items
        list_items = chart_list.find_all('li', class_=re.compile(r'ipc-metadata-list-summary-item'))
        
        for i, item in enumerate(list_items[:100], 1):
            try:
                parsed = self._parse_list_item(item, i, item_type)
                if parsed and parsed.rating >= self.min_rating:
                    items.append(parsed)
            except Exception as e:
                logger.debug(f"Failed to parse item {i}: {e}")
                continue

        return items

    def _parse_list_item(self, item, rank: int, item_type: str) -> Optional[IMDBItem]:
        """Parse a single chart list item."""
        # Title - look for the title link/div
        title_elem = item.find('a', class_='ipc-title-link-wrapper')
        if not title_elem:
            title_elem = item.find('div', class_=lambda x: x and 'ipc-title' in x and 'cli-title' in x)
        
        if not title_elem:
            return None

        title_text = title_elem.get_text(strip=True)
        
        # Extract IMDB ID from link
        imdb_id = None
        if title_elem.name == 'a' and 'href' in title_elem.attrs:
            match = re.search(r'/title/(tt\d+)/', title_elem['href'])
            if match:
                imdb_id = match.group(1)
        else:
            # Try to find link within the item
            link = item.find('a', href=re.compile(r'/title/tt\d+/'))
            if link:
                match = re.search(r'/title/(tt\d+)/', link['href'])
                if match:
                    imdb_id = match.group(1)

        # Extract year and clean title
        year = None
        metadata_div = item.find('div', class_=lambda x: x and 'cli-title-metadata' in x)
        if metadata_div:
            metadata_text = metadata_div.get_text(strip=True)
            year_match = re.search(r'(20\d{2}|19\d{2})', metadata_text)
            if year_match:
                year = int(year_match.group(1))
        
        clean_title = re.sub(r'^\d+\.\s*', '', title_text)
        if year:
            clean_title = clean_title.replace(str(year), '').strip()
        clean_title = clean_title.strip()

        # Extract rating - from span with ipc-rating-star--rating class
        rating = 0.0
        rating_span = item.find('span', class_='ipc-rating-star--rating')
        if rating_span:
            rating_text = rating_span.get_text(strip=True)
            match = re.search(r'(\d+\.?\d*)', rating_text)
            if match:
                rating = float(match.group(1))

        # Extract votes - from the parent ipc-rating-star span
        votes = None
        if rating_span:
            parent = rating_span.find_parent('span', class_=lambda x: x and 'ipc-rating-star' in ' '.join(x) and 'imdb' in ' '.join(x))
            if parent:
                votes_text = parent.get_text(strip=True)
                match = re.search(r'\(([\d,]+)\)', votes_text)
                if match:
                    votes = int(match.group(1).replace(',', ''))

        if not imdb_id:
            imdb_id = f"pseudo_{clean_title.lower().replace(' ', '_')}"

        return IMDBItem(
            rank=rank,
            title=clean_title,
            year=year,
            rating=rating,
            imdb_id=imdb_id,
            type=item_type,
            votes=votes
        )

    def get_most_popular_movies(self, limit: int = 50) -> List[IMDBItem]:
        """Get most popular movies (moviemeter)."""
        logger.info("Fetching IMDB Most Popular Movies...")
        html = self._fetch_page(IMDB_MOST_POPULAR_MOVIES)
        if not html:
            return []
        items = self._parse_chart_page(html, "movie")
        return items[:limit]

    def get_most_popular_tv(self, limit: int = 50) -> List[IMDBItem]:
        """Get most popular TV shows (tvmeter)."""
        logger.info("Fetching IMDB Most Popular TV...")
        html = self._fetch_page(IMDB_MOST_POPULAR_TV)
        if not html:
            return []
        items = self._parse_chart_page(html, "tv")
        return items[:limit]

    def get_top_250_movies(self, limit: int = 50) -> List[IMDBItem]:
        """Get Top 250 movies."""
        logger.info("Fetching IMDB Top 250 Movies...")
        html = self._fetch_page(IMDB_TOP_250_MOVIES)
        if not html:
            return []
        items = self._parse_chart_page(html, "movie")
        return items[:limit]

    def get_top_250_tv(self, limit: int = 50) -> List[IMDBItem]:
        """Get Top 250 TV shows."""
        logger.info("Fetching IMDB Top 250 TV...")
        html = self._fetch_page(IMDB_TOP_250_TV)
        if not html:
            return []
        items = self._parse_chart_page(html, "tv")
        return items[:limit]

    def get_all_popular(self, limit_per_chart: int = 30) -> Dict[str, List[IMDBItem]]:
        """Get all popular charts combined."""
        return {
            'movies_moviemeter': self.get_most_popular_movies(limit_per_chart),
            'tv_tvmeter': self.get_most_popular_tv(limit_per_chart),
            'movies_top250': self.get_top_250_movies(limit_per_chart),
            'tv_top250': self.get_top_250_tv(limit_per_chart),
        }


def search_imdb_rating(title: str, year: Optional[int] = None) -> Optional[float]:
    """Search IMDB for a specific title's rating (fallback)."""
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    with IMDBScraper(min_rating=7.0) as scraper:
        print("=== Most Popular Movies ===")
        for item in scraper.get_most_popular_movies(10):
            print(f"  #{item.rank} {item.title} ({item.year}) - {item.rating}/10 [{item.imdb_id}]")
        
        print("\n=== Most Popular TV ===")
        for item in scraper.get_most_popular_tv(10):
            print(f"  #{item.rank} {item.title} ({item.year}) - {item.rating}/10 [{item.imdb_id}]")