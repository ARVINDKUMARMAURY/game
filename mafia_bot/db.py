"""
MongoDB persistence for cross-game stats and leaderboard.
Uses Motor (async) since the bot runs on python-telegram-bot's asyncio event loop.

Fully optional: if MONGO_URI isn't set, every function here silently no-ops
so the bot still works fine without a database.
"""

import os
from datetime import datetime, timezone

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except ImportError:
    AsyncIOMotorClient = None

MONGO_URI = os.environ.get("MONGO_URI", "")
DB_NAME = os.environ.get("MONGO_DB_NAME", "mafia_bot")

_client = None
_db = None


def _get_db():
    global _client, _db
    if _db is None:
        if not MONGO_URI or AsyncIOMotorClient is None:
            return None
        _client = AsyncIOMotorClient(MONGO_URI)
        _db = _client[DB_NAME]
    return _db


async def record_game_result(chat_id, results, winner):
    """
    results: list of dicts {user_id, name, role, alive, won}
    winner: "mafia" | "citizens" | "jester"
    """
    db = _get_db()
    if db is None:
        return

    await db.games.insert_one({
        "chat_id": chat_id,
        "winner": winner,
        "players": results,
        "played_at": datetime.now(timezone.utc),
    })

    for r in results:
        await db.players.update_one(
            {"user_id": r["user_id"]},
            {
                "$set": {"name": r["name"]},
                "$inc": {
                    "games_played": 1,
                    "wins": 1 if r["won"] else 0,
                },
            },
            upsert=True,
        )


async def get_user_stats(user_id):
    db = _get_db()
    if db is None:
        return None
    return await db.players.find_one({"user_id": user_id})


async def get_leaderboard(limit=10):
    db = _get_db()
    if db is None:
        return []
    cursor = db.players.find().sort("wins", -1).limit(limit)
    return [doc async for doc in cursor]
