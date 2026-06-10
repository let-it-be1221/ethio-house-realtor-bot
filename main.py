"""Main orchestrator for the Ethio House Realtor Telegram Bot.

This module coordinates scraping, deduplication, and posting to Telegram.
It runs on a schedule, scraping Ethiopian real estate websites and posting
new listings to the Telegram channel.
"""

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime
from typing import List

from config import SCRAPE_INTERVAL_MINUTES, CONTACT_PHONE
from database import Database
from telegram_bot import TelegramPoster
from scrapers import ALL_SCRAPERS

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("ethio_realtor_bot.log"),
    ],
)
logger = logging.getLogger(__name__)


class EthioRealtorBot:
    """Main bot orchestrator that manages scraping and posting."""

    def __init__(self):
        self.db = Database()
        self.poster = TelegramPoster()
        self.running = False
        self.scrapers = [ScraperClass() for ScraperClass in ALL_SCRAPERS]
        logger.info(f"Initialized bot with {len(self.scrapers)} scrapers")

    async def run_scrape_cycle(self):
        """Run one complete scrape cycle across all sources."""
        cycle_start = datetime.now()
        logger.info(f"=== Starting scrape cycle at {cycle_start.strftime('%Y-%m-%d %H:%M:%S')} ===")

        total_new = 0
        total_duplicate = 0
        total_failed = 0

        for scraper in self.scrapers:
            try:
                logger.info(f"Running scraper: {scraper.source_name}")
                listings = scraper.scrape_all()
                logger.info(f"[{scraper.source_name}] Found {len(listings)} listings")

                for listing in listings:
                    try:
                        # Check if already posted
                        if self.db.is_already_posted(listing):
                            total_duplicate += 1
                            logger.debug(f"Skipping duplicate: {listing.get('title', 'Unknown')[:50]}")
                            continue

                        # Post to Telegram
                        message_id = await self.poster.post_listing(listing)

                        if message_id:
                            # Mark as posted in database
                            self.db.mark_as_posted(listing, message_id)
                            total_new += 1
                            logger.info(f"New listing posted: {listing.get('title', 'Unknown')[:50]}")
                            # Rate limiting: delay between posts
                            await asyncio.sleep(3)
                        else:
                            total_failed += 1
                            logger.warning(f"Failed to post: {listing.get('title', 'Unknown')[:50]}")

                    except Exception as e:
                        total_failed += 1
                        logger.error(f"Error processing listing: {e}")
                        continue

                # Delay between scrapers
                await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"Error running scraper {scraper.source_name}: {e}")
                continue

        cycle_end = datetime.now()
        duration = (cycle_end - cycle_start).total_seconds()

        logger.info(
            f"=== Scrape cycle completed in {duration:.0f}s ===\n"
            f"  New listings posted: {total_new}\n"
            f"  Duplicates skipped: {total_duplicate}\n"
            f"  Failed to post: {total_failed}\n"
            f"  Total in database: {self.db.get_posted_count()}"
        )

        # If new listings were found, that's great; if not, remain silent
        if total_new == 0:
            logger.info("No new listings found this cycle. Bot remains silent as configured.")

    async def run(self):
        """Run the bot continuously with scheduled scraping."""
        self.running = True
        logger.info("Starting Ethio House Realtor Bot...")
        logger.info(f"Channel: t.me/Ethio_House_Realtor")
        logger.info(f"Contact: {CONTACT_PHONE}")
        logger.info(f"Scrape interval: every {SCRAPE_INTERVAL_MINUTES} minutes")

        # Test Telegram connection
        connected = await self.poster.test_connection()
        if not connected:
            logger.error("Failed to connect to Telegram. Please check your bot token and channel settings.")
            logger.error("Make sure the bot is added as an admin to the channel @Ethio_House_Realtor")
            return

        logger.info("Bot started successfully! Monitoring Ethiopian real estate sites...")

        # Run first scrape immediately
        try:
            await self.run_scrape_cycle()
        except Exception as e:
            logger.error(f"Error in initial scrape cycle: {e}")

        # Schedule subsequent scrapes
        interval_seconds = SCRAPE_INTERVAL_MINUTES * 60
        while self.running:
            try:
                logger.info(f"Next scrape cycle in {SCRAPE_INTERVAL_MINUTES} minutes...")
                # Sleep in small intervals to allow graceful shutdown
                for _ in range(interval_seconds):
                    if not self.running:
                        break
                    await asyncio.sleep(1)

                if self.running:
                    await self.run_scrape_cycle()

            except Exception as e:
                logger.error(f"Error in scrape cycle: {e}")
                # Wait a bit before retrying
                await asyncio.sleep(60)

    def stop(self):
        """Stop the bot gracefully."""
        self.running = False
        logger.info("Bot stopping...")


async def main():
    """Main entry point."""
    bot = EthioRealtorBot()

    # Set up signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        bot.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        bot.stop()
    finally:
        logger.info("Bot shut down complete")


if __name__ == "__main__":
    asyncio.run(main())
