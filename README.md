# 🎮 Free Game Notifier v6.2

> *"Your personal Discord gremlin that hunts free games so you don't have to."*

Tired of refreshing the Epic Games Store, GamerPower, and 17 browser tabs like a raccoon digging through digital trash for free crumbs?

This bot does the hunting for you. It stalks public APIs 24/7, sniffs out fresh free games (and optionally free DLC, keys, and betas), then screams them into your Discord server with rich embeds and one-click buttons — and now it even **reminds you before they expire** and lets your members **opt in to pings with a single tap**.

No more FOMO. No more *"wait... that was free yesterday?!"* regrets.

---

## 🚀 What's new in v6.0

| Feature | What it does |
| --- | --- |
| ⏰ **Last-chance reminders** | Re-pings the channel a few hours before a giveaway ends so nobody misses it. |
| 🔔 **Self-serve alert opt-in** | Members tap a button to grab/drop the ping role themselves — no admin role-juggling. |
| 🔮 **`/upcoming`** | Shows *next* week's free Epic games before they go live. |
| 📈 **`/fgstats`** | Lifetime games announced, total **$ value given away**, and value free right now. |
| 💵 **`MIN_WORTH` filter** | Skip the shovelware — only announce games worth real money. |
| 🎁 **Loot & beta toggles** | Optionally catch free in-game loot, keys, and beta access. |
| 🧹 **Smarter dedup** | The same game from two sources is announced **once**, with the best claim link. |

### Fixes carried in from v5.x
- ✅ **Epic claim links actually work now.** Epic frequently returns `productSlug: null`; links are rebuilt from `offerMappings` / `catalogNs.mappings`, with a namespace+id purchase-URL fallback.
- ✅ **Startup message reliably sends** (moved to `on_ready`, where the channel cache exists).
- ✅ **Permanent dedup** — each giveaway is announced exactly once (no more 24h re-spam).
- ✅ **Only announces live offers** (validates the promo's start/end window).
- ✅ **No privileged intents required** — simpler bot setup.
- ✅ **"Was $59.99 → FREE" flex** and **localized live countdowns** via Discord timestamps.


## 🎉 Claim & Share (Win Share Kit) — *new in v6.1*

Every announcement now has a bright green **🎉 Share My Win!** button. Tapping it gives the user a private, copy-paste-ready post for X / Bluesky / Threads — randomized hype copy, clean hashtags, the real claim link, and a nod back to your bot. There's an optional **📢 Post my win to the channel** button for a public flex, a **`/sharewin`** command to generate a kit for any current freebie on demand, and community totals (**Wins Shared**, **Public Flexes**) shown in `/fgstats`.

> 🧠 **Why it actually works:** the Share button is *persistent and stateless* — it reconstructs the game from the announcement message itself, so it keeps working after the view's timeout **and across bot restarts**. (A naive per-message-state button silently dies after ~10 minutes or any redeploy.)

---

## ✨ Features at a glance

- 🔔 **Rich embeds** — title, description, thumbnail, price flex, and a live expiry countdown.
- 🔘 **One-click buttons** — Claim Now · Google Trends · Details.
- 🧠 **Anti-spam SQLite brain** — each game announced exactly once, forever.
- ⚡ **Slash commands** — 12 in total, incl. `/currentfree`, `/upcoming`, `/sharewin`, `/fgclear`, `/fgconfig`, `/fghelp` (run `/fghelp` to see them all).
- 🛡️ **Zero ban risk** — read-only public APIs. No scraping, no logins.
- 🌍 **Region-friendly** — set your Epic locale & country.
- 🔄 **Fully configurable** — interval, ping role, min worth, reminders, categories — all via `.env`.

---

## 📦 Installation

```bash
git clone https://github.com/ghostfriend1/freegamenotifier.git
cd freegamenotifier
pip install -r requirements.txt
```

1. Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications) and copy its **token**.
2. Invite the bot with the **`bot`** and **`applications.commands`** scopes.
3. Grant these permissions:
   - **Send Messages**, **Embed Links**, **Use Slash Commands** — required.
   - **Mention @everyone, @here, and All Roles** — needed for role/@here pings (or make the ping role "mentionable").
   - **Manage Roles** — needed only for the `/fgpanel` opt-in button. The bot's own role must sit **above** the alert role in *Server Settings → Roles*.
4. Copy `.env.example` to `.env` and fill in your values.
5. Run it:

```bash
python main.py
```

The bot syncs its slash commands, posts a startup message, and immediately begins hunting. Keep it running 24/7 for maximum freebie coverage.

> 💡 **First run is quiet by default.** With `QUIET_SEED=true`, the bot silently records whatever is already free so it doesn't flood you, then only announces *new* games going forward. Set `QUIET_SEED=false` if you want a full dump on first launch.


## 🛠️ Quality-of-life & admin — *new in v6.2*

