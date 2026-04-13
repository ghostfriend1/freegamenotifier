import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from dotenv import load_dotenv

# ===================== CONFIG =====================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
PING_ROLE_ID = int(os.getenv("PING_ROLE_ID", 0))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", 15))
DB_FILE = "free_games_v3.db"

GAMERPOWER_BASE = "https://gamerpower.com/api"
EPIC_API = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"

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
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_games (
                giveaway_id TEXT PRIMARY KEY,
                title TEXT,
                platform TEXT,
                source TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_stats (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        self.conn.commit()

    def is_seen(self, giveaway_id: str) -> bool:
        cursor = self.conn.execute("SELECT 1 FROM seen_games WHERE giveaway_id = ?", (giveaway_id,))
        return cursor.fetchone() is not None

    def mark_seen(self, giveaway_id: str, title: str, platform: str, source: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_games (giveaway_id, title, platform, source) VALUES (?, ?, ?, ?)",
            (giveaway_id, title, platform, source),
        )
        self.conn.commit()

    def update_last_check(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_stats (key, value) VALUES (?, ?)",
            ("last_check", datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_seen_count(self) -> int:
        cursor = self.conn.execute("SELECT COUNT(*) FROM seen_games")
        return cursor.fetchone()[0]

    def close(self):
        self.conn.close()


class ClaimView(discord.ui.View):
    def __init__(self, claim_url: str, trends_url: str, info_url: str):
        super().__init__(timeout=None)  # Persistent view (recommended for giveaways)
        self.add_item(discord.ui.Button(label="Claim Now", url=claim_url, style=discord.ButtonStyle.success))
        self.add_item(discord.ui.Button(label="Google Trends", url=trends_url, style=discord.ButtonStyle.primary))
        self.add_item(discord.ui.Button(label="More Info", url=info_url, style=discord.ButtonStyle.secondary))


class FreeGameNotifier(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database()
        self.session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        if not DISCORD_TOKEN or not CHANNEL_ID:
            logger.error("Missing required environment variables (DISCORD_TOKEN or CHANNEL_ID)")
            raise SystemExit(1)

        self.session = aiohttp.ClientSession()
        self.check_free_games.start()
        await self.tree.sync()
        logger.info("Free Game Notifier v3 started - Slash commands synced")

        channel = self.get_channel(CHANNEL_ID)
        if channel:
            await channel.send("**🟢 Free Game Notifier v3 is now online and monitoring GamerPower + Epic Games!**")

    async def close(self):
        if self.session:
            await self.session.close()
        self.db.close()
        await super().close()

    async def _fetch_with_retry(self, url: str, params: Optional[Dict] = None, max_retries: int = 3) -> Any:
        for attempt in range(max_retries):
            try:
                async with self.session.get(url, params=params, timeout=20) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error("Failed to fetch %s after %d attempts: %s", url, max_retries, e)
                    raise
                await asyncio.sleep(2 ** attempt * 1.5)
        return None

    def create_game_embed(self, game: Dict[str, Any]) -> tuple[discord.Embed, ClaimView]:
        embed = discord.Embed(
            title=f"🎮 {game['title']}",
            description=game.get("description", "No description available.")[:500],
            color=0x00ff00,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Platform", value=game.get("platform", "Unknown"), inline=True)
        embed.add_field(name="Source", value=game.get("source", "Unknown"), inline=True)
        embed.add_field(name="Worth", value=game.get("worth", "Free"), inline=True)

        if game.get("end_date"):
            embed.add_field(name="Ends", value=game["end_date"], inline=True)

        if game.get("image"):
            embed.set_image(url=game["image"])

        embed.set_footer(text=f"ID: {game['id']} • Checked via FreeGameNotifier v3")

        trends_url = f"https://trends.google.com/trends/explore?q={quote_plus(game['title'])}"
        view = ClaimView(
            claim_url=game["claim_url"],
            trends_url=trends_url,
            info_url=game["info_url"]
        )
        return embed, view

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_free_games(self):
        try:
            logger.info("Starting free games check cycle...")
            new_games = await self.fetch_new_free_games()

            if not new_games:
                logger.info("No new games found this cycle.")
                self.db.update_last_check()
                return

            channel = self.get_channel(CHANNEL_ID)
            if not channel:
                logger.error("Configured CHANNEL_ID not found!")
                return

            ping = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@here"

            for game in new_games:
                embed, view = self.create_game_embed(game)
                await channel.send(
                    f"{ping} **New FREE / Promo Game Dropped!**",
                    embed=embed,
                    view=view
                )
                self.db.mark_seen(
                    game["id"], game["title"], game.get("platform", "Unknown"), game.get("source", "Unknown")
                )

            logger.info(f"Successfully posted {len(new_games)} new game(s)")
            self.db.update_last_check()

        except Exception as e:
            logger.exception("Critical error in check_free_games task")
            channel = self.get_channel(CHANNEL_ID)
            if channel:
                await channel.send("⚠️ **Free Game Notifier encountered an error** (will retry on next cycle).")

    async def fetch_new_free_games(self) -> List[Dict[str, Any]]:
        new_games: List[Dict[str, Any]] = []

        # 1. GamerPower Giveaways
        try:
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
                            "platform": item.get("platforms", "PC"),
                            "description": (item.get("instructions") or item.get("description", ""))[:400],
                            "image": item.get("image"),
                            "worth": item.get("worth", "Free"),
                            "end_date": item.get("end_date"),
                            "claim_url": item.get("open_giveaway_url") or item.get("gamerpower_url", ""),
                            "info_url": f"https://gamerpower.com/giveaway/{gid}",
                            "source": "GamerPower"
                        }
                        new_games.append(game)
        except Exception as e:
            logger.error("GamerPower fetch failed: %s", e)

        # 2. Epic Games Store Free Games (Improved)
        try:
            data = await self._fetch_with_retry(
                EPIC_API,
                params={"locale": "en-US", "country": "US", "includePromotions": "true"}
            )
            if data and "data" in data and "Catalog" in data["data"]:
                for game in data["data"]["Catalog"]["searchStore"]["elements"]:
                    promotions = game.get("promotions", {})
                    if not promotions or not promotions.get("promotionalOffers"):
                        continue

                    for offer in promotions["promotionalOffers"]:
                        for promo in offer.get("promotionalOffers", []):
                            if promo.get("discountSetting", {}).get("discountPercentage") == 0:
                                gid = f"epic_{game.get('id')}"
                                if not self.db.is_seen(gid):
                                    new_games.append({
                                        "id": gid,
                                        "title": game.get("title", "Unknown Epic Game"),
                                        "platform": "Epic Games Store",
                                        "description": game.get("description", "")[:400],
                                        "image": game.get("keyImages", [{}])[0].get("url"),
                                        "worth": "Free",
                                        "end_date": promo.get("endDate"),
                                        "claim_url": f"https://store.epicgames.com/en-US/p/{game.get('productSlug', '')}",
                                        "info_url": f"https://store.epicgames.com/en-US/p/{game.get('productSlug', '')}",
                                        "source": "Epic Games"
                                    })
        except Exception as e:
            logger.error("Epic Games fetch failed: %s", e)

        # Optional: deduplicate by title (fuzzy match could be added later)
        seen_titles = set()
        unique_games = []
        for g in new_games:
            title_lower = g["title"].lower().strip()
            if title_lower not in seen_titles:
                seen_titles.add(title_lower)
                unique_games.append(g)

        return unique_games


# ===================== BOT START =====================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN not found in .env")
        exit(1)

    bot = FreeGameNotifier()
    bot.run(DISCORD_TOKEN)
