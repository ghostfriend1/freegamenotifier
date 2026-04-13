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
OWNER_ID = int(os.getenv("OWNER_ID", 0))          # ← Add your Discord User ID here
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
        if not DISCORD_TOKEN or not CHANNEL_ID:
            logger.error("Missing DISCORD_TOKEN or CHANNEL_ID in .env")
            return

        self.session = aiohttp.ClientSession()
        self.check_free_games.start()
        await self.tree.sync()
        logger.info("✅ Free Game Notifier v3.6 started with /clearold command")

        channel = self.get_channel(CHANNEL_ID)
        if channel:
            await channel.send("🚀 **Free Game Notifier v3.6** online • Use `/clearold` to clean old bot messages.")

    # ===================== CLEAR OLD BOT MESSAGES =====================
    @app_commands.command(name="clearold", description="Delete the bot's old notification messages (owner only)")
    @app_commands.describe(amount="Number of bot messages to delete (default 50, max 500)")
    async def clearold(self, interaction: discord.Interaction, amount: int = 50):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ Only the bot owner can use this command.", ephemeral=True)
            return

        if amount < 1 or amount > 500:
            amount = 50

        await interaction.response.defer(ephemeral=True)

        channel = self.get_channel(CHANNEL_ID)
        if not channel:
            await interaction.followup.send("❌ Notification channel not found.", ephemeral=True)
            return

        def is_bot_message(message: discord.Message):
            return message.author.id == self.user.id

        try:
            deleted = await channel.purge(limit=amount, check=is_bot_message, bulk=True)
            count = len(deleted)
            await interaction.followup.send(
                f"✅ Deleted **{count}** old bot notification message(s) from the channel.",
                ephemeral=True
            )
            logger.info(f"Cleared {count} old bot messages via /clearold")
        except discord.Forbidden:
            await interaction.followup.send("❌ Missing 'Manage Messages' permission in the channel.", ephemeral=True)
        except Exception as e:
            logger.error(f"Clearold failed: {e}")
            await interaction.followup.send(f"❌ Error while clearing messages: {e}", ephemeral=True)

    # ===================== SYNC COMMAND (Owner Only) =====================
    @app_commands.command(name="sync", description="Force sync slash commands (owner only)")
    async def sync(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ You are not authorized.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self.tree.clear_commands(guild=None)
            synced = await self.tree.sync()
            await interaction.followup.send(f"✅ Synced {len(synced)} global commands.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

    # (The rest of the code - check_free_games, fetch_new_free_games, create_game_embed, fgstatus, current_free - stays exactly the same as v3.5)

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_free_games(self):
        try:
            logger.info("🔍 Checking for new free / promo games...")
            new_games = await self.fetch_new_free_games()

            if not new_games:
                logger.info("No new games found this cycle.")
                self.db.update_last_check()
                return

            channel = self.get_channel(CHANNEL_ID)
            if not channel:
                logger.error(f"Channel {CHANNEL_ID} not found!")
                return

            ping = f"<@&{PING_ROLE_ID}>" if PING_ROLE_ID else "@here"
            posted_count = 0

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
                posted_count += 1

            logger.info(f"🚀 Successfully posted {posted_count} new game(s)")
            self.db.update_last_check()

        except Exception as e:
            logger.exception("Critical error in check_free_games task")
            channel = self.get_channel(CHANNEL_ID)
            if channel:
                await channel.send("⚠️ **Notifier encountered a transient error** (will retry automatically).")

    async def fetch_new_free_games(self) -> List[Dict[str, Any]]:
        new_games: List[Dict[str, Any]] = []

        # GamerPower
        try:
            data = await self._fetch_with_retry(
                f"{GAMERPOWER_BASE}/giveaways",
                params={"platform": "pc", "type": "game", "sort-by": "date"}
            )
            if isinstance(data, list):
                for item in data:
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
                            "gamerpower_url": item.get("gamerpower_url", f"https://gamerpower.com/giveaway/{gid}"),
                            "source": "GamerPower",
                            "is_promo": str(item.get("worth", "")).startswith("$")
                        })
        except Exception as e:
            logger.error(f"GamerPower fetch failed: {e}")

        # Epic Games
        try:
            epic_data = await self._fetch_with_retry(
                EPIC_API,
                params={"locale": EPIC_LOCALE, "country": EPIC_COUNTRY}
            )
            if epic_data and isinstance(epic_data.get("data"), dict):
                elements = epic_data["data"].get("Catalog", {}).get("searchStore", {}).get("elements", [])
                for elem in elements:
                    promotions = elem.get("promotions") or {}
                    for offer_set in promotions.get("promotionalOffers", []):
                        for offer in offer_set.get("promotionalOffers", []):
                            if offer.get("discountSetting", {}).get("discountPercentage") == 0:
                                gid = f"epic_{elem.get('id') or elem.get('productSlug')}"
                                if gid and not self.db.is_seen(gid):
                                    image = next((img.get("url") for img in elem.get("keyImages", []) 
                                                  if img.get("type") in ("Thumbnail", "OfferImageWide", "DieselStoreFrontTall")), None)
                                    new_games.append({
                                        "id": gid,
                                        "title": elem.get("title", "Unknown Epic Game"),
                                        "platform": "Epic Games Store",
                                        "description": elem.get("description", "")[:400],
                                        "image": image,
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
        platform = game.get("platform", "Unknown")
        claim_url = game.get("open_giveaway_url") or "https://gamerpower.com"
        gamerpower_url = game.get("gamerpower_url", "https://gamerpower.com")

        worth = game.get("worth", "Free")
        promo_text = f"Was {worth} → FREE!" if game.get("is_promo") and worth != "Free" else "FREE now!"

        query = urllib.parse.quote_plus(f"{title} {platform} player count hype")
        trends_url = f"https://trends.google.com/trends/explore?q={query}"

        embed = discord.Embed(
            title=f"🎮 {title} — {promo_text}",
            url=claim_url,
            description=game.get("description", "No description available.")[:500],
            color=0x00FF00,
            timestamp=datetime.now(timezone.utc),
        )

        if game.get("image"):
            embed.set_thumbnail(url=game["image"])

        embed.add_field(name="🔗 Claim Link", value=f"[Open Giveaway]({claim_url})", inline=False)
        embed.add_field(
            name="📈 Trends • Player Base • Hype",
            value=f"[🔍 Google Trends]({trends_url})\nPlatform: {platform}\nSource: {game.get('source', 'Unknown')}",
            inline=False,
        )

        if game.get("end_date"):
            embed.add_field(name="⏰ Expires", value=game["end_date"], inline=True)

        embed.set_footer(text="v3.6 • Anti-Repost + /clearold")

        view = ClaimView(claim_url, trends_url, gamerpower_url)
        return embed, view

    @app_commands.command(name="fgstatus", description="Show free game notifier health & stats")
    async def fgstatus(self, interaction: discord.Interaction):
        last = self.db.get_last_check() or "Never"
        count = self.db.get_seen_count()
        await interaction.response.send_message(
            f"**Free Game Notifier v3.6 Status**\n"
            f"✅ Online\n"
            f"📊 Games tracked: **{count}**\n"
            f"🕒 Last check: {last}\n"
            f"🔄 Interval: {CHECK_INTERVAL_MINUTES} min\n"
            f"🗑️ Use /clearold to clean old messages",
            ephemeral=True
        )

    @app_commands.command(name="currentfree", description="List current free/promos (top 5)")
    async def current_free(self, interaction: discord.Interaction):
        await interaction.response.defer()
        games = await self.fetch_new_free_games()
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
    bot.run(DISCORD_TOKEN)
