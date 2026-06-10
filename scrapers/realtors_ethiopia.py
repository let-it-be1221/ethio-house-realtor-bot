"""Scraper for RealtorsEthiopia.com - Ethiopian realtor and property listing platform."""

import re
import logging
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class RealtorsEthiopiaScraper(BaseScraper):
    """Scraper for realtorsethiopia.com property listings."""

    def __init__(self):
        super().__init__()
        self.source_name = "realtors_ethiopia"
        self.base_url = "https://realtorsethiopia.com"
        self.listing_pages = [
            f"{self.base_url}/properties/",
            f"{self.base_url}/properties/?status=for-sale",
            f"{self.base_url}/properties/?status=for-rent",
            f"{self.base_url}/property-category/house/",
            f"{self.base_url}/property-category/apartment/",
            f"{self.base_url}/property-category/villa/",
            f"{self.base_url}/property-category/land/",
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

                # Find property links
                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    if "/property/" in href:
                        full_url = href if href.startswith("http") else self.base_url + href
                        all_urls.add(full_url)

                # Try pagination
                for page_num in range(2, 4):
                    paginated_url = f"{page_url}page/{page_num}/" if page_url.endswith("/") else f"{page_url}/page/{page_num}/"
                    try:
                        resp = self._fetch_page(paginated_url)
                        if resp:
                            page_soup = BeautifulSoup(resp.text, "lxml")
                            for link in page_soup.find_all("a", href=True):
                                href = link["href"]
                                if "/property/" in href:
                                    full_url = href if href.startswith("http") else self.base_url + href
                                    all_urls.add(full_url)
                    except Exception:
                        break

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
            title = ""
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(strip=True)
            if not title:
                title_tag = soup.find("h2", class_=re.compile(r"title|entry", re.I))
                if title_tag:
                    title = title_tag.get_text(strip=True)
            if not title:
                title = "Property in Ethiopia"

            # Price
            price = ""
            price_tag = soup.find("span", class_=re.compile(r"price|Price", re.I))
            if not price_tag:
                price_tag = soup.find("div", class_=re.compile(r"price|Price", re.I))
            if price_tag:
                price = price_tag.get_text(strip=True)
            if not price:
                price_match = re.search(r"\d{1,3}(,\d{3})*(\.\d+)?\s*(ETB|Birr|USD|\$|Br)", soup.get_text(), re.I)
                if price_match:
                    price = price_match.group(0)

            # Location
            location = ""
            for tag in soup.find_all(["span", "div", "li"], class_=re.compile(r"location|address|city", re.I)):
                text = tag.get_text(strip=True)
                if len(text) < 200:
                    location = text
                    break

            # Also try to find location from property details table
            if not location:
                for dt in soup.find_all("dt"):
                    if "location" in dt.get_text(strip=True).lower() or "address" in dt.get_text(strip=True).lower():
                        dd = dt.find_next_sibling("dd")
                        if dd:
                            location = dd.get_text(strip=True)
                            break

            # Size
            size = ""
            size_match = re.search(r"(\d+)\s*(sqm|sq\.?\s*m|m²|square)", soup.get_text(), re.I)
            if size_match:
                size = size_match.group(0)

            # Listing type
            listing_type = "Sale"
            text_lower = (url + " " + soup.get_text()[:1000]).lower()
            if "for rent" in text_lower or "rent" in text_lower:
                listing_type = "Rent"

            # Image
            image_url = ""
            og_img = soup.find("meta", property="og:image")
            if og_img and og_img.get("content"):
                image_url = og_img["content"]
            else:
                # Try featured image
                feat_img = soup.find("img", class_=re.compile(r"wp-post-image|featured|property", re.I))
                if feat_img:
                    image_url = feat_img.get("src", "") or feat_img.get("data-src", "")
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
            desc_div = soup.find("div", class_=re.compile(r"description|detail|property-detail|content", re.I))
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
