# free_game_notifier_v3.py
import os
import asyncio
import logging
import sqlite3
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ===================== CONFIG =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
PING_ROLE_ID = int(os.getenv("PING_ROLE_ID", 0))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", 15))
DB_FILE = "free_games_v3.db"
GAMERPOWER_BASE = "https://gamerpower.com/api"
EPIC_LOCALE = "en-US"
EPIC_COUNTRY = "US"

# ===================== LOGGING =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_games (
                giveaway_id TEXT PRIMARY KEY,
                title TEXT,
                platform TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_stats (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    def is_seen(self, giveaway_id: str) -> bool:
        cursor = self.conn.execute("SELECT 1 FROM seen_games WHERE giveaway_id = ?", (giveaway_id,))
        return cursor.fetchone() is not None

    def mark_seen(self, giveaway_id: str, title: str, platform: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_games (giveaway_id, title, platform) VALUES (?, ?, ?)",
            (giveaway_id, title, platform)
        )
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
        return row[0] if row else None

    def get_seen_count(self) -> int:
        cursor = self.conn.execute("SELECT COUNT(*) FROM seen_games")
        return cursor.fetchone()[0]

    def close(self):
        self.conn.close()

class ClaimView(discord.ui.View):
    def __init__(self, claim_url: str, trends_url: str, gamerpower_url: str):
        super().__init__(timeout=3600)  # 1 hour
        self.add_item(discord.ui.Button(label="🔗 Claim Now", url=claim_url, style=discord.ButtonStyle.success))
        self.add_item(discord.ui.Button(label="📈 Google Trends", url=trends_url, style=discord.ButtonStyle.primary))
        self.add_item(discord.ui.Button(label="📋 GamerPower Page", url=gamerpower_url, style=discord.ButtonStyle.secondary))

class FreeGameNotifier(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database()
        self.session: aiohttp.ClientSession | None = None
        self.last_check: Optional[datetime] = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.check_free_games.start()
        await self.tree.sync()  # Slash commands
        logger.info("✅ v3 Slash commands synced & background task started")
        # Startup health ping
        channel = self.get_channel(CHANNEL_ID)
        if channel:
            await channel.send("🚀 **Free Game Notifier v3 is online and monitoring all major platforms!**")

    async def close(self):
        if self.session:
            await self.session.close()
        self.db.close()
        await super().close()

    async def _fetch_with_retry(self, url: str, params: Optional[Dict] = None, max_retries: int = 3) -> Any:
        """Zero-ban-risk retry wrapper – public endpoints only."""
        for attempt in range(max_retries):
            try:
                async with self.session.get(url, params=params, timeout=15) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error("API fetch failed after %d retries: %s", max_retries, e)
                    raise
                await asyncio.sleep(2 ** attempt)  # exponential backoff
        return None

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_free_games(self):
        try:
            logger.info("🔍 v3 Checking for new free / promo games...")
            new_games = await self.fetch_new_free_games()

            if new_games:
                channel = self.get_channel(CHANNEL_ID)
                if not channel:
                    logger.error("Channel %s not found", CHANNEL_ID)
                    return

                ping = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@here"
                for game in new_games:
                    embed, view = self.create_game_embed(game)
                    await channel.send(f"{ping} **New FREE / Promo game dropped!**", embed=embed, view=view)
                    self.db.mark_seen(game["id"], game["title"], game.get("platform", "Unknown"))
                logger.info("🚀 Posted %d new games", len(new_games))

            self.db.update_last_check()
            self.last_check = datetime.now(timezone.utc)
        except Exception as e:
            logger.exception("Error in check_free_games: %s", e)
            channel = self.get_channel(CHANNEL_ID)
            if channel:
                await channel.send(f"⚠️ **Notifier encountered a transient error** (will retry automatically). {e}")

    async def fetch_new_free_games(self) -> List[Dict[str, Any]]:
        new_games = []

        # 1. GamerPower – covers Steam, Epic, GOG, itch.io, Ubisoft, etc. (includes paid→free promos)
        data = await self._fetch_with_retry(
            f"{GAMERPOWER_BASE}/giveaways",
            params={"platform": "pc", "type": "game", "sort-by": "date"}
        )
        if data:
            for item in data:
                gid = str(item.get("id"))
                if gid and not self.db.is_seen(gid):
                    game = {
                        "id": gid,
                        "title": item.get("title", "Unknown Game"),
                        "platform": item.get("platform", "PC").title(),
                        "description": (item.get("instructions") or item.get("description", ""))[:300],
                        "image": item.get("image"),
                        "worth": item.get("worth", "Free"),
                        "end_date": item.get("end_date"),
                        "open_giveaway_url": item.get("open_giveaway_url", "https://gamerpower.com"),
                        "gamerpower_url": f"https://gamerpower.com/giveaway/{gid}",
                        "source": "GamerPower",
                        "is_promo": item.get("worth", "").startswith("$")  # paid → free flag
                    }
                    new_games.append(game)

        # 2. Epic fallback (rock-solid for weekly promos)
        epic_data = await self._fetch_with_retry(
            "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions",
            params={"locale": EPIC_LOCALE, "country": EPIC_COUNTRY, "size": 1000}
        )
        if epic_data:
            elements = epic_data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
            for elem in elements:
                price = elem.get("price", {}).get("totalPrice", {}).get("fmtPrice", {})
                if price.get("discountPrice") in ("0", "Free") and elem.get("promotions", {}).get("promotionalOffers"):
                    gid = elem.get("id") or elem.get("productSlug")
                    if gid and not self.db.is_seen(gid):
                        new_games.append({
                            "id": gid,
                            "title": elem.get("title", "Unknown"),
                            "platform": "Epic Games Store",
                            "description": elem.get("description", "")[:300],
                            "image": next((img["url"] for img in elem.get("keyImages", []) if img.get("type") in ("Thumbnail", "OfferImageWide")), None),
                            "worth": "Free (Promo)",
                            "end_date": None,
                            "open_giveaway_url": f"https://store.epicgames.com/en-US/p/{elem.get('productSlug', '')}",
                            "gamerpower_url": "https://gamerpower.com",
                            "source": "Epic",
                            "is_promo": True
                        })

        # Dedupe + safety
        seen = set()
        deduped = []
        for g in new_games:
            key = f"{g['title'].lower()}|{g['platform']}|{g['id']}"
            if key not in seen:
                seen.add(key)
                deduped.append(g)
        return deduped

    def create_game_embed(self, game: Dict[str, Any]) -> tuple[discord.Embed, ClaimView]:
        title = game["title"]
        platform = game.get("platform", "PC")
        claim_url = game.get("open_giveaway_url", "https://gamerpower.com")
        gamerpower_url = game.get("gamerpower_url", "https://gamerpower.com")

        # Paid → free callout
        worth = game.get("worth", "Free")
        promo_text = f"**Was {worth} → FREE for limited time!**" if game.get("is_promo") and worth != "Free" else "FREE now!"

        query = f"{title.replace(' ', '+')}+{platform}+player+count+hype+downloads+trends+2026"
        trends_url = f"https://www.google.com/search?q={query}"

        embed = discord.Embed(
            title=f"🎮 {title} — {promo_text}",
            url=claim_url,
            description=game.get("description", "No description available")[:500],
            color=0x00FF00,
            timestamp=datetime.now(timezone.utc),
        )
        if game.get("image"):
            embed.set_thumbnail(url=game["image"])

        embed.add_field(name="🔗 Claim Link", value=f"[Open Giveaway]({claim_url})", inline=False)
        embed.add_field(
            name="📈 Trends • Player Base • Hype • Downloads",
            value=f"[🔍 Google Search]({trends_url})\nPlatform: {platform}\nSource: {game.get('source', 'GamerPower')}",
            inline=False,
        )
        if game.get("end_date"):
            embed.add_field(name="⏰ Expires", value=game["end_date"], inline=True)

        embed.set_footer(text="v3 • Public APIs only • Zero ban risk • Bot by DeveloperGrok")
        view = ClaimView(claim_url, trends_url, gamerpower_url)
        return embed, view

    @app_commands.command(name="status", description="Show notifier health & stats")
    async def status(self, interaction: discord.Interaction):
        last = self.db.get_last_check() or "Never"
        count = self.db.get_seen_count()
        await interaction.response.send_message(
            f"**Free Game Notifier v3 Status**\n"
            f"✅ Online\n"
            f"📊 Games tracked: {count}\n"
            f"🕒 Last check: {last}\n"
            f"🔄 Checking every {CHECK_INTERVAL_MINUTES} min\n"
            f"🛡️ Public read-only APIs – 100% ban-proof",
            ephemeral=True
        )

    @app_commands.command(name="currentfree", description="List current free/promos (top 5)")
    async def current_free(self, interaction: discord.Interaction):
        await interaction.response.defer()
        games = await self.fetch_new_free_games()  # re-use logic
        if not games:
            await interaction.followup.send("No active free/promos right now.")
            return
        for game in games[:5]:
            embed, view = self.create_game_embed(game)
            await interaction.followup.send(embed=embed, view=view)

if __name__ == "__main__":
    if not DISCORD_TOKEN or not CHANNEL_ID:
        logger.error("Missing DISCORD_TOKEN or CHANNEL_ID in .env")
        exit(1)
    bot = FreeGameNotifier()
    try:
        asyncio.run(bot.start(DISCORD_TOKEN))
    finally:
        logger.info("v3 Bot shutting down cleanly")