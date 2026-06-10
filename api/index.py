"""Vercel Serverless: Ethiopian Real Estate → Telegram Bot

Architecture: Posts max 5 NEW property listings/day to @Ethio_House_Realtor.
Each listing comes from a DIFFERENT source site (no same-site repeats per day).
No listing is ever posted twice (dedup via URL hashes).

How it works:
  - /api/rotate  →  runs ONE scraper, finds ONE new listing, posts it  (5-8 sec)
  - External cron (cron-job.org) hits /api/rotate every 2-3 hours
  - State stored in GitHub Gist (free, persistent, serverless-friendly)
  - Daily limit: 5 posts max, each from a different site

Setup:
  1. Deploy to Vercel
  2. Hit /api/init-gist once to create state storage
  3. Set up cron-job.org: GET https://your-app.vercel.app/api/rotate every 2-3 hours
"""

import hashlib
import json
import logging
import os
import random
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuration (secrets from environment variables) ─────────────
# IMPORTANT: Set these in Vercel Dashboard > Settings > Environment Variables
#   TELEGRAM_BOT_TOKEN = your bot token from @BotFather
#   GITHUB_PAT = your GitHub personal access token (for Gist state storage)
#   CONTACT_PHONE = fallback phone number (default: 0949024661)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@Ethio_House_Realtor")
CONTACT_PHONE = os.environ.get("CONTACT_PHONE", "0949024661")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

GITHUB_PAT = os.environ.get("GITHUB_PAT", "")
GIST_STATE_FILE = "ethio_bot_state.json"

MAX_DAILY_POSTS = 5
MAX_HASH_STORE = 500  # Keep last 500 posted hashes to prevent repeats

