import sqlite3
import hashlib
import logging
from datetime import datetime
from config import DB_PATH

logger = logging.getLogger(__name__)


class Database:
    """SQLite database for tracking posted listings to avoid duplicates."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize the database schema."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listing_hash TEXT UNIQUE NOT NULL,
                    source TEXT NOT NULL,
                    title TEXT,
                    price TEXT,
                    location TEXT,
                    size TEXT,
                    listing_type TEXT,
                    description TEXT,
                    image_url TEXT,
                    source_url TEXT,
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    telegram_message_id INTEGER
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_listing_hash ON listings(listing_hash)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_source ON listings(source)
            """)
            conn.commit()
            logger.info("Database initialized successfully")

    def _generate_hash(self, listing_data: dict) -> str:
        """Generate a unique hash for a listing based on key fields."""
        hash_input = (
            f"{listing_data.get('source', '')}"
            f"{listing_data.get('title', '')}"
            f"{listing_data.get('price', '')}"
            f"{listing_data.get('location', '')}"
            f"{listing_data.get('source_url', '')}"
        )
        return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    def is_already_posted(self, listing_data: dict) -> bool:
        """Check if a listing has already been posted."""
        listing_hash = self._generate_hash(listing_data)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM listings WHERE listing_hash = ?",
                (listing_hash,)
            )
            return cursor.fetchone() is not None

    def mark_as_posted(self, listing_data: dict, telegram_message_id: int = None):
        """Mark a listing as posted in the database."""
        listing_hash = self._generate_hash(listing_data)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO listings (
                        listing_hash, source, title, price, location, size,
                        listing_type, description, image_url, source_url,
                        telegram_message_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    listing_hash,
                    listing_data.get("source", ""),
                    listing_data.get("title", ""),
                    listing_data.get("price", ""),
                    listing_data.get("location", ""),
                    listing_data.get("size", ""),
                    listing_data.get("listing_type", ""),
                    listing_data.get("description", ""),
                    listing_data.get("image_url", ""),
                    listing_data.get("source_url", ""),
                    telegram_message_id,
                ))
                conn.commit()
                logger.info(f"Marked listing as posted: {listing_data.get('title', 'Unknown')[:50]}")
            except sqlite3.IntegrityError:
                logger.debug(f"Listing already in database: {listing_hash[:16]}...")

    def get_posted_count(self, source: str = None) -> int:
        """Get the count of posted listings, optionally filtered by source."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            if source:
                cursor.execute("SELECT COUNT(*) FROM listings WHERE source = ?", (source,))
            else:
                cursor.execute("SELECT COUNT(*) FROM listings")
            return cursor.fetchone()[0]

    def get_recent_listings(self, limit: int = 10):
        """Get the most recently posted listings."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT title, price, location, source, posted_at
                FROM listings
                ORDER BY posted_at DESC
                LIMIT ?
            """, (limit,))
            return cursor.fetchall()

    def cleanup_old_entries(self, days: int = 90):
        """Remove entries older than specified days to keep database manageable."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM listings
                WHERE posted_at < datetime('now', ?)
            """, (f"-{days} days",))
            deleted = cursor.rowcount
            conn.commit()
            logger.info(f"Cleaned up {deleted} entries older than {days} days")
