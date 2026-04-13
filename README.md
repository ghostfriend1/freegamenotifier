🎮 Free Game Notifier v3
"Your personal Discord gremlin that hunts free games so you don't have to"
Tired of refreshing Epic Games Store, GamerPower, and 17 browser tabs like a raccoon digging through digital trash for free crumbs?
This bot does the hunting for you.
It relentlessly stalks public APIs 24/7, sniffs out fresh free games and juicy limited-time promos the moment they appear, then screams them into your Discord server with beautiful embeds and one-click buttons.
No more FOMO. No more "wait... that was free yesterday?!" regrets. Just pure, unfiltered freebie justice.
🚀 Features (The Good Stuff)
🔔 Instant Rich Embeds — Title, description, thumbnail, "Was $69.99 → FREE" flex, and expiration date (when the devs remember to include one).
🔘 Glorious One-Click Buttons:
Claim Now → Teleports you straight to the store page before your friends can react.
Google Trends → Instantly see if the internet is collectively losing its mind over the game.
GamerPower Page → Full instructions, screenshots, and the sacred "how to claim" copypasta.
🧠 Anti-Spam Brain — Lightweight SQLite database ensures each game is announced exactly once. Duplicates get yeeted into oblivion.
⚡ Slash Commands:
/fgstatus — "Is the bot still alive or did it achieve sentience and quit?"
/currentfree — Emergency dump of the top 5 current free/promos.
🛡️ Zero Ban Risk — Uses only public read-only APIs. No scraping, no logins, no "your account has been terminated for excessive greed."
🌍 Region-Friendly — Configure Epic locale & country so you actually get games available in your part of the world.
🔄 Fully Configurable — Polling interval, ping role, locale, and country — all via .env.
📦 Installation (Surprisingly Painless)
Clone the repo:
git clone https://github.com/ghostfriend1/freegamenotifier.git
cd freegamenotifier
pip install -r requirements.txt
Create a Discord bot at the Discord Developer Portal and copy its token.
Invite the bot to your server with bot and applications.commands scopes.
Grant it permissions to: Send Messages, Embed Links, and Use Slash Commands.
Copy .env.example to .env and fill in the values:

DISCORD_TOKEN=your_bot_token_here
CHANNEL_ID=123456789012345678
PING_ROLE_ID=987654321098765432
CHECK_INTERVAL_MINUTES=15
EPIC_LOCALE=en-US
EPIC_COUNTRY=US

Run the bot:
python main.py
The bot will sync its slash commands, post a startup message, and immediately begin its sacred mission of hunting free games. Keep it running 24/7 for maximum freebie coverage.
💬 Slash Commands
Command	Description
/fgstatus	Shows bot health, games tracked, last check time, and polling interval.
/currentfree	Instantly displays up to 5 currently free or promotional games.
🧠 How It Works (For the Nerds)
Every CHECK_INTERVAL_MINUTES, the bot:
Politely asks the GamerPower API for new PC giveaways.
Gently pokes the Epic Games free promotions endpoint (respecting your chosen region).
Checks its tiny SQLite brain: "Have I yelled about this one before?"
If it's new → crafts a glorious embed and delivers it with maximum hype.
If something breaks (network hiccup, API having a bad day) → posts a polite warning and tries again next cycle like a responsible adult.
All API calls are asynchronous (aiohttp), retried on transient failures, and logged for your debugging pleasure.
🔧 Customization (For the Chaos Agents)
Want different embed colors or extra buttons? Edit create_game_embed() in main.py.
Want to add Steam, GOG, Ubisoft, etc.? Extend fetch_new_free_games().
Want it to check every 5 minutes? Crank up CHECK_INTERVAL_MINUTES (please don't DDoS the APIs).
Want to ping a specific role instead of @here? Set PING_ROLE_ID.
🤝 Contributing
Found a bug? Got a feature idea? Think the humor needs to be cranked to 11? Open an issue or submit a pull request. Bonus points for funny commit messages and not breaking the deduplication system (it's the only thing protecting us from eternal duplicate spam).
🙏 Acknowledgements
GamerPower — Keeping the free game dream alive since forever.
Epic Games — For occasionally giving away actually decent titles.
discord.py & aiohttp — Doing the heavy lifting while we sit back and collect free games.
Every gamer who has ever screamed “IT’S FREE ON EPIC RIGHT NOW” in a Discord chat.
Always free. Always chaotic. Never missing another giveaway again.
Happy gaming, you magnificent cheapskates. 🥳🎮