def _check_config():
    """Check if required environment variables are set."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not GITHUB_PAT:
        missing.append("GITHUB_PAT")
    return missing


# ══════════════════════════════════════════════════════════════════
# GIST STATE MANAGER  (persistent storage for serverless)
# ══════════════════════════════════════════════════════════════════
class GistState:
    """Use a private GitHub Gist as a tiny JSON database.

    Why Gist? Vercel serverless has no persistent filesystem, and
    we need to track: daily post count, posted hashes, scraper rotation.
    GitHub Gists are free, reliable, and fast (~0.3s per read/write).
    """

    _cached_gist_id = None  # Module-level cache survives within same instance

    def __init__(self):
        self.headers = {
            "Authorization": f"token {GITHUB_PAT}",
            "Accept": "application/vnd.github.v3+json",
        }

    def _find_gist(self) -> str:
        """Find our state gist by filename. Returns gist ID or empty string."""
        if GistState._cached_gist_id:
            return GistState._cached_gist_id
        try:
            r = requests.get(
                "https://api.github.com/gists?per_page=100",
                headers=self.headers, timeout=8,
            )
            if r.status_code == 200:
                for gist in r.json():
                    if GIST_STATE_FILE in gist.get("files", {}):
                        GistState._cached_gist_id = gist["id"]
                        return gist["id"]
        except Exception as e:
            logger.error(f"Gist find error: {e}")
        return ""

    def _create_gist(self) -> str:
        """Create a new private gist for state storage."""
        state = self._default_state()
        try:
            r = requests.post(
                "https://api.github.com/gists",
                headers=self.headers,
                json={
                    "description": "Ethio House Realtor Bot - State Storage",
                    "public": False,
                    "files": {GIST_STATE_FILE: {"content": json.dumps(state, indent=2)}},
                },
                timeout=8,
            )
            if r.status_code == 201:
                gid = r.json()["id"]
                GistState._cached_gist_id = gid
                logger.info(f"Created new gist: {gid}")
                return gid
        except Exception as e:
            logger.error(f"Gist create error: {e}")
        return ""

    @staticmethod
    def _default_state() -> dict:
        return {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "posts_today": 0,
            "next_scraper_idx": 0,
            "today_sources": [],
            "posted_hashes": [],
            "gist_id": "",
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    def load(self) -> dict:
        """Read current state from Gist. Creates gist if none exists."""
        gid = self._find_gist()
        if not gid:
            gid = self._create_gist()
        if not gid:
            return self._default_state()
        try:
            r = requests.get(
                f"https://api.github.com/gists/{gid}",
                headers=self.headers, timeout=8,
            )
            if r.status_code == 200:
                content = r.json()["files"][GIST_STATE_FILE]["content"]
                state = json.loads(content)
                state["gist_id"] = gid
                return state
        except Exception as e:
            logger.error(f"Gist load error: {e}")
        return self._default_state()

    def save(self, state: dict):
        """Write updated state back to Gist."""
        gid = state.get("gist_id") or self._find_gist() or self._create_gist()
        if not gid:
            logger.error("Cannot save state: no gist ID available")
            return
        state["last_updated"] = datetime.now(timezone.utc).isoformat()
        # Prune old hashes to keep the Gist file small
        if len(state.get("posted_hashes", [])) > MAX_HASH_STORE:
            state["posted_hashes"] = state["posted_hashes"][-MAX_HASH_STORE:]
        try:
            requests.patch(
                f"https://api.github.com/gists/{gid}",
                headers=self.headers,
                json={"files": {GIST_STATE_FILE: {"content": json.dumps(state, indent=2)}}},
                timeout=8,
            )
        except Exception as e:
            logger.error(f"Gist save error: {e}")


# ══════════════════════════════════════════════════════════════════
# BASE SCRAPER
# ══════════════════════════════════════════════════════════════════
class BaseScraper(ABC):
    """Base class for all property scrapers.

    Key method: scrape_one_new(posted_hashes) → finds ONE new listing
    that hasn't been posted yet. Designed to complete in 5-8 seconds.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
        })
        self.source_name = self.__class__.__name__

    def _fetch_page(self, url: str, retries: int = 1) -> Optional[requests.Response]:
        """Fetch a URL with random User-Agent. Retries=1 for speed."""
        self.session.headers["User-Agent"] = random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        ])
        for attempt in range(retries + 1):
            try:
                r = self.session.get(url, timeout=8, allow_redirects=True)
                r.raise_for_status()
                return r
            except Exception as e:
                logger.warning(f"Fetch failed ({attempt + 1}/{retries + 1}): {url} - {e}")
                if attempt < retries:
                    time.sleep(1)
        return None

    @abstractmethod
    def get_listing_urls(self) -> List[str]:
        """Fetch a category/index page and return individual listing URLs."""

    @abstractmethod
    def scrape_listing(self, url: str) -> Optional[Dict]:
        """Scrape a single listing detail page and return listing dict."""

    def scrape_one_new(self, posted_hashes: Set[str]) -> Optional[Dict]:
        """Find and scrape ONE new (unposted) listing.

        Checks each listing URL against posted_hashes. Returns the first
        new listing found that passes validation, or None if all are 
        already posted or invalid.
        """
        try:
            urls = self.get_listing_urls()
            logger.info(f"[{self.source_name}] Found {len(urls)} listing URLs")
            for url in urls[:8]:  # Check up to 8 URLs
                url_hash = hashlib.sha256(url.encode()).hexdigest()[:32]
                if url_hash in posted_hashes:
                    logger.debug(f"[{self.source_name}] Skipping already-posted: {url}")
                    continue
                listing = self.scrape_listing(url)
                if listing:
                    listing["source"] = self.source_name
                    listing["url_hash"] = url_hash
                    # Validate data quality before accepting
                    if _validate_listing(listing):
                        return listing
                    else:
                        logger.warning(f"[{self.source_name}] Listing failed validation, skipping: {url}")
                time.sleep(0.5)
        except Exception as e:
            logger.error(f"[{self.source_name}] scrape_one_new error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ══════════════════════════════════════════════════════════════════
def _extract_phone(soup) -> str:
    """Extract owner phone from a listing page using multiple methods."""
    owner_phone = ""
    # Method 1: tel: href links (most reliable)
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if href.startswith("tel:"):
            owner_phone = href.replace("tel:", "").strip()
            break
    # Method 2: Labeled fields (Phone:, Tel:, Contact:)
    if not owner_phone:
        page_text = soup.get_text()
        m = re.search(
            r"(?:Phone|Tel|Mobile|Contact|Call)[\s:]*([+]?[0-9][\d\s\-]{6,})",
            page_text, re.I,
        )
        if m:
            owner_phone = m.group(1).strip()
    # Method 3: Ethiopian phone pattern
    if not owner_phone:
        m = re.search(r"(?:(?:\+251|0)(?:9|11|7)\d{7,8})", soup.get_text())
        if m:
            owner_phone = m.group(0).strip()
    # Clean and validate
    if owner_phone:
        owner_phone = re.sub(r"[^+0-9]", "", owner_phone)
        if len(re.sub(r"[^0-9]", "", owner_phone)) < 9:
            owner_phone = ""
    return owner_phone


def _extract_bedrooms(text: str) -> str:
    """Extract bedroom count from text."""
    m = re.search(r"(\d+)\s*(?:Bedroom|Bed|BR|br)", text, re.I)
    return m.group(0) if m else ""


def _extract_bathrooms(text: str) -> str:
    """Extract bathroom count from text."""
    m = re.search(r"(\d+)\s*(?:Bathroom|Bath|BA|ba)", text, re.I)
    return m.group(0) if m else ""


def _validate_listing(listing: dict) -> bool:
    """Validate that a listing has enough quality to post.
    
    Rejects listings with:
    - No title
    - Price containing filter/navigation text (e.g. "Min Price", "Max Price")
    - Location containing filter text
    - Description with HTML tags
    """
    title = listing.get("title", "")
    price = listing.get("price", "")
    location = listing.get("location", "")
    desc = listing.get("description", "")
    
    # Must have a title
    if not title or title == "Property in Ethiopia":
        return False
    
    # Price must not contain navigation/filter text
    if price:
        bad_price_words = ["min price", "max price", "any", "filter", "10,000etb", "25,000etb", "50,000etb"]
        if any(w in price.lower() for w in bad_price_words):
            listing["price"] = ""  # Clear bad price, still allow posting
    
    # Location must not contain filter/navigation text
    if location:
        bad_loc_words = ["filter", "search", "reset", "open on google", "bedroom"]
        if any(w in location.lower() for w in bad_loc_words):
            listing["location"] = ""
    
    # Clean HTML tags from description
    if desc:
        desc = re.sub(r"<[^>]+>", "", desc)
        listing["description"] = desc.strip()
    
    # Must have at least price or description
    if not listing.get("price") and not listing.get("description"):
        return False
    
    return True


# ══════════════════════════════════════════════════════════════════
# SITE-SPECIFIC EXTRACTORS
# ══════════════════════════════════════════════════════════════════

def _extract_realethio(soup, url) -> Optional[Dict]:
    """Extract listing data from realethio.com property pages.
    
    Site structure:
      - h1: title
      - div.price / span.price: "190,000,000ETB"
      - a[href^="tel:"]: "+251911619180"
      - div.h-area / div.h-beds / div.h-baths: details
      - meta og:image: property photo
      - meta description: property description
    """
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"]
    
    # Price: get FIRST element with class "price" (the main listing price)
    price = ""
    price_tag = soup.find(["span", "div"], class_="price")
    if price_tag:
        price = price_tag.get_text(strip=True)
    if not price:
        price_tag = soup.find(["span", "div"], class_="item-price")
        if price_tag:
            price = price_tag.get_text(strip=True)
    
    # Clean price: remove duplicate currency codes
    if price:
        price = re.sub(r"(ETB|Birr|USD|\$|Br)\s*(ETB|Birr|USD|\$|Br)", r"\1", price, flags=re.I)
    
    # Location: extract from title or meta
    location = ""
    # Try "Area:" label in text
    area_m = re.search(r"Area:\s*([A-Za-z\s,]+?)(?:\n|Country|$)", soup.get_text(), re.I)
    if area_m:
        location = area_m.group(1).strip()
    if not location:
        # Extract from title: "Bole, 3 Bedrooms house for sale, Addis Ababa"
        loc_m = re.match(r"^([A-Za-z][A-Za-z\s]+?),\s*\d", title)
        if loc_m:
            location = loc_m.group(1).strip()
    if not location:
        loc_m = re.search(r"(?:in|at)\s+([A-Za-z][A-Za-z\s]+?)(?:\s*[-,|]|\s*\d)", title, re.I)
        if loc_m:
            location = loc_m.group(1).strip()
    
    # Size
    size = ""
    size_m = re.search(r"(\d[\d,]*\.?\d*)\s*(m²|sqm|sq\.?\s*m)", soup.get_text(), re.I)
    if size_m:
        size = size_m.group(0)
    
    # Bedrooms & Bathrooms
    bedrooms = _extract_bedrooms(soup.get_text())
    bathrooms = _extract_bathrooms(soup.get_text())
    
    # Listing type
    listing_type = "Sale"
    text_lower = (url + " " + soup.get_text()[:2000]).lower()
    if "for rent" in text_lower or "for-rent" in url.lower():
        listing_type = "Rent"
    
    # Image: prefer og:image over logo
    image_url = ""
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        img_src = og_img["content"]
        if "logo" not in img_src.lower():
            image_url = img_src
    if not image_url:
        for img in soup.find_all("img"):
            src = img.get("src", "") or img.get("data-src", "")
            alt = img.get("alt", "").lower()
            if src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]):
                if not any(s in src.lower() + alt for s in ["logo", "icon", "avatar", "gravatar", "placeholder"]):
                    image_url = src
                    break
    if image_url and not image_url.startswith("http"):
        from urllib.parse import urljoin
        image_url = urljoin(url, image_url)
    
    # Description: from meta description (cleaner than page text)
    desc = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        desc = re.sub(r"<[^>]+>", "", meta["content"]).strip()[:800]
    if not desc:
        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            desc = re.sub(r"<[^>]+>", "", og_desc["content"]).strip()[:800]
    
    # Phone
    owner_phone = _extract_phone(soup)
    
    listing = {
        "title": title, "price": price, "location": location, "size": size,
        "bedrooms": bedrooms, "bathrooms": bathrooms,
        "listing_type": listing_type, "image_url": image_url,
        "description": desc, "source_url": url, "owner_phone": owner_phone,
    }
    return listing if title else None


