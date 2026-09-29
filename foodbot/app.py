import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import json
import logging

import httpx
from fastapi import FastAPI, HTTPException, Request

from .clients import Telegram
from .config import Settings
from .database import Database
from .engine import Engine
from .schedule import utcnow
from .worker import run_worker


def owner_update(update, owner):
    if not isinstance(update, dict) or type(update.get("update_id")) is not int:
        return False
    callback = update.get("callback_query")
    if callback is not None and not isinstance(callback, dict):
        return False
    message = callback.get("message") if callback else update.get("message")
    if not isinstance(message, dict):
        return False
    author = (callback if callback else message).get("from", {})
    chat = message.get("chat", {})
    return (isinstance(author, dict) and isinstance(chat, dict)
            and author.get("id") == owner and chat.get("id") == owner
            and chat.get("type") == "private")


def create_app(settings=None, start_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings.from_env()
        # Keep HTTP clients from logging URLs containing Telegram credentials.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        database = Database(config.database)
        async with httpx.AsyncClient(timeout=25) as client:
            engine = Engine(database, config, client)
            engine.initialize(utcnow())
            app.state.config = config
            app.state.database = database
            app.state.telegram = Telegram(client, config.token)
            app.state.worker_seen = utcnow()
            task = asyncio.create_task(run_worker(engine, app.state.telegram, app.state)) if start_worker else None
            try:
                yield
            finally:
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    app = FastAPI(title="Foodbot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        if (utcnow() - app.state.worker_seen).total_seconds() > 180:
            raise HTTPException(503, "Worker not responding")
        with app.state.database.connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.post("/telegram/webhook")
    async def webhook(request: Request):
        secret = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(secret.encode(), app.state.config.secret.encode()):
            raise HTTPException(403, "Forbidden")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 128_000:
                raise HTTPException(413, "Request too large")
        try:
            update = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON")
        if not owner_update(update, app.state.config.owner):
            return {"ok": True}
        app.state.database.enqueue(update)
        # Clearing the button spinner is best effort, independent of durable processing.
        if update.get("callback_query"):
            try:
                await asyncio.wait_for(app.state.telegram.call("answerCallbackQuery", {
                    "callback_query_id": update["callback_query"]["id"]}), timeout=2)
            except Exception:
                pass
        return {"ok": True}

    return app


app = create_app()
