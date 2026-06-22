#!/usr/bin/env python3
"""
Free Game Notifier - v6.2
Discord bot that monitors and announces currently-free PC games (and optionally
free DLC, keys, and beta access) from GamerPower + the Epic Games Store.

New in v6.2 — Quality-of-life & admin
-------------------------------------
- 🧹 /fgclear   : delete the bot's OWN recent messages (confirm + perm gate; 14-day aware).
- ⚙️ /fgconfig  : change ping role, min worth, reminder hours, loot/beta AT RUNTIME (persisted).
- 🧪 /fgtest    : post a sample announcement to preview embeds/buttons (no ping).
- ❓ /fghelp     : full command directory.   ➕ /fginvite : ready-made invite link.
- 📶 Latency shown in /fgstatus.

New in v6.1 — Claim & Share ("Win Share Kit")
---------------------------------------------
- 🎉 "Share My Win!" button on every announcement -> ephemeral, copy-paste-ready
  social text (randomized templates, clean hashtags, real claim link, bot promo).
- 📢 Optional "post my win to the channel" celebration button (ALLOW_PUBLIC_SHARE).
- 🎉 /sharewin command to build a share kit for any current freebie on demand.
- 📈 Community share counters surfaced in /fgstats.
- The Share button is PERSISTENT + STATELESS: it rebuilds the game from the
  message, so it keeps working after the view times out and across restarts
  (the common pitfall with per-message-state buttons).

Highlights from v6.0
--------------------
- ⏰ Last-chance reminders     : re-pings before a giveaway expires (EXPIRY_REMINDER_HOURS).
- 🔔 Self-serve alert opt-in   : members click a button to toggle the ping role themselves.
- 🔮 /upcoming                 : next week's free Epic games.
- 📈 /fgstats                  : lifetime games announced, total $ given away, value free now.
- 💵 MIN_WORTH filter          : only announce games worth real money.
- 🎁 Loot / beta toggles       : optionally catch free in-game loot, keys, and betas.
- 🧹 Smarter dedup             : same game from two sources is announced once.

Earlier fixes (v5.x) retained: working Epic claim links (productSlug is often null),
startup message sent from on_ready, current-window validation, robust channel access,
no privileged intents, "was $X -> FREE" flex, Discord dynamic-timestamp expiry,
quiet-seed on first run, optional instant guild sync.

Run with: python main.py
Deploy:   Docker / Railway ready (mount a volume so the .db persists).
"""

import os
import random
import asyncio
import logging
import sqlite3
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from dotenv import load_dotenv

load_dotenv()


# ============== CONFIG ==============
def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or 0)
PING_ROLE_ID = int(os.getenv("PING_ROLE_ID") or 0)
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES") or 15)
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
GUILD_ID = int(os.getenv("GUILD_ID") or 0)              # optional: instant slash sync for one guild

QUIET_SEED = _flag("QUIET_SEED", "true")               # silently record existing freebies on first run
MIN_WORTH = _float("MIN_WORTH", 0.0)                    # skip games worth less than this (0 = no filter)
EXPIRY_REMINDER_HOURS = int(os.getenv("EXPIRY_REMINDER_HOURS") or 6)  # 0 = disable last-chance pings
INCLUDE_LOOT = _flag("INCLUDE_LOOT", "false")          # also announce in-game loot / key giveaways
INCLUDE_BETA = _flag("INCLUDE_BETA", "false")          # also announce beta-access giveaways
POST_DELAY_SECONDS = _float("POST_DELAY_SECONDS", 1.5) # delay between announcements (rate-limit friendly)

# Claim & Share ("Win Share Kit")
ALLOW_PUBLIC_SHARE = _flag("ALLOW_PUBLIC_SHARE", "true")  # allow the "post my win to channel" button
SHARE_PROMO_URL = os.getenv("SHARE_PROMO_URL", "https://github.com/ghostfriend1/freegamenotifier")

DB_FILE = os.getenv("DB_FILE", "free_games_v3.db")

GAMERPOWER_BASE = "https://gamerpower.com/api"
EPIC_API = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
EPIC_LOCALE = os.getenv("EPIC_LOCALE", "en-US")
EPIC_COUNTRY = os.getenv("EPIC_COUNTRY", "US")

ALERT_ROLE_BUTTON_ID = "fgn:toggle_alert_role"
SHARE_WIN_BUTTON_ID = "fgn:share_win"
VERSION = "6.2.0"
BOT_NAME = "Free Game Notifier"

# Map a platform string to a clean social hashtag.
PLATFORM_HASHTAGS = {
    "epic": "EpicGames", "steam": "Steam", "gog": "GOG", "ubisoft": "Ubisoft",
    "ea": "EAApp", "itch": "itchio", "amazon": "PrimeGaming", "prime": "PrimeGaming",
}

