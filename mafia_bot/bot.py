import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.error import Forbidden, BadRequest

from config import BOT_TOKEN
from mafia_game import MafiaGame, Phase, MIN_PLAYERS, DAY_SECONDS, NIGHT_SECONDS
from db import record_game_result, get_user_stats, get_leaderboard

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("mafia-bot")

# chat_id -> MafiaGame (lets multiple groups run games at once)
games: dict[int, MafiaGame] = {}
# user_id -> chat_id, so we know which game a DM callback/action/relay message belongs to
player_chat: dict[int, int] = {}
# cached bot username for building deep links, filled in on first use
_bot_username = None


def mention(user_id, name):
    return f"[{name}](tg://user?id={user_id})"


async def get_bot_username(context: ContextTypes.DEFAULT_TYPE):
    global _bot_username
    if _bot_username is None:
        me = await context.bot.get_me()
        _bot_username = me.username
    return _bot_username


async def announce_join(context: ContextTypes.DEFAULT_TYPE, game, uid, name):
    """Adds a player to the lobby, wires player_chat, and posts the confirmation in the group."""
    ok, err = game.add_player(uid, name)
    if not ok:
        return ok, err
    player_chat[uid] = game.chat_id
    await context.bot.send_message(
        game.chat_id, f"✅ {mention(uid, name)} joined! ({len(game.players)} players)"
    )
    return True, None


# ---------------------------------------------------------------------
# Lobby
# ---------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    # Deep-link join: user tapped "Start bot & join" from /join in a group lobby.
    if args and args[0].startswith("join_"):
        try:
            chat_id = int(args[0].split("_", 1)[1])
        except ValueError:
            chat_id = None

        game = games.get(chat_id) if chat_id is not None else None
        uid = update.effective_user.id
        name = update.effective_user.first_name

        if not game or game.phase != Phase.LOBBY:
            await update.effective_chat.send_message(
                "That lobby isn't open anymore — ask the host to /newgame again, then /join."
            )
            return

        ok, err = await announce_join(context, game, uid, name)
        if not ok:
            await update.effective_chat.send_message(err)
            return

        await update.effective_chat.send_message(
            "✅ You're set and joined the lobby! Head back to the group — I'll DM you your role here once the game starts.",
            reply_markup=rules_button_markup(),
        )
        return

    await update.effective_chat.send_message(
        "🌙 Welcome to **Mafia**! Use /newgame in a group to open a lobby.",
        reply_markup=rules_button_markup(),
    )


async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.effective_chat.send_message("Start a lobby inside a group chat.")
        return

    existing = games.get(chat_id)
    if existing and existing.phase != Phase.ENDED:
        await update.effective_chat.send_message(
            "A game is already running here. Host can /endgame to stop it."
        )
        return

    game = MafiaGame(chat_id, update.effective_user.id)
    games[chat_id] = game
    game.add_player(update.effective_user.id, update.effective_user.first_name)
    player_chat[update.effective_user.id] = chat_id

    await update.effective_chat.send_message(
        f"🌙 **Mafia lobby created** by {mention(update.effective_user.id, update.effective_user.first_name)}!\n\n"
        f"Tap /join to enter. Need at least {MIN_PLAYERS} players.\n"
        "Host starts with /startgame when ready.",
        reply_markup=rules_button_markup(),
    )


async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = games.get(chat_id)
    if not game or game.phase != Phase.LOBBY:
        await update.effective_chat.send_message("No open lobby. Start one with /newgame.")
        return

    uid = update.effective_user.id
    name = update.effective_user.first_name

    if uid in game.players:
        await update.effective_chat.send_message("You already joined.")
        return

    # Every /join goes through the bot's DM — this guarantees the bot can message the
    # player later for their role, and keeps the join flow identical for everyone
    # regardless of whether they've talked to the bot before.
    bot_username = await get_bot_username(context)
    deep_link = f"https://t.me/{bot_username}?start=join_{chat_id}"
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔓 Tap to join", url=deep_link)]]
    )
    await update.effective_chat.send_message(
        f"{name}, tap below — it opens my DM and adds you to the lobby automatically.",
        reply_markup=markup,
    )


async def leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = games.get(chat_id)
    if not game or game.phase != Phase.LOBBY:
        await update.effective_chat.send_message("No open lobby to leave.")
        return
    uid = update.effective_user.id
    if game.remove_player(uid):
        player_chat.pop(uid, None)
        await update.effective_chat.send_message(f"{update.effective_user.first_name} left the lobby.")
    else:
        await update.effective_chat.send_message("You're not in the lobby.")


