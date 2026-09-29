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

    def reminders(self):
        with self.db.connect() as db:
            return db.execute("SELECT * FROM outbox WHERE kind='reminder'").fetchall()

    async def test_home_has_only_three_actions_and_start_preserves_edits(self):
        reply = await self.command("/start")
        self.assertEqual([b["text"] for row in reply["reply_markup"]["inline_keyboard"] for b in row],
                         ["Review groceries", "Shop", "Snooze"])
        self.assertEqual(len(self.db.snapshot()["items"]), 5)
        await self.command("/remove eggs")
        await self.command("/start")
        self.assertEqual(len(self.db.snapshot()["items"]), 4)

    async def test_add_prompt_without_ai_and_batch_validation(self):
        await self.start()
        await self.command("add", True)
        await self.command("coffee, apples")
        self.assertEqual(len(self.db.snapshot()["items"]), 7)
        await self.command("add", True)
        before = self.db.snapshot()["items"]
        await self.command("tea, 0 eggs")
        self.assertEqual(self.db.snapshot()["items"], before)
        self.assertEqual(self.db.snapshot()["awaiting"], "add")
        await self.command("tea")
        self.assertEqual(len(self.db.snapshot()["items"]), 8)

    async def test_items_show_plain_names_or_typed_numbers(self):
        await self.start()
        reply = await self.command("/list")
        self.assertIn("• eggs\n", reply["text"])
        self.assertNotIn("package", reply["text"])
        reply = await self.command("/add 3 lemons, milk")
        self.assertIn("• 3 lemons", reply["text"])
        self.assertIn("• milk", reply["text"])
        reply = await self.command("/add 5 lemons")
        self.assertIn("• 5 lemons", reply["text"])
        self.assertNotIn("3 lemons", reply["text"])
        reply = await self.command("/remove lemons")
        self.assertNotIn("lemons", reply["text"])

    async def test_add_and_remove_say_what_changed(self):
        await self.start()
        reply = await self.command("/add milk")
        self.assertTrue(reply["text"].startswith("Milk has been added.\n\n"))
        reply = await self.command("/remove eggs")
        self.assertTrue(reply["text"].startswith("Eggs have been removed."))
        await self.command("add", True)
        reply = await self.command("coffee, 3 lemons, apples")
        self.assertTrue(reply["text"].startswith("Coffee, 3 lemons and apples have been added."))
        reply = await self.command("remove", True)
        tap = reply["reply_markup"]["inline_keyboard"][0][0]["callback_data"]  # hash browns
        reply = await self.command(tap, True)
        self.assertTrue(reply["text"].startswith("Hash browns have been removed."))
        reply = await self.command("home", True)
        self.assertNotIn("One small step", reply["text"])

    async def test_old_units_are_removed_on_upgrade(self):
        await self.start()
        with self.db.transaction() as db:
            old = self.db.read(db)
            old["schema"] = 2
            old["items"] = [{"name": "bread", "quantity": 1, "unit": "package"},
                            {"name": "eggs", "quantity": 12, "unit": "each"},
                            {"name": "milk", "quantity": 2, "unit": "gallon"}]
            self.db.save(db, old)
        self.engine.initialize(NOW)
        reply = await self.command("/list")
        self.assertIn("• bread\n• 12 eggs\n• 2 gallon milk", reply["text"])

    async def test_duplicate_events_do_not_apply_twice(self):
        await self.start()
        await self.command("/add coffee", update_id=100)
        revision = self.db.snapshot()["revision"]
        await self.command("/add coffee", update_id=100)
        self.assertEqual(self.db.snapshot()["revision"], revision)

    async def test_remove_buttons_are_revision_checked(self):
        await self.start()
        reply = await self.command("remove", True)
        remove = reply["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await self.command(remove, True)
        self.assertNotIn("eggs", [x["name"] for x in self.db.snapshot()["items"]])
        reply = await self.command(remove, True)
        self.assertIn("list changed", reply["text"])
        self.assertEqual(len(self.db.snapshot()["items"]), 4)
        await self.command("delete:-1:" + str(self.db.snapshot()["revision"]), True)
        self.assertEqual(len(self.db.snapshot()["items"]), 4)

    async def test_removed_items_stay_removed_across_weeks(self):
        await self.start()
        await self.command("/remove eggs")
        self.engine.tick(NOW + timedelta(days=7))
        await self.command("/checkin")
        self.assertNotIn("eggs", [x["name"] for x in self.db.snapshot()["items"]])

    async def test_snooze_asks_then_accepts_number_after_restart(self):
        await self.start()
        reply = await self.command("snooze", True)
        self.assertIn("How many days", reply["text"])
        self.assertIsNone(self.db.snapshot()["snoozed_until"])
        self.engine = Engine(Database(self.settings.database), self.settings, self.client)
        self.engine.initialize(NOW)
        for invalid in ("0", "91", "-1", "1.5", "tomorrow", ""):
            reply = await self.command(invalid)
            self.assertIn("whole number", reply["text"])
            self.assertEqual(self.db.snapshot()["awaiting"], "snooze")
            self.assertIsNone(self.db.snapshot()["snoozed_until"])
        await self.command("10 days")
        wake = parse(self.db.snapshot()["snoozed_until"])
        self.assertIsNone(self.db.snapshot()["awaiting"])
        self.engine.tick(NOW + timedelta(days=7))
        self.assertEqual(len(self.reminders()), 0)
        self.engine.tick(wake)
        self.assertEqual(len(self.reminders()), 1)

    async def test_cancel_and_navigation_exit_prompts(self):
        await self.start()
        await self.command("snooze", True)
        await self.command("cancel", True)
        self.assertIsNone(self.db.snapshot()["awaiting"])
        self.assertIsNone(self.db.snapshot()["snoozed_until"])
        await self.command("add", True)
        await self.command("list", True)
        await self.command("coffee")
        self.assertNotIn("coffee", [x["name"] for x in self.db.snapshot()["items"]])

    async def test_shop_only_returns_list_without_network_or_purchase(self):
        await self.start()
        before = copy.deepcopy(self.db.snapshot()["items"])
        reply = await self.command("shop", True)
        self.assertIn("copy it", reply["text"])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.db.snapshot()["items"], before)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM purchases").fetchone()[0], 0)

    async def test_empty_list_stays_empty_and_offers_add(self):
        await self.start()
        for item in list(self.db.snapshot()["items"]):
            await self.command("/remove " + item["name"])
        await self.command("/start")
        self.engine.tick(NOW)
        reply = await self.command("/shop")
        self.assertIn("empty", reply["text"])
        self.assertEqual(self.db.snapshot()["items"], [])
        self.assertIn("Add item", str(reply["reply_markup"]))

    async def test_done_stops_followups_but_preserves_list(self):
        await self.start()
        self.engine.tick(NOW)
        before = copy.deepcopy(self.db.snapshot()["items"])
        await self.command("/done")
        self.assertEqual(self.db.snapshot()["items"], before)
        self.assertIsNone(self.db.snapshot()["followup"])
        self.engine.tick(NOW + timedelta(days=1))
        self.assertEqual(len(self.reminders()), 1)
        self.engine.tick(NOW + timedelta(days=7))
        self.assertEqual(len(self.reminders()), 2)

    async def test_legacy_inventory_controls_cannot_change_list(self):
        await self.start()
        before = self.db.snapshot()["items"]
        for command in ("stockmenu", "stock:0:5", "essentials", "confirm:1:5", "seed"):
            reply = await self.command(command, True)
            self.assertIn("simplified", reply["text"])
            self.assertEqual(self.db.snapshot()["items"], before)
        self.assertEqual(self.requests, [])

    async def test_migration_preserves_list_and_snooze_and_ignores_due_inventory(self):
        await self.start()
        await self.command("/snooze 10")
        with self.db.transaction() as db:
            old = self.db.read(db)
            old.update(schema=1, staples=[{"name": "old staple", "due": stamp(NOW)}], skipped=[],
                       active=False, cycle=2, link="https://old.example", pending={"actions": []})
            self.db.save(db, old)
        before = self.db.snapshot()
        self.engine.initialize(NOW)
        state = self.db.snapshot()
        self.assertEqual(state["schema"], 3)
        self.assertEqual(state["items"], before["items"])
        self.assertEqual(state["snoozed_until"], before["snoozed_until"])
        self.assertEqual(state["legacy_inventory"]["staples"], before["staples"])
        self.assertIsNone(state["pending"])
        await self.command("/resume")
        self.engine.tick(NOW + timedelta(days=1))
        self.assertNotIn("old staple", [x["name"] for x in self.db.snapshot()["items"]])

    async def test_no_inventory_trigger_between_weekly_checks(self):
        await self.start()
        await self.command("/done")
        self.engine.tick(NOW + timedelta(days=2))
        self.assertEqual(len(self.reminders()), 0)

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

    async def test_pause_and_snooze_cancel_queued_nudges(self):
        await self.start()
        self.engine.tick(NOW)
        await self.command("/snooze 3")
        telegram = FakeTelegram()
        while await deliver_one(self.db, self.settings, telegram, NOW):
            pass
        self.assertFalse(any("Let's make a little time" in x["text"] for x in telegram.sent))
        await self.command("/pause")
        self.engine.tick(NOW + timedelta(days=30))
        self.assertEqual(len(self.reminders()), 1)

    async def test_quiet_hours(self):
        await self.start()
        self.engine.tick(NOW + timedelta(hours=12))
        self.assertEqual(len(self.reminders()), 0)

    async def test_explicit_snooze_returns_at_promised_time_after_late_nudge(self):
        await self.start()
        late = NOW + timedelta(hours=10)
        self.engine.tick(late)
        await self.command("/snooze 1", now=late)
        self.engine.tick(NOW + timedelta(days=1))
        self.assertEqual(len(self.reminders()), 2)

    async def test_resume_waits_for_promised_slot(self):
        await self.start()
        await self.command("/pause")
        await self.command("/resume", now=NOW + timedelta(hours=2))
        self.engine.tick(NOW + timedelta(hours=2))
        self.assertEqual(len(self.reminders()), 0)
        self.engine.tick(NOW + timedelta(days=1))
        self.assertEqual(len(self.reminders()), 1)

    async def test_failed_delivery_retries_without_applying_input_twice(self):
        await self.start()
        failed = FakeTelegram(fail=True)
        self.assertFalse(await deliver_one(self.db, self.settings, failed, NOW))
        telegram = FakeTelegram()
        while await deliver_one(self.db, self.settings, telegram, NOW + timedelta(hours=1)):
            pass
        self.assertEqual(len(telegram.sent), 1)
        self.assertEqual(len(self.db.snapshot()["items"]), 5)

    async def test_ai_proposal_requires_confirmation_and_validates(self):
        await self.start()
        self.engine.settings = replace(self.settings, openai_key="fake")
        proposal = {"clarification": "", "actions": [
            {"type": "remove", "item": "eggs", "quantity": None, "days": 1},
            {"type": "add", "item": "coffee", "quantity": 2, "days": 1}]}
        payload = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(proposal)}]}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
            self.engine.client = client
            reply = await self.command("Remove eggs and add two packages of coffee")
        self.assertIn("eggs", [x["name"] for x in self.db.snapshot()["items"]])
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