# Randomized, copy-paste-ready share templates ({title}, {platform}, {platform_tag}).
SHARE_TEMPLATES = [
    "Just claimed **{title}** for FREE on {platform}! 🎮🔥\n\n"
    "Never missing another drop thanks to my free-game notifier. Join the hunt!\n\n"
    "#FreeGames #{platform_tag} #FreeGameNotifier",

    "Snagged **{title}** for $0 on {platform}! 🎁\n\n"
    "Free games delivered straight to my Discord — highly recommend.\n\n"
    "#FreeGames #{platform_tag} #GamingDeals",

    "Another W in the books — **{title}** is mine for FREE! 🏆\n\n"
    "Powered by the best free-game tracker in Discord. Don't sleep on these.\n\n"
    "#FreeGames #{platform_tag} #GamerPower",

    "Wallet stays full, library keeps growing 📈 — grabbed **{title}** free on {platform}!\n\n"
    "My Discord pings me the second these go live. 🔔\n\n"
    "#FreeGames #{platform_tag} #FreeGameFriday",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(BOT_NAME)

# Sample game used by /fgtest when nothing is currently free.
DEMO_GAME: Dict[str, Any] = {
    "id": "demo_sample",
    "title": "Demo Quest: Sample Edition",
    "platform": "PC",
    "description": "This is a sample announcement so you can preview embeds, buttons, "
                   "and channel permissions. It pings no one.",
    "image": None,
    "worth": "$19.99",
    "worth_value": 19.99,
    "end_date": None,
    "claim_url": "https://gamerpower.com",
    "details_url": "https://gamerpower.com",
    "kind_tag": "🎮 Game",
    "source": "Demo",
}


# ============== HELPERS ==============
def parse_dt(value: Any) -> Optional[datetime]:
    """Parse the date formats these APIs emit into an aware UTC datetime.

    Epic:       '2025-05-04T15:00:00.000Z'
    GamerPower: '2025-05-04 23:59:00' (UTC, no tz marker)
    Returns None if it can't be parsed.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("n/a", "none", "unknown"):
        return None
    candidate = s.replace("Z", "+00:00")
    if "T" not in candidate and " " in candidate:
        candidate = candidate.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def discord_ts(value: Any, style: str = "R", with_full: bool = True) -> Optional[str]:
    """Render a date as a Discord dynamic timestamp (localized, live countdown)."""
    dt = parse_dt(value)
    if dt is None:
        return str(value)[:40] if value else None
    unix = int(dt.timestamp())
    return f"<t:{unix}:{style}> (<t:{unix}:f>)" if with_full else f"<t:{unix}:{style}>"


def parse_worth(value: Any) -> Optional[float]:
    """'$59.99' / '1,299.00' -> float. 'N/A'/None/'$0' -> None."""
    if value is None:
        return None
    s = str(value).strip().lower().replace("$", "").replace(",", "").replace("usd", "").strip()
    if not s or s in ("n/a", "free", "0", "0.0", "0.00"):
        return None
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def norm_title(title: str) -> str:
    return " ".join(str(title).lower().split())


def clean_announcement_title(embed_title: Optional[str]) -> str:
    """Recover the bare game title from an announcement embed title."""
    if not embed_title:
        return "a free game"
    t = embed_title.removeprefix("🎁 ").removesuffix(" — FREE RIGHT NOW").strip()
    return t or "a free game"


def platform_hashtag(platform: str) -> str:
    """Turn a platform string into a clean #Hashtag-safe token."""
    p = str(platform).lower()
    for key, tag in PLATFORM_HASHTAGS.items():
        if key in p:
            return tag
    first = str(platform).split(",")[0]
    cleaned = "".join(ch for ch in first if ch.isalnum())
    return cleaned or "PC"


def build_share_text(title: str, platform: str, claim_url: Optional[str] = None) -> str:
    """Build copy-paste-ready social text (kept under 1000 chars for Discord)."""
    display_platform = str(platform).split(",")[0].strip() or "PC"
    text = random.choice(SHARE_TEMPLATES).format(
        title=title, platform=display_platform, platform_tag=platform_hashtag(platform))
    if claim_url and claim_url.startswith("http"):
        text += f"\n\n🔗 Claim it: {claim_url}"
    text += f"\n\n🎮 Get free-game alerts → {SHARE_PROMO_URL}"
    return text[:1000]


def extract_game_from_message(message: discord.Message) -> Dict[str, Any]:
    """Reconstruct game info from an announcement message (stateless share button).

    This is what lets the Share button keep working forever and across restarts:
    we read the title/platform/image/claim-link back from the message itself
    instead of relying on in-memory per-message state.
    """
    title, platform, image, claim_url = "a free game", "PC", None, None
    if message.embeds:
        e = message.embeds[0]
        title = clean_announcement_title(e.title)
        if e.thumbnail and e.thumbnail.url:
            image = e.thumbnail.url
        for field in e.fields:
            if field.name and "platform" in field.name.lower():
                platform = (field.value or platform).split(",")[0].strip()
    # Pull the "Claim Now" link from the message's buttons.
    for row in message.components:
        for comp in getattr(row, "children", []):
            url = getattr(comp, "url", None)
            label = (getattr(comp, "label", "") or "").lower()
            if url and "claim" in label:
                claim_url = url
    if not claim_url:
        for row in message.components:
            for comp in getattr(row, "children", []):
                if getattr(comp, "url", None):
                    claim_url = comp.url
                    break
            if claim_url:
                break
    return {"title": title, "platform": platform, "image": image, "claim_url": claim_url}


class Database:
    """SQLite persistence for permanently-seen giveaways, reminders, and stats."""

    def __init__(self, db_file: str = DB_FILE):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_games (
                giveaway_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                platform TEXT,
                source TEXT,
                announced INTEGER DEFAULT 1,
                last_posted TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                giveaway_id TEXT PRIMARY KEY,
                reminded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_stats (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_last_posted ON seen_games(last_posted)")
        # --- migrate older DBs (v4.x / v5.x had no 'announced' column) ---
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(seen_games)").fetchall()]
        if "announced" not in cols:
            self.conn.execute("ALTER TABLE seen_games ADD COLUMN announced INTEGER DEFAULT 1")
        self.conn.commit()

    # seen / dedup
    def has_been_announced(self, giveaway_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_games WHERE giveaway_id = ?", (giveaway_id,)
        ).fetchone()
        return row is not None

    def title_already_posted(self, title: str) -> bool:
        """True if a game with this title was actually announced (not just seeded)."""
        row = self.conn.execute(
            "SELECT 1 FROM seen_games WHERE LOWER(TRIM(title)) = ? AND announced = 1",
            (norm_title(title),),
        ).fetchone()
        return row is not None

    def mark_posted(self, giveaway_id: str, title: str, platform: str,
                    source: str = "Unknown", announced: bool = True) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO seen_games "
            "(giveaway_id, title, platform, source, announced, last_posted) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (giveaway_id, title, platform, source, 1 if announced else 0, now),
        )
        self.conn.commit()

    def get_announced_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM seen_games WHERE announced = 1").fetchone()
        return row[0] if row else 0

    def get_tracked_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM seen_games").fetchone()
        return row[0] if row else 0

    def get_source_breakdown(self) -> List[Tuple[str, int]]:
        return list(self.conn.execute(
            "SELECT source, COUNT(*) FROM seen_games WHERE announced = 1 "
            "GROUP BY source ORDER BY COUNT(*) DESC"
        ).fetchall())

    # reminders
    def has_been_reminded(self, giveaway_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM reminders WHERE giveaway_id = ?", (giveaway_id,)
        ).fetchone()
        return row is not None

    def mark_reminded(self, giveaway_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO reminders (giveaway_id, reminded_at) VALUES (?, ?)",
            (giveaway_id, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    # generic stats
    def get_stat(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM bot_stats WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_stat(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_stats (key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    def update_last_check(self) -> None:
        self.set_stat("last_check", datetime.now(timezone.utc).isoformat())

    def get_last_check(self) -> Optional[str]:
        return self.get_stat("last_check")

    def add_given_away_value(self, worth_value: Optional[float]) -> None:
        if not worth_value or worth_value <= 0:
            return
        current = 0.0
        try:
            current = float(self.get_stat("total_worth_usd") or 0)
        except ValueError:
            current = 0.0
        self.set_stat("total_worth_usd", str(round(current + worth_value, 2)))

    def get_total_given_away(self) -> float:
        try:
            return float(self.get_stat("total_worth_usd") or 0)
        except ValueError:
            return 0.0

    def increment_counter(self, name: str, by: int = 1) -> int:
        key = f"count_{name}"
        try:
            current = int(self.get_stat(key) or 0)
        except ValueError:
            current = 0
        current += by
        self.set_stat(key, str(current))
        return current

    def get_counter(self, name: str) -> int:
        try:
            return int(self.get_stat(f"count_{name}") or 0)
        except ValueError:
            return 0

    # runtime config (overrides .env at run time, persisted across restarts)
    def get_config(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM bot_config WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_config(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self.conn.execute("DELETE FROM bot_config WHERE key = ?", (key,))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def all_config(self) -> Dict[str, str]:
        return {k: v for k, v in self.conn.execute("SELECT key, value FROM bot_config").fetchall()}

    def close(self) -> None:
        self.conn.close()


class GameActionView(discord.ui.View):
    """Announcement buttons: Claim / Trends / Details (links) + a persistent
    'Share My Win!' button.

    The view is persistent (timeout=None) and the Share button is STATELESS — its
    callback rebuilds the game from the message, so it keeps working forever and
    after restarts. Link-button URLs differ per message; the persistent template
    registered via bot.add_view() only needs to own the Share custom_id.
    """

    def __init__(self,
                 claim_url: str = "https://gamerpower.com",
                 trends_url: str = "https://trends.google.com",
                 details_url: str = "https://gamerpower.com"):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="🎮 Claim Now", url=claim_url,
                                        style=discord.ButtonStyle.link, row=0))
        self.add_item(discord.ui.Button(label="📈 Google Trends", url=trends_url,
                                        style=discord.ButtonStyle.link, row=0))
        self.add_item(discord.ui.Button(label="📋 Details", url=details_url,
                                        style=discord.ButtonStyle.link, row=0))

    @discord.ui.button(label="🎉 Share My Win!", style=discord.ButtonStyle.success,
                       custom_id=SHARE_WIN_BUTTON_ID, row=1)
    async def share(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        game = extract_game_from_message(interaction.message)
        try:
            bot.db.increment_counter("shares")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - never let stats break the UX
            pass

        share_text = build_share_text(game["title"], game["platform"], game.get("claim_url"))
        embed = discord.Embed(
            title=f"🎉 Share your {game['title']} win!",
            description="Copy the text below and post it on X, Bluesky, Threads — anywhere.",
            color=0x00C853, timestamp=datetime.now(timezone.utc))
        if game.get("image"):
            embed.set_thumbnail(url=game["image"])
        embed.add_field(name="📣 Ready-to-post text", value=f"```\n{share_text}\n```", inline=False)
        embed.add_field(name="💡 Pro tip",
                        value="Attach the game's thumbnail for max engagement, and tag a friend "
                              "who loves free games!", inline=False)
        embed.set_footer(text=f"{BOT_NAME} v{VERSION} • Spread the free-game love!")

        view = SharePostView(game["title"], game.get("image")) if ALLOW_PUBLIC_SHARE else None
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class SharePostView(discord.ui.View):
    """Optional 'post my win publicly' button shown inside the ephemeral share kit."""

    def __init__(self, title: str, image: Optional[str]):
        super().__init__(timeout=600)
        self.title = title
        self.image = image

    @discord.ui.button(label="📢 Post my win to the channel", style=discord.ButtonStyle.primary)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        celebration = discord.Embed(
            description=f"🎉 {interaction.user.mention} just claimed **{self.title}** for FREE! 🎮",
            color=0x00C853)
        if self.image:
            celebration.set_thumbnail(url=self.image)
        celebration.set_footer(text=f"Shared via {BOT_NAME} • /currentfree for more freebies")
        try:
            await interaction.channel.send(
                embed=celebration,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            try:
                bot.db.increment_counter("public_posts")  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            button.disabled = True  # one flex per kit, no spam
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("✅ Posted to the channel — nice flex!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to post in this channel.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Couldn't post: {e}", ephemeral=True)


class ConfirmClearView(discord.ui.View):
    """Confirm/cancel gate for the destructive /fgclear command."""

    def __init__(self, amount: int, requester_id: int):
        super().__init__(timeout=60)
        self.amount = amount
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("These buttons aren't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🧹 Yes, delete them", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        bot = interaction.client
        try:
            deleted = await bot._clear_own_messages(interaction.channel, self.amount)  # type: ignore[attr-defined]
            await interaction.edit_original_response(
                content=f"🧹 Done — removed **{deleted}** of my message(s).", embed=None, view=None)
        except discord.Forbidden:
            await interaction.edit_original_response(
                content="❌ I need **Manage Messages** + **Read Message History** here to do that.",
                embed=None, view=None)
        except discord.HTTPException as e:
            await interaction.edit_original_response(
                content=f"❌ Something went wrong: {e}", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="✖️ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Cancelled — nothing was deleted.", embed=None, view=None)
        self.stop()


class NotifyRoleView(discord.ui.View):
    """Persistent self-serve button to opt in/out of the free-game ping role."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔔 Toggle Free-Game Alerts",
                       style=discord.ButtonStyle.primary,
                       custom_id=ALERT_ROLE_BUTTON_ID)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_id = interaction.client.ping_role_id()  # type: ignore[attr-defined]
        if not role_id:
            await interaction.response.send_message(
                "⚠️ The alert role isn't configured (set `PING_ROLE_ID` or use `/fgconfig`).",
                ephemeral=True)
            return
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this inside a server.", ephemeral=True)
            return
        role = guild.get_role(role_id)
        if role is None:
            await interaction.response.send_message("❌ Alert role not found on this server.", ephemeral=True)
            return
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Free-game alerts opt-out")
                await interaction.response.send_message(
                    "🔕 Done — you'll no longer be pinged for free games.", ephemeral=True)
            else:
                await member.add_roles(role, reason="Free-game alerts opt-in")
                await interaction.response.send_message(
                    "🔔 You're in! You'll be pinged when new free games drop.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I can't manage that role. I need **Manage Roles**, and my role must sit "
                "**above** the alert role in Server Settings → Roles.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.response.send_message(f"❌ Something went wrong: {e}", ephemeral=True)


class FreeGameNotifier(commands.Bot):
    """Free Game Notifier bot."""

    def __init__(self):
        intents = discord.Intents.default()  # message_content NOT required (slash-only)
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db = Database()
        self.session: Optional[aiohttp.ClientSession] = None
        self.start_time: datetime = datetime.now(timezone.utc)
        self._startup_announced = False

    # ---------- runtime config resolvers (DB overrides .env) ----------
    def ping_role_id(self) -> int:
        v = self.db.get_config("ping_role_id")
        return int(v) if v is not None else PING_ROLE_ID

    def min_worth(self) -> float:
        v = self.db.get_config("min_worth")
        try:
            return float(v) if v is not None else MIN_WORTH
        except ValueError:
            return MIN_WORTH

    def reminder_hours(self) -> int:
        v = self.db.get_config("reminder_hours")
        try:
            return int(v) if v is not None else EXPIRY_REMINDER_HOURS
        except ValueError:
            return EXPIRY_REMINDER_HOURS

    def include_loot(self) -> bool:
        v = self.db.get_config("include_loot")
        return (v == "1") if v is not None else INCLUDE_LOOT

    def include_beta(self) -> bool:
        v = self.db.get_config("include_beta")
        return (v == "1") if v is not None else INCLUDE_BETA

    def ping_token(self) -> str:
        rid = self.ping_role_id()
        return f"<@&{rid}>" if rid else "@here"

    # ---------- lifecycle ----------
    async def setup_hook(self) -> None:
        if not DISCORD_TOKEN or CHANNEL_ID == 0:
            logger.critical("❌ Missing DISCORD_TOKEN or CHANNEL_ID — aborting.")
            return

        self.session = aiohttp.ClientSession(
            headers={"User-Agent": f"{BOT_NAME}/{VERSION} (+https://github.com/ghostfriend1/freegamenotifier)"}
        )

        self.add_view(NotifyRoleView())   # persistent: alert opt-in button
        self.add_view(GameActionView())    # persistent: 'Share My Win!' button on announcements
        self.register_slash_commands()

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info(f"📌 Slash commands synced to guild {GUILD_ID} (instant).")
        else:
            await self.tree.sync()
            logger.info("📌 Slash commands synced globally (may take up to ~1h to appear).")

        self.check_free_games.start()
        logger.info(f"✅ {BOT_NAME} v{VERSION} initialized "
                    f"(min_worth=${MIN_WORTH:g}, reminders={EXPIRY_REMINDER_HOURS}h, "
                    f"loot={INCLUDE_LOOT}, beta={INCLUDE_BETA}).")

    def register_slash_commands(self) -> None:
        @self.tree.command(name="fgstatus", description="📊 Bot health: uptime, tracked games, last check.")
        async def fgstatus(interaction: discord.Interaction):
            await self._handle_fgstatus(interaction)

        @self.tree.command(name="currentfree", description="🎮 List currently free PC games right now.")
        async def currentfree(interaction: discord.Interaction):
            await self._handle_currentfree(interaction)

        @self.tree.command(name="upcoming", description="🔮 See next week's free Epic Games.")
        async def upcoming(interaction: discord.Interaction):
            await self._handle_upcoming(interaction)

        @self.tree.command(name="fgstats", description="📈 Lifetime stats: games & $ value given away.")
        async def fgstats(interaction: discord.Interaction):
            await self._handle_fgstats(interaction)

        @self.tree.command(name="sharewin", description="🎉 Generate a share kit for a currently free game.")
        @app_commands.describe(game="Part of a game's title (optional — defaults to the top freebie).")
        async def sharewin(interaction: discord.Interaction, game: Optional[str] = None):
            await self._handle_sharewin(interaction, game)

        @self.tree.command(name="fghelp", description="❓ List everything this bot can do.")
        async def fghelp(interaction: discord.Interaction):
            await self._handle_fghelp(interaction)

        @self.tree.command(name="fginvite", description="➕ Get an invite link with the right permissions.")
        async def fginvite(interaction: discord.Interaction):
            await self._handle_fginvite(interaction)

        @self.tree.command(name="fgclear", description="🧹 Delete the bot's own recent messages here.")
        @app_commands.describe(amount="How many of MY recent messages to remove (default 50, max 200).")
        async def fgclear(interaction: discord.Interaction, amount: Optional[int] = 50):
            await self._handle_fgclear(interaction, amount or 50)

        @self.tree.command(name="fgtest", description="🧪 Post a sample announcement to preview it (owner only).")
        async def fgtest(interaction: discord.Interaction):
            await self._handle_fgtest(interaction)

        @self.tree.command(name="fgconfig", description="⚙️ View or change settings at runtime (owner only).")
        @app_commands.describe(
            ping_role="Role to ping on new games (clears to @here if you pick @everyone).",
            min_worth="Only announce games worth at least this (USD). 0 = off.",
            reminder_hours="Hours before expiry to send a last-chance ping. 0 = off.",
            include_loot="Also announce free in-game loot / keys.",
            include_beta="Also announce free beta access.")
        async def fgconfig(interaction: discord.Interaction,
                           ping_role: Optional[discord.Role] = None,
                           min_worth: Optional[float] = None,
                           reminder_hours: Optional[int] = None,
                           include_loot: Optional[bool] = None,
                           include_beta: Optional[bool] = None):
            await self._handle_fgconfig(interaction, ping_role, min_worth,
                                        reminder_hours, include_loot, include_beta)

        @self.tree.command(name="fgpanel", description="🔔 Post the alert opt-in panel (owner only).")
        async def fgpanel(interaction: discord.Interaction):
            await self._handle_fgpanel(interaction)

        @self.tree.command(name="forcecheck", description="🔧 Force an immediate scan (owner only).")
        async def forcecheck(interaction: discord.Interaction):
            await self._handle_forcecheck(interaction)

        logger.info("📌 Commands: /fgstatus /currentfree /upcoming /fgstats /sharewin /fghelp "
                    "/fginvite /fgclear /fgtest /fgconfig /fgpanel /forcecheck")

    async def on_ready(self) -> None:
        logger.info(f"🎉 Logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching,
                                      name="for free PC games 🎮 | /fgstatus")
        )
        if not self._startup_announced:
            self._startup_announced = True
            channel = await self._get_channel()
            if channel:
                try:
                    await channel.send(
                        f"🚀 **{BOT_NAME} v{VERSION}** is online!\n"
                        f"Monitoring **currently free PC games** from GamerPower + Epic Games Store.\n"
                        f"Try `/currentfree`, `/upcoming`, or `/fgstats`."
                    )
                except discord.HTTPException as e:
                    logger.warning(f"Could not send startup message: {e}")
            else:
                logger.warning(f"Startup message skipped: channel {CHANNEL_ID} not reachable.")

    async def close(self) -> None:
        logger.info("🛑 Shutting down bot...")
        if self.session and not self.session.closed:
            await self.session.close()
        self.db.close()
        await super().close()

    async def _get_channel(self) -> Optional[discord.abc.Messageable]:
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(CHANNEL_ID)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                logger.error(f"❌ Could not access channel {CHANNEL_ID}: {e}")
                return None
        return channel  # type: ignore[return-value]

    async def _clear_own_messages(self, channel: discord.abc.Messageable, amount: int) -> int:
        """Delete up to `amount` of the bot's OWN recent messages in a channel.

        Handles Discord's 14-day bulk-delete limit: newer messages are bulk
        deleted (fast), older ones are removed individually (slower). Falls back
        to single deletes if the bot lacks Manage Messages.
        """
        me = self.user.id  # type: ignore[union-attr]
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        collected: List[discord.Message] = []
        async for msg in channel.history(limit=1000):
            if msg.author.id == me:
                collected.append(msg)
                if len(collected) >= amount:
                    break

        recent = [m for m in collected if m.created_at > cutoff]
        old = [m for m in collected if m.created_at <= cutoff]
        deleted = 0

        i = 0
        while i < len(recent):
            chunk = recent[i:i + 100]
            i += 100
            try:
                if len(chunk) == 1:
                    await chunk[0].delete()
                else:
                    await channel.delete_messages(chunk)  # type: ignore[attr-defined]
                deleted += len(chunk)
            except discord.Forbidden:
                for m in chunk:  # no Manage Messages -> delete our own one by one
                    try:
                        await m.delete()
                        deleted += 1
                        await asyncio.sleep(0.7)
                    except discord.HTTPException:
                        pass
            except discord.HTTPException:
                pass

        for m in old:  # >14 days: must be single deletes
            try:
                await m.delete()
                deleted += 1
                await asyncio.sleep(0.7)
            except discord.HTTPException:
                pass
        return deleted

    # ---------- core check ----------
    async def _perform_free_game_check(self) -> int:
        posted_count = 0
        try:
            logger.info("🔍 Scanning for currently free PC games...")
            current_games = await self.fetch_current_free_games()

            channel = await self._get_channel()
            if not channel:
                logger.error("❌ Target channel not found or bot lacks access.")
                self.db.update_last_check()
                return 0

            # First-ever run against an empty DB: optionally seed silently.
            if QUIET_SEED and self.db.get_tracked_count() == 0 and current_games:
                for game in current_games:
                    self.db.mark_posted(game["id"], game["title"], game.get("platform", "PC"),
                                        game.get("source", "Unknown"), announced=False)
                logger.info(f"🌱 Quiet seed: recorded {len(current_games)} existing freebie(s) "
                            f"without announcing (first run).")
                self.db.update_last_check()
                return 0

            ping = self.ping_token()
            allowed = discord.AllowedMentions(everyone=True, roles=True, users=False)
            min_worth = self.min_worth()

            for game in current_games:
                gid = game["id"]
                if self.db.has_been_announced(gid) or self.db.title_already_posted(game["title"]):
                    continue
                wv = game.get("worth_value")
                if min_worth > 0 and wv is not None and wv < min_worth:
                    self.db.mark_posted(gid, game["title"], game.get("platform", "PC"),
                                        game.get("source", "Unknown"), announced=False)
                    continue
                embed, view = self.create_game_embed(game)
                try:
                    await channel.send(content=f"{ping} **NEW FREE PC GAME!**",
                                       embed=embed, view=view, allowed_mentions=allowed)
                    self.db.mark_posted(gid, game["title"], game.get("platform", "PC"),
                                        game.get("source", "Unknown"), announced=True)
                    self.db.add_given_away_value(wv)
                    posted_count += 1
                    if POST_DELAY_SECONDS > 0:
                        await asyncio.sleep(POST_DELAY_SECONDS)
                except discord.HTTPException as http_err:
                    logger.error(f"Failed to post embed for {game['title']}: {http_err}")

            # Last-chance reminders for games expiring soon.
            await self._send_expiry_reminders(current_games, channel)

            if posted_count:
                logger.info(f"🚀 Announced {posted_count} new free game(s).")
            else:
                logger.info("No new games to announce this cycle.")
            self.db.update_last_check()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"💥 Error during free game check: {e}")
        return posted_count

    async def _send_expiry_reminders(self, games: List[Dict[str, Any]],
                                     channel: discord.abc.Messageable) -> None:
        reminder_hours = self.reminder_hours()
        if reminder_hours <= 0:
            return
        now = datetime.now(timezone.utc)
        soon = now + timedelta(hours=reminder_hours)
        allowed = discord.AllowedMentions(everyone=True, roles=True, users=False)
        ping = self.ping_token()
        for game in games:
            end = parse_dt(game.get("end_date"))
            if not end or not (now < end <= soon):
                continue
            gid = game["id"]
            # Only remind about games we actually announced, and only once.
            if not self.db.title_already_posted(game["title"]):
                continue
            if self.db.has_been_reminded(gid):
                continue
            try:
                await channel.send(
                    content=f"⏰ {ping} **Last chance!** **{game['title']}** is still free — "
                            f"ends {discord_ts(game.get('end_date'), 'R', with_full=False)}.\n"
                            f"[Claim it now →]({game.get('claim_url', 'https://gamerpower.com')})",
                    allowed_mentions=allowed,
                )
                self.db.mark_reminded(gid)
                if POST_DELAY_SECONDS > 0:
                    await asyncio.sleep(POST_DELAY_SECONDS)
            except discord.HTTPException as e:
                logger.error(f"Failed to send reminder for {game['title']}: {e}")

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES, reconnect=True)
    async def check_free_games(self):
        await self._perform_free_game_check()

    @check_free_games.before_loop
    async def before_check_loop(self):
        await self.wait_until_ready()

    # ---------- fetching ----------
    async def _fetch_with_retry(self, url: str, params: Optional[Dict[str, Any]] = None,
                                max_retries: int = 3) -> Any:
        assert self.session is not None, "HTTP session not initialised"
        for attempt in range(max_retries):
            try:
                async with self.session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=25)
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == max_retries - 1:
                    logger.error(f"❌ Final retry failed for {url}: {e}")
                    raise
                wait = (2 ** attempt) * 1.2
                logger.warning(f"Retry {attempt + 1}/{max_retries} for {url} in {wait:.1f}s...")
                await asyncio.sleep(wait)
        return None

    @staticmethod
    def _epic_store_url(elem: Dict[str, Any]) -> str:
        slug = None
        for mapping in (elem.get("offerMappings") or []):
            if mapping.get("pageSlug"):
                slug = mapping["pageSlug"]
                break
        if not slug:
            for mapping in ((elem.get("catalogNs") or {}).get("mappings") or []):
                if mapping.get("pageSlug"):
                    slug = mapping["pageSlug"]
                    break
        if not slug:
            slug = elem.get("productSlug") or elem.get("urlSlug")
        locale = EPIC_LOCALE.lower()
        if slug:
            return f"https://store.epicgames.com/{locale}/p/{slug}"
        namespace, offer_id = elem.get("namespace"), elem.get("id")
        if namespace and offer_id:
            return f"https://store.epicgames.com/{locale}/purchase?offers=1-{namespace}-{offer_id}"
        return "https://store.epicgames.com/free-games"

    @staticmethod
    def _epic_original_price(elem: Dict[str, Any]) -> Optional[str]:
        try:
            fmt = elem["price"]["totalPrice"]["fmtPrice"]
            original = fmt.get("originalPrice")
            if original and original not in ("0",):
                return original
        except (KeyError, TypeError):
            pass
        return None

    @staticmethod
    def _epic_image(elem: Dict[str, Any]) -> Optional[str]:
        key_images = elem.get("keyImages") or []
        for wanted in ("OfferImageWide", "DieselStoreFrontWide", "Thumbnail", "OfferImageTall"):
            for img in key_images:
                if img.get("type") == wanted and img.get("url"):
                    return img["url"]
        return key_images[0].get("url") if key_images else None

    @staticmethod
    def _extract_epic(elem: Dict[str, Any], kind: str = "current") -> Optional[Dict[str, Any]]:
        """Build a normalized game dict from an Epic element for 'current' or 'upcoming' free offers."""
        if elem.get("offerType") == "ADD_ON":
            return None
        promotions = elem.get("promotions") or {}
        key = "promotionalOffers" if kind == "current" else "upcomingPromotionalOffers"
        now = datetime.now(timezone.utc)
        match, start_date, end_date = False, None, None
        for offer_set in (promotions.get(key) or []):
            for offer in (offer_set.get("promotionalOffers") or []):
                disc = (offer.get("discountSetting") or {}).get("discountPercentage")
                if disc != 0:
                    continue
                s, e = parse_dt(offer.get("startDate")), parse_dt(offer.get("endDate"))
                if kind == "current":
                    if (s is None or s <= now) and (e is None or now <= e):
                        match, end_date = True, offer.get("endDate")
                        break
                else:
                    if s and s > now:
                        match, start_date, end_date = True, offer.get("startDate"), offer.get("endDate")
                        break
            if match:
                break
        if not match:
            return None
        title = str(elem.get("title", "")).strip()
        if not title or title.lower() == "mystery game":
            return None
        original = FreeGameNotifier._epic_original_price(elem)
        url = FreeGameNotifier._epic_store_url(elem)
        return {
            "id": f"epic_{elem.get('id') or elem.get('productSlug') or title}",
            "title": title,
            "platform": "Epic Games Store",
            "description": str(elem.get("description") or "Free on Epic this week!")[:450],
            "image": FreeGameNotifier._epic_image(elem),
            "worth": original,
            "worth_value": parse_worth(original),
            "start_date": start_date,
            "end_date": end_date,
            "claim_url": url,
            "details_url": url,
            "source": "Epic Games",
        }

    def _gamerpower_types(self) -> str:
        types = ["game"]
        if self.include_loot():
            types.append("loot")
        if self.include_beta():
            types.append("beta")
        return "+".join(types)  # GamerPower /filter groups with '+'

    async def _fetch_gamerpower(self) -> List[Dict[str, Any]]:
        games: List[Dict[str, Any]] = []
        try:
            type_param = self._gamerpower_types()
            if type_param == "game":
                data = await self._fetch_with_retry(
                    f"{GAMERPOWER_BASE}/giveaways",
                    {"type": "game", "platform": "pc", "sort-by": "value"})
            else:
                data = await self._fetch_with_retry(
                    f"{GAMERPOWER_BASE}/filter",
                    {"platform": "pc", "type": type_param, "sort-by": "value"})
            if isinstance(data, list):
                for item in data:
                    platforms_str = str(item.get("platforms", "")).lower()
                    if not any(p in platforms_str for p in
                               ("pc", "steam", "gog", "epic", "ubisoft", "windows", "itch", "ea")):
                        continue
                    gid = str(item.get("id") or item.get("giveaway_id") or "").strip()
                    if not gid:
                        continue
                    worth = item.get("worth")
                    gtype = str(item.get("type", "game")).lower()
                    tag = {"loot": "🎁 Loot", "beta": "🧪 Beta"}.get(gtype, "🎮 Game")
                    games.append({
                        "id": f"gp_{gid}",
                        "title": str(item.get("title", "Unknown Game")).strip(),
                        "platform": item.get("platforms", "PC"),
                        "description": str(item.get("instructions") or item.get("description")
                                           or "No description.")[:450],
                        "image": item.get("image") or item.get("thumbnail"),
                        "worth": worth if (worth and str(worth).upper() != "N/A") else None,
                        "worth_value": parse_worth(worth),
                        "end_date": item.get("end_date"),
                        "claim_url": item.get("open_giveaway_url")
                        or item.get("open_giveaway") or f"https://gamerpower.com/giveaway/{gid}",
                        "details_url": f"https://gamerpower.com/giveaway/{gid}",
                        "kind_tag": tag,
                        "source": "GamerPower",
                    })
        except Exception as e:  # noqa: BLE001
            logger.error(f"GamerPower API error: {e}")
        return games

    async def _fetch_epic(self, kind: str = "current") -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            epic_data = await self._fetch_with_retry(
                EPIC_API, {"locale": EPIC_LOCALE, "country": EPIC_COUNTRY, "allowCountries": EPIC_COUNTRY})
            if isinstance(epic_data, dict) and isinstance(epic_data.get("data"), dict):
                elements = (epic_data["data"].get("Catalog", {})
                            .get("searchStore", {}).get("elements", []) or [])
                for elem in elements:
                    g = self._extract_epic(elem, kind=kind)
                    if g:
                        out.append(g)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Epic Games API error ({kind}): {e}")
        return out

    @staticmethod
    def _dedupe_prefer_richer(games: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Collapse the same title from different sources into one entry.

        Prefers the Epic-sourced entry (best claim link), otherwise the one with
        a price and image. Deterministic so DB ids stay stable across cycles.
        """
        best: Dict[str, Dict[str, Any]] = {}

        def score(g: Dict[str, Any]) -> tuple:
            return (1 if g.get("source") == "Epic Games" else 0,
                    1 if g.get("worth_value") else 0,
                    1 if g.get("image") else 0)

        for g in games:
            key = norm_title(g["title"])
            if key not in best or score(g) > score(best[key]):
                best[key] = g
        return list(best.values())

    async def fetch_current_free_games(self) -> List[Dict[str, Any]]:
        gp, epic = await asyncio.gather(self._fetch_gamerpower(), self._fetch_epic("current"))
        unique = self._dedupe_prefer_richer(gp + epic)
        logger.info(f"Fetched {len(unique)} unique currently-free item(s) across sources.")
        return unique

    async def _gamerpower_worth_now(self) -> Tuple[Optional[str], Optional[int]]:
        try:
            data = await self._fetch_with_retry(f"{GAMERPOWER_BASE}/worth", {"platform": "pc", "type": "game"})
            if isinstance(data, dict):
                return data.get("worth_estimation_usd"), data.get("active_giveaways_number")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"GamerPower /worth failed: {e}")
        return None, None

    # ---------- presentation ----------
    def create_game_embed(self, game: Dict[str, Any]) -> Tuple[discord.Embed, GameActionView]:
        title = game.get("title", "Unknown Game")
        claim_url = game.get("claim_url") or "https://gamerpower.com"
        details_url = game.get("details_url") or "https://gamerpower.com"
        trends_url = f"https://trends.google.com/trends/explore?q={urllib.parse.quote_plus(title)}"

        kind_tag = game.get("kind_tag", "🎮 Game")
        embed = discord.Embed(
            title=f"🎁 {title} — FREE RIGHT NOW",
            description=game.get("description", "No description available."),
            color=0x00C853,
            timestamp=datetime.now(timezone.utc),
        )
        if game.get("image"):
            embed.set_thumbnail(url=game["image"])

        worth = game.get("worth")
        worth_display = f"~~{worth}~~ → **FREE**" if worth else "**FREE**"
        embed.add_field(name="💰 Worth", value=worth_display, inline=True)
        embed.add_field(name="🖥️ Platform", value=str(game.get("platform", "PC")), inline=True)
        embed.add_field(name="📦 Type", value=kind_tag, inline=True)

        expiry = discord_ts(game.get("end_date"))
        if expiry:
            embed.add_field(name="⏰ Expires", value=expiry, inline=False)

        embed.add_field(name="🔗 Quick Links",
                        value=f"[Claim Now]({claim_url}) • [Trends]({trends_url}) • [Details]({details_url})",
                        inline=False)
        embed.set_footer(text=f"{BOT_NAME} v{VERSION} • {game.get('source', 'GamerPower')} • Stay frosty 🧊")
        return embed, GameActionView(claim_url, trends_url, details_url)

    # ---------- slash command handlers ----------
    async def _handle_fgstatus(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        uptime_str = str(datetime.now(timezone.utc) - self.start_time).split(".")[0]
        last_check = self.db.get_last_check()

        embed = discord.Embed(title=f"📊 {BOT_NAME} v{VERSION} Status",
                              color=0x5865F2, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="⏱️ Uptime", value=uptime_str, inline=True)
        embed.add_field(name="🔄 Interval", value=f"{CHECK_INTERVAL_MINUTES} min", inline=True)
        embed.add_field(name="📶 Latency", value=f"{round(self.latency * 1000)} ms", inline=True)
        embed.add_field(name="🎮 Announced", value=str(self.db.get_announced_count()), inline=True)
        embed.add_field(name="🕒 Last Check", value=(last_check[:19] if last_check else "Never"), inline=True)
        embed.add_field(name="📍 Channel", value=f"<#{CHANNEL_ID}>", inline=True)
        rid = self.ping_role_id()
        embed.add_field(name="🔔 Ping", value=(f"<@&{rid}>" if rid else "@here"), inline=True)
        embed.add_field(name="🌍 Epic Region", value=f"{EPIC_LOCALE} / {EPIC_COUNTRY}", inline=True)
        mw = self.min_worth()
        embed.add_field(name="💵 Min Worth", value=(f"${mw:g}" if mw else "Off"), inline=True)
        rh = self.reminder_hours()
        embed.add_field(name="⏰ Reminders",
                        value=(f"{rh}h before end" if rh else "Off"), inline=True)
        extras = ", ".join([t for t, on in (("Loot", self.include_loot()),
                                             ("Beta", self.include_beta())) if on]) or "Games only"
        embed.add_field(name="🎁 Categories", value=extras, inline=False)
        embed.set_footer(text="Dedup: SQLite (permanent) • Sources: GamerPower + Epic")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _handle_currentfree(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            games = await self.fetch_current_free_games()
            if not games:
                await interaction.followup.send(
                    "😔 No currently free PC games found right now. The hunt continues!", ephemeral=True)
                return
            games.sort(key=lambda g: (g.get("worth_value") or 0), reverse=True)
            embed = discord.Embed(
                title="🎮 Currently Free PC Games",
                description=f"Showing **{min(len(games), 8)}** of **{len(games)}** active freebie(s), "
                            f"highest value first.",
                color=0x00C853, timestamp=datetime.now(timezone.utc))
            for i, game in enumerate(games[:8], 1):
                worth = game.get("worth")
                worth_line = f"**Worth:** ~~{worth}~~ → FREE\n" if worth else ""
                expiry = discord_ts(game.get("end_date"), "R", with_full=False)
                expiry_line = f"**Ends:** {expiry}\n" if expiry else ""
                claim = game.get("claim_url", "#")
                embed.add_field(name=f"{i}. {game['title'][:60]}",
                                value=(f"**Platform:** {game.get('platform', 'PC')}\n"
                                       f"{worth_line}{expiry_line}[Claim →]({claim})"),
                                inline=True)
            if len(games) > 8:
                embed.set_footer(text=f"+ {len(games) - 8} more • {BOT_NAME} v{VERSION}")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:  # noqa: BLE001
            logger.error(f"/currentfree error: {e}")
            await interaction.followup.send("❌ Error fetching current games. Try again later.", ephemeral=True)

    async def _handle_upcoming(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            games = await self._fetch_epic("upcoming")
            if not games:
                await interaction.followup.send(
                    "🔮 No upcoming free Epic games announced yet — check back soon!", ephemeral=True)
                return
            games.sort(key=lambda g: parse_dt(g.get("start_date")) or datetime.max.replace(tzinfo=timezone.utc))
            embed = discord.Embed(
                title="🔮 Coming Soon — Free on Epic",
                description="Mark your calendar! These go free shortly:",
                color=0x9B59B6, timestamp=datetime.now(timezone.utc))
            for i, game in enumerate(games[:8], 1):
                worth = game.get("worth")
                worth_line = f"~~{worth}~~ → FREE\n" if worth else ""
                starts = discord_ts(game.get("start_date"), "R", with_full=False)
                start_line = f"**Free from:** {starts}\n" if starts else ""
                embed.add_field(name=f"{i}. {game['title'][:60]}",
                                value=f"{worth_line}{start_line}[Preview →]({game.get('claim_url', '#')})",
                                inline=True)
            embed.set_footer(text=f"{BOT_NAME} v{VERSION} • Epic upcoming promotions")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:  # noqa: BLE001
            logger.error(f"/upcoming error: {e}")
            await interaction.followup.send("❌ Error fetching upcoming games. Try again later.", ephemeral=True)

    async def _handle_fgstats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        announced = self.db.get_announced_count()
        given = self.db.get_total_given_away()
        worth_now, count_now = await self._gamerpower_worth_now()
        breakdown = self.db.get_source_breakdown()

        embed = discord.Embed(title="📈 Free Game Notifier — Lifetime Stats",
                              color=0xF1C40F, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="🎮 Games Announced", value=f"**{announced}**", inline=True)
        embed.add_field(name="💸 Value Given Away", value=f"**${given:,.2f}**", inline=True)
        shares = self.db.get_counter("shares")
        if shares:
            embed.add_field(name="🎉 Wins Shared", value=f"**{shares}**", inline=True)
        posts = self.db.get_counter("public_posts")
        if posts:
            embed.add_field(name="📢 Public Flexes", value=f"**{posts}**", inline=True)
        if count_now is not None:
            embed.add_field(name="🟢 Free Right Now", value=f"{count_now} games", inline=True)
        if worth_now:
            embed.add_field(name="💰 Value Available Now", value=str(worth_now), inline=True)
        if breakdown:
            lines = "\n".join(f"• {src or 'Unknown'}: {cnt}" for src, cnt in breakdown[:5])
            embed.add_field(name="📡 By Source", value=lines, inline=False)
        uptime = str(datetime.now(timezone.utc) - self.start_time).split(".")[0]
        embed.set_footer(text=f"Uptime {uptime} • {BOT_NAME} v{VERSION}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _handle_sharewin(self, interaction: discord.Interaction, query: Optional[str]) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            games = await self.fetch_current_free_games()
            if not games:
                await interaction.followup.send(
                    "😔 Nothing's free right now, so there's no win to share yet!", ephemeral=True)
                return
            if query:
                matches = [g for g in games if query.lower() in g["title"].lower()]
                if not matches:
                    titles = ", ".join(g["title"] for g in games[:8])
                    await interaction.followup.send(
                        f"🔍 No current freebie matches “{query}”. Currently free: {titles}", ephemeral=True)
                    return
                target = matches[0]
            else:
                target = max(games, key=lambda g: (g.get("worth_value") or 0))

            self.db.increment_counter("shares")
            share_text = build_share_text(target["title"], target.get("platform", "PC"),
                                          target.get("claim_url"))
            embed = discord.Embed(
                title=f"🎉 Share your {target['title']} win!",
                description="Copy the text below and post it anywhere.",
                color=0x00C853, timestamp=datetime.now(timezone.utc))
            if target.get("image"):
                embed.set_thumbnail(url=target["image"])
            embed.add_field(name="📣 Ready-to-post text", value=f"```\n{share_text}\n```", inline=False)
            embed.set_footer(text=f"{BOT_NAME} v{VERSION} • Spread the free-game love!")
            view = SharePostView(target["title"], target.get("image")) if ALLOW_PUBLIC_SHARE else None
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:  # noqa: BLE001
            logger.error(f"/sharewin error: {e}")
            await interaction.followup.send("❌ Couldn't build a share kit right now.", ephemeral=True)

    async def _handle_fghelp(self, interaction: discord.Interaction) -> None:
        is_owner = interaction.user.id == OWNER_ID
        embed = discord.Embed(
            title=f"❓ {BOT_NAME} v{VERSION} — Commands",
            description="Your free-game hunting companion. Here's what I can do:",
            color=0x5865F2)
        embed.add_field(
            name="🎮 Everyone",
            value=("`/currentfree` — what's free right now\n"
                   "`/upcoming` — next week's free Epic games\n"
                   "`/fgstats` — lifetime stats & community shares\n"
                   "`/sharewin` — make a share kit for a freebie\n"
                   "`/fgstatus` — bot health & settings\n"
                   "`/fginvite` — add me to another server\n"
                   "`/fgclear` — remove my own messages (needs Manage Messages)\n"
                   "`/fghelp` — this menu"),
            inline=False)
        if is_owner:
            embed.add_field(
                name="🛠️ Owner",
                value=("`/fgconfig` — change settings at runtime\n"
                       "`/fgtest` — preview a sample announcement\n"
                       "`/fgpanel` — post the alert opt-in button\n"
                       "`/forcecheck` — scan for new games now"),
                inline=False)
        embed.set_footer(text="Tip: tap 🎉 Share My Win! on any announcement to flex your free haul.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _handle_fginvite(self, interaction: discord.Interaction) -> None:
        perms = discord.Permissions(
            send_messages=True, embed_links=True, read_message_history=True,
            manage_messages=True, manage_roles=True, mention_everyone=True,
            use_application_commands=True, add_reactions=True)
        url = discord.utils.oauth_url(
            self.user.id, permissions=perms, scopes=("bot", "applications.commands"))  # type: ignore[union-attr]
        embed = discord.Embed(
            title="➕ Add Free Game Notifier to your server",
            description=f"[**Click here to invite me**]({url})\n\n"
                        f"Permissions requested: Send Messages, Embed Links, Read History, "
                        f"Manage Messages (for `/fgclear`), Manage Roles (for the alert button), "
                        f"and Mention Everyone (for pings).",
            color=0x00C853)
        embed.set_footer(text=f"{BOT_NAME} v{VERSION}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _handle_fgclear(self, interaction: discord.Interaction, amount: int) -> None:
        amount = max(1, min(amount, 200))
        is_owner = interaction.user.id == OWNER_ID
        has_perm = False
        if interaction.guild and isinstance(interaction.user, discord.Member):
            has_perm = interaction.channel.permissions_for(interaction.user).manage_messages
        if not (is_owner or has_perm):
            await interaction.response.send_message(
                "🔒 You need the **Manage Messages** permission here (or be the bot owner).",
                ephemeral=True)
            return
        embed = discord.Embed(
            title="🧹 Clear my messages?",
            description=(f"I'll scan recent history and delete up to **{amount}** of **my own** "
                         f"messages in <#{interaction.channel.id}>.\n\n"
                         f"Messages older than 14 days are removed one-by-one (slower). "
                         f"This can't be undone."),
            color=0xE67E22)
        await interaction.response.send_message(
            embed=embed, view=ConfirmClearView(amount, interaction.user.id), ephemeral=True)

    async def _handle_fgtest(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("🔒 Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        channel = await self._get_channel()
        if not channel:
            await interaction.followup.send("❌ Target channel not reachable.", ephemeral=True)
            return
        try:
            games = await self.fetch_current_free_games()
        except Exception:  # noqa: BLE001
            games = []
        game = max(games, key=lambda g: (g.get("worth_value") or 0)) if games else DEMO_GAME
        embed, view = self.create_game_embed(game)
        try:
            await channel.send(content="🧪 **Sample announcement (test — no ping)**",
                               embed=embed, view=view,
                               allowed_mentions=discord.AllowedMentions.none())
            await interaction.followup.send(
                f"✅ Posted a sample using **{game['title']}**. Buttons are live — try them!",
                ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I can't post there. I need **Send Messages** + **Embed Links**.", ephemeral=True)

    async def _handle_fgconfig(self, interaction: discord.Interaction,
                               ping_role: Optional[discord.Role], min_worth: Optional[float],
                               reminder_hours: Optional[int], include_loot: Optional[bool],
                               include_beta: Optional[bool]) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("🔒 Owner only.", ephemeral=True)
            return
        changed = []
        if ping_role is not None:
            # @everyone role id == guild id; treat that as "use @here".
            new_id = 0 if (interaction.guild and ping_role.id == interaction.guild.id) else ping_role.id
            self.db.set_config("ping_role_id", str(new_id))
            changed.append(f"Ping → {'@here' if new_id == 0 else f'<@&{new_id}>'}")
        if min_worth is not None:
            mw = max(0.0, min_worth)
            self.db.set_config("min_worth", str(mw))
            changed.append(f"Min worth → {'Off' if mw == 0 else f'${mw:g}'}")
        if reminder_hours is not None:
            rh = max(0, reminder_hours)
            self.db.set_config("reminder_hours", str(rh))
            changed.append(f"Reminders → {'Off' if rh == 0 else f'{rh}h'}")
        if include_loot is not None:
            self.db.set_config("include_loot", "1" if include_loot else "0")
            changed.append(f"Loot → {'On' if include_loot else 'Off'}")
        if include_beta is not None:
            self.db.set_config("include_beta", "1" if include_beta else "0")
            changed.append(f"Beta → {'On' if include_beta else 'Off'}")

        embed = discord.Embed(
            title="⚙️ Runtime Configuration",
            color=0x5865F2, timestamp=datetime.now(timezone.utc))
        if changed:
            embed.description = "✅ Updated:\n" + "\n".join(f"• {c}" for c in changed)
        else:
            embed.description = "Current settings (pass options to change them):"
        rid = self.ping_role_id()
        embed.add_field(name="🔔 Ping", value=(f"<@&{rid}>" if rid else "@here"), inline=True)
        embed.add_field(name="💵 Min Worth",
                        value=(f"${self.min_worth():g}" if self.min_worth() else "Off"), inline=True)
        embed.add_field(name="⏰ Reminders",
                        value=(f"{self.reminder_hours()}h" if self.reminder_hours() else "Off"), inline=True)
        embed.add_field(name="🎁 Loot", value=("On" if self.include_loot() else "Off"), inline=True)
        embed.add_field(name="🧪 Beta", value=("On" if self.include_beta() else "Off"), inline=True)
        embed.set_footer(text="Changes persist across restarts. Reset by passing the .env default.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _handle_fgpanel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("🔒 Owner only.", ephemeral=True)
            return
        rid = self.ping_role_id()
        if not rid:
            await interaction.response.send_message(
                "⚠️ Set a ping role first (`PING_ROLE_ID` or `/fgconfig ping_role:`).", ephemeral=True)
            return
        channel = await self._get_channel()
        if not channel:
            await interaction.response.send_message("❌ Target channel not reachable.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🔔 Free-Game Alerts",
            description=(f"Want a ping whenever a new free game drops?\n"
                         f"Tap the button below to get (or remove) the <@&{rid}> role.\n\n"
                         f"You can opt out any time by tapping it again."),
            color=0x00C853)
        embed.set_footer(text=f"{BOT_NAME} v{VERSION}")
        await channel.send(embed=embed, view=NotifyRoleView(),
                           allowed_mentions=discord.AllowedMentions.none())
        await interaction.response.send_message("✅ Opt-in panel posted.", ephemeral=True)

    async def _handle_forcecheck(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("🔒 Owner only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        logger.info(f"Force check requested by owner {interaction.user} ({interaction.user.id})")
        try:
            posted = await self._perform_free_game_check()
            await interaction.followup.send(
                f"✅ Force check complete — **{posted}** new game(s) announced. See channel/logs.",
                ephemeral=True)
        except Exception as e:  # noqa: BLE001
            logger.exception("Force check failed")
            await interaction.followup.send(f"❌ Force check error: {str(e)[:150]}", ephemeral=True)


def main() -> None:
    if not DISCORD_TOKEN or CHANNEL_ID == 0:
        logger.critical("❌ Configuration error: set DISCORD_TOKEN and CHANNEL_ID in .env")
        raise SystemExit(1)
    if OWNER_ID == 0:
        logger.warning("⚠️ OWNER_ID not set — owner commands (/forcecheck, /fgpanel) will be unusable.")

    bot = FreeGameNotifier()
    try:
        bot.run(DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        logger.info("Bot process ended.")


if __name__ == "__main__":
    main()