- **`/fgclear [amount]`** — delete the bot's **own** recent messages in a channel. Confirmation step, permission-gated (bot owner or **Manage Messages**), and it respects Discord's 14-day bulk-delete limit (older messages are removed one-by-one).
- **`/fgconfig`** — change the **ping role, min worth, reminder hours, and loot/beta toggles at runtime** — no `.env` edit or redeploy. Settings persist in the database; reset by passing the original `.env` value.
- **`/fgtest`** *(owner)* — post a sample announcement to preview embeds, buttons, and permissions during setup (pings no one).
- **`/fghelp`** — a clean directory of every command.
- **`/fginvite`** — generates an OAuth invite link with exactly the permissions the bot needs.
- **Latency** is now shown in `/fgstatus`.

---

## ⚙️ Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | **Required.** Your bot token. |
| `CHANNEL_ID` | — | **Required.** Channel to post in. |
| `OWNER_ID` | `0` | Your user ID; unlocks `/forcecheck` and `/fgpanel`. |
| `PING_ROLE_ID` | `0` | Role to ping (0 = `@here`). |
| `CHECK_INTERVAL_MINUTES` | `15` | Minutes between scans. |
| `EPIC_LOCALE` / `EPIC_COUNTRY` | `en-US` / `US` | Epic region. |
| `MIN_WORTH` | `0` | Only announce games worth ≥ this (USD). Unknown prices always pass. |
| `EXPIRY_REMINDER_HOURS` | `6` | Hours before expiry to send a last-chance ping (0 = off). |
| `INCLUDE_LOOT` | `false` | Also announce free in-game loot/keys. |
| `INCLUDE_BETA` | `false` | Also announce free beta access. |
| `POST_DELAY_SECONDS` | `1.5` | Delay between posts (rate-limit friendly). |
| `ALLOW_PUBLIC_SHARE` | `true` | Show the "post my win to the channel" button. |
| `SHARE_PROMO_URL` | repo URL | Link included in generated share text (invite/landing page). |
| `GUILD_ID` | `0` | Set to one server ID for instant slash sync (testing). |
| `QUIET_SEED` | `true` | Silently seed an empty DB on first run. |
| `DB_FILE` | `free_games_v3.db` | SQLite path (mount a volume to persist). |

> ⚙️ Ping role, min worth, reminder hours, and loot/beta can also be changed live with **`/fgconfig`** — those overrides are stored in the database and survive restarts.

---

## 💬 Slash commands

| Command | Who | Description |
| --- | --- | --- |
| `/fgstatus` | Everyone | Health: uptime, interval, tracked count, last check, config. |
| `/currentfree` | Everyone | Currently free PC games, highest value first. |
| `/upcoming` | Everyone | Next week's free Epic games. |
| `/fgstats` | Everyone | Lifetime games announced, $ value given away, value free now, wins shared. |
| `/sharewin` | Everyone | Generate a copy-paste social share kit for a current freebie. |
| `/fghelp` | Everyone | Full command directory. |
| `/fginvite` | Everyone | Invite link with the right permissions. |
| `/fgclear` | Owner / Manage Messages | Delete the bot's own recent messages here. |
| `/fgtest` | Owner | Post a sample announcement to preview it. |
| `/fgconfig` | Owner | Change settings (ping role, min worth, reminders, loot/beta) at runtime. |
| `/fgpanel` | Owner | Posts the public 🔔 alert opt-in button to the channel. |
| `/forcecheck` | Owner | Forces an immediate scan. |

---

## 🧠 How it works

Every `CHECK_INTERVAL_MINUTES` the bot:

1. Asks the **GamerPower** API for active PC giveaways (games, plus loot/beta if enabled).
2. Pokes the **Epic Games** free-promotions endpoint for your region.
3. Collapses duplicates across sources (one announcement per game, best link wins).
4. Checks its SQLite brain — *"Have I yelled about this one?"* — and announces only new games.
5. If anything is expiring within `EXPIRY_REMINDER_HOURS`, fires a **last-chance** ping.

All API calls are async (`aiohttp`), retried with backoff, and logged.

---

## 🐳 Deployment (Docker / Railway)

A `Dockerfile` and `railway.json` are included. **Mount a volume** at your `DB_FILE` path so the dedup database (and stats) survive restarts — otherwise the bot forgets what it has announced and may re-spam after a redeploy.

```bash
docker build -t freegamenotifier .
docker run -d --env-file .env -v fgn-data:/app freegamenotifier
```

---

## 🔧 Customization

- **Embed colors / extra buttons** → edit `create_game_embed()`.
- **More sources (GOG, Steam, Ubisoft…)** → GamerPower already aggregates most; extend `_fetch_gamerpower()` or add a new `_fetch_*` and include it in `fetch_current_free_games()`.
- **Quieter channel** → raise `MIN_WORTH` and/or set `INCLUDE_LOOT=false`.

---

## 🤝 Contributing

Found a bug? Got a feature idea? Open an issue or a PR. Bonus points for funny commit messages and for not breaking the deduplication system (it's the only thing standing between us and eternal duplicate spam).

---

## 🙏 Acknowledgements

- **GamerPower** — keeping the free-game dream alive (and powering this bot's data; attribution required & happily given).
- **Epic Games** — for occasionally giving away actually-decent titles.
- **discord.py** & **aiohttp** — doing the heavy lifting.

Always free. Always chaotic. Never missing another giveaway again. Happy gaming, you magnificent cheapskates. 🥳🎮
