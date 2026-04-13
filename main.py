# free_game_notifier_v3.py
import os
import asyncio
import logging
import sqlite3
import hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import discord
from discord.ext import commands, tasks
import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ===================== CONFIG =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)
PING_ROLE_ID = int(os.getenv("PING_ROLE_ID") or 0)
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES") or 15)
DB_FILE = "free_games_v3.db"

GAMERPOWER_BASE = "https://gamerpower.com/api"
EPIC_API = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
EPIC_LOCALE = "en-US"
EPIC_COUNTRY = "US"

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_games (
                unique_id TEXT PRIMARY KEY,
                giveaway_id TEXT,
                title TEXT,
                platform TEXT,
                claim_url TEXT,
                source TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_stats (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    def _normalize_title(self, title: str) -> str:
        if not title:
            return ""
        # Aggressive normalization to catch similar titles
        return (title.strip()
                .lower()
                .replace("free", "")
                .replace("giveaway", "")
                .replace("promo", "")
                .strip())

    def is_seen(self, giveaway_id: Optional[str] = None,
                title: Optional[str] = None,
                platform: Optional[str] = None,
                claim_url: Optional[str] = None) -> bool:
        """Multi-layer duplicate prevention"""
        if not title:
            return False

        norm_title = self._normalize_title(title)

        # Layer 1: giveaway_id (fastest when stable)
        if giveaway_id:
            cursor = self.conn.execute(
                "SELECT 1 FROM seen_games WHERE giveaway_id = ?", (giveaway_id,)
            )
            if cursor.fetchone():
                return True

        # Layer 2: normalized title + platform
        if platform:
            cursor = self.conn.execute(
                "SELECT 1 FROM seen_games WHERE title = ? AND platform = ?",
                (norm_title, platform.lower())
            )
            if cursor.fetchone():
                return True

        # Layer 3: hash of title + claim_url (most robust)
        if claim_url:
            unique_key = hashlib.md5(f"{norm_title}{claim_url}".encode()).hexdigest()
            cursor = self.conn.execute(
                "SELECT 1 FROM seen_games WHERE unique_id = ?", (unique_key,)
            )
            if cursor.fetchone():
                return True

        return False

    def mark_seen(self, giveaway_id: Optional[str] = None,
                  title: str = "",
                  platform: str = "Unknown",
                  claim_url: Optional[str] = None,
                  source: str = "Unknown"):
        """Mark game as seen with multiple keys"""
        if not title:
            return

        norm_title = self._normalize_title(title)
        now = datetime.now(timezone.utc).isoformat()

        # Generate stable unique_id
        if claim_url:
            unique_id = hashlib.md5(f"{norm_title}{claim_url}".encode()).hexdigest()
        else:
            unique_id = hashlib.md5(f"{norm_title}{platform}".encode()).hexdigest()

        self.conn.execute("""
            INSERT OR REPLACE INTO seen_games 
            (unique_id, giveaway_id, title, platform, claim_url, source, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (unique_id, giveaway_id, norm_title, platform.lower(), claim_url, source, now))
        self.conn.commit()

    def cleanup_old_entries(self, days: int = 90):
        """Keep database clean"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        self.conn.execute("DELETE FROM seen_games WHERE last_seen < ?", (cutoff,))
        self.conn.commit()

    def update_last_check(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_stats (key, value) VALUES (?, ?)",
            ("last_check", datetime.now(timezone.utc).isoformat())
        )
        self.conn.commit()

    def get_last_check(self) -> Optional[str]:
        cursor = self.conn.execute("SELECT value FROM bot_stats WHERE key = ?", ("last_check",))
        row = cursor.fetchone()
        return row["value"] if row else None

    def get_seen_count(self) -> int:
        cursor = self.conn.execute("SELECT COUNT(*) FROM seen_games")
        return cursor.fetchone()[0]

    def close(self):
        self.conn.close()


class ClaimView(discord.ui.View):
    def __init__(self, claim_url: str, trends_url: str, gamerpower_url: str):
        super().__init__(timeout=3600)
        self.add_item(discord.ui.Button(label="Claim Now", url=claim_url, style=discord.ButtonStyle.success))
        self.add_item(discord.ui.Button(label="Google Trends", url=trends_url, style=discord.ButtonStyle.primary))
        self.add_item(discord.ui.Button(label="GamerPower Page", url=gamerpower_url, style=discord.ButtonStyle.secondary))


class FreeGameNotifier(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database()
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_check: Optional[datetime] = None

    async def setup_hook(self):
        if not DISCORD_TOKEN or not CHANNEL_ID:
            logger.error("Missing DISCORD_TOKEN or CHANNEL_ID in .env")
            return

        self.session = aiohttp.ClientSession()
        self.check_free_games.start()
        await self.tree.sync()
        logger.info("Free Game Notifier v3.2 (anti-duplicate) started")

        channel = self.get_channel(CHANNEL_ID)
        if channel:
            await channel.send("**Free Game Notifier v3.2 is online** — Improved duplicate prevention active!")

    async def close(self):
        if self.session:
            await self.session.close()
        self.db.close()
        await super().close()

    async def _fetch_with_retry(self, url: str, params: Optional[Dict[str, Any]] = None, max_retries: int = 3) -> Any:
        for attempt in range(max_retries):
            try:
                async with self.session.get(url, params=params, timeout=20) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"API fetch failed after {max_retries} attempts: {e}")
                    raise
                await asyncio.sleep(2 ** attempt * 1.5)
        return None

    async def fetch_new_free_games(self) -> List[Dict[str, Any]]:
        new_games: List[Dict[str, Any]] = []

        # ===================== GAMERPOWER =====================
        try:
            data = await self._fetch_with_retry(
                f"{GAMERPOWER_BASE}/giveaways",
                params={"platform": "pc", "type": "game", "sort-by": "date"}
            )
            if isinstance(data, list):
                for item in data:
                    gid = str(item.get("id")) if item.get("id") else None
                    title = item.get("title", "Unknown Game")
                    platform = item.get("platform", "PC").title()
                    claim_url = item.get("open_giveaway_url") or f"https://gamerpower.com/giveaway/{gid}"

                    if self.db.is_seen(giveaway_id=gid, title=title, platform=platform, claim_url=claim_url):
                        continue

                    new_games.append({
                        "id": gid,
                        "title": title,
                        "platform": platform,
                        "description": (item.get("instructions") or item.get("description", ""))[:400],
                        "image": item.get("image"),
                        "worth": item.get("worth", "Free"),
                        "end_date": item.get("end_date"),
                        "open_giveaway_url": claim_url,
                        "gamerpower_url": f"https://gamerpower.com/giveaway/{gid}" if gid else "https://gamerpower.com",
                        "source": "GamerPower",
                        "is_promo": str(item.get("worth", "")).startswith("$")
                    })
        except Exception as e:
            logger.error(f"GamerPower fetch failed: {e}")

        # ===================== EPIC GAMES =====================
        try:
            epic_data = await self._fetch_with_retry(
                EPIC_API,
                params={"locale": EPIC_LOCALE, "country": EPIC_COUNTRY, "size": 1000}
            )
            if epic_data and "data" in epic_data:
                elements = epic_data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
                for elem in elements:
                    price_info = elem.get("price", {}).get("totalPrice", {}).get("fmtPrice", {})
                    promotions = elem.get("promotions", {}).get("promotionalOffers")

                    if price_info.get("discountPrice") in ("0", "Free") and promotions:
                        title = elem.get("title", "Unknown Epic Game")
                        product_slug = elem.get("productSlug") or ""
                        claim_url = f"https://store.epicgames.com/en-US/p/{product_slug}" if product_slug else ""
                        gid = elem.get("id") or product_slug

                        if not claim_url or self.db.is_seen(
                            giveaway_id=gid, title=title, platform="Epic Games Store", claim_url=claim_url
                        ):
                            continue

                        # Get best image
                        image = None
                        for img in elem.get("keyImages", []):
                            if img.get("type") in ("Thumbnail", "OfferImageWide", "DieselStoreFrontTall"):
                                image = img.get("url")
                                break

                        new_games.append({
                            "id": gid,
                            "title": title,
                            "platform": "Epic Games Store",
                            "description": elem.get("description", "")[:400],
                            "image": image,
                            "worth": "Free (Epic Promo)",
                            "end_date": None,
                            "open_giveaway_url": claim_url,
                            "gamerpower_url": claim_url,
                            "source": "Epic Games",
                            "is_promo": True
                        })
        except Exception as e:
            logger.error(f"Epic fetch failed: {e}")

        return new_games

    def create_game_embed(self, game: Dict[str, Any]) -> tuple:
        embed = discord.Embed(
            title=f"🎮 {game['title']}",
            description=game.get("description", "No description available."),
            color=0x00ff00 if not game.get("is_promo") else 0xffaa00
        )
        if game.get("image"):
            embed.set_image(url=game["image"])

        embed.add_field(name="Platform", value=game.get("platform", "Unknown"), inline=True)
        embed.add_field(name="Worth", value=game.get("worth", "Free"), inline=True)

        if game.get("end_date"):
            embed.add_field(name="Ends", value=game["end_date"], inline=True)

        embed.set_footer(text=f"Source: {game.get('source', 'Unknown')} • Free Game Notifier v3.2")

        trends_url = f"https://trends.google.com/trends/explore?q={urllib.parse.quote_plus(game['title'])}"
        view = ClaimView(
            claim_url=game["open_giveaway_url"],
            trends_url=trends_url,
            gamerpower_url=game.get("gamerpower_url", game["open_giveaway_url"])
        )
        return embed, view

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_free_games(self):
        try:
            logger.info("Checking for new free / promo games...")
            new_games = await self.fetch_new_free_games()

            if not new_games:
                logger.info("No new games this cycle.")
                self.db.update_last_check()
                self.last_check = datetime.now(timezone.utc)
                return

            channel = self.get_channel(CHANNEL_ID)
            if not channel:
                logger.error(f"Channel {CHANNEL_ID} not found!")
                return

            ping = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@here"

            for game in new_games:
                embed, view = self.create_game_embed(game)
                await channel.send(
                    f"{ping} **New FREE / Promo Game Dropped!**",
                    embed=embed,
                    view=view
                )
                # Mark as seen ONLY after successful post
                self.db.mark_seen(
                    giveaway_id=game.get("id"),
                    title=game["title"],
                    platform=game.get("platform", "Unknown"),
                    claim_url=game.get("open_giveaway_url"),
                    source=game.get("source", "Unknown")
                )

            logger.info(f"Successfully posted {len(new_games)} new game(s)")
            self.db.update_last_check()
            self.last_check = datetime.now(timezone.utc)

            # Weekly cleanup
            if datetime.now(timezone.utc).hour == 3:
                self.db.cleanup_old_entries(days=75)

        except Exception as e:
            logger.exception(f"Error in check_free_games: {e}")
            channel = self.get_channel(CHANNEL_ID)
            if channel:
                await channel.send("**Notifier encountered a transient error** (will retry automatically).")


# ===================== RUN BOT =====================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not found in .env file!")
    else:
        bot = FreeGameNotifier()
        bot.run(DISCORD_TOKEN)
