import os
import asyncio
import logging
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)
PING_ROLE_ID = int(os.getenv("PING_ROLE_ID") or 0)
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES") or 15)
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
DB_FILE = "free_games_v3.db"

GAMERPOWER_BASE = "https://gamerpower.com/api"
EPIC_API = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
EPIC_LOCALE = "en-US"
EPIC_COUNTRY = "US"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS seen_games (
            giveaway_id TEXT PRIMARY KEY, title TEXT, platform TEXT, source TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_posted TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS bot_stats (key TEXT PRIMARY KEY, value TEXT)""")
        self.conn.commit()

    def is_seen(self, giveaway_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen_games WHERE giveaway_id = ?", (giveaway_id,)).fetchone() is not None

    def mark_seen(self, giveaway_id: str, title: str, platform: str, source: str = "Unknown"):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO seen_games (giveaway_id, title, platform, source, last_posted) VALUES (?, ?, ?, ?, ?)",
            (giveaway_id, title, platform, source, now)
        )
        self.conn.commit()

    def update_last_check(self):
        self.conn.execute("INSERT OR REPLACE INTO bot_stats (key, value) VALUES (?, ?)",
                          ("last_check", datetime.now(timezone.utc).isoformat()))
        self.conn.commit()


class ClaimView(discord.ui.View):
    def __init__(self, claim_url: str, trends_url: str, gamerpower_url: str):
        super().__init__(timeout=3600)
        self.add_item(discord.ui.Button(label="🔗 Claim Now", url=claim_url, style=discord.ButtonStyle.success))
        self.add_item(discord.ui.Button(label="📈 Google Trends", url=trends_url, style=discord.ButtonStyle.primary))
        self.add_item(discord.ui.Button(label="📋 GamerPower Page", url=gamerpower_url, style=discord.ButtonStyle.secondary))


class FreeGameNotifier(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database()
        self.session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        if not DISCORD_TOKEN or CHANNEL_ID == 0:
            logger.error("Missing DISCORD_TOKEN or CHANNEL_ID")
            return

        self.session = aiohttp.ClientSession()
        self.check_free_games.start()
        await self.tree.sync()
        logger.info("✅ Free Game Notifier v4.1 (PC Only) started")

        channel = self.get_channel(CHANNEL_ID)
        if channel:
            await channel.send("🚀 **Free Game Notifier v4.1** is online — Fetching **PC games only** from all major platforms!")

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES, reconnect=True)
    async def check_free_games(self):
        try:
            logger.info("🔍 Checking for new PC free games...")
            new_games = await self.fetch_new_free_games()

            if not new_games:
                logger.info("No new PC games found this cycle.")
                self.db.update_last_check()
                return

            channel = self.get_channel(CHANNEL_ID)
            if not channel:
                logger.error("Channel not found")
                return

            ping = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@here"
            posted = 0
            for game in new_games:
                embed, view = self.create_game_embed(game)
                await channel.send(f"{ping} **New FREE PC Game!**", embed=embed, view=view)
                self.db.mark_seen(game["id"], game["title"], game.get("platform", "PC"), game.get("source", "Unknown"))
                posted += 1

            logger.info(f"🚀 Posted {posted} new PC game(s)")
            self.db.update_last_check()

        except Exception as e:
            logger.exception(f"Error in check_free_games: {e}")

    @check_free_games.before_loop
    async def before_check(self):
        await self.wait_until_ready()

    async def _fetch_with_retry(self, url: str, params: Optional[Dict] = None, max_retries: int = 3):
        for attempt in range(max_retries):
            try:
                async with self.session.get(url, params=params, timeout=20) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except Exception as e:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt * 1.5)

    async def fetch_new_free_games(self) -> List[Dict[str, Any]]:
        new_games: List[Dict[str, Any]] = []

        # 1. GamerPower - All platforms, then filter for PC only
        try:
            data = await self._fetch_with_retry(f"{GAMERPOWER_BASE}/giveaways", {"type": "game", "sort-by": "date"})
            if isinstance(data, list):
                for item in data:
                    platforms = str(item.get("platforms", "")).lower()
                    if not any(p in platforms for p in ["pc", "steam", "gog", "epic", "ubisoft", "windows"]):
                        continue  # Skip non-PC games

                    gid = str(item.get("id") or "")
                    if gid and not self.db.is_seen(gid):
                        new_games.append({
                            "id": gid,
                            "title": item.get("title", "Unknown Game"),
                            "platform": item.get("platforms", "PC"),
                            "description": (item.get("instructions") or item.get("description", ""))[:400],
                            "image": item.get("image"),
                            "worth": item.get("worth", "Free"),
                            "end_date": item.get("end_date"),
                            "open_giveaway_url": item.get("open_giveaway_url") or "",
                            "gamerpower_url": f"https://gamerpower.com/giveaway/{gid}",
                            "source": "GamerPower",
                            "is_promo": str(item.get("worth", "")).startswith("$")
                        })
        except Exception as e:
            logger.error(f"GamerPower fetch failed: {e}")

        # 2. Direct Epic Games (PC only by nature)
        try:
            epic_data = await self._fetch_with_retry(EPIC_API, {"locale": EPIC_LOCALE, "country": EPIC_COUNTRY})
            if epic_data and isinstance(epic_data.get("data"), dict):
                elements = epic_data["data"].get("Catalog", {}).get("searchStore", {}).get("elements", [])
                for elem in elements:
                    promotions = elem.get("promotions") or {}
                    for offer_set in promotions.get("promotionalOffers", []):
                        for offer in offer_set.get("promotionalOffers", []):
                            if offer.get("discountSetting", {}).get("discountPercentage") == 0:
                                gid = f"epic_{elem.get('id') or elem.get('productSlug')}"
                                if gid and not self.db.is_seen(gid):
                                    new_games.append({
                                        "id": gid,
                                        "title": elem.get("title", "Epic Game"),
                                        "platform": "Epic Games Store",
                                        "description": elem.get("description", "")[:400],
                                        "image": None,
                                        "worth": "Free",
                                        "end_date": offer.get("endDate"),
                                        "open_giveaway_url": f"https://store.epicgames.com/en-US/p/{elem.get('productSlug', '')}",
                                        "gamerpower_url": "https://gamerpower.com",
                                        "source": "Epic Games",
                                        "is_promo": True
                                    })
        except Exception as e:
            logger.error(f"Epic fetch failed: {e}")

        # Deduplication
        seen = set()
        deduped = []
        for g in new_games:
            key = (g["title"].lower().strip(), g.get("platform", ""))
            if key not in seen:
                seen.add(key)
                deduped.append(g)
        return deduped

    def create_game_embed(self, game: Dict[str, Any]) -> tuple[discord.Embed, ClaimView]:
        title = game.get("title", "Unknown Game")
        claim_url = game.get("open_giveaway_url") or "https://gamerpower.com"
        trends_url = f"https://trends.google.com/trends/explore?q={urllib.parse.quote_plus(title)}"

        embed = discord.Embed(
            title=f"🎮 {title}",
            description=game.get("description", "No description available.")[:500],
            color=0x00ff00,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="🔗 Claim", value=f"[Open Giveaway]({claim_url})", inline=False)
        embed.add_field(name="📈 Trends", value=f"[Google Trends]({trends_url})", inline=False)
        embed.add_field(name="Platform", value=game.get("platform", "PC"), inline=True)
        embed.add_field(name="Source", value=game.get("source", "GamerPower"), inline=True)

        if game.get("end_date"):
            embed.add_field(name="⏰ Expires", value=game["end_date"], inline=True)

        view = ClaimView(claim_url, trends_url, game.get("gamerpower_url", "https://gamerpower.com"))
        return embed, view


if __name__ == "__main__":
    if not DISCORD_TOKEN or CHANNEL_ID == 0:
        logger.error("Missing DISCORD_TOKEN or CHANNEL_ID")
        exit(1)
    bot = FreeGameNotifier()
    bot.run(DISCORD_TOKEN)