async def endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = games.get(chat_id)
    if not game:
        await update.effective_chat.send_message("No active game here.")
        return
    if update.effective_user.id != game.host_id:
        await update.effective_chat.send_message("Only the host can end the game.")
        return

    for uid in game.players:
        player_chat.pop(uid, None)
    game.end_game()
    games.pop(chat_id, None)
    await update.effective_chat.send_message("Game ended.")


# ---------------------------------------------------------------------
# Start game -> role DMs -> day/night loop
# ---------------------------------------------------------------------

def role_flavor(role):
    return {
        "Mafia": "Eliminate citizens at night. Blend in during the day. You can chat privately with other Mafia by DMing me during the night.",
        "Doctor": "Each night, choose one player to protect from elimination (you may protect yourself).",
        "Detective": "Each night, investigate one player to learn their true role.",
        "Vigilante": "You have ONE bullet for the whole game. Use it at night to eliminate someone — but if you kill an innocent, you'll die of guilt.",
        "Jester": "You have no power and aren't aligned with Mafia or Citizens. You win ALONE if the group votes you out during the day.",
        "Citizen": "No special power. Use discussion and voting to find the Mafia.",
    }.get(role, "")


async def startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game = games.get(chat_id)
    if not game or game.phase != Phase.LOBBY:
        await update.effective_chat.send_message("No lobby to start. Use /newgame first.")
        return
    if update.effective_user.id != game.host_id:
        await update.effective_chat.send_message("Only the host can start the game.")
        return
    if not game.can_start():
        await update.effective_chat.send_message(
            f"Need at least {MIN_PLAYERS} players. Currently {len(game.players)}."
        )
        return

    game.assign_roles()

    failed_dm = []
    for uid, player in game.players.items():
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=f"🎭 **Your secret role:** `{player.role}`\n\n{role_flavor(player.role)}",
            )
        except (Forbidden, BadRequest):
            failed_dm.append(player.name)

    if failed_dm:
        await update.effective_chat.send_message(
            "⚠️ Couldn't DM roles to: " + ", ".join(failed_dm) +
            "\nEveryone must start the bot in PM first (open the bot → Start), then /endgame and /newgame again."
        )
        game.end_game()
        games.pop(chat_id, None)
        return

    role_counts = {}
    for p in game.players.values():
        role_counts[p.role] = role_counts.get(p.role, 0) + 1
    counts_text = ", ".join(f"{v} {k}" for k, v in role_counts.items())

    await update.effective_chat.send_message(
        f"🎮 **Game started!** Roles sent via DM.\nRole distribution: {counts_text}\n\n"
        "🌞 **Day 1** begins — discuss who you suspect, then vote."
    )

    asyncio.create_task(run_day(context, chat_id))


# ---------------------------------------------------------------------
# Day phase
# ---------------------------------------------------------------------

async def run_day(context: ContextTypes.DEFAULT_TYPE, chat_id):
    game = games.get(chat_id)
    if not game or game.phase == Phase.ENDED:
        return

    game.start_day()

    buttons = [
        [InlineKeyboardButton(p.name, callback_data=f"dvote:{chat_id}:{p.user_id}")]
        for p in game.alive_players()
    ]
    markup = InlineKeyboardMarkup(buttons)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🗳️ **Voting open** ({DAY_SECONDS}s) — who do you suspect?",
        reply_markup=markup,
    )

    await asyncio.sleep(DAY_SECONDS)

    if games.get(chat_id) is game and game.phase == Phase.DAY:
        await resolve_day(context, chat_id)


async def resolve_day(context: ContextTypes.DEFAULT_TYPE, chat_id):
    game = games.get(chat_id)
    if not game or game.phase != Phase.DAY:
        return

    eliminated_id, counts, is_tie = game.tally_day_votes()

    lines = [f"{game.players[uid].name}: {c} vote(s)" for uid, c in sorted(counts.items(), key=lambda x: -x[1])]
    summary = "\n".join(lines) if lines else "No votes cast."

    if is_tie:
        await context.bot.send_message(chat_id, f"🤝 **Tie — nobody eliminated.**\n\n{summary}")
    elif eliminated_id:
        p = game.players[eliminated_id]
        await context.bot.send_message(chat_id, f"❌ **{p.name} was voted out.** They were **{p.role}**.\n\n{summary}")
    else:
        await context.bot.send_message(chat_id, "No votes cast — nobody eliminated.")

    if game.check_jester_win(eliminated_id):
        await announce_winner(context, chat_id, "jester")
        return

    winner = game.check_winner()
    if winner:
        await announce_winner(context, chat_id, winner)
        return

    await run_night(context, chat_id)


