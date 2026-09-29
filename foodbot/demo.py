"""Offline, disposable preview; no accounts, API keys, or messages sent."""
import asyncio
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile

import httpx
from .config import Settings
from .database import Database
from .engine import Engine


async def main():
    with tempfile.TemporaryDirectory() as temp:
        config = Settings("offline", 1, "x" * 32, str(Path(temp) / "demo.sqlite3"))
        db = Database(config.database)
        async with httpx.AsyncClient() as client:
            engine = Engine(db, config, client)
            now = datetime(2026, 9, 28, 17, tzinfo=timezone.utc)
            engine.initialize(now - timedelta(minutes=1))
            for i, command in enumerate(["/start", "seed", "/stocked eggs | 5", "snooze for 3 days", "/status", "/shop"]):
                update = {"update_id": i, "message": {"text": command}}
                if command == "seed":
                    update = {"update_id": i, "callback_query": {"data": "seed", "message": {}}}
                db.enqueue(update)
                await engine.process_one(now)
                with db.connect() as conn:
                    message = json.loads(conn.execute("SELECT payload FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0])
                print(f"\nYOU: {command}\nFOODBOT: {message['text']}")
                for row in message.get("reply_markup", {}).get("inline_keyboard", []):
                    print("  " + " | ".join("[" + b["text"] + "]" for b in row))
            print("\nPreview finished. The temporary grocery data is discarded; nothing was sent or ordered.")


if __name__ == "__main__":
    asyncio.run(main())
