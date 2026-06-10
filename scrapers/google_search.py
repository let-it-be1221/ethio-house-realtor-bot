"""Google Search-based scraper for Ethiopian real estate listings.

This scraper uses Google search results to find property listings across
multiple Ethiopian real estate websites, making it more resilient to
individual site changes and outages.
"""

import re
import logging
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class GoogleSearchScraper(BaseScraper):
    """Scraper that uses Google search to find Ethiopian property listings."""

    def __init__(self):
        super().__init__()
        self.source_name = "google_search"
        self.search_queries = [
            "site:realtorsethiopia.com house for sale ethiopia",
            "site:realtorsethiopia.com apartment for rent addis ababa",
            "site:estateethiopia.com house for sale",
            "site:estateethiopia.com apartment for rent",
            "site:megbex.com property ethiopia",
            "site:ethiopiaproperty.com house sale",
            "ethiopia house for sale 2024 2025",
            "addis ababa apartment for rent 2024 2025",
            "ethiopia villa for sale addis ababa",
            "ethiopia real estate property listing",
            "addis ababa house for sale bole cmc",
            "ethiopia condominium for sale rent",
        ]

    def get_listing_urls(self) -> List[str]:
        """Search Google for Ethiopian real estate listing URLs."""
        all_urls = set()
        known_real_estate_domains = [
            "realtorsethiopia.com",
            "estateethiopia.com",
            "megbex.com",
            "ethiopiaproperty.com",
            "zemenfinancial.com",
            "beteseb.com",
            "qemer.com",
            "habesha.com",
            "homeethiopia.com",
        ]

        for query in self.search_queries:
            try:
                search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}&num=20"
                response = self._fetch_page(search_url)
                if not response:
                    continue

                soup = BeautifulSoup(response.text, "lxml")

                # Extract URLs from search results
                for link in soup.find_all("a", href=True):
                    href = link["href"]
                    # Google search results contain actual URLs in /url?q= format
                    if "/url?q=" in href:
                        actual_url = href.split("/url?q=")[1].split("&")[0]
                    elif href.startswith("http") and "google.com" not in href:
                        actual_url = href
                    else:
                        continue

                    # Only keep URLs from known real estate domains or that look like property listings
                    if any(domain in actual_url for domain in known_real_estate_domains):
                        all_urls.add(actual_url)
                    elif any(kw in actual_url.lower() for kw in ["/property/", "/listing/", "/houses/", "/apartment/", "/villa/"]):
                        if any(kw in actual_url.lower() for kw in ["ethiopia", "addis", "ababa"]):
                            all_urls.add(actual_url)

                import time
                import random
                time.sleep(random.uniform(3, 6))

            except Exception as e:
                logger.error(f"Error searching for '{query}': {e}")

        return list(all_urls)

    def scrape_listing(self, url: str) -> Optional[Dict]:
        """Scrape a listing found via search. Uses generic extraction."""
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
                og_title = soup.find("meta", property="og:title")
                if og_title and og_title.get("content"):
                    title = og_title["content"]
            if not title:
                title = "Property in Ethiopia"

            # Price
            price = ""
            price_match = re.search(
                r"(?:Price|price|PRICE)[:\s]*(\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*(?:ETB|Birr|USD|\$|Br|birr))",
                soup.get_text(), re.I
            )
            if price_match:
                price = price_match.group(1).strip()
            if not price:
                price_match2 = re.search(r"\d{1,3}(,\d{3})+(\.\d+)?\s*(ETB|Birr|USD|\$|Br)", soup.get_text(), re.I)
                if price_match2:
                    price = price_match2.group(0)

            # Location
            location = ""
            for tag in soup.find_all(["span", "div", "p", "li"], class_=re.compile(r"location|address|city|area", re.I)):
                text = tag.get_text(strip=True)
                if any(k in text.lower() for k in ["addis", "bole", "cmc", "ethiopia", "kazanchis", "mercato", "ayat"]):
                    if len(text) < 200:
                        location = text
                        break

            if not location:
                loc_match = re.search(
                    r"(?:Location|location|Address|address|Area|area)[:\s]*([A-Za-z\s,]+(?:Addis|Bole|CMC|Kazanchis|Ayat|Lideta|Kera|Jemo|Mekanisa)[A-Za-z\s,]*)",
                    soup.get_text(), re.I
                )
                if loc_match:
                    location = loc_match.group(1).strip()[:100]

            # Size
            size = ""
            size_match = re.search(r"(\d+)\s*(sqm|sq\.?\s*m|m²|square\s*meter)", soup.get_text(), re.I)
            if size_match:
                size = size_match.group(0)

            # Listing type
            listing_type = "Sale"
            text_lower = (url + " " + soup.get_text()[:2000]).lower()
            if "for rent" in text_lower or "rent" in text_lower[:500]:
                if "for sale" not in text_lower[:500]:
                    listing_type = "Rent"

            # Image
            image_url = ""
            og_img = soup.find("meta", property="og:image")
            if og_img and og_img.get("content"):
                image_url = og_img["content"]
            else:
                for img in soup.find_all("img"):
                    src = img.get("src", "") or img.get("data-src", "") or img.get("data-lazy-src", "")
                    if src and any(ext in src.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                        if not any(skip in src.lower() for skip in ["logo", "icon", "avatar", "banner-ad", "gravatar"]):
                            image_url = src
                            break

            if image_url and not image_url.startswith("http"):
                from urllib.parse import urljoin
                image_url = urljoin(url, image_url)

            # Description
            description = ""
            desc_div = soup.find("div", class_=re.compile(r"description|detail|content|property-detail", re.I))
            if desc_div:
                description = desc_div.get_text(strip=True)[:1000]
            if not description:
                meta_desc = soup.find("meta", attrs={"name": "description"})
                if meta_desc and meta_desc.get("content"):
                    description = meta_desc["content"][:1000]
            if not description:
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
