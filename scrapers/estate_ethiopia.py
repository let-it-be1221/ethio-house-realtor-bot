"""Scraper for EstateEthiopia.com - One of Ethiopia's largest real estate platforms."""

import re
import logging
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class EstateEthiopiaScraper(BaseScraper):
    """Scraper for estateethiopia.com property listings."""

    def __init__(self):
        super().__init__()
        self.source_name = "estate_ethiopia"
        self.base_url = "https://estateethiopia.com"
        self.listing_pages = [
            f"{self.base_url}/property-category/houses-for-sale/",
            f"{self.base_url}/property-category/houses-for-rent/",
            f"{self.base_url}/property-category/apartments-for-sale/",
            f"{self.base_url}/property-category/apartments-for-rent/",
            f"{self.base_url}/property-category/villa-for-sale/",
            f"{self.base_url}/property-category/villa-for-rent/",
            f"{self.base_url}/property-category/condominium/",
            f"{self.base_url}/property-category/land-for-sale/",
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

                # Find property listing links
                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    if "/property/" in href and href not in all_urls:
                        full_url = href if href.startswith("http") else self.base_url + href
                        all_urls.add(full_url)

                # Check for pagination
                pagination = soup.find_all("a", class_="page-numbers")
                for page_link in pagination:
                    if page_link.get("href"):
                        try:
                            resp = self._fetch_page(page_link["href"])
                            if resp:
                                page_soup = BeautifulSoup(resp.text, "lxml")
                                for link in page_soup.find_all("a", href=True):
                                    href = link["href"]
                                    if "/property/" in href and href not in all_urls:
                                        full_url = href if href.startswith("http") else self.base_url + href
                                        all_urls.add(full_url)
                        except Exception:
                            pass

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
            # Title
            title_tag = soup.find("h1") or soup.find("h2", class_="entry-title")
            title = title_tag.get_text(strip=True) if title_tag else "Property in Ethiopia"

            # Price
            price = ""
            price_tag = soup.find("span", class_=re.compile(r"price|Price", re.I))
            if not price_tag:
                price_tag = soup.find(string=re.compile(r"\d{1,3}(,\d{3})*\s*(ETB|Birr|USD|\$)", re.I))
            if price_tag:
                price = price_tag.get_text(strip=True) if hasattr(price_tag, "get_text") else str(price_tag).strip()

            # Location
            location = ""
            loc_tag = soup.find("span", class_=re.compile(r"location|address", re.I))
            if not loc_tag:
                loc_tag = soup.find(string=re.compile(r"(Addis|Bole|CMC|Kazanchis|Mercato|Sarbet|Kera|Jemo|Ayat|Gulen|Lideta|Mekanisa|Fit Ber|Meshualekia)", re.I))
            if loc_tag:
                location = loc_tag.get_text(strip=True) if hasattr(loc_tag, "get_text") else str(loc_tag).strip()
            else:
                # Try to find in address-like elements
                for tag in soup.find_all(["div", "span", "p"], class_=re.compile(r"location|address|area", re.I)):
                    text = tag.get_text(strip=True)
                    if any(k in text.lower() for k in ["addis", "bole", "cmc", "ethiopia"]):
                        location = text
                        break

            # Size
            size = ""
            size_tag = soup.find(string=re.compile(r"\d+\s*(sqm|sq\.?\s*m|m²|square)", re.I))
            if size_tag:
                size = size_tag.strip() if isinstance(size_tag, str) else size_tag.get_text(strip=True)
            else:
                for tag in soup.find_all(["div", "span", "li"]):
                    text = tag.get_text(strip=True)
                    if re.search(r"\d+\s*(sqm|sq\.?\s*m|m²|square)", text, re.I):
                        size = text
                        break

            # Listing type (sale/rent)
            listing_type = "Sale"
            url_lower = url.lower()
            page_text = soup.get_text().lower()
            if "for rent" in url_lower or "for-rent" in url_lower or "rent" in page_text[:500]:
                listing_type = "Rent"

            # Image
            image_url = ""
            img_tag = soup.find("img", class_=re.compile(r"wp-post-image|property-image|featured", re.I))
            if not img_tag:
                # Try og:image meta tag
                og_img = soup.find("meta", property="og:image")
                if og_img and og_img.get("content"):
                    image_url = og_img["content"]
                else:
                    # Try first large image
                    for img in soup.find_all("img"):
                        src = img.get("src", "") or img.get("data-src", "")
                        if src and any(ext in src.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                            if not any(skip in src.lower() for skip in ["logo", "icon", "avatar", "banner-ad"]):
                                image_url = src
                                break
            else:
                image_url = img_tag.get("src", "") or img_tag.get("data-src", "")

            if image_url and not image_url.startswith("http"):
                image_url = self.base_url + image_url

            # Description
            description = ""
            desc_tag = soup.find("div", class_=re.compile(r"description|content|detail", re.I))
            if desc_tag:
                description = desc_tag.get_text(strip=True)[:1000]
            else:
                # Fallback: grab the main content area
                content = soup.find("div", class_=re.compile(r"entry-content|property-content", re.I))
                if content:
                    description = content.get_text(strip=True)[:1000]

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

            # Only return if we have at least a title and some useful info
            if listing["title"] and (listing["price"] or listing["location"] or listing["description"]):
                return listing

        except Exception as e:
            logger.error(f"Error parsing listing {url}: {e}")

        return None
