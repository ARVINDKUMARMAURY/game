import os
from dotenv import load_dotenv

# Loads variables from a .env file in the repo root, if present.
# On Railway this is a no-op (no .env file there) — Railway injects its own
# dashboard-configured env vars directly, which os.environ.get() below still picks up.
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

if not BOT_TOKEN:
    raise RuntimeError(
        "Set BOT_TOKEN — copy .env.example to .env and fill it in (local), "
        "or add it in Railway's Variables tab (production)."
    )