def _extract_ethiopianproperties(soup, url) -> Optional[Dict]:
    """Extract listing data from ethiopianproperties.com.
    
    Site structure:
      - h1: title
      - div.price-and-type: "$2,400 Per Sq M- Apartment, Residential"
      - span.property-meta-size: "104 Sq M"
      - span.property-meta-bedrooms: "1 Bedroom"
      - span.property-meta-bath: "1 Bathroom"
      - a[href^="tel:"]: "+251-911-088-114"
      - meta og:image: property photo
    """
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    
    # Price from .price-and-type
    price = ""
    price_div = soup.find("div", class_="price-and-type")
    if price_div:
        price_text = price_div.get_text(strip=True)
        # Extract just the price part (before the dash/type)
        price_m = re.match(r"([\$]?[\d,]+(?:\.\d+)?(?:\s*Per\s*\w+)?)", price_text)
        if price_m:
            price = price_m.group(1)
    if not price:
        # Try .price class
        price_tag = soup.find(["span", "div"], class_="price")
        if price_tag:
            price = price_tag.get_text(strip=True)
    
    # Size from .property-meta-size
    size = ""
    size_tag = soup.find(class_="property-meta-size")
    if size_tag:
        size = size_tag.get_text(strip=True)
    
    # Bedrooms
    bedrooms = ""
    bed_tag = soup.find(class_="property-meta-bedrooms")
    if bed_tag:
        bedrooms = bed_tag.get_text(strip=True)
    
    # Bathrooms
    bathrooms = ""
    bath_tag = soup.find(class_="property-meta-bath")
    if bath_tag:
        bathrooms = bath_tag.get_text(strip=True)
    
    # Location from title
    location = ""
    loc_m = re.search(r"(?:in|at)\s+([A-Za-z][A-Za-z\s]+?)(?:\s*[-,|]|\s*$)", title, re.I)
    if loc_m:
        location = loc_m.group(1).strip()
    # Also try address in footer
    if not location:
        addr_m = re.search(r"Address:\s*([^\n]{5,100})", soup.get_text(), re.I)
        if addr_m:
            location = addr_m.group(1).strip()
    
    # Type
    listing_type = "Rent" if "for rent" in (url + title).lower() else "Sale"
    
    # Image
    image_url = ""
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content") and "logo" not in og_img["content"].lower():
        image_url = og_img["content"]
    if not image_url:
        for img in soup.find_all("img"):
            src = img.get("src", "") or img.get("data-src", "")
            if src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]):
                if not any(s in src.lower() for s in ["logo", "icon", "avatar"]):
                    image_url = src
                    break
    if image_url and not image_url.startswith("http"):
        from urllib.parse import urljoin
        image_url = urljoin(url, image_url)
    
    # Description
    desc = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        desc = re.sub(r"<[^>]+>", "", meta["content"]).strip()[:800]
    
    # Phone
    owner_phone = _extract_phone(soup)
    
    listing = {
        "title": title, "price": price, "location": location, "size": size,
        "bedrooms": bedrooms, "bathrooms": bathrooms,
        "listing_type": listing_type, "image_url": image_url,
        "description": desc, "source_url": url, "owner_phone": owner_phone,
    }
    return listing if title else None


def _extract_betoch(soup, url) -> Optional[Dict]:
    """Extract listing data from betoch.et.
    
    Site structure:
      - h1: "2-Bed Apartment for Sale in Yerer, Addis Ababa | Hosea Real Estate"
      - div.box-price: "12.500.000,00Br"
      - a[href^="tel:"]: "+251911084039"
      - meta og:image: property photo
      - text: "Asking Price: 11.5 Million ETB", "100 m²"
    """
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
        # Remove site name suffix
        title = re.sub(r"\s*\|\s*betoch\.et\s*$", "", title, flags=re.I)
        title = re.sub(r"\s*\|\s*Hosea Real Estate\s*$", "", title, flags=re.I)
    
    # Price: from .box-price
    price = ""
    box_price = soup.find(class_="box-price")
    if box_price:
        price = box_price.get_text(strip=True)
        # Convert "12.500.000,00Br" to "12,500,000 Br"
        price = re.sub(r"\.", ",", price)  # dot→comma
        price = re.sub(r",(\d{2})$", r".\1", price)  # last comma→decimal
        price = re.sub(r"(\d)(Br)$", r"\1 \2", price)
    if not price:
        # Try "Asking Price:" in text
        ask_m = re.search(r"Asking Price:\s*([\d,.]+\s*(?:Million\s*)?(?:ETB|Birr|Br|USD|\$))", soup.get_text(), re.I)
        if ask_m:
            price = ask_m.group(1).strip()
    
    # Size
    size = ""
    size_m = re.search(r"(\d[\d,.]*)\s*(m²|sqm|sq\.?\s*m)", soup.get_text(), re.I)
    if size_m:
        size = size_m.group(0)
    
    # Bedrooms & Bathrooms from text
    bedrooms = _extract_bedrooms(soup.get_text())
    bathrooms = _extract_bathrooms(soup.get_text())
    
    # Location from title
    location = ""
    loc_m = re.search(r"(?:in|at)\s+([A-Za-z][A-Za-z\s]+?)(?:\s*,\s*|\s*\|)", title, re.I)
    if loc_m:
        location = loc_m.group(1).strip()
    # Add "Addis Ababa" or city if in title
    city_m = re.search(r"(Addis Ababa|Dire Dawa|Bahir Dar|Hawassa|Adama|Mekelle)", title, re.I)
    if city_m:
        city = city_m.group(0)
        if city.lower() not in location.lower():
            location = f"{location}, {city}" if location else city
    
    # Type
    listing_type = "Rent" if "for rent" in (url + title).lower() else "Sale"
    
    # Image
    image_url = ""
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content") and "logo" not in og_img["content"].lower():
        image_url = og_img["content"]
    if not image_url:
        for img in soup.find_all("img"):
            src = img.get("src", "") or img.get("data-src", "")
            if src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]):
                if not any(s in src.lower() for s in ["logo", "icon", "avatar"]):
                    image_url = src
                    break
    if image_url and not image_url.startswith("http"):
        from urllib.parse import urljoin
        image_url = urljoin(url, image_url)
    
    # Description
    desc = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        desc = re.sub(r"<[^>]+>", "", meta["content"]).strip()[:800]
    
    # Phone
    owner_phone = _extract_phone(soup)
    
    listing = {
        "title": title, "price": price, "location": location, "size": size,
        "bedrooms": bedrooms, "bathrooms": bathrooms,
        "listing_type": listing_type, "image_url": image_url,
        "description": desc, "source_url": url, "owner_phone": owner_phone,
    }
    return listing if title else None


