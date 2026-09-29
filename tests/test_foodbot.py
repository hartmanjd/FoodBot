import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from foodbot.app import create_app
from foodbot.assistant import Proposal
from foodbot.clients import shopping_link
from foodbot.config import Settings
from foodbot.database import Database
from foodbot.engine import Engine
from foodbot.schedule import next_weekly, local_slot, stamp, parse, is_quiet
from foodbot.worker import deliver_one

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc)  # Monday 10 a.m. Pacific


class FakeTelegram:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, owner, payload):
        if self.fail:
            raise httpx.ConnectError("offline")
        self.sent.append(payload)


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings("test-token", 123, "x" * 32, str(Path(self.temp.name) / "test.sqlite3"))
        self.db = Database(self.settings.database)
        self.requests = []

        def handler(request):
            self.requests.append(request)
            return httpx.Response(200, json={"products_link_url": "https://www.instacart.com/store/test"})

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.engine = Engine(self.db, self.settings, self.client)
        self.engine.initialize(NOW - timedelta(minutes=1))
        self.uid = 0

    async def asyncTearDown(self):
        await self.client.aclose()
        self.temp.cleanup()

    async def command(self, text, callback=False, now=NOW, update_id=None):
        self.uid += 1
        msg = {"chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": text}
        update = {"update_id": self.uid if update_id is None else update_id, "message": msg}
        if callback:
            update = {"update_id": update["update_id"], "callback_query": {
                "id": str(self.uid), "from": {"id": 123}, "message": msg, "data": text}}
        self.db.enqueue(update)
        await self.engine.process_one(now)
        with self.db.connect() as db:
            return json.loads(db.execute("SELECT payload FROM outbox ORDER BY id DESC LIMIT 1").fetchone()[0])

    async def start(self):
        await self.command("/start")
        await self.command("seed", True)

    def reminders(self):
        with self.db.connect() as db:
            return db.execute("SELECT * FROM outbox WHERE kind='reminder'").fetchall()

    async def test_personal_starters_and_duplicate_events(self):
        await self.start()
        state = self.db.snapshot()
        self.assertEqual([x["name"] for x in state["staples"]],
                         ["eggs", "hash browns", "Greek yogurt", "bread", "English muffins"])
        await self.command("/add coffee | 2 | package", update_id=100)
        revision = self.db.snapshot()["revision"]
        await self.command("/add coffee | 2 | package", update_id=100)
        self.assertEqual(self.db.snapshot()["revision"], revision)
        self.assertEqual(len(self.db.snapshot()["items"]), 6)

    async def test_snooze_survives_restart_and_suppresses_weekly(self):
        await self.start()
        await self.command("snooze for 10 days")
        wake = parse(self.db.snapshot()["snoozed_until"])
        engine = Engine(Database(self.settings.database), self.settings, self.client)
        engine.tick(NOW + timedelta(days=7))
        self.assertEqual(len(self.reminders()), 0)
        engine.tick(wake)
        self.assertEqual(len(self.reminders()), 1)
        self.assertIsNone(self.db.snapshot()["snoozed_until"])

    async def test_followup_limit_and_restart_no_duplicate(self):
        await self.start()
        telegram = FakeTelegram()
        for day in range(7):
            now = NOW + timedelta(days=day)
            self.engine.tick(now)
            self.engine.tick(now)
            while await deliver_one(self.db, self.settings, telegram, now):
                pass
        self.assertEqual(len(self.reminders()), 3)
        self.engine.tick(NOW + timedelta(days=7))
        self.assertEqual(len(self.reminders()), 4)

    async def test_zero_followups(self):
        await self.start()
        self.engine.settings = replace(self.settings, max_followups=0)
        self.engine.tick(NOW)
        self.assertIsNone(self.db.snapshot()["followup"])

    async def test_skip_is_temporary_and_does_not_reappear_on_checkin(self):
        await self.start()
        await self.command("/skip eggs")
        await self.command("/checkin")
        self.assertNotIn("eggs", [x["name"] for x in self.db.snapshot()["items"]])
        self.assertIn("eggs", [x["name"] for x in self.db.snapshot()["staples"]])
        self.engine.tick(NOW + timedelta(days=7))
        self.assertIn("eggs", [x["name"] for x in self.db.snapshot()["items"]])

    async def test_stocked_defers_item_and_removes_from_current_list(self):
        await self.start()
        await self.command("/stocked eggs | 10")
        await self.command("/checkin", now=NOW + timedelta(days=7))
        self.assertNotIn("eggs", [x["name"] for x in self.db.snapshot()["items"]])
        await self.command("/checkin", now=NOW + timedelta(days=10))
        self.assertIn("eggs", [x["name"] for x in self.db.snapshot()["items"]])

    async def test_stocked_all_does_not_create_an_empty_followup(self):
        await self.start()
        self.engine.tick(NOW)
        for name in [x["name"] for x in self.db.snapshot()["staples"]]:
            await self.command(f"/stocked {name} | 10")
        self.assertFalse(self.db.snapshot()["active"])
        self.assertIsNone(self.db.snapshot()["followup"])
        telegram = FakeTelegram()
        while await deliver_one(self.db, self.settings, telegram, NOW):
            pass
        self.assertFalse(any("Might you" in x["text"] for x in telegram.sent))

    async def test_confirm_records_once_and_stale_button_cannot_buy_new_list(self):
        await self.start()
        reply = await self.command("/ordered")
        confirm = reply["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await self.command(confirm, True)
        await self.command(confirm, True)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM purchases").fetchone()[0], 1)
        self.assertEqual(self.db.snapshot()["items"], [])
        self.assertIsNone(self.db.snapshot()["followup"])
        await self.command("/add coffee")
        await self.command(confirm, True)
        self.assertEqual(len(self.db.snapshot()["items"]), 1)

    async def test_edit_invalidates_purchase_confirmation(self):
        await self.start()
        reply = await self.command("/ordered")
        confirm = reply["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await self.command("/skip bread")
        reply = await self.command(confirm, True)
        self.assertIn("older list", reply["text"])
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM purchases").fetchone()[0], 0)

    async def test_due_staple_initiates_after_purchase(self):
        await self.start()
        reply = await self.command("/ordered")
        await self.command(reply["reply_markup"]["inline_keyboard"][0][0]["callback_data"], True)
        # Set a short replenishment estimate before the weekly slot.
        with self.db.transaction() as db:
            state = self.db.read(db)
            state["staples"][0]["due"] = stamp(NOW + timedelta(days=2))
            self.db.save(db, state)
        self.engine.tick(NOW + timedelta(days=2))
        self.assertEqual(len(self.reminders()), 1)
        self.assertEqual(self.db.snapshot()["items"][0]["name"], "eggs")

    async def test_shop_link_is_cached_and_never_records_purchase(self):
        await self.start()
        self.engine.settings = replace(self.settings, instacart_key="fake")
        await self.command("/shop")
        await self.command("/shop")
        self.assertEqual(len(self.requests), 1)
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["line_items"][0]["line_item_measurements"], [{"quantity": 12, "unit": "each"}])
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM purchases").fetchone()[0], 0)
        await self.command("/skip eggs")
        await self.command("/shop")
        self.assertEqual(len(self.requests), 2)

    async def test_instacart_failure_keeps_list(self):
        await self.start()
        before = copy.deepcopy(self.db.snapshot()["items"])
        self.engine.settings = replace(self.settings, instacart_key="fake")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
            self.engine.client = client
            reply = await self.command("/shop")
        self.assertIn("list is saved", reply["text"])
        self.assertEqual(self.db.snapshot()["items"], before)

    async def test_pause_and_snooze_cancel_queued_nudges(self):
        await self.start()
        self.engine.tick(NOW)
        await self.command("/snooze 3")
        telegram = FakeTelegram()
        while await deliver_one(self.db, self.settings, telegram, NOW):
            pass
        self.assertFalse(any("Might you" in x["text"] for x in telegram.sent))
        await self.command("/pause")
        self.engine.tick(NOW + timedelta(days=30))
        self.assertEqual(len(self.reminders()), 1)

    async def test_quiet_hours(self):
        await self.start()
        self.engine.tick(NOW + timedelta(hours=12))  # 10 p.m.
        self.assertEqual(len(self.reminders()), 0)

    async def test_explicit_snooze_returns_at_promised_time_after_late_nudge(self):
        await self.start()
        late = NOW + timedelta(hours=10)  # 8 p.m. after downtime
        self.engine.tick(late)
        await self.command("/snooze 1", now=late)
        self.engine.tick(NOW + timedelta(days=1))
        self.assertEqual(len(self.reminders()), 2)

    async def test_resume_waits_for_promised_slot(self):
        await self.start()
        await self.command("/pause")
        await self.command("/stocked eggs | 1")
        with self.db.transaction() as db:
            state = self.db.read(db)
            state["active"] = False
            self.db.save(db, state)
        await self.command("/resume", now=NOW + timedelta(hours=2))
        self.engine.tick(NOW + timedelta(hours=2))
        self.assertEqual(len(self.reminders()), 0)
        self.engine.tick(NOW + timedelta(days=1))
        self.assertEqual(len(self.reminders()), 1)

    async def test_changed_schedule_preserves_snooze(self):
        await self.start()
        await self.command("/snooze 10")
        saved = self.db.snapshot()["snoozed_until"]
        changed = replace(self.settings, day=2, checkin_time="11:00")
        engine = Engine(self.db, changed, self.client)
        engine.initialize(NOW)
        self.assertEqual(self.db.snapshot()["snoozed_until"], saved)
        self.assertEqual(parse(self.db.snapshot()["next_checkin"]), NOW + timedelta(days=2, hours=1))

    async def test_failed_delivery_retries_without_applying_input_twice(self):
        await self.start()
        failed = FakeTelegram(fail=True)
        self.assertFalse(await deliver_one(self.db, self.settings, failed, NOW))
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT attempts FROM outbox ORDER BY id LIMIT 1").fetchone()[0], 1)
        telegram = FakeTelegram()
        while await deliver_one(self.db, self.settings, telegram, NOW + timedelta(hours=1)):
            pass
        self.assertEqual(len(telegram.sent), 2)
        self.assertEqual(len(self.db.snapshot()["items"]), 5)

    async def test_invalid_input_leaves_state_unchanged(self):
        await self.start()
        before = self.db.snapshot()
        for command in ("/add eggs | nan | each", "/snooze 0", "/stocked eggs | -1", "/staple eggs | 1 | carton | 3 | yes"):
            await self.command(command)
            self.assertEqual(self.db.snapshot(), before)

    async def test_ai_proposal_requires_confirmation_and_validates(self):
        await self.start()
        self.engine.settings = replace(self.settings, openai_key="fake")
        proposal = {"clarification": "", "actions": [
            {"type": "skip", "item": "eggs", "quantity": 1, "unit": "each", "days": 1},
            {"type": "add", "item": "coffee", "quantity": 2, "unit": "package", "days": 1}]}
        payload = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(proposal)}]}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
            self.engine.client = client
            reply = await self.command("Skip eggs and add two packages of coffee")
        self.assertIn("eggs", [x["name"] for x in self.db.snapshot()["items"]])
        self.assertNotIn("coffee", [x["name"] for x in self.db.snapshot()["items"]])
        action = reply["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await self.command(action, True)
        names = [x["name"] for x in self.db.snapshot()["items"]]
        self.assertNotIn("eggs", names)
        self.assertIn("coffee", names)

    async def test_ai_failure_preserves_commands(self):
        await self.start()
        self.engine.settings = replace(self.settings, openai_key="fake")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429))) as client:
            self.engine.client = client
            reply = await self.command("I want some breakfast stuff")
        self.assertIn("unchanged", reply["text"])
        await self.command("/add coffee")
        self.assertEqual(len(self.db.snapshot()["items"]), 6)


