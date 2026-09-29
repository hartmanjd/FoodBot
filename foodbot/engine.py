import copy
import json
import logging
import re
import secrets
from datetime import timedelta

from .assistant import UNITS, interpret
from .schedule import stamp, parse, local_slot, next_weekly, next_daily, pretty, is_quiet

log = logging.getLogger(__name__)

HELP = """One small step at a time 🌱
Review groceries — add or remove items with the buttons.
Shop — get a list to copy into your shopping app.
Snooze — choose how many days until the next check-in.

/list — review groceries
/add coffee — add an item
/remove coffee — remove an item
/snooze — choose a delay (or /snooze 3)
/shop — get your shopping list
/done — finish this week's check-ins; keep your list
/pause — stop automatic check-ins
/resume — restart check-ins
/status — see your next reminder
/checkin — try a check-in now

Optional quantities: /add eggs | 12 | each
Units: each, package, gallon, liter, oz, lb, can, bunch.
Your list stays saved between trips until you change it."""

STARTERS = [("eggs", 12, "each"), ("hash browns", 1, "package"),
            ("Greek yogurt", 1, "package"), ("bread", 1, "package"),
            ("English muffins", 1, "package")]


def button(text, data):
    return {"text": text, "callback_data": data}


def menu():
    return [[button("Review groceries", "list"), button("Shop", "shop")],
            [button("Snooze", "snooze")]]


def review_menu():
    return [[button("Add item", "add"), button("Remove item", "remove")],
            [button("Back", "home")]]


def cancel_menu():
    return [[button("Cancel", "cancel")]]


def initial(settings, now):
    return {"schema": 2, "started": False, "paused": False, "snoozed_until": None,
            "next_checkin": stamp(next_weekly(now, settings)), "followup": None,
            "followups": 0, "generation": 0, "revision": 0,
            "items": [], "pending": None, "awaiting": None, "last_nudge": None,
            "cooldown_until": None}


def normalized(name):
    name = " ".join(name.strip().split())
    if not name or len(name) > 80:
        raise ValueError("Use an item name between 1 and 80 characters.")
    return name


def find_item(items, name):
    return next((x for x in items if x["name"].casefold() == name.casefold()), None)


def edit(state):
    state["revision"] += 1
    state["generation"] += 1
    state["pending"] = None


def cancel_reminders(state):
    state["generation"] += 1
    state["followup"] = None
    state["followups"] = 0


def add_item(state, name, quantity=1, unit="package"):
    name = normalized(name)
    if unit not in UNITS or not 0 < quantity <= 100:
        raise ValueError("Quantity must be 0–100 (above zero); see /help for units.")
    old = find_item(state["items"], name)
    if not old and len(state["items"]) >= 20:
        raise ValueError("Keep this list to 20 items. Remove an item before adding more.")
    if old:
        old.update(quantity=quantity, unit=unit)
    else:
        state["items"].append({"name": name, "quantity": quantity, "unit": unit})
    edit(state)


def list_text(state):
    if not state["items"]:
        return "Your grocery list is empty. Tap Add item in Review groceries to get started."
    return "Your groceries 🛒\n" + "\n".join(
        f"• {x['name']} — {x['quantity']:g} {x['unit']}" for x in state["items"])


