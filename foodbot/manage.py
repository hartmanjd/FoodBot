"""Beginner-friendly local setup helpers: python -m foodbot.manage --help."""
import argparse
import asyncio
from getpass import getpass
import json
import os
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from .clients import Telegram, shopping_link
from .config import Settings


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["identify", "secret", "webhook", "webhook-info", "check", "instacart-test", "backup"])
    parser.add_argument("value", nargs="?", help="HTTPS domain for webhook; destination path for backup")
    args = parser.parse_args()
    load_dotenv()
    if args.command == "secret":
        print(secrets.token_urlsafe(32))
        return
    if args.command == "backup":
        if not args.value:
            parser.error("Provide a NEW backup file path.")
        source = Path(os.getenv("DATABASE_PATH", "data/foodbot.sqlite3"))
        if not source.is_file():
            parser.error("Database does not exist yet.")
        target = Path(args.value)
        if target.exists():
            parser.error("Choose a new file path; refusing to overwrite an existing backup.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
        print(f"Backup saved to {target}")
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN") or getpass("Paste your Telegram bot token (hidden): ")
    async with httpx.AsyncClient(timeout=30) as client:
        telegram = Telegram(client, token)
        if args.command == "identify":
            info = await telegram.call("getWebhookInfo", {})
            if info.get("url"):
                print("A webhook is already connected. identify is only for initial setup; it won't disconnect your bot.")
                return
            bot = await telegram.call("getMe", {})
            print(f"In Telegram, open @{bot['username']} and send /start now.")
            result = await telegram.call("getUpdates", {"timeout": 20, "allowed_updates": ["message"]})
            people = {(x["message"]["from"]["id"], x["message"]["from"].get("first_name", ""))
                      for x in result if "message" in x and x["message"]["chat"]["type"] == "private"}
            if not people:
                print("No messages found. Send /start to the bot, then run this again.")
            for uid, name in sorted(people):
                print(f"Private chat from {name!r}: TELEGRAM_OWNER_ID={uid}. Choose your own ID.")
            return
        if args.command == "webhook-info":
            info = await telegram.call("getWebhookInfo", {})
            print(json.dumps({key: info.get(key) for key in ("url", "pending_update_count", "last_error_message")}, indent=2))
            return
        config = Settings.from_env()
        if args.command == "check":
            bot = await telegram.call("getMe", {})
            print(f"Settings valid. Telegram bot: @{bot['username']}. Owner ID: {config.owner}.")
            print(f"Timezone: {config.timezone}; weekly day: {config.day} (Monday=0); time: {config.checkin_time}.")
            print("AI: " + ("configured" if config.openai_key else "optional, not configured"))
            print("Instacart: " + (config.instacart_env if config.instacart_key else "optional, not configured"))
        elif args.command == "webhook":
            url = urlparse(args.value or "")
            if url.scheme != "https" or not url.hostname or url.path not in ("", "/") or url.query or url.fragment or url.username:
                parser.error("Provide your HTTPS domain only, for example https://foodbot-example.up.railway.app")
            await telegram.call("setWebhook", {
                "url": args.value.rstrip("/") + "/telegram/webhook",
                "secret_token": config.secret,
                "allowed_updates": ["message", "callback_query"],
                "max_connections": 1,
            })
            print("Webhook connected. Send /start to your bot in Telegram.")
        elif args.command == "instacart-test":
            if not config.instacart_key:
                parser.error("Set INSTACART_API_KEY first.")
            link = await shopping_link(client, config, [{"name": "eggs", "quantity": 12, "unit": "each"}])
            print(f"{config.instacart_env} shopping link (no purchase made): {link}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except httpx.HTTPStatusError as exc:
        print(f"Service returned HTTP {exc.response.status_code}. Check your token, provider access, and configuration.")
        raise SystemExit(1)
    except (httpx.HTTPError, ValueError) as exc:
        # Do not print HTTP exception strings containing credential-bearing URLs.
        print(f"Setup failed ({type(exc).__name__}). Check .env and your connection; see docs/SETUP.md.")
        raise SystemExit(1)
