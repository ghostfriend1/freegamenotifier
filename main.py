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

# ===================== CONFIG WITH VALIDATION =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
try:
    CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)
except ValueError:
    CHANNEL_ID = 0

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

# Log config at startup for debugging
logger.info(f"Loaded CHANNEL_ID: {CHANNEL_ID}")
logger.info(f"Loaded OWNER_ID: {OWNER_ID}")
logger.info(f"Loaded PING_ROLE_ID: {PING_ROLE_ID}")


class Database:
    # ... (keep your existing Database class exactly as it is)

    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS seen_games (
            giveaway_id TEXT PRIMARY KEY, title TEXT, platform TEXT, source TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_posted TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS bot_stats (key TEXT PRIMARY KEY, value TEXT)""")
        self.conn.commit()

    # ... (keep is_seen, mark_seen, update_last_check, get_last_check, get_seen_count, close)


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
        if not DISCORD_TOKEN:
            logger.error("DISCORD_TOKEN is missing!")
            return
        if CHANNEL_ID == 0:
            logger.error("CHANNEL_ID is 0 or invalid! Check Railway Variables.")

        self.session = aiohttp.ClientSession()
        self.check_free_games.start()
        await self.tree.sync()
        logger.info("✅ Free Game Notifier v3.7 started")

        channel = self.get_channel(CHANNEL_ID)
        if channel:
            await channel.send("🚀 **Free Game Notifier v3.7** is online. Use `/debug` for diagnostics.")
        else:
            logger.error(f"Failed to get channel with ID {CHANNEL_ID}")

    # ===================== DEBUG COMMAND =====================
    @app_commands.command(name="debug", description="Show environment and channel debug info (owner only)")
    async def debug(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
            return

        chan = self.get_channel(CHANNEL_ID)
        try:
            fetched = await self.fetch_channel(CHANNEL_ID) if not chan else None
        except Exception as e:
            fetched = f"Error: {e}"

        await interaction.response.send_message(
            f"**Debug Info**\n"
            f"CHANNEL_ID configured: `{CHANNEL_ID}`\n"
            f"get_channel() result: `{chan}`\n"
            f"Channel name (if found): `{chan.name if chan else 'None'}`\n"
            f"fetch_channel result: `{fetched}`\n"
            f"Bot is in {len(self.guilds)} guild(s)\n"
            f"OWNER_ID: `{OWNER_ID}`",
            ephemeral=True
        )

    # Keep your existing /clearold, /sync, /fgstatus, /currentfree, check_free_games, fetch_new_free_games, create_game_embed ...

    # (Paste the rest of your previous functions here - clearold, sync, fgstatus, current_free, check_free_games, etc.)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("DISCORD_TOKEN missing")
        exit(1)
    bot = FreeGameNotifier()
    bot.run(DISCORD_TOKEN)