class Engine:
    def __init__(self, database, settings, client):
        self.db, self.settings, self.client = database, settings, client

    def initialize(self, now):
        with self.db.transaction() as db:
            state = self.db.read(db) or initial(self.settings, now)
            if state.get("schema", 1) < 2:
                # Preserve historical inventory settings without using them for reminders.
                state["legacy_inventory"] = {key: state.pop(key) for key in
                    ("staples", "skipped", "cycle", "active", "link") if key in state}
                state["schema"] = 2
                state["awaiting"] = None
                state["pending"] = None
                state["revision"] += 1
                state["generation"] += 1
                # Old queued messages contain controls that no longer apply.
                db.execute("UPDATE outbox SET sent=1 WHERE sent=0")
            schedule = [self.settings.timezone, self.settings.day, self.settings.checkin_time]
            if state.get("schedule") != schedule:
                state["next_checkin"] = stamp(next_weekly(now, self.settings))
                if state["followup"]:
                    state["followup"] = stamp(next_daily(now, self.settings))
                state["generation"] += 1
                state["schedule"] = schedule
            self.db.save(db, state)

    def nudge(self, state, now, followup=False):
        if followup:
            return "A little grocery nudge 🌱 Your list is here whenever you're ready. One tap is enough."
        return "Grocery check-in 🌱 Let's make a little time for groceries. Review your list, shop, or snooze until a better day."

    def tick(self, now):
        with self.db.transaction() as db:
            state = self.db.read(db)
            if not state["started"] or state["paused"] or is_quiet(now, self.settings):
                return
            waking = state["snoozed_until"] and now >= parse(state["snoozed_until"])
            if not waking and state["last_nudge"] and (now - parse(state["last_nudge"])) < timedelta(hours=20):
                return
            if state["snoozed_until"] and now < parse(state["snoozed_until"]):
                return
            if state["cooldown_until"] and now < parse(state["cooldown_until"]):
                return
            # Do not create another reminder while one is waiting for delivery.
            if db.execute("SELECT 1 FROM outbox WHERE sent=0 AND kind='reminder' AND generation=?",
                          (state["generation"],)).fetchone():
                return
            weekly = now >= parse(state["next_checkin"])
            wake = bool(state["snoozed_until"])
            followup = bool(state["followup"] and now >= parse(state["followup"]))
            if not (weekly or wake or followup):
                return
            if weekly:
                state["followups"] = 0
                state["next_checkin"] = stamp(next_weekly(now, self.settings))
            text = self.nudge(state, now, followup=followup and not weekly and not wake)
            state["last_nudge"] = stamp(now)
            if followup and not weekly and not wake:
                state["followups"] += 1
            state["snoozed_until"] = None
            if state["followups"] < self.settings.max_followups:
                state["followup"] = stamp(local_slot(now, self.settings, 1))
            else:
                state["followup"] = None
                state["cooldown_until"] = state["next_checkin"]
            self.db.queue(db, text, menu(), "reminder", state["generation"])
            self.db.save(db, state)

    def snooze(self, state, days, now):
        if not 1 <= days <= 90:
            raise ValueError("Choose 1–90 days, for example /snooze 3.")
        cancel_reminders(state)
        state["paused"] = False
        state["snoozed_until"] = stamp(local_slot(now, self.settings, days))
        state["cooldown_until"] = None
        return f"Absolutely. I'll check in {pretty(state['snoozed_until'], self.settings)}. Your list is saved."

    def apply_action(self, state, action, now):
        if action["type"] == "add":
            add_item(state, action["item"], action["quantity"], action["unit"])
        elif action["type"] == "remove":
            self.remove(state, action["item"])
        elif action["type"] == "snooze":
            self.snooze(state, action["days"], now)
        else:
            raise ValueError("That action is no longer supported.")

    def remove(self, state, name):
        name = normalized(name)
        if not find_item(state["items"], name):
            raise ValueError("I couldn't find that item. Tap Remove item to choose from your list.")
        state["items"] = [x for x in state["items"] if x["name"].casefold() != name.casefold()]
        edit(state)

    def add_input(self, state, text):
        # One item per line (or comma); the whole batch is validated before commit.
        entries = re.split(r"[\n,]+", text)
        for entry in entries:
            parts = [x.strip() for x in entry.split("|")]
            if not 1 <= len(parts) <= 3:
                raise ValueError("Use an item name, or name | quantity | unit.")
            add_item(state, parts[0], float(parts[1]) if len(parts) > 1 else 1,
                     parts[2] if len(parts) > 2 else "package")

    async def process_one(self, now):
        with self.db.connect() as db:
            row = db.execute("SELECT * FROM inbox WHERE done=0 ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False
        update = json.loads(row["payload"])
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        text = callback.get("data", "") if callback else message.get("text", "")
        state = self.db.snapshot()
        candidate = copy.deepcopy(state)
        try:
            reply, buttons, _ = await self.handle(candidate, text, bool(callback), now)
        except (ValueError, IndexError, KeyError):
            candidate = state
            reply = "I couldn't apply that. Use an item name, or name | quantity | unit. Your list is unchanged."
            buttons = cancel_menu() if state.get("awaiting") else review_menu()
        # External work finishes before the transaction; only one worker mutates state.
        with self.db.transaction() as db:
            if db.execute("SELECT done FROM inbox WHERE id=?", (row["id"],)).fetchone()[0]:
                return True
            self.db.save(db, candidate)
            self.db.queue(db, reply, buttons)
            db.execute("UPDATE inbox SET done=1 WHERE id=?", (row["id"],))
        return True

    async def handle(self, state, text, callback, now):
        if callback:
            command, _, arg = text.partition(":")
        else:
            text = text.strip()
            match = re.fullmatch(r"(?:snooze|snooze for) (\d+)(?: days?)?", text, re.I)
            if match:
                text = "/snooze " + match[1]
            if text.lower() in ("ordered", "i ordered", "i've ordered"):
                text = "/done"
            if text.lower() in ("help", "list", "pause", "resume", "status", "snooze", "shop", "cancel", "done"):
                text = "/" + text.lower()
            command, _, arg = text.partition(" ")
            command = command.lower().split("@")[0].lstrip("/")
            if not text.startswith("/"):
                command = "__natural__"
        arg = arg.strip()

        # Prompts persist across restarts. Any explicit command or button leaves
        # the old prompt; ordinary text answers it without requiring an AI key.
        awaiting = state.get("awaiting")
        if command == "__natural__" and awaiting:
            if awaiting == "snooze":
                match = re.fullmatch(r"([0-9]{1,2})(?: days?)?", text, re.I)
                if not match or not 1 <= int(match[1]) <= 90:
                    return "How many days until your next check-in? Send a whole number from 1 to 90, or tap Cancel.", cancel_menu(), None
                reply = self.snooze(state, int(match[1]), now)
                state["awaiting"] = None
                return reply, menu(), None
            if awaiting == "add":
                if not text:
                    return "Send the item you'd like to add, or tap Cancel.", cancel_menu(), None
                self.add_input(state, text)
                state["awaiting"] = None
                return list_text(state), review_menu(), None
        if command != "__natural__":
            state["awaiting"] = None
            if command != "apply":
                state["pending"] = None

        if command == "start":
            if not state["started"]:
                if not state["items"]:
                    for name, qty, unit in STARTERS:
                        add_item(state, name, qty, unit)
                state["started"] = True
            return ("Hi! I'm Foodbot 🌱 Let's keep grocery shopping simple.\n"
                    f"Your next weekly check-in is {pretty(state['next_checkin'], self.settings)}.", menu(), None)
        if not state["started"]:
            return "Send /start first so I can begin your grocery check-ins.", None, None
        if command == "home":
            return "One small step toward groceries 🌱", menu(), None
        if command == "help":
            return HELP, menu(), None
        if command == "list":
            return list_text(state), review_menu(), None
        if command == "checkin":
            return self.nudge(state, now), menu(), None
        if command == "status":
            if state["paused"]:
                next_text = "Automatic reminders are paused. /resume brings them back."
            elif state["snoozed_until"]:
                next_text = "Snoozed until " + pretty(state["snoozed_until"], self.settings)
            elif state["cooldown_until"] and parse(state["cooldown_until"]) > now:
                next_text = "Taking a quiet break until " + pretty(state["cooldown_until"], self.settings)
            else:
                dates = [state["next_checkin"]]
                if state["followup"]:
                    dates.append(state["followup"])
                next_text = "Next reminder due: " + pretty(min(dates), self.settings)
            return (next_text + f"\nTimezone: {self.settings.timezone}. Quiet hours: "
                    f"{self.settings.quiet_start}:00–{self.settings.quiet_end}:00.\n"
                    f"At most {self.settings.max_followups} follow-ups per cycle."
                    "\nOverdue reminders arrive at the next eligible worker check; no backlog of nudges.", menu(), None)
        if command == "pause":
            state["paused"] = True
            cancel_reminders(state)
            return "Check-ins paused. Your groceries are saved. Send /resume whenever you're ready.", None, None
        if command == "resume":
            state["paused"] = False
            state["snoozed_until"] = None
            state["cooldown_until"] = None
            cancel_reminders(state)
            state["next_checkin"] = stamp(next_daily(now, self.settings))
            state["cooldown_until"] = state["next_checkin"]
            return f"Welcome back. I'll check in {pretty(state['next_checkin'], self.settings)}.", menu(), None
        if command == "snooze":
            if not arg:
                state["awaiting"] = "snooze"
                return "How many days until your next check-in? Send a number from 1 to 90.", cancel_menu(), None
            if not re.fullmatch(r"[0-9]{1,2}", arg) or not 1 <= int(arg) <= 90:
                state["awaiting"] = "snooze"
                return "Choose a whole number from 1 to 90 days, or tap Cancel.", cancel_menu(), None
            return self.snooze(state, int(arg), now), menu(), None
        if command == "add":
            if not arg:
                state["awaiting"] = "add"
                return "What would you like to add? Send an item name, or several names separated by commas or new lines.\nFor quantities, try eggs | 12 | each.", cancel_menu(), None
            self.add_input(state, arg)
            return list_text(state), review_menu(), None
        if command in ("remove", "skip"):
            if arg:
                self.remove(state, arg)
                return list_text(state), review_menu(), None
            if not state["items"]:
                return "Your list is empty. Tap Add item to get started.", review_menu(), None
            return "Tap an item to remove it.", [
                [button(x["name"], f"delete:{i}:{state['revision']}")]
                for i, x in enumerate(state["items"])
            ] + [[button("Back to groceries", "list")]], None
        if command == "delete":
            index, revision = map(int, arg.split(":"))
            if revision != state["revision"] or not 0 <= index < len(state["items"]):
                return "Your list changed. Tap Remove item again to choose from the current list.", review_menu(), None
            self.remove(state, state["items"][index]["name"])
            return list_text(state), review_menu(), None
        if command == "shop":
            if not state["items"]:
                return "Your list is empty. Add a few groceries first.", review_menu(), None
            return "Here's your shopping list—copy it into your store app or take it with you.\n\n" + list_text(state) + "\n\nWhen you've finished shopping, send /done to stop this week's follow-ups.", menu(), None
        if command in ("done", "ordered"):
            cancel_reminders(state)
            state["snoozed_until"] = None
            state["next_checkin"] = stamp(next_weekly(now, self.settings))
            state["cooldown_until"] = state["next_checkin"]
            return "All set 🌱 I'll check in next week. Your list stays saved for the next trip.", menu(), None
        if command in ("stockmenu", "stock", "stocked", "essentials", "staples", "staple", "forget", "seed", "confirm"):
            return "We've simplified things. Use Review groceries to add or remove items, Shop for your list, or Snooze for a later check-in.", menu(), None
        if command == "apply":
            pending = state["pending"]
            if not pending or pending["id"] != arg or parse(pending["expires"]) < now:
                return "That suggestion has expired or the list changed. Send the request again.", menu(), None
            for action in pending["actions"]:
                self.apply_action(state, action, now)
            state["pending"] = None
            return "Done.\n\n" + list_text(state), review_menu(), None
        if command == "cancel":
            state["pending"] = None
            return "Cancelled. Your list and reminder timing are unchanged.", menu(), None
        if not text or text.startswith("/") or callback:
            return "Use /help for commands. Receipt uploads aren't supported in this first version.", None, None
        if not self.settings.openai_key:
            return "Tap Review groceries to add or remove items, or Snooze to choose your next check-in.", menu(), None
        try:
            proposal = await interpret(self.client, self.settings, text[:2000], state)
            if proposal.clarification or not proposal.actions:
                return proposal.clarification or "What would you like to add or remove?", None, None
            actions = [x.model_dump() for x in proposal.actions]
            test_state = copy.deepcopy(state)
            for action in actions:
                self.apply_action(test_state, action, now)
            token = secrets.token_hex(6)
            state["pending"] = {"id": token, "actions": actions, "expires": stamp(now + timedelta(minutes=30))}
            summaries = []
            for a in actions:
                summaries.append({"add": f"Set {a['item']} to {a['quantity']:g} {a['unit']}",
                                  "remove": f"Remove {a['item']} from your list",
                                  "snooze": f"Snooze all nudges for {a['days']} days"}[a["type"]])
            return "Apply these edits?\n" + "\n".join("• " + x for x in summaries), [[button("Apply", "apply:" + token), button("Cancel", "cancel")]], None
        except Exception as exc:
            log.warning("Language parsing failed (%s)", type(exc).__name__)
            return "I couldn't interpret that just now. Your groceries are unchanged. Tap Review groceries to edit your list, or Snooze to choose a delay.", menu(), None
