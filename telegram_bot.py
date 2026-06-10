"""Telegram bot module for posting real estate listings to the channel."""

import logging
import io
import asyncio
from typing import Optional
import requests
from telegram import Bot, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import TelegramError, RetryAfter, TimedOut
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL, CONTACT_PHONE

logger = logging.getLogger(__name__)


class TelegramPoster:
    """Handles posting real estate listings to the Telegram channel."""

    def __init__(self):
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self.channel = TELEGRAM_CHANNEL

    def _format_listing_message(self, listing: dict) -> str:
        """Format a listing into a Telegram message with emojis and structure."""
        title = listing.get("title", "Property in Ethiopia")
        price = listing.get("price", "Price on request")
        location = listing.get("location", "Ethiopia")
        size = listing.get("size", "")
        listing_type = listing.get("listing_type", "Sale")
        description = listing.get("description", "")
        source_url = listing.get("source_url", "")

        # Determine emoji for listing type
        type_emoji = "\U0001F4B0" if listing_type.lower() == "sale" else "\U0001F513"  # money bag / open lock
        type_label = "FOR SALE" if listing_type.lower() == "sale" else "FOR RENT"

        # Build message
        message_parts = [
            f"\U0001F3E0 *{title}*",
            "",
            f"{type_emoji} *{type_label}*",
            f"\U0001F4B5 *Price:* {price}" if price else "\U0001F4B5 *Price:* Contact for price",
            f"\U0001F4CD *Location:* {location}" if location else "",
        ]

        if size:
            message_parts.append(f"\U0001F4D0 *Size:* {size}")

        message_parts.extend([
            "",
            "\U0001F4DD *Description:*",
            f"{description[:800]}{'...' if len(description) > 800 else ''}" if description else "No description available.",
            "",
            f"\U0001F4DE *Contact:* {CONTACT_PHONE}",
        ])

        if source_url:
            message_parts.append(f"\U0001F517 *Source:* [View Original]({source_url})")

        # Add hashtags
        hashtags = ["#EthiopiaRealEstate", "#EthiopiaProperty"]
        if listing_type.lower() == "sale":
            hashtags.append("#ForSale")
        elif listing_type.lower() == "rent":
            hashtags.append("#ForRent")
        if location:
            # Add location-based hashtag
            loc_words = location.split(",")
            if loc_words:
                loc_hashtag = loc_words[0].strip().replace(" ", "")
                if loc_hashtag:
                    hashtags.append(f"#{loc_hashtag}")
        message_parts.append("")
        message_parts.append(" ".join(hashtags))

        return "\n".join(part for part in message_parts if part is not None)

    async def post_listing(self, listing: dict) -> Optional[int]:
        """Post a single listing to the Telegram channel. Returns message_id on success."""
        message_text = self._format_listing_message(listing)
        image_url = listing.get("image_url", "")

        try:
            # Try to post with image first
            if image_url:
                image_bytes = self._download_image(image_url)
                if image_bytes:
                    try:
                        message = await self.bot.send_photo(
                            chat_id=self.channel,
                            photo=image_bytes,
                            caption=message_text,
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        logger.info(f"Posted listing with image: {listing.get('title', 'Unknown')[:50]}")
                        return message.message_id
                    except TelegramError as e:
                        if "can't parse" in str(e).lower() or "markdown" in str(e).lower():
                            # Retry without markdown
                            try:
                                message = await self.bot.send_photo(
                                    chat_id=self.channel,
                                    photo=image_bytes,
                                    caption=message_text,
                                )
                                logger.info(f"Posted listing with image (plain text): {listing.get('title', 'Unknown')[:50]}")
                                return message.message_id
                            except TelegramError:
                                pass
                        elif "too long" in str(e).lower():
                            # Shorten message and retry
                            short_text = self._format_short_message(listing)
                            try:
                                message = await self.bot.send_photo(
                                    chat_id=self.channel,
                                    photo=image_bytes,
                                    caption=short_text,
                                    parse_mode=ParseMode.MARKDOWN,
                                )
                                return message.message_id
                            except TelegramError:
                                pass
                        logger.warning(f"Failed to post with image, falling back to text: {e}")

            # Fallback: post without image
            try:
                message = await self.bot.send_message(
                    chat_id=self.channel,
                    text=message_text,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=False,
                )
                logger.info(f"Posted listing (text only): {listing.get('title', 'Unknown')[:50]}")
                return message.message_id
            except TelegramError as e:
                if "can't parse" in str(e).lower() or "markdown" in str(e).lower():
                    # Retry without markdown formatting
                    try:
                        message = await self.bot.send_message(
                            chat_id=self.channel,
                            text=message_text,
                            disable_web_page_preview=False,
                        )
                        logger.info(f"Posted listing (plain text): {listing.get('title', 'Unknown')[:50]}")
                        return message.message_id
                    except TelegramError as e2:
                        logger.error(f"Failed to post listing even with plain text: {e2}")
                        return None
                elif "too long" in str(e).lower():
                    short_text = self._format_short_message(listing)
                    try:
                        message = await self.bot.send_message(
                            chat_id=self.channel,
                            text=short_text,
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        return message.message_id
                    except TelegramError:
                        try:
                            message = await self.bot.send_message(
                                chat_id=self.channel,
                                text=short_text,
                            )
                            return message.message_id
                        except TelegramError as e3:
                            logger.error(f"Failed to post shortened listing: {e3}")
                            return None
                else:
                    logger.error(f"Failed to post listing: {e}")
                    return None

        except RetryAfter as e:
            logger.warning(f"Rate limited by Telegram. Retry after {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
            return await self.post_listing(listing)
        except TimedOut:
            logger.warning("Telegram request timed out, retrying...")
            await asyncio.sleep(5)
            return None
        except Exception as e:
            logger.error(f"Unexpected error posting listing: {e}")
            return None

    def _format_short_message(self, listing: dict) -> str:
        """Format a shorter version of the listing message."""
        title = listing.get("title", "Property in Ethiopia")
        price = listing.get("price", "Contact for price")
        location = listing.get("location", "Ethiopia")
        listing_type = listing.get("listing_type", "Sale")
        source_url = listing.get("source_url", "")

        type_label = "FOR SALE" if listing_type.lower() == "sale" else "FOR RENT"

        message = (
            f"\U0001F3E0 {title}\n\n"
            f"{type_label}\n"
            f"\U0001F4B5 Price: {price}\n"
            f"\U0001F4CD Location: {location}\n"
            f"\U0001F4DE Contact: {CONTACT_PHONE}"
        )
        if source_url:
            message += f"\n\U0001F517 Source: {source_url}"

        message += "\n\n#EthiopiaRealEstate"
        return message

    def _download_image(self, url: str) -> Optional[bytes]:
        """Download an image and return its bytes."""
        if not url:
            return None
        try:
            response = requests.get(url, timeout=20, stream=True, allow_redirects=True)
            response.raise_for_status()
            # Verify it's an image
            content_type = response.headers.get("Content-Type", "")
            if "image" in content_type or any(url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
                # Limit image size to 10MB (Telegram limit)
                image_bytes = response.content
                if len(image_bytes) <= 10 * 1024 * 1024:
                    return image_bytes
                else:
                    logger.warning(f"Image too large ({len(image_bytes)} bytes), skipping")
        except Exception as e:
            logger.warning(f"Failed to download image {url}: {e}")
        return None

    async def post_multiple(self, listings: list) -> int:
        """Post multiple listings with delays between them. Returns count of successful posts."""
        successful = 0
        for listing in listings:
            message_id = await self.post_listing(listing)
            if message_id:
                successful += 1
                # Delay between posts to avoid rate limiting
                await asyncio.sleep(3)
            else:
                # Longer delay if failed
                await asyncio.sleep(5)
        return successful

    async def test_connection(self) -> bool:
        """Test the bot connection and channel access."""
        try:
            me = await self.bot.get_me()
            logger.info(f"Bot connected: @{me.username} ({me.first_name})")
            # Try to get channel info
            try:
                chat = await self.bot.get_chat(self.channel)
                logger.info(f"Channel accessible: {chat.title or chat.username or self.channel}")
            except TelegramError as e:
                logger.warning(f"Cannot access channel {self.channel}: {e}")
                logger.warning("Make sure the bot is added as an admin to the channel")
            return True
        except TelegramError as e:
            logger.error(f"Bot connection test failed: {e}")
            return False
