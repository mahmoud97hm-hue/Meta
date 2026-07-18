"""
conftest.py — sets dummy env vars BEFORE importing the bot modules so the
integration test can run without a real MetaApi/OANDA/Telegram configuration.

In production, state.py's _require_env hard-fails on missing vars (correct
behaviour). Here we only stub for the isolated test harness.
"""
import os

for _k in ("METAAPI_TOKEN", "ACCOUNT_ID", "TG_TOKEN", "OANDA_ACCOUNT", "OANDA_TOKEN"):
    os.environ.setdefault(_k, f"TEST_{_k}")

os.environ.setdefault("OANDA_BASE_URL", "https://api-fxpractice.oanda.com")
