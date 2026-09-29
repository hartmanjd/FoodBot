import asyncio
import json
import logging
from datetime import timedelta

import httpx
from .schedule import utcnow, stamp, is_quiet

log = logging.getLogger(__name__)


async def deliver_one(database, settings, telegram, now):
    with database.transaction() as db:
        state = database.read(db)
        db.execute("UPDATE outbox SET sent=1 WHERE sent=0 AND kind='reminder' AND generation<>?",
                   (state["generation"],))
        # Replies are sent even in quiet hours: the owner just asked for them.
        row = db.execute("SELECT * FROM outbox WHERE sent=0 AND retry_at<=? "
                         "AND (kind<>'reminder' OR ?=0) ORDER BY id LIMIT 1",
                         (stamp(now), int(is_quiet(now, settings)))).fetchone()
    if not row:
        return False
    try:
        payload = json.loads(row["payload"])
        # Replies go back to the chat they came from. Reminders have no chat_id,
        # so they go to the owner's private chat.
        chat_id = payload.pop("chat_id", settings.owner)
        await telegram.send(chat_id, payload)
    except Exception as exc:
        attempts = row["attempts"] + 1
        delay = min(3600, 15 * (2 ** min(attempts, 8)))
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
            try:
                delay = max(delay, int(exc.response.json()["parameters"]["retry_after"]))
            except (ValueError, KeyError, TypeError):
                pass
        # Never log exception text: Telegram request URLs contain the bot token.
        log.warning("Telegram delivery failed (%s); retry in %ss", type(exc).__name__, delay)
        with database.connect() as db:
            db.execute("UPDATE outbox SET attempts=?,retry_at=? WHERE id=?",
                       (attempts, stamp(now + timedelta(seconds=delay)), row["id"]))
        return False
    with database.connect() as db:
        db.execute("UPDATE outbox SET sent=1 WHERE id=?", (row["id"],))
    return True


async def run_worker(engine, telegram, app_state):
    while True:
        try:
            # Process new input before sending queued nudges so a snooze cancels them.
            for _ in range(30):
                if not await engine.process_one(utcnow()):
                    break
            engine.tick(utcnow())
            for _ in range(10):
                if not await deliver_one(engine.db, engine.settings, telegram, utcnow()):
                    break
            app_state.worker_seen = utcnow()
        except Exception as exc:
            log.error("Worker iteration failed (%s)", type(exc).__name__)
        await asyncio.sleep(2)
