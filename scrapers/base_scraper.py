"""Base scraper class for Ethiopian real estate websites."""

import random
import time
import logging
import requests
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from config import USER_AGENTS, REQUEST_TIMEOUT, MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Abstract base class for all real estate scrapers."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })
        self.source_name = self.__class__.__name__

    def _get_random_user_agent(self) -> str:
        """Return a random user agent to avoid detection."""
        return random.choice(USER_AGENTS)

    def _fetch_page(self, url: str, retries: int = MAX_RETRIES) -> Optional[requests.Response]:
        """Fetch a web page with retry logic and random user agent rotation."""
        self.session.headers["User-Agent"] = self._get_random_user_agent()

        for attempt in range(retries):
            try:
                logger.info(f"Fetching: {url} (attempt {attempt + 1}/{retries})")
                response = self.session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as e:
                logger.warning(f"Request failed for {url}: {e}")
                if attempt < retries - 1:
                    sleep_time = RETRY_DELAY * (attempt + 1) + random.uniform(1, 3)
                    logger.info(f"Retrying in {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
                else:
                    logger.error(f"All retries exhausted for {url}")
        return None

    def _fetch_image(self, url: str) -> Optional[bytes]:
        """Download an image and return its bytes."""
        if not url:
            return None
        try:
            self.session.headers["User-Agent"] = self._get_random_user_agent()
            response = self.session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "image" in content_type or url.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                return response.content
        except Exception as e:
            logger.warning(f"Failed to download image {url}: {e}")
        return None

    @abstractmethod
    def get_listing_urls(self) -> List[str]:
        """Scrape the main listing pages and return individual listing URLs."""
        pass

    @abstractmethod
    def scrape_listing(self, url: str) -> Optional[Dict]:
        """Scrape an individual listing page and return structured data."""
        pass

    def scrape_all(self) -> List[Dict]:
        """Main entry point: scrape all listings from this source."""
        listings = []
        try:
            urls = self.get_listing_urls()
            logger.info(f"[{self.source_name}] Found {len(urls)} listing URLs")

            for url in urls:
                try:
                    listing = self.scrape_listing(url)
                    if listing:
                        listing["source"] = self.source_name
                        listings.append(listing)
                    # Polite delay between requests
                    time.sleep(random.uniform(2, 5))
                except Exception as e:
                    logger.error(f"Error scraping {url}: {e}")
                    continue

            logger.info(f"[{self.source_name}] Successfully scraped {len(listings)} listings")
        except Exception as e:
            logger.error(f"[{self.source_name}] Error during scrape_all: {e}")

        return listings