# ---------------------------------------------------------------------
# Night phase
# ---------------------------------------------------------------------

async def run_night(context: ContextTypes.DEFAULT_TYPE, chat_id):
    game = games.get(chat_id)
    if not game or game.phase == Phase.ENDED:
        return

    game.start_night()
    await context.bot.send_message(
        chat_id,
        f"🌙 **Night falls** ({NIGHT_SECONDS}s). Mafia, Doctor, Detective and Vigilante — check your DMs."
    )

    alive_targets = game.alive_players()

    # Mafia kill prompt (also tells them they can chat with each other here)
    for mafia_id in game.alive_mafia():
        buttons = [
            [InlineKeyboardButton(p.name, callback_data=f"nkill:{chat_id}:{p.user_id}")]
            for p in alive_targets if p.user_id != mafia_id
        ]
        try:
            await context.bot.send_message(
                mafia_id,
                "🔪 Choose your target. You can also just type a message here — it'll be relayed to your fellow Mafia.",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except (Forbidden, BadRequest):
            pass

    # Doctor save prompt
    if game.doctor_id and game.players[game.doctor_id].alive:
        buttons = [
            [InlineKeyboardButton(p.name, callback_data=f"nsave:{chat_id}:{p.user_id}")]
            for p in alive_targets
        ]
        try:
            await context.bot.send_message(
                game.doctor_id, "💉 Choose who to protect tonight:", reply_markup=InlineKeyboardMarkup(buttons)
            )
        except (Forbidden, BadRequest):
            pass

    # Detective investigate prompt
    if game.detective_id and game.players[game.detective_id].alive:
        buttons = [
            [InlineKeyboardButton(p.name, callback_data=f"ninv:{chat_id}:{p.user_id}")]
            for p in alive_targets if p.user_id != game.detective_id
        ]
        try:
            await context.bot.send_message(
                game.detective_id, "🔍 Choose who to investigate:", reply_markup=InlineKeyboardMarkup(buttons)
            )
        except (Forbidden, BadRequest):
            pass

    # Vigilante shoot prompt (only if bullet not yet used)
    if game.vigilante_id and game.players[game.vigilante_id].alive and not game.vigilante_used:
        buttons = [
            [InlineKeyboardButton(p.name, callback_data=f"nvig:{chat_id}:{p.user_id}")]
            for p in alive_targets if p.user_id != game.vigilante_id
        ]
        buttons.append([InlineKeyboardButton("Skip (save your bullet)", callback_data=f"nvigskip:{chat_id}")])
        try:
            await context.bot.send_message(
                game.vigilante_id,
                "🔫 You have one bullet for the whole game. Use it tonight, or skip?",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except (Forbidden, BadRequest):
            pass

    await asyncio.sleep(NIGHT_SECONDS)

    if games.get(chat_id) is game and game.phase == Phase.NIGHT:
        await resolve_night(context, chat_id)


async def resolve_night(context: ContextTypes.DEFAULT_TYPE, chat_id):
    game = games.get(chat_id)
    if not game or game.phase != Phase.NIGHT:
        return

    eliminated = game.resolve_night()

    if eliminated:
        lines = []
        for uid, reason in eliminated:
            p = game.players[uid]
            if reason == "guilt":
                lines.append(f"😔 **{p.name}** (Vigilante) died of guilt after shooting an innocent.")
            else:
                lines.append(f"☠️ **{p.name}** was found dead. They were **{p.role}**.")
        await context.bot.send_message(chat_id, "\n".join(lines))
    else:
        await context.bot.send_message(chat_id, "😌 Nobody died last night.")

    winner = game.check_winner()
    if winner:
        await announce_winner(context, chat_id, winner)
        return

    await run_day(context, chat_id)


# ---------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------

async def on_day_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id_str, target_id_str = query.data.split(":")
    chat_id, target_id = int(chat_id_str), int(target_id_str)
    game = games.get(chat_id)
    if not game or game.phase != Phase.DAY:
        await query.answer("Voting isn't open right now.", show_alert=True)
        return

    ok, err = game.cast_vote(query.from_user.id, target_id)
    if not ok:
        await query.answer(err, show_alert=True)
        return
    await query.answer(f"Voted for {game.players[target_id].name}.")

    if game.all_voted():
        await resolve_day(context, chat_id)


async def on_night_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id_str, target_id_str = query.data.split(":")
    chat_id, target_id = int(chat_id_str), int(target_id_str)
    game = games.get(chat_id)
    if not game:
        await query.answer("Game not found.", show_alert=True)
        return
    ok, err = game.submit_kill(query.from_user.id, target_id)
    if not ok:
        await query.answer(err, show_alert=True)
        return
    await query.answer(f"Target locked: {game.players[target_id].name}")
    await query.edit_message_text(f"🔪 You chose to eliminate {game.players[target_id].name}.")


async def on_night_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id_str, target_id_str = query.data.split(":")
    chat_id, target_id = int(chat_id_str), int(target_id_str)
    game = games.get(chat_id)
    if not game:
        await query.answer("Game not found.", show_alert=True)
        return
    ok, err = game.submit_save(query.from_user.id, target_id)
    if not ok:
        await query.answer(err, show_alert=True)
        return
    await query.answer(f"Protecting: {game.players[target_id].name}")
    await query.edit_message_text(f"💉 You chose to protect {game.players[target_id].name}.")


async def on_night_investigate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id_str, target_id_str = query.data.split(":")
    chat_id, target_id = int(chat_id_str), int(target_id_str)
    game = games.get(chat_id)
    if not game:
        await query.answer("Game not found.", show_alert=True)
        return
    ok, result = game.submit_investigate(query.from_user.id, target_id)
    if not ok:
        await query.answer(result, show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(f"🔍 {game.players[target_id].name}'s true role is: **{result}**", parse_mode="Markdown")


async def on_night_vig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id_str, target_id_str = query.data.split(":")
    chat_id, target_id = int(chat_id_str), int(target_id_str)
    game = games.get(chat_id)
    if not game:
        await query.answer("Game not found.", show_alert=True)
        return
    ok, err = game.submit_vig_kill(query.from_user.id, target_id)
    if not ok:
        await query.answer(err, show_alert=True)
        return
    await query.answer(f"Bullet spent on: {game.players[target_id].name}")
    await query.edit_message_text(f"🔫 You fired at {game.players[target_id].name}. Hope they deserved it.")


async def on_night_vig_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id_str = query.data.split(":")
    chat_id = int(chat_id_str)
    game = games.get(chat_id)
    if not game or game.phase != Phase.NIGHT or query.from_user.id != game.vigilante_id:
        await query.answer("Not allowed.", show_alert=True)
        return
    await query.answer("Saved your bullet for another night.")
    await query.edit_message_text("🔫 You held your fire tonight.")


# ---------------------------------------------------------------------
# Mafia night coordination relay
# ---------------------------------------------------------------------

async def relay_mafia_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any private text a living Mafia member sends during the night gets relayed to the rest of the Mafia."""
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    chat_id = player_chat.get(uid)
    if not chat_id:
        return
    game = games.get(chat_id)
    if not game or game.phase != Phase.NIGHT:
        return
    if uid not in game.mafia_ids or not game.players[uid].alive:
        return

    sender_name = game.players[uid].name
    text = update.message.text
    others = [mid for mid in game.alive_mafia() if mid != uid]
    if not others:
        return
    for mid in others:
        try:
            await context.bot.send_message(mid, f"🔪 **{sender_name}:** {text}")
        except (Forbidden, BadRequest):
            pass


# ---------------------------------------------------------------------
# End game + stats persistence
# ---------------------------------------------------------------------

async def announce_winner(context: ContextTypes.DEFAULT_TYPE, chat_id, winner):
    game = games[chat_id]
    role_reveal = "\n".join(f"{p.name}: {p.role}" for p in game.players.values())

    if winner == "mafia":
        text = f"🔪 **Mafia wins!**\n\n**Final roles:**\n{role_reveal}"
    elif winner == "jester":
        jname = game.players[game.jester_id].name
        text = f"🃏 **{jname} the Jester wins!** They tricked everyone into voting them out.\n\n**Final roles:**\n{role_reveal}"
    else:
        text = f"🎉 **Citizens win!**\n\n**Final roles:**\n{role_reveal}"

    results = []
    for p in game.players.values():
        if winner == "jester":
            won = p.user_id == game.jester_id
        elif winner == "mafia":
            won = p.role == "Mafia"
        else:
            won = p.role not in ("Mafia", "Jester")
        results.append({"user_id": p.user_id, "name": p.name, "role": p.role, "alive": p.alive, "won": won})

    try:
        await record_game_result(chat_id, results, winner)
    except Exception as e:
        log.error(f"Failed to record game result: {e}")

    for uid in game.players:
        player_chat.pop(uid, None)
    game.end_game()
    games.pop(chat_id, None)
    await context.bot.send_message(chat_id, text)


# ---------------------------------------------------------------------
# Stats / leaderboard (no-op reply if MONGO_URI isn't configured)
# ---------------------------------------------------------------------

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = await get_user_stats(update.effective_user.id)
    if not doc:
        await update.effective_chat.send_message(
            "No games recorded for you yet. (Stats need MONGO_URI configured on the bot.)"
        )
        return
    played = doc.get("games_played", 0)
    wins = doc.get("wins", 0)
    win_rate = f"{(wins / played * 100):.0f}%" if played else "0%"
    await update.effective_chat.send_message(
        f"📊 **{doc.get('name', update.effective_user.first_name)}**\n"
        f"Games played: {played}\nWins: {wins}\nWin rate: {win_rate}"
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = await get_leaderboard(10)
    if not top:
        await update.effective_chat.send_message(
            "No stats yet. (Leaderboard needs MONGO_URI configured on the bot.)"
        )
        return
    lines = [
        f"{i+1}. {d.get('name', '?')} — {d.get('wins', 0)} wins / {d.get('games_played', 0)} games"
        for i, d in enumerate(top)
    ]
    await update.effective_chat.send_message("🏆 **Leaderboard**\n\n" + "\n".join(lines))


# ---------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------

def rules_text():
    return (
        "**🌙 Mafia — how to play**\n\n"
        "/newgame — open a lobby (in a group)\n"
        "/join — join the lobby\n"
        "/leave — leave before start\n"
        f"/startgame — host starts (min {MIN_PLAYERS} players)\n"
        "/endgame — host ends the game\n"
        "/stats — your win/loss record\n"
        "/leaderboard — top players\n\n"
        "**Roles:**\n"
        "🔪 Mafia — kills at night, has a private night chat with other Mafia\n"
        "💉 Doctor — saves one player each night\n"
        "🔍 Detective — investigates one player each night\n"
        "🔫 Vigilante — one bullet for the whole game, kills an innocent = dies of guilt (unlocked at 8+ players)\n"
        "🃏 Jester — wins alone if voted out during the day (unlocked at 9+ players)\n"
        "🙂 Citizen — no power\n\n"
        "Day = discuss + vote to eliminate. Night = Mafia kills, Doctor saves, Detective investigates, Vigilante may shoot. "
        "Citizens win by eliminating all Mafia. Mafia wins when they equal or outnumber the rest."
    )


def rules_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📖 Rules & Roles", callback_data="showrules")]]
    )


async def mafiahelp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(rules_text())


async def on_show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Anyone — even mid-game, even not in the game — can tap this to see roles & rules right in the chat."""
    query = update.callback_query
    await query.answer()
    await context.bot.send_message(query.message.chat.id, rules_text())


if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newgame", newgame))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("leave", leave))
    app.add_handler(CommandHandler("startgame", startgame))
    app.add_handler(CommandHandler("endgame", endgame))
    app.add_handler(CommandHandler("mafiahelp", mafiahelp))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("leaderboard", leaderboard))

    app.add_handler(CallbackQueryHandler(on_day_vote, pattern=r"^dvote:"))
    app.add_handler(CallbackQueryHandler(on_night_kill, pattern=r"^nkill:"))
    app.add_handler(CallbackQueryHandler(on_night_save, pattern=r"^nsave:"))
    app.add_handler(CallbackQueryHandler(on_night_investigate, pattern=r"^ninv:"))
    app.add_handler(CallbackQueryHandler(on_night_vig, pattern=r"^nvig:"))
    app.add_handler(CallbackQueryHandler(on_night_vig_skip, pattern=r"^nvigskip:"))
    app.add_handler(CallbackQueryHandler(on_show_rules, pattern=r"^showrules$"))

    # Mafia night coordination — relay any private text from a living Mafia member to the rest
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, relay_mafia_chat))

    log.info("Starting Mafia bot...")
    app.run_polling()
