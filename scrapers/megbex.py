"""Scraper for Megbex.com - Ethiopian real estate and property listing platform."""

import re
import logging
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class MegbexScraper(BaseScraper):
    """Scraper for megbex.com property listings."""

    def __init__(self):
        super().__init__()
        self.source_name = "megbex"
        self.base_url = "https://megbex.com"
        self.listing_pages = [
            f"{self.base_url}/properties/",
            f"{self.base_url}/properties/for-sale/",
            f"{self.base_url}/properties/for-rent/",
            f"{self.base_url}/category/houses/",
            f"{self.base_url}/category/apartments/",
        ]

    def get_listing_urls(self) -> List[str]:
        """Scrape listing pages for individual property URLs."""
        all_urls = set()

        for page_url in self.listing_pages:
            try:
                response = self._fetch_page(page_url)
                if not response:
                    continue

                soup = BeautifulSoup(response.text, "lxml")

                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    if any(pattern in href for pattern in ["/property/", "/listing/", "/properties/"]):
                        full_url = href if href.startswith("http") else self.base_url + href
                        if full_url not in all_urls and not full_url.endswith("/properties/"):
                            all_urls.add(full_url)

            except Exception as e:
                logger.error(f"Error fetching {page_url}: {e}")

        return list(all_urls)

    def scrape_listing(self, url: str) -> Optional[Dict]:
        """Scrape an individual property listing page."""
        response = self._fetch_page(url)
        if not response:
            return None

        soup = BeautifulSoup(response.text, "lxml")

        try:
            title = ""
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)
            if not title:
                title = "Property in Ethiopia"

            # Price
            price = ""
            price_tag = soup.find(string=re.compile(r"\d{1,3}(,\d{3})*\s*(ETB|Birr|USD|\$|Br)", re.I))
            if price_tag:
                price = price_tag.strip() if isinstance(price_tag, str) else price_tag.get_text(strip=True)
            for tag in soup.find_all(["span", "div", "p"], class_=re.compile(r"price", re.I)):
                price = tag.get_text(strip=True)
                break

            # Location
            location = ""
            for tag in soup.find_all(["span", "div", "p"], class_=re.compile(r"location|address", re.I)):
                text = tag.get_text(strip=True)
                if len(text) < 200:
                    location = text
                    break

            # Size
            size = ""
            size_match = re.search(r"(\d+)\s*(sqm|sq\.?\s*m|m²|square)", soup.get_text(), re.I)
            if size_match:
                size = size_match.group(0)

            # Listing type
            listing_type = "Sale"
            if "rent" in (url + " " + soup.get_text()[:500]).lower():
                listing_type = "Rent"

            # Image
            image_url = ""
            og_img = soup.find("meta", property="og:image")
            if og_img and og_img.get("content"):
                image_url = og_img["content"]
            else:
                for img in soup.find_all("img"):
                    src = img.get("src", "") or img.get("data-src", "")
                    if src and any(ext in src.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                        if not any(skip in src.lower() for skip in ["logo", "icon", "avatar"]):
                            image_url = src
                            break

            if image_url and not image_url.startswith("http"):
                image_url = self.base_url + image_url

            # Description
            description = ""
            desc_div = soup.find("div", class_=re.compile(r"description|detail|content", re.I))
            if desc_div:
                description = desc_div.get_text(strip=True)[:1000]
            else:
                article = soup.find("article")
                if article:
                    description = article.get_text(strip=True)[:1000]

            listing = {
                "title": title,
                "price": price,
                "location": location,
                "size": size,
                "listing_type": listing_type,
                "image_url": image_url,
                "description": description,
                "source_url": url,
            }

            if listing["title"] and (listing["price"] or listing["location"] or listing["description"]):
                return listing

        except Exception as e:
            logger.error(f"Error parsing listing {url}: {e}")

        return None
