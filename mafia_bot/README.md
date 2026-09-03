# Mafia Telegram Bot

Full "Mafia/Werewolf" party game — day/night cycle, role-specific night actions,
per-group isolated state, Mafia coordination chat, extra roles, and MongoDB
stats/leaderboard. Built with `python-telegram-bot` v21.

## Roles
- **Mafia** — kills one player each night. Scales with player count (1 for 5-6, 2 for 7-9, 3 for 10). Can privately chat with other alive Mafia by just DMing the bot during the night — it relays messages between them.
- **Doctor** — protects one player each night (can self-protect).
- **Detective** — investigates one player's true role each night.
- **Vigilante** *(unlocked at 8+ players)* — one bullet for the whole game. Shoots at night; if the target isn't Mafia, the Vigilante dies of guilt the same night.
- **Jester** *(unlocked at 9+ players)* — no power, not aligned with either side. Wins alone if voted out during the **day**.
- **Citizen** — no power, wins by finding and voting out all Mafia.

## Stats & leaderboard
- `/stats` — your personal win/loss record
- `/leaderboard` — top 10 players by wins across all groups

Backed by MongoDB via Motor. **Fully optional** — if `MONGO_URI` isn't set, the bot
runs fine without it; `/stats` and `/leaderboard` just say no data is available yet.

## Deploy on Railway

This repo is set up for Railway (`Procfile` + root `requirements.txt` + `runtime.txt`).

1. On Railway: **New Project → Deploy from GitHub repo** → select `ARVINDKUMARMAURY/game`.
2. Railway auto-detects Python via `requirements.txt` and runs the process defined in `Procfile` (`worker: python mafia_bot/bot.py`).
3. Go to your service → **Variables** and add:
   - `BOT_TOKEN` — from @BotFather
   - `MONGO_URI` — optional, for `/stats` and `/leaderboard` (e.g. a MongoDB Atlas connection string, or Railway's own MongoDB plugin URI)
   - `MONGO_DB_NAME` — optional, defaults to `mafia_bot`
4. Deploy. Since this is a **worker** (long-polling bot, not a web server), Railway won't assign it a public URL — that's expected, just check the **Deployments → Logs** tab to confirm it says "Starting Mafia bot...".
5. If Railway defaults to a "web" process type instead of picking up `worker` from the Procfile, go to **Settings → Deploy → Start Command** and set it manually to:
   ```
   python mafia_bot/bot.py
   ```

## Setup (local / VPS)

1. Install dependencies (from the repo root, one level up from `mafia_bot/`):
   ```
   pip install -r requirements.txt
   ```

2. Get a `BOT_TOKEN` from @BotFather, and (optional, for `/stats`/`/leaderboard`) a MongoDB
   connection string — same DB you already use for KingUserBot/DNS-CHAT-BOT works fine,
   just point it at a new database name.

3. Copy `.env.example` (repo root) to `.env` and fill in your values:
   ```
   cp .env.example .env
   ```
   Then edit `.env`:
   ```
   BOT_TOKEN=your_bot_token
   MONGO_URI=your_mongodb_connection_string   # optional
   MONGO_DB_NAME=mafia_bot                     # optional, defaults to mafia_bot
   ```
   `.env` is gitignored — it never gets pushed to GitHub.

4. Run:
   ```
   python mafia_bot/bot.py
   ```

   For VPS: `pm2 start mafia_bot/bot.py --interpreter python3 --name mafia-bot`.

## Commands (in a group)
- `/newgame` — open a lobby
- `/join` — join
- `/leave` — leave before start
- `/startgame` — host starts (min 5 players; everyone must have DM'd the bot at least once first)
- `/endgame` — host ends the game
- `/mafiahelp` — rules
- `/stats` — your record (any chat, including DM)
- `/leaderboard` — top players (any chat, including DM)

## How the Mafia night chat works
During the night phase, any text a living Mafia member sends to the bot in **private**
gets automatically relayed to every other living Mafia member's DM, prefixed with the
sender's name. No special command needed — they just type normally in their DM with the bot.

## Things you could extend further
- Multiple Mafia currently each get their own kill button — if they pick different targets, whoever's vote lands last before the timer wins (simple v1). Could add a "confirm as team" step instead.
- Add more roles: Bodyguard (dies instead of their target), Mayor (double vote), Serial Killer (third faction).
- `/stats` per-group leaderboard instead of global — filter `db.games` by `chat_id`.
- Timers (`DAY_SECONDS`, `NIGHT_SECONDS`) — constants at top of `mafia_game.py`.