def _extract_livingethio(soup, url) -> Optional[Dict]:
    """Extract listing data from livingethio.com property-details pages.
    
    Site structure (React-rendered, but HTML has data):
      - h1: "Elegant 7-Bedroom House for Sale in Ayat, Addis Ababa"
      - Text contains: "Br. 120,000,000", "For Sale", "Ayat, Addis Ababa"
      - Overview section: Property Type, Bedrooms, Bathrooms, Garage
      - Description section: detailed text
      - img: actual property photos (not logo)
      - Phone in text: +251947002233
    """
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    
    # Price: look for "Br. XXX,XXX,XXX" pattern in the main content
    price = ""
    text = soup.get_text(separator="\n", strip=True)
    lines = [l for l in text.split("\n") if l.strip()]
    
    # Find "Br. NUMBER" pattern (this is how LivingEthio shows prices)
    price_m = re.search(r"Br\.\s*([\d,]+(?:\.\d+)?)", text)
    if price_m:
        price = f"Br. {price_m.group(1)}"
    if not price:
        price_m = re.search(r"([\d,]+(?:\.\d+)?)\s*(ETB|Birr|USD|\$)", text)
        if price_m:
            price = price_m.group(0)
    
    # Location: from title "in AREA, Addis Ababa"
    location = ""
    loc_m = re.search(r"(?:in|at)\s+([A-Za-z][A-Za-z\s]+?),\s*(Addis Ababa|Ethiopia)", title, re.I)
    if loc_m:
        location = f"{loc_m.group(1).strip()}, {loc_m.group(2).strip()}"
    if not location:
        # Look for "Ayat, Addis Ababa" pattern in text
        loc_m = re.search(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),\s*(Addis Ababa)", text)
        if loc_m:
            location = loc_m.group(0)
    
    # Bedrooms, Bathrooms from Overview section
    bedrooms = ""
    bathrooms = ""
    for i, line in enumerate(lines):
        if line.strip().isdigit() and i + 1 < len(lines):
            next_line = lines[i + 1].strip().lower()
            if "bedroom" in next_line:
                bedrooms = f"{line} {next_line}"
            elif "bathroom" in next_line:
                bathrooms = f"{line} {next_line}"
    
    # Size
    size = ""
    size_m = re.search(r"(\d[\d,]*\.?\d*)\s*(m²|sqm|sq\.?\s*m)", text, re.I)
    if size_m:
        size = size_m.group(0)
    
    # Property type
    listing_type = "Rent" if "for rent" in (url + title).lower() else "Sale"
    
    # Image: find actual property image (not the logo.webp)
    image_url = ""
    for img in soup.find_all("img"):
        src = img.get("src", "") or img.get("data-src", "")
        alt = img.get("alt", "").lower()
        if src and "logo" not in src.lower() and "le-logo" not in src.lower():
            if src.startswith("http") and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]):
                if title.lower()[:20] in alt.lower()[:20] or "property" in alt:
                    image_url = src
                    break
    # Fallback: any non-logo image with matching alt
    if not image_url:
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src and "cloudfront.net" in src and "le-logo" not in src and "logo" not in src:
                image_url = src
                break
    
    # Description: find the actual description paragraph (not nav/filters)
    desc = ""
    # Look for the paragraph that starts after "Description" heading
    desc_started = False
    desc_parts = []
    for line in lines:
        if line.strip() == "Description":
            desc_started = True
            continue
        if desc_started:
            if line.strip() in ["Address", "Area", "Overview", "Features", "Continue Reading", "Save", "Share"]:
                break
            if len(line.strip()) > 30:  # Skip short labels
                desc_parts.append(line.strip())
    if desc_parts:
        desc = " ".join(desc_parts)[:800]
    if not desc:
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            desc = re.sub(r"<[^>]+>", "", meta["content"]).strip()[:800]
    
    # Phone
    owner_phone = _extract_phone(soup)
    
    listing = {
        "title": title, "price": price, "location": location, "size": size,
        "bedrooms": bedrooms, "bathrooms": bathrooms,
        "listing_type": listing_type, "image_url": image_url,
        "description": desc, "source_url": url, "owner_phone": owner_phone,
    }
    return listing if title else None


def _extract_shegahome(soup, url) -> Optional[Dict]:
    """Extract listing data from shegahome.com property pages."""
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"]
    
    # Price
    price = ""
    for tag in soup.find_all(["span", "div", "p"], class_=re.compile(r"price", re.I)):
        p = tag.get_text(strip=True)
        if re.search(r"\d", p) and len(p) < 50:
            price = p
            break
    if not price:
        m = re.search(r"([\d,]+(?:\.\d+)?)\s*(ETB|Birr|USD|\$|Br)", soup.get_text(), re.I)
        if m:
            price = m.group(0)
    
    # Location from title
    location = ""
    loc_m = re.search(r"(?:in|at)\s+([A-Za-z][A-Za-z\s]+?)(?:\s*[-,|]|\s*$)", title, re.I)
    if loc_m:
        location = loc_m.group(1).strip()
    
    # Size, Bedrooms, Bathrooms
    size = ""
    size_m = re.search(r"(\d[\d,]*\.?\d*)\s*(m²|sqm|sq\.?\s*m)", soup.get_text(), re.I)
    if size_m:
        size = size_m.group(0)
    bedrooms = _extract_bedrooms(soup.get_text())
    bathrooms = _extract_bathrooms(soup.get_text())
    
    listing_type = "Rent" if "for rent" in (url + title).lower() else "Sale"
    
    # Image
    image_url = ""
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content") and "logo" not in og_img["content"].lower():
        image_url = og_img["content"]
    if not image_url:
        for img in soup.find_all("img"):
            src = img.get("src", "") or img.get("data-src", "")
            if src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]):
                if not any(s in src.lower() for s in ["logo", "icon", "avatar"]):
                    image_url = src
                    break
    if image_url and not image_url.startswith("http"):
        from urllib.parse import urljoin
        image_url = urljoin(url, image_url)
    
    desc = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        desc = re.sub(r"<[^>]+>", "", meta["content"]).strip()[:800]
    
    owner_phone = _extract_phone(soup)
    
    listing = {
        "title": title, "price": price, "location": location, "size": size,
        "bedrooms": bedrooms, "bathrooms": bathrooms,
        "listing_type": listing_type, "image_url": image_url,
        "description": desc, "source_url": url, "owner_phone": owner_phone,
    }
    return listing if title else None


