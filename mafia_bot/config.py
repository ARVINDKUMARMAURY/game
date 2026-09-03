import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN as an environment variable (or hardcode it here for local testing).")
