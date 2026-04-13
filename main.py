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

# ===================== CONFIG =====================
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

logger.info(f"Loaded CHANNEL_ID: {CHANNEL_ID}")
logger.info(f"Loaded OWNER_ID: {OWNER_ID}")


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

    def get_last_check(self) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM bot_stats WHERE key = ?", ("last_check",)).fetchone()
        return row[0] if row else None

    def get_seen_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen_games").fetchone()[0]

    def close(self):
        self.conn.close()


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
            logger.error(f"Missing DISCORD_TOKEN or invalid CHANNEL_ID ({CHANNEL_ID})")
            return

        self.session = aiohttp.ClientSession()
        self.check_free_games.start()
        await self.tree.sync()
        logger.info("✅ Free Game Notifier v3.7 started successfully")

        channel = self.get_channel(CHANNEL_ID)
        if channel:
            await channel.send("🚀 **Free Game Notifier v3.7** is online and ready!")
        else:
            logger.error(f"Could not find channel with ID {CHANNEL_ID}")

    # ===================== DEBUG =====================
    @app_commands.command(name="debug", description="Show debug info (owner only)")
    async def debug(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Owner only", ephemeral=True)
            return
        chan = self.get_channel(CHANNEL_ID)
        await interaction.response.send_message(
            f"**Debug**\nCHANNEL_ID: `{CHANNEL_ID}`\nFound channel: `{chan.name if chan else None}`\nBot in {len(self.guilds)} guilds",
            ephemeral=True
        )

    # ===================== CLEAR OLD =====================
    @app_commands.command(name="clearold", description="Delete bot's old messages (owner only)")
    async def clearold(self, interaction: discord.Interaction, amount: int = 50):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Owner only", ephemeral=True)
            return
        channel = self.get_channel(CHANNEL_ID)
        if not channel:
            await interaction.response.send_message("Channel not found", ephemeral=True)
            return
        def is_bot(msg): return msg.author.id == self.user.id
        deleted = await channel.purge(limit=amount, check=is_bot)
        await interaction.response.send_message(f"Deleted {len(deleted)} messages", ephemeral=True)

    # ===================== GAME TASK =====================
    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_free_games(self):
        try:
            logger.info("🔍 Checking for new free games...")
            new_games = await self.fetch_new_free_games()

            if not new_games:
                self.db.update_last_check()
                return

            channel = self.get_channel(CHANNEL_ID)
            if not channel:
                logger.error("Channel not found in task")
                return

            ping = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@here"
            for game in new_games:
                embed, view = self.create_game_embed(game)
                await channel.send(f"{ping} **New FREE / Promo Game Dropped!**", embed=embed, view=view)
                self.db.mark_seen(game["id"], game["title"], game.get("platform", "Unknown"), game.get("source", "Unknown"))

            logger.info(f"🚀 Posted {len(new_games)} games")
            self.db.update_last_check()
        except Exception as e:
            logger.exception("Error in check_free_games")

    async def _fetch_with_retry(self, url: str, params: Optional[Dict] = None, max_retries: int = 3):
        for attempt in range(max_retries):
            try:
                async with self.session.get(url, params=params, timeout=20) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Failed to fetch {url}")
                    raise
                await asyncio.sleep(2 ** attempt * 1.5)

    async def fetch_new_free_games(self) -> List[Dict[str, Any]]:
        new_games = []
        # GamerPower + Epic logic (same as before)
        try:
            data = await self._fetch_with_retry(f"{GAMERPOWER_BASE}/giveaways", {"platform": "pc", "type": "game", "sort-by": "date"})
            if isinstance(data, list):
                for item in data:
                    gid = str(item.get("id") or "")
                    if gid and not self.db.is_seen(gid):
                        new_games.append({
                            "id": gid,
                            "title": item.get("title", "Unknown"),
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
            logger.error(f"GamerPower error: {e}")

        # Epic (simplified for now)
        try:
            epic_data = await self._fetch_with_retry(EPIC_API, {"locale": EPIC_LOCALE, "country": EPIC_COUNTRY})
            if epic_data and "data" in epic_data:
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
                                        "end_date": None,
                                        "open_giveaway_url": f"https://store.epicgames.com/p/{elem.get('productSlug','')}",
                                        "gamerpower_url": "https://gamerpower.com",
                                        "source": "Epic Games",
                                        "is_promo": True
                                    })
        except Exception as e:
            logger.error(f"Epic error: {e}")

        # Dedup
        seen = set()
        return [g for g in new_games if (key := (g["title"].lower().strip(), g.get("platform", ""))) not in seen and not seen.add(key)]

    def create_game_embed(self, game: Dict[str, Any]) -> tuple[discord.Embed, ClaimView]:
        title = game.get("title", "Unknown Game")
        claim_url = game.get("open_giveaway_url", "https://gamerpower.com")
        trends_url = f"https://trends.google.com/trends/explore?q={urllib.parse.quote_plus(title)}"

        embed = discord.Embed(title=f"🎮 {title}", description=game.get("description", "")[:500], color=0x00ff00)
        embed.add_field(name="Claim", value=f"[Open]({claim_url})", inline=False)
        embed.add_field(name="Trends", value=f"[Google Trends]({trends_url})", inline=False)
        view = ClaimView(claim_url, trends_url, game.get("gamerpower_url", "https://gamerpower.com"))
        return embed, view

    @app_commands.command(name="fgstatus", description="Bot status")
    async def fgstatus(self, interaction: discord.Interaction):
        await interaction.response.send_message("✅ Bot is running", ephemeral=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN or CHANNEL_ID == 0:
        logger.error("Missing DISCORD_TOKEN or CHANNEL_ID")
        exit(1)
    bot = FreeGameNotifier()
    bot.run(DISCORD_TOKEN)