def _extract_zegebeya(soup, url) -> Optional[Dict]:
    """Extract listing data from zegebeya.com property pages."""
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"]
    
    # Price
    price = ""
    for tag in soup.find_all(["span", "div", "p"], class_=re.compile(r"price", re.I)):
        p = tag.get_text(strip=True)
        if re.search(r"\d", p) and len(p) < 50:
            price = p
            break
    if not price:
        m = re.search(r"([\d,]+(?:\.\d+)?)\s*(ETB|Birr|USD|\$|Br)", soup.get_text(), re.I)
        if m:
            price = m.group(0)
    
    # Location from title
    location = ""
    loc_m = re.search(r"(?:in|at)\s+([A-Za-z][A-Za-z\s]+?)(?:\s*[-,|]|\s*$)", title, re.I)
    if loc_m:
        location = loc_m.group(1).strip()
    
    size = ""
    size_m = re.search(r"(\d[\d,]*\.?\d*)\s*(m²|sqm|sq\.?\s*m)", soup.get_text(), re.I)
    if size_m:
        size = size_m.group(0)
    bedrooms = _extract_bedrooms(soup.get_text())
    bathrooms = _extract_bathrooms(soup.get_text())
    
    listing_type = "Rent" if "for rent" in (url + title).lower() else "Sale"
    
    image_url = ""
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content") and "logo" not in og_img["content"].lower():
        image_url = og_img["content"]
    if not image_url:
        for img in soup.find_all("img"):
            src = img.get("src", "") or img.get("data-src", "")
            if src and any(e in src.lower() for e in [".jpg", ".jpeg", ".png", ".webp"]):
                if not any(s in src.lower() for s in ["logo", "icon", "avatar"]):
                    image_url = src
                    break
    if image_url and not image_url.startswith("http"):
        from urllib.parse import urljoin
        image_url = urljoin(url, image_url)
    
    desc = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        desc = re.sub(r"<[^>]+>", "", meta["content"]).strip()[:800]
    
    owner_phone = _extract_phone(soup)
    
    listing = {
        "title": title, "price": price, "location": location, "size": size,
        "bedrooms": bedrooms, "bathrooms": bathrooms,
        "listing_type": listing_type, "image_url": image_url,
        "description": desc, "source_url": url, "owner_phone": owner_phone,
    }
    return listing if title else None


# ══════════════════════════════════════════════════════════════════
# SCRAPER IMPLEMENTATIONS (verified working sites as of 2026)
# ══════════════════════════════════════════════════════════════════
class RealEthioScraper(BaseScraper):
    """Scraper for realethio.com — houses, apartments, villas in Addis Ababa."""

    def __init__(self):
        super().__init__()
        self.source_name = "RealEthio"
        self.base_url = "https://realethio.com"
        self._categories = [
            "/property-type/house-for-sale/",
            "/property-type/house-for-rent/",
            "/property-type/apartment-for-sale/",
            "/property-type/apartment-for-rent/",
        ]

    def get_listing_urls(self) -> List[str]:
        urls = set()
        page = random.choice(self._categories)
        resp = self._fetch_page(self.base_url + page)
        if not resp:
            resp = self._fetch_page(self.base_url)
        if resp:
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                if "/property/" in a["href"] and "/property-type/" not in a["href"]:
                    url = a["href"] if a["href"].startswith("http") else self.base_url + a["href"]
                    urls.add(url)
        return list(urls)

    def scrape_listing(self, url: str) -> Optional[Dict]:
        resp = self._fetch_page(url)
        if not resp:
            return None
        return _extract_realethio(BeautifulSoup(resp.text, "lxml"), url)


class ZeGebeyaScraper(BaseScraper):
    """Scraper for zegebeya.com — Ethiopia's free real estate marketplace."""

    def __init__(self):
        super().__init__()
        self.source_name = "ZeGebeya"
        self.base_url = "https://zegebeya.com"
        self._categories = [
            "/house-for-rent/",
            "/for-rent/",
            "/for-sale/",
        ]

    def get_listing_urls(self) -> List[str]:
        urls = set()
        page = random.choice(self._categories)
        resp = self._fetch_page(self.base_url + page)
        if not resp:
            resp = self._fetch_page(self.base_url)
        if resp:
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                if "/property/" in a["href"]:
                    url = a["href"] if a["href"].startswith("http") else self.base_url + a["href"]
                    urls.add(url)
        return list(urls)

    def scrape_listing(self, url: str) -> Optional[Dict]:
        resp = self._fetch_page(url)
        if not resp:
            return None
        return _extract_zegebeya(BeautifulSoup(resp.text, "lxml"), url)


class EthiopianPropertiesScraper(BaseScraper):
    """Scraper for ethiopianproperties.com — offices, houses, apartments."""

    def __init__(self):
        super().__init__()
        self.source_name = "EthiopianProperties"
        self.base_url = "https://www.ethiopianproperties.com"
        self._categories = [
            "/property-type/residential/?property-status=for-sale",
            "/property-type/residential/?property-status=for-rent",
            "/property-type/commercial/?property-status=for-sale",
        ]

    def get_listing_urls(self) -> List[str]:
        urls = set()
        page = random.choice(self._categories)
        resp = self._fetch_page(self.base_url + page)
        if not resp:
            resp = self._fetch_page(self.base_url)
        if resp:
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                if "/property/" in a["href"] and "/property-type/" not in a["href"]:
                    url = a["href"] if a["href"].startswith("http") else self.base_url + a["href"]
                    urls.add(url)
        return list(urls)

    def scrape_listing(self, url: str) -> Optional[Dict]:
        resp = self._fetch_page(url)
        if not resp:
            return None
        return _extract_ethiopianproperties(BeautifulSoup(resp.text, "lxml"), url)


