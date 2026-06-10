"""Serverless-compatible deduplication using Telegram channel message history.

Since Vercel's filesystem is read-only (no SQLite), we check for duplicates
by looking at recently posted messages in the Telegram channel.
This approach is perfectly suited for serverless deployment.
"""

import logging
import hashlib
from typing import Set, Optional
import requests

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = "8679294538:AAEPeJP28dFG1sT7ovq1XuqoYil2Mn_k1Nk"
TELEGRAM_CHANNEL = "@Ethio_House_Realtor"


class ServerlessDB:
    """Deduplication using Telegram channel history + in-memory set per invocation."""

    def __init__(self):
        self._posted_hashes: Set[str] = set()
        self._loaded = False

    def _generate_hash(self, listing_data: dict) -> str:
        """Generate a unique hash for a listing."""
        hash_input = (
            f"{listing_data.get('source', '')}"
            f"{listing_data.get('title', '')}"
            f"{listing_data.get('price', '')}"
            f"{listing_data.get('location', '')}"
            f"{listing_data.get('source_url', '')}"
        )
        return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:32]

    def load_recent_posts(self):
        """Load recently posted listing hashes from Telegram channel history."""
        if self._loaded:
            return

        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChat"
            params = {"chat_id": TELEGRAM_CHANNEL}
            requests.get(url, params=params, timeout=10)

            # Get recent messages from channel
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"limit": 100, "timeout": 0}
            resp = requests.get(url, params=params, timeout=15)

            if resp.status_code == 200:
                data = resp.json()
                for update in data.get("result", []):
                    msg = update.get("message") or update.get("channel_post") or update.get("edited_channel_post")
                    if msg:
                        text = msg.get("text") or msg.get("caption") or ""
                        # Hash the message text as a dedup key
                        msg_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
                        self._posted_hashes.add(msg_hash)

                logger.info(f"Loaded {len(self._posted_hashes)} recent post hashes from Telegram")
        except Exception as e:
            logger.warning(f"Could not load Telegram history (non-critical): {e}")

        self._loaded = True

    def is_already_posted(self, listing_data: dict) -> bool:
        """Check if a listing has already been posted."""
        listing_hash = self._generate_hash(listing_data)
        return listing_hash in self._posted_hashes

    def mark_as_posted(self, listing_data: dict):
        """Mark a listing as posted (in-memory for this invocation)."""
        listing_hash = self._generate_hash(listing_data)
        self._posted_hashes.add(listing_hash)
        logger.info(f"Marked as posted: {listing_data.get('title', 'Unknown')[:50]}")

    def get_posted_count(self) -> int:
        """Get count of known posted listings in this invocation."""
        return len(self._posted_hashes)