class ScheduleTests(unittest.TestCase):
    def test_dst_snooze_preserves_ten_am(self):
        before = datetime(2026, 10, 31, 17, tzinfo=timezone.utc)
        config = Settings("t", 1, "x" * 32)
        self.assertEqual(local_slot(before, config, 1), datetime(2026, 11, 1, 18, tzinfo=timezone.utc))
        self.assertEqual(next_weekly(before, config), datetime(2026, 11, 2, 18, tzinfo=timezone.utc))

    def test_spring_dst_and_quiet_boundaries(self):
        config = Settings("t", 1, "x" * 32)
        before = datetime(2026, 3, 7, 18, tzinfo=timezone.utc)
        self.assertEqual(local_slot(before, config, 1), datetime(2026, 3, 8, 17, tzinfo=timezone.utc))
        self.assertTrue(is_quiet(datetime(2026, 9, 29, 4, tzinfo=timezone.utc), config))
        self.assertFalse(is_quiet(datetime(2026, 9, 29, 16, tzinfo=timezone.utc), config))


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_owner_private_chat_and_durable_deduplication(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Settings("test", 123, "x" * 32, str(Path(temp) / "db"))
            app = create_app(config, start_worker=False)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                update = {"update_id": 99, "message": {"from": {"id": 123}, "chat": {"id": 123, "type": "private"}, "text": "/start"}}
                self.assertEqual((await client.post("/telegram/webhook", json=update)).status_code, 403)
                headers = {"X-Telegram-Bot-Api-Secret-Token": config.secret}
                for _ in range(2):
                    self.assertEqual((await client.post("/telegram/webhook", json=update, headers=headers)).status_code, 200)
                update["update_id"] = 100
                update["message"]["from"]["id"] = 999
                await client.post("/telegram/webhook", json=update, headers=headers)
                update["message"]["from"]["id"] = 123
                update["message"]["chat"]["type"] = "group"
                await client.post("/telegram/webhook", json=update, headers=headers)
                with Database(config.database).connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM inbox").fetchone()[0], 1)
                self.assertEqual((await client.get("/health")).json(), {"status": "ok"})
                self.assertEqual((await client.post("/telegram/webhook", content="bad json", headers=headers)).status_code, 400)
                self.assertEqual((await client.post("/telegram/webhook", content="x" * 128001, headers=headers)).status_code, 413)


if __name__ == "__main__":
    unittest.main()