class ShegaHomeScraper(BaseScraper):
    """Scraper for shegahome.com — Ethiopian real estate listings."""

    def __init__(self):
        super().__init__()
        self.source_name = "ShegaHome"
        self.base_url = "https://shegahome.com"

    def get_listing_urls(self) -> List[str]:
        urls = set()
        resp = self._fetch_page(self.base_url)
        if resp:
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/properties/" in href and "/properties/city/" not in href:
                    url = href if href.startswith("http") else self.base_url + href
                    urls.add(url)
        return list(urls)

    def scrape_listing(self, url: str) -> Optional[Dict]:
        resp = self._fetch_page(url)
        if not resp:
            return None
        return _extract_shegahome(BeautifulSoup(resp.text, "lxml"), url)


class BetochScraper(BaseScraper):
    """Scraper for betoch.et — houses and apartments for sale/rent."""

    def __init__(self):
        super().__init__()
        self.source_name = "Betoch"
        self.base_url = "https://betoch.et"

    def get_listing_urls(self) -> List[str]:
        urls = set()
        resp = self._fetch_page(self.base_url)
        if resp:
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                if "/properties/" in a["href"]:
                    url = a["href"] if a["href"].startswith("http") else self.base_url + a["href"]
                    urls.add(url)
        return list(urls)

    def scrape_listing(self, url: str) -> Optional[Dict]:
        resp = self._fetch_page(url)
        if not resp:
            return None
        return _extract_betoch(BeautifulSoup(resp.text, "lxml"), url)


class LivingEthioScraper(BaseScraper):
    """Scraper for livingethio.com — verified listings in Addis Ababa.
    
    LivingEthio is a JavaScript-rendered React site. Individual property
    pages use /site/property-details/ID. The homepage contains
    property-detail IDs embedded in the source code.
    """

    def __init__(self):
        super().__init__()
        self.source_name = "LivingEthio"
        self.base_url = "https://livingethio.com"

    def get_listing_urls(self) -> List[str]:
        urls = set()
        resp = self._fetch_page(self.base_url)
        if resp:
            ids = re.findall(r"property-details/(\d+)", resp.text)
            for pid in ids[:8]:
                urls.add(f"{self.base_url}/site/property-details/{pid}")
        if len(urls) < 3:
            categories = [
                "/site/property/house-for-rent",
                "/site/property/apartment-for-rent",
                "/site/property/apartment-for-sale",
            ]
            page = random.choice(categories)
            resp2 = self._fetch_page(self.base_url + page)
            if resp2:
                ids2 = re.findall(r"property-details/(\d+)", resp2.text)
                for pid in ids2[:8]:
                    urls.add(f"{self.base_url}/site/property-details/{pid}")
        return list(urls)

    def scrape_listing(self, url: str) -> Optional[Dict]:
        resp = self._fetch_page(url)
        if not resp:
            return None
        return _extract_livingethio(BeautifulSoup(resp.text, "lxml"), url)


# ── All scrapers in rotation ──
ALL_SCRAPERS = [
    RealEthioScraper,
    ZeGebeyaScraper,
    EthiopianPropertiesScraper,
    ShegaHomeScraper,
    BetochScraper,
    LivingEthioScraper,
]

SCRAPER_MAP = {s.__name__: s for s in ALL_SCRAPERS}
SCRAPER_NAMES = list(SCRAPER_MAP.keys())


