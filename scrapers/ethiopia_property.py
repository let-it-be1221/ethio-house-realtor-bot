"""Scraper for EthiopiaProperty.com - Ethiopian real estate listings platform."""

import re
import logging
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class EthiopiaPropertyScraper(BaseScraper):
    """Scraper for ethiopiaproperty.com property listings."""

    def __init__(self):
        super().__init__()
        self.source_name = "ethiopia_property"
        self.base_url = "https://ethiopiaproperty.com"
        self.listing_pages = [
            f"{self.base_url}/properties/?status=for-sale",
            f"{self.base_url}/properties/?status=for-rent",
            f"{self.base_url}/properties/?type=house",
            f"{self.base_url}/properties/?type=apartment",
            f"{self.base_url}/properties/?type=villa",
            f"{self.base_url}/properties/",
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

                # Find property links - look for common patterns
                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    if any(pattern in href for pattern in ["/property/", "/properties/", "/listing/"]):
                        if href not in all_urls and not href.endswith("/properties/"):
                            full_url = href if href.startswith("http") else self.base_url + href
                            all_urls.add(full_url)

                # Try pagination (up to 3 pages)
                for page_num in range(2, 4):
                    paginated_url = f"{page_url}&page={page_num}" if "?" in page_url else f"{page_url}?page={page_num}"
                    try:
                        resp = self._fetch_page(paginated_url)
                        if resp:
                            page_soup = BeautifulSoup(resp.text, "lxml")
                            for link in page_soup.find_all("a", href=True):
                                href = link["href"]
                                if any(pattern in href for pattern in ["/property/", "/properties/", "/listing/"]):
                                    if href not in all_urls:
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
            else:
                title_tag = soup.find("h2") or soup.find("h3")
                if title_tag:
                    title = title_tag.get_text(strip=True)

            if not title:
                title = "Property in Ethiopia"

            # Price
            price = ""
            for selector in [
                soup.find("span", class_=re.compile(r"price", re.I)),
                soup.find("div", class_=re.compile(r"price", re.I)),
                soup.find("li", class_=re.compile(r"price", re.I)),
            ]:
                if selector:
                    price = selector.get_text(strip=True)
                    break
            if not price:
                price_match = re.search(r"\d{1,3}(,\d{3})*(\.\d+)?\s*(ETB|Birr|USD|\$|Br)", soup.get_text(), re.I)
                if price_match:
                    price = price_match.group(0)

            # Location
            location = ""
            for selector in [
                soup.find("span", class_=re.compile(r"location|address", re.I)),
                soup.find("div", class_=re.compile(r"location|address", re.I)),
                soup.find("li", class_=re.compile(r"location|address", re.I)),
            ]:
                if selector:
                    location = selector.get_text(strip=True)
                    break

            # Size
            size = ""
            size_match = re.search(r"(\d+)\s*(sqm|sq\.?\s*m|m²|square\s*meter)", soup.get_text(), re.I)
            if size_match:
                size = size_match.group(0)

            # Listing type
            listing_type = "Sale"
            page_text = (url + " " + soup.get_text()[:1000]).lower()
            if "rent" in page_text and "sale" not in page_text:
                listing_type = "Rent"
            elif "for rent" in page_text:
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
                        if not any(skip in src.lower() for skip in ["logo", "icon", "avatar", "banner"]):
                            image_url = src
                            break

            if image_url and not image_url.startswith("http"):
                image_url = self.base_url + image_url

            # Description
            description = ""
            desc_tag = soup.find("div", class_=re.compile(r"description|detail|content", re.I))
            if desc_tag:
                description = desc_tag.get_text(strip=True)[:1000]
            else:
                content = soup.find("article") or soup.find("div", class_="content")
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

            if listing["title"] and (listing["price"] or listing["location"] or listing["description"]):
                return listing

        except Exception as e:
            logger.error(f"Error parsing listing {url}: {e}")

        return None