# ══════════════════════════════════════════════════════════════════
# TELEGRAM POSTER
# ══════════════════════════════════════════════════════════════════
class TelegramPoster:
    """Posts property listings to the Telegram channel with images and formatting."""

    def _get_contact(self, listing: dict) -> str:
        """Return the real owner phone if found, otherwise fallback to default."""
        owner_phone = listing.get("owner_phone", "")
        if owner_phone and len(re.sub(r"[^0-9]", "", owner_phone)) >= 9:
            return owner_phone
        return CONTACT_PHONE

    def _format_message(self, listing: dict) -> str:
        """Format a full Telegram message with Markdown formatting."""
        title = listing.get("title", "Property in Ethiopia")
        price = listing.get("price", "Contact for price")
        location = listing.get("location", "Ethiopia")
        size = listing.get("size", "")
        bedrooms = listing.get("bedrooms", "")
        bathrooms = listing.get("bathrooms", "")
        listing_type = listing.get("listing_type", "Sale")
        desc = listing.get("description", "")
        source_url = listing.get("source_url", "")
        contact = self._get_contact(listing)
        source_name = listing.get("source", "")
        type_label = "FOR RENT" if listing_type.lower() == "rent" else "FOR SALE"

        parts = [
            f"\U0001F3E0 *{title}*", "",
            f"\U0001F513 *{type_label}*",
            f"\U0001F4B5 *Price:* {price}",
            f"\U0001F4CD *Location:* {location}",
        ]
        if size:
            parts.append(f"\U0001F4D0 *Size:* {size}")
        if bedrooms:
            parts.append(f"\U0001F6CF *Bedrooms:* {bedrooms}")
        if bathrooms:
            parts.append(f"\U0001F6BF *Bathrooms:* {bathrooms}")
        if source_name:
            parts.append(f"\U0001F310 *Source:* {source_name}")
        parts.extend([
            "", "\U0001F4DD *Description:*",
            f"{desc[:700]}{'...' if len(desc) > 700 else ''}" if desc else "No description available.",
            "", f"\U0001F4DE *Contact:* {contact}",
        ])
        if source_url:
            parts.append(f"\U0001F517 *View Original:* [Click Here]({source_url})")
        tags = [
            "#EthiopiaRealEstate", "#EthiopiaProperty", "#AddisAbaba",
            "#ForRent" if listing_type.lower() == "rent" else "#ForSale",
        ]
        parts.extend(["", " ".join(tags)])
        return "\n".join(parts)

    def _format_short(self, listing: dict) -> str:
        """Plain-text fallback if Markdown parsing fails."""
        title = listing.get("title", "Property in Ethiopia")
        price = listing.get("price", "Contact for price")
        location = listing.get("location", "Ethiopia")
        listing_type = listing.get("listing_type", "Sale")
        source_url = listing.get("source_url", "")
        contact = self._get_contact(listing)
        type_label = "FOR RENT" if listing_type.lower() == "rent" else "FOR SALE"

        msg = (f"\U0001F3E0 {title}\n\n{type_label}\n"
               f"\U0001F4B5 Price: {price}\n\U0001F4CD Location: {location}\n"
               f"\U0001F4DE Contact: {contact}")
        if source_url:
            msg += f"\n\U0001F517 Source: {source_url}"
        msg += "\n\n#EthiopiaRealEstate"
        return msg

    def _download_image(self, url: str) -> Optional[bytes]:
        """Download an image from URL. Returns bytes or None."""
        if not url:
            return None
        try:
            r = requests.get(url, timeout=10, stream=True,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            data = r.content
            if 1024 <= len(data) <= 10 * 1024 * 1024:  # 1KB - 10MB
                return data
        except Exception:
            pass
        return None

    def post_listing(self, listing: dict) -> Optional[int]:
        """Post a listing to the Telegram channel. Returns message_id or None."""
        text = self._format_message(listing)
        short_text = self._format_short(listing)
        image_url = listing.get("image_url", "")
        image_bytes = self._download_image(image_url) if image_url else None

        # Strategy 1: Image + Markdown caption
        if image_bytes:
            try:
                r = requests.post(f"{TELEGRAM_API}/sendPhoto", data={
                    "chat_id": TELEGRAM_CHANNEL,
                    "caption": text,
                    "parse_mode": "Markdown",
                }, files={"photo": ("photo.jpg", image_bytes)}, timeout=20)
                if r.status_code == 200:
                    return r.json().get("result", {}).get("message_id")
            except Exception:
                pass

            # Strategy 2: Image + plain caption
            try:
                r = requests.post(f"{TELEGRAM_API}/sendPhoto", data={
                    "chat_id": TELEGRAM_CHANNEL,
                    "caption": short_text,
                }, files={"photo": ("photo.jpg", image_bytes)}, timeout=20)
                if r.status_code == 200:
                    return r.json().get("result", {}).get("message_id")
            except Exception:
                pass

        # Strategy 3: Text-only + Markdown
        try:
            r = requests.post(f"{TELEGRAM_API}/sendMessage", data={
                "chat_id": TELEGRAM_CHANNEL,
                "text": text,
                "parse_mode": "Markdown",
            }, timeout=20)
            if r.status_code == 200:
                return r.json().get("result", {}).get("message_id")
        except Exception:
            pass

        # Strategy 4: Text-only + plain
        try:
            r = requests.post(f"{TELEGRAM_API}/sendMessage", data={
                "chat_id": TELEGRAM_CHANNEL,
                "text": short_text,
            }, timeout=20)
            if r.status_code == 200:
                return r.json().get("result", {}).get("message_id")
        except Exception:
            pass

        return None


# ══════════════════════════════════════════════════════════════════
# CORE: ROTATE (the main logic that runs per cron call)
# ══════════════════════════════════════════════════════════════════
def run_rotate() -> dict:
    """Run ONE scraper, find ONE new listing, post it.

    Designed to complete within Vercel's 10-second timeout.
    Each call:
      1. Loads state from GitHub Gist
      2. Checks daily post limit (5 max)
      3. Runs the next scraper in rotation (skipping today's already-used sources)
      4. Finds ONE new listing not in posted_hashes
      5. Posts to Telegram
      6. Saves state back to Gist

    Returns a status dict describing what happened.
    """
    # Check required env vars
    missing = _check_config()
    if missing:
        return {
            "status": "config_missing",
            "error": f"Missing environment variables: {', '.join(missing)}",
            "hint": "Set them in Vercel Dashboard > Settings > Environment Variables",
        }

    start_time = time.time()
    gist = GistState()
    state = gist.load()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Reset daily counter if it's a new day
    if state.get("date") != today:
        logger.info(f"New day detected: {state.get('date')} → {today}. Resetting counters.")
        state["date"] = today
        state["posts_today"] = 0
        state["today_sources"] = []

    # Check daily limit
    posts_today = state.get("posts_today", 0)
    if posts_today >= MAX_DAILY_POSTS:
        return {
            "status": "daily_limit_reached",
            "posts_today": posts_today,
            "max_daily": MAX_DAILY_POSTS,
            "date": today,
            "elapsed": round(time.time() - start_time, 2),
        }

    # Build posted hashes set for dedup
    posted_hashes = set(state.get("posted_hashes", []))
    today_sources = state.get("today_sources", [])

    # Try scrapers in rotation, skipping already-used sources today
    poster = TelegramPoster()
    idx = state.get("next_scraper_idx", 0)

    result = {
        "status": "no_new_listings",
        "posts_today": posts_today,
        "max_daily": MAX_DAILY_POSTS,
        "date": today,
        "tried_scrapers": [],
    }

    for attempt in range(len(ALL_SCRAPERS)):
        # Timeout guard: leave 2 seconds for Gist save
        if time.time() - start_time > 8:
            logger.warning("Approaching timeout, stopping rotation")
            result["timeout_guard"] = True
            break

        scraper_class = ALL_SCRAPERS[idx % len(ALL_SCRAPERS)]
        scraper_name = scraper_class.__name__
        result["tried_scrapers"].append(scraper_name)

        # Skip if this source was already used today (different-site requirement)
        if scraper_name in today_sources:
            logger.info(f"Skipping {scraper_name}: already used today")
            idx += 1
            continue

        logger.info(f"Running scraper: {scraper_name}")
        scraper = scraper_class()
        listing = scraper.scrape_one_new(posted_hashes)

        if listing:
            # Found a new listing - post it!
            msg_id = poster.post_listing(listing)
            if msg_id:
                state["posts_today"] = posts_today + 1
                state["posted_hashes"] = list(posted_hashes) + [listing["url_hash"]]
                state["today_sources"] = today_sources + [scraper_name]
                state["next_scraper_idx"] = (idx + 1) % len(ALL_SCRAPERS)
                gist.save(state)

                return {
                    "status": "posted",
                    "scraper": scraper_name,
                    "title": listing.get("title", ""),
                    "price": listing.get("price", ""),
                    "location": listing.get("location", ""),
                    "contact": poster._get_contact(listing),
                    "posts_today": posts_today + 1,
                    "max_daily": MAX_DAILY_POSTS,
                    "date": today,
                    "elapsed": round(time.time() - start_time, 2),
                }
            else:
                logger.warning(f"Failed to post listing from {scraper_name}")

        # Move to next scraper
        idx += 1

    # No new listings found from any scraper
    state["next_scraper_idx"] = idx % len(ALL_SCRAPERS)
    gist.save(state)
    result["elapsed"] = round(time.time() - start_time, 2)
    return result


# ══════════════════════════════════════════════════════════════════
# FLASK ROUTES (Vercel Python entry points)
# ══════════════════════════════════════════════════════════════════
@app.route("/", methods=["GET"])
def home():
    """Landing page for the bot."""
    return """
    <html>
    <head><title>Ethio House Realtor Bot</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 750px;
               margin: 50px auto; padding: 20px; background: #f8f9fa; color: #333; }
        h1 { color: #2c3e50; }
        .card { background: white; border-radius: 12px; padding: 24px; margin: 16px 0;
                box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
        .endpoint { padding: 10px 16px; margin: 6px 0; background: #e8f4f8; border-radius: 6px;
                    font-family: monospace; font-size: 14px; }
        .endpoint a { color: #2980b9; text-decoration: none; }
        .endpoint a:hover { text-decoration: underline; }
        .badge { display: inline-block; padding: 3px 10px; border-radius: 12px;
                 font-size: 12px; font-weight: 600; }
        .badge-green { background: #d4edda; color: #155724; }
        .badge-blue { background: #cce5ff; color: #004085; }
        .limit { font-size: 28px; font-weight: 700; color: #2980b9; }
    </style></head>
    <body>
        <div class="card">
            <h1>\U0001F3E0 Ethio House Realtor Bot</h1>
            <p>Automatically scrapes Ethiopian real estate listings and posts them to Telegram.</p>
            <p><span class="badge badge-green">Running</span>
               <span class="badge badge-blue">5 Posts/Day</span></p>
        </div>
        <div class="card">
            <h3>Bot Info</h3>
            <ul>
                <li><b>Channel:</b> <a href="https://t.me/Ethio_House_Realtor">@Ethio_House_Realtor</a></li>
                <li><b>Daily Limit:</b> <span class="limit">5</span> new listings/day</li>
                <li><b>Contact:</b> 0949024661</li>
                <li><b>Sources:</b> 6 Ethiopian real estate sites</li>
                <li><b>Rule:</b> Each post from a DIFFERENT site, no duplicates ever</li>
            </ul>
        </div>
        <div class="card">
            <h3>API Endpoints</h3>
            <div class="endpoint"><a href="/api/status">/api/status</a> — Bot status & daily count</div>
            <div class="endpoint"><a href="/api/rotate">/api/rotate</a> — Run next scraper (cron target)</div>
            <div class="endpoint"><a href="/api/init-gist">/api/init-gist</a> — Initialize state storage</div>
            <div class="endpoint"><a href="/api/scrape?source=EstateEthiopia">/api/scrape?source=X</a> — Manual scrape</div>
            <div class="endpoint"><a href="/api/reset-daily">/api/reset-daily</a> — Reset daily counter</div>
        </div>
        <div class="card">
            <h3>How It Works</h3>
            <ol>
                <li>External cron (cron-job.org) hits <code>/api/rotate</code> every 2-3 hours</li>
                <li>Each call runs ONE scraper, finds ONE new listing, posts it</li>
                <li>Max 5 posts/day, each from a different source site</li>
                <li>State stored in GitHub Gist (survives Vercel cold starts)</li>
                <li>Dedup via URL hashing — no listing ever posted twice</li>
            </ol>
        </div>
        <p style="margin-top:20px;color:#aaa;text-align:center">Powered by Vercel Serverless</p>
    </body>
    </html>
    """


@app.route("/api/rotate", methods=["GET", "POST"])
def api_rotate():
    """Run the next scraper in rotation. External cron target.

    This is the main endpoint. Call it every 2-3 hours via cron-job.org.
    Each call: loads state → checks daily limit → runs one scraper →
    posts one new listing → saves state. Completes in 5-8 seconds.
    """
    try:
        result = run_rotate()
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Rotate error: {e}")
        return jsonify({"error": str(e), "status": "error"}), 500


@app.route("/api/scrape", methods=["GET", "POST"])
def api_scrape():
    """Manual scrape: run one specific scraper by name.

    Query params: ?source=ScraperName (e.g. EstateEthiopia, RealtorsEthiopia)
    """
    source = request.args.get("source", "")
    if not source or source not in SCRAPER_MAP:
        return jsonify({
            "error": "Provide ?source=ScraperName",
            "available_scrapers": SCRAPER_NAMES,
        }), 400

    try:
        gist = GistState()
        state = gist.load()
        posted_hashes = set(state.get("posted_hashes", []))
        scraper = SCRAPER_MAP[source]()
        listing = scraper.scrape_one_new(posted_hashes)

        if listing:
            poster = TelegramPoster()
            msg_id = poster.post_listing(listing)
            if msg_id:
                state["posted_hashes"] = list(posted_hashes) + [listing["url_hash"]]
                today_sources = state.get("today_sources", [])
                if source not in today_sources:
                    today_sources.append(source)
                state["today_sources"] = today_sources
                state["posts_today"] = state.get("posts_today", 0) + 1
                gist.save(state)
                return jsonify({
                    "status": "posted",
                    "message_id": msg_id,
                    "listing": listing,
                }), 200
            return jsonify({"status": "post_failed", "listing": listing}), 500
        return jsonify({"status": "no_new_listing", "source": source}), 200
    except Exception as e:
        logger.error(f"Scrape error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def api_status():
    """Bot status with current daily count and state info."""
    missing = _check_config()
    config_status = "ready" if not missing else f"missing: {', '.join(missing)}"

    try:
        gist = GistState()
        state = gist.load()
        return jsonify({
            "bot": "Ethio House Realtor Bot",
            "channel": "t.me/Ethio_House_Realtor",
            "config": config_status,
            "status": "running" if not missing else "needs_config",
            "max_daily_posts": MAX_DAILY_POSTS,
            "posts_today": state.get("posts_today", 0),
            "date": state.get("date", ""),
            "today_sources": state.get("today_sources", []),
            "total_posted_hashes": len(state.get("posted_hashes", [])),
            "scrapers": SCRAPER_NAMES,
            "next_scraper_idx": state.get("next_scraper_idx", 0),
            "gist_id": state.get("gist_id", ""),
            "last_updated": state.get("last_updated", ""),
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/init-gist", methods=["GET", "POST"])
def api_init_gist():
    """Initialize the GitHub Gist for state storage.

    Call this once after deploying. It creates the Gist if it doesn't exist
    and returns the current state.
    """
    try:
        gist = GistState()
        state = gist.load()
        return jsonify({
            "status": "ok",
            "gist_id": state.get("gist_id", ""),
            "state": state,
            "message": "Gist initialized successfully. State storage is ready.",
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset-daily", methods=["GET", "POST"])
def api_reset_daily():
    """Reset the daily post counter. Useful for testing."""
    try:
        gist = GistState()
        state = gist.load()
        state["posts_today"] = 0
        state["today_sources"] = []
        state["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        gist.save(state)
        return jsonify({
            "status": "daily_counter_reset",
            "posts_today": 0,
            "date": state["date"],
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Vercel Python runtime expects the Flask app
if __name__ == "__main__":
    app.run(debug=True)
