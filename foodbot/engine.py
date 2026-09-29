import copy
import json
import logging
import re
import secrets
from datetime import timedelta

from .assistant import interpret
from .schedule import stamp, parse, local_slot, next_weekly, next_daily, pretty, is_quiet

log = logging.getLogger(__name__)

HELP = """One small step at a time 🌱
Groceries — see your list, add or remove items, or start a new list.
Shop — see your current list with the date and time.
Snooze is in the Monday check-in message: it sets an extra reminder.

/list — see your groceries
/add coffee — add an item
/remove coffee, eggs — remove one or more items
/newlist eggs, milk — replace your whole list
/snooze 3 — an extra reminder in 3 days (Monday check-in stays)
/shop — see your current list
/done — finish this week's check-ins; keep your list
/pause — stop automatic check-ins
/resume — restart check-ins
/status — see your next reminder
/checkin — try a check-in now

Want an amount? Put the number first: /add 3 lemons
Your list stays saved between trips until you change it."""

STARTERS = ["eggs", "hash browns", "Greek yogurt", "bread", "English muffins"]


def button(text, data):
    return {"text": text, "callback_data": data}


def menu():
    """The home screen: just Groceries and Shop."""
    return [[button("Groceries", "list"), button("Shop", "shop")]]


def reminder_menu():
    """Buttons on the automatic check-in messages. Only these have Snooze."""
    return [[button("Groceries", "list"), button("Shop", "shop")],
            [button("Snooze", "snooze")]]


def review_menu():
    return [[button("Add item", "add"), button("Remove item", "remove")],
            [button("New list", "newlist"), button("Back", "home")]]


def remove_buttons(state):
    """One button per item, plus a Done button. Tap as many as you like."""
    rows = []
    for i, item in enumerate(state["items"]):
        rows.append([button(item_text(item), f"delete:{i}:{state['revision']}")])
    rows.append([button("Done removing", "list")])
    return rows


def cancel_menu():
    return [[button("Cancel", "cancel")]]


def initial(settings, now):
    return {"schema": 3, "started": False, "paused": False, "snoozed_until": None,
            "next_checkin": stamp(next_weekly(now, settings)), "followup": None,
            "followups": 0, "generation": 0, "revision": 0,
            "items": [], "pending": None, "awaiting": {}, "last_nudge": None,
            "cooldown_until": None}


def normalized(name):
    name = " ".join(name.strip().split())
    if not name or len(name) > 80:
        raise ValueError("Use an item name between 1 and 80 characters.")
    return name


def split_quantity(text):
    """Turn "3 lemons" into ("lemons", 3). Plain "eggs" becomes ("eggs", None)."""
    text = text.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s+(.+)", text)
    if match:
        return match[2], float(match[1])
    return text, None


def item_text(item):
    """How an item is shown: "eggs", or "3 lemons" if it has a number."""
    if item.get("quantity") is None:
        return item["name"]
    return f"{item['quantity']:g} {item['name']}"


def changed_text(names, verb):
    """(["milk"], "added") -> "Milk has been added."
    (["eggs"], "removed") -> "Eggs have been removed." """
    if len(names) == 1:
        joined = names[0]
    else:
        joined = ", ".join(names[:-1]) + " and " + names[-1]
    # Simple grammar guess: several items, or a name ending in "s", gets "have".
    if len(names) > 1 or names[0].endswith("s"):
        helper = "have"
    else:
        helper = "has"
    sentence = f"{joined} {helper} been {verb}."
    return sentence[0].upper() + sentence[1:]


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


def add_item(state, name, quantity=None):
    name = normalized(name)
    if quantity is not None and not 0 < quantity <= 999:
        raise ValueError("Numbers must be above 0 and at most 999.")
    old = find_item(state["items"], name)
    if not old and len(state["items"]) >= 20:
        raise ValueError("Keep this list to 20 items. Remove an item before adding more.")
    if old:
        # Adding an item that's already there just updates its number (or clears it).
        old["quantity"] = quantity
    else:
        state["items"].append({"name": name, "quantity": quantity})
    edit(state)


def list_text(state):
    if not state["items"]:
        return "Your grocery list is empty. Tap Add item to get started."
    return "Your groceries 🛒\n" + "\n".join(
        "• " + item_text(x) for x in state["items"])


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
            if not isinstance(state.get("awaiting"), dict):
                # Each person now has their own open question (like "What would you like to add?"),
                # so one person's typing can't answer the other person's question.
                state["awaiting"] = {}
            schedule = [self.settings.timezone, self.settings.day, self.settings.checkin_time]
            if state.get("schedule") != schedule:
                state["next_checkin"] = stamp(next_weekly(now, self.settings))
                if state["followup"]:
                    state["followup"] = stamp(next_daily(now, self.settings))
                state["generation"] += 1
                state["schedule"] = schedule
            if state["schema"] < 3:
                # Older lists stored a unit on every item ("bread — 1 package").
                # Drop the units so the list shows plain names.
                for item in state["items"]:
                    unit = item.pop("unit", None)
                    if unit == "package" or (unit == "each" and item.get("quantity") == 1):
                        item["quantity"] = None
                    elif unit and unit != "each":
                        item["name"] = unit + " " + item["name"]  # "2 gallon milk"
                state["schema"] = 3
                state["pending"] = None
                state["revision"] += 1
            self.db.save(db, state)

    def nudge(self, state, now, followup=False, extra=False):
        if extra:
            return "Here's the extra grocery reminder you asked for 🌱 Your list is ready when you are."
        if followup:
            return "A little grocery nudge 🌱 Your list is here whenever you're ready. One tap is enough."
        return ("Grocery check-in 🌱 Let's make a little time for groceries. "
                "Check your list, shop, or tap Snooze for an extra reminder in a few days.")

    def tick(self, now):
        with self.db.transaction() as db:
            state = self.db.read(db)
            if not state["started"] or state["paused"] or is_quiet(now, self.settings):
                return
            # "snoozed_until" is the extra reminder from Snooze. It never delays
            # the regular Monday check-in; it's one more reminder on top of it.
            extra = bool(state["snoozed_until"]) and now >= parse(state["snoozed_until"])
            if not extra and state["last_nudge"] and (now - parse(state["last_nudge"])) < timedelta(hours=20):
                return
            if not extra and state["cooldown_until"] and now < parse(state["cooldown_until"]):
                return
            # Do not create another reminder while one is waiting for delivery.
            if db.execute("SELECT 1 FROM outbox WHERE sent=0 AND kind='reminder' AND generation=?",
                          (state["generation"],)).fetchone():
                return
            weekly = now >= parse(state["next_checkin"])
            followup = bool(state["followup"] and now >= parse(state["followup"]))
            if not (weekly or extra or followup):
                return
            if weekly:
                state["followups"] = 0
                state["next_checkin"] = stamp(next_weekly(now, self.settings))
            only_followup = followup and not weekly and not extra
            only_extra = extra and not weekly
            text = self.nudge(state, now, followup=only_followup, extra=only_extra)
            state["last_nudge"] = stamp(now)
            if only_followup:
                state["followups"] += 1
            if extra:
                state["snoozed_until"] = None
            if only_extra:
                # The extra reminder is a one-off: no follow-ups after it.
                state["followup"] = None
            elif state["followups"] < self.settings.max_followups:
                state["followup"] = stamp(local_slot(now, self.settings, 1))
            else:
                state["followup"] = None
                state["cooldown_until"] = state["next_checkin"]
            self.db.queue(db, text, reminder_menu(), "reminder", state["generation"])
            self.db.save(db, state)

    def snooze(self, state, days, now):
        if not 1 <= days <= 90:
            raise ValueError("Choose 1–90 days, for example /snooze 3.")
        cancel_reminders(state)
        state["paused"] = False
        state["snoozed_until"] = stamp(local_slot(now, self.settings, days))
        state["cooldown_until"] = None
        return (f"Got it. I'll send an extra reminder {pretty(state['snoozed_until'], self.settings)}.\n"
                f"Your regular check-in is still {pretty(state['next_checkin'], self.settings)}.")

    def apply_action(self, state, action, now):
        if action["type"] == "add":
            add_item(state, action["item"], action["quantity"])
            added = item_text({"name": action["item"], "quantity": action["quantity"]})
            return changed_text([added], "added")
        elif action["type"] == "remove":
            removed = self.remove(state, action["item"])
            return changed_text([removed], "removed")
        elif action["type"] == "snooze":
            return self.snooze(state, action["days"], now)
        else:
            raise ValueError("That action is no longer supported.")

    def remove(self, state, name):
        name, _ = split_quantity(name)  # "/remove 3 lemons" also works
        name = normalized(name)
        item = find_item(state["items"], name)
        if not item:
            raise ValueError(f"I couldn't find {name} on your list.")
        state["items"] = [x for x in state["items"] if x["name"].casefold() != name.casefold()]
        edit(state)
        return item["name"]

    def add_input(self, state, text):
        # One item per line (or comma); the whole batch is validated before commit.
        # "eggs" adds just eggs. "3 lemons" adds lemons with the number 3.
        entries = re.split(r"[\n,]+", text)
        added = []
        for entry in entries:
            name, quantity = split_quantity(entry)
            add_item(state, name, quantity)
            added.append(item_text({"name": normalized(name), "quantity": quantity}))
        return added

    def remove_input(self, state, text):
        # Several items separated by commas or new lines: "eggs, bread".
        # If any one of them isn't on the list, nothing is removed.
        removed = []
        for entry in re.split(r"[\n,]+", text):
            removed.append(self.remove(state, entry))
        return removed

    def new_list(self, state, text):
        # Throw away the old list and add the new items.
        state["items"] = []
        self.add_input(state, text)
        return "Your new list is ready.\n\n" + list_text(state)

    async def process_one(self, now):
        with self.db.connect() as db:
            row = db.execute("SELECT * FROM inbox WHERE done=0 ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False
        update = json.loads(row["payload"])
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        text = callback.get("data", "") if callback else message.get("text", "")
        # Who sent it, and which chat to answer in (private chat or the shared group).
        sender = (callback or message).get("from", {}).get("id")
        chat_id = message.get("chat", {}).get("id")
        state = self.db.snapshot()
        candidate = copy.deepcopy(state)
        try:
            reply, buttons, _ = await self.handle(candidate, text, bool(callback), now, sender)
        except (ValueError, IndexError, KeyError) as exc:
            candidate = state
            if isinstance(exc, ValueError):
                reply = f"{exc} Your list is unchanged."
            else:
                reply = "I couldn't apply that. Your list is unchanged."
            waiting = state["awaiting"].get(str(sender))
            buttons = cancel_menu() if waiting else review_menu()
        # External work finishes before the transaction; only one worker mutates state.
        with self.db.transaction() as db:
            if db.execute("SELECT done FROM inbox WHERE id=?", (row["id"],)).fetchone()[0]:
                return True
            self.db.save(db, candidate)
            self.db.queue(db, reply, buttons, chat_id=chat_id)
            db.execute("UPDATE inbox SET done=1 WHERE id=?", (row["id"],))
        return True

    async def handle(self, state, text, callback, now, sender=None):
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
        # Each person has their own open question, saved under their Telegram ID.
        who = str(sender)
        awaiting = state["awaiting"].get(who)
        if command == "__natural__" and awaiting:
            if awaiting == "snooze":
                match = re.fullmatch(r"([0-9]{1,2})(?: days?)?", text, re.I)
                if not match or not 1 <= int(match[1]) <= 90:
                    return "In how many days should I send the extra reminder? Send a whole number from 1 to 90, or tap Cancel.", cancel_menu(), None
                reply = self.snooze(state, int(match[1]), now)
                state["awaiting"].pop(who, None)
                return reply, menu(), None
            if not text:
                return "Send the items, or tap Cancel.", cancel_menu(), None
            if awaiting == "add":
                added = self.add_input(state, text)
                state["awaiting"].pop(who, None)
                return changed_text(added, "added") + "\n\n" + list_text(state), review_menu(), None
            if awaiting == "remove":
                removed = self.remove_input(state, text)
                state["awaiting"].pop(who, None)
                return changed_text(removed, "removed") + "\n\n" + list_text(state), review_menu(), None
            if awaiting == "newlist":
                reply = self.new_list(state, text)
                state["awaiting"].pop(who, None)
                return reply, review_menu(), None
        if command != "__natural__":
            state["awaiting"].pop(who, None)
            if command != "apply":
                state["pending"] = None

        if command == "start":
            if not state["started"]:
                if not state["items"]:
                    for name in STARTERS:
                        add_item(state, name)
                state["started"] = True
            return ("Hi! I'm Foodbot 🌱 Let's keep grocery shopping simple.\n"
                    f"Your next weekly check-in is {pretty(state['next_checkin'], self.settings)}.", menu(), None)
        if not state["started"]:
            return "Send /start first so I can begin your grocery check-ins.", None, None
        if command == "home":
            return "Main menu 🌱", menu(), None
        if command == "help":
            return HELP, menu(), None
        if command == "list":
            return list_text(state), review_menu(), None
        if command == "checkin":
            return self.nudge(state, now), reminder_menu(), None
        if command == "status":
            if state["paused"]:
                next_text = "Automatic reminders are paused. /resume brings them back."
            elif state["cooldown_until"] and parse(state["cooldown_until"]) > now:
                next_text = "Taking a quiet break until " + pretty(state["cooldown_until"], self.settings)
            else:
                dates = [state["next_checkin"]]
                if state["followup"]:
                    dates.append(state["followup"])
                next_text = "Next reminder due: " + pretty(min(dates), self.settings)
            if state["snoozed_until"] and not state["paused"]:
                next_text += "\nExtra reminder: " + pretty(state["snoozed_until"], self.settings)
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
                state["awaiting"][who] = "snooze"
                return ("In how many days should I send an extra reminder? Send a number from 1 to 90.\n"
                        "Your regular Monday check-in stays the same.", cancel_menu(), None)
            if not re.fullmatch(r"[0-9]{1,2}", arg) or not 1 <= int(arg) <= 90:
                state["awaiting"][who] = "snooze"
                return "Choose a whole number from 1 to 90 days, or tap Cancel.", cancel_menu(), None
            return self.snooze(state, int(arg), now), menu(), None
        if command == "add":
            if not arg:
                state["awaiting"][who] = "add"
                return "What would you like to add? Send an item name, or several names separated by commas or new lines.\nWant an amount? Put the number first, like 3 lemons.", cancel_menu(), None
            added = self.add_input(state, arg)
            return changed_text(added, "added") + "\n\n" + list_text(state), review_menu(), None
        if command in ("remove", "skip"):
            if arg:
                removed = self.remove_input(state, arg)
                return changed_text(removed, "removed") + "\n\n" + list_text(state), review_menu(), None
            if not state["items"]:
                return "Your list is empty. Tap Add item to get started.", review_menu(), None
            state["awaiting"][who] = "remove"
            return ("Tap items to remove them, or type several at once like eggs, bread.",
                    remove_buttons(state), None)
        if command == "delete":
            index, revision = map(int, arg.split(":"))
            if revision != state["revision"] or not 0 <= index < len(state["items"]):
                return "Your list changed. Tap Remove item again to choose from the current list.", review_menu(), None
            removed = self.remove(state, state["items"][index]["name"])
            if not state["items"]:
                return changed_text([removed], "removed") + "\n\nYour list is now empty.", review_menu(), None
            # Keep the remove buttons open so you can tap another one.
            state["awaiting"][who] = "remove"
            return (changed_text([removed], "removed") + " Tap another item, or Done removing.",
                    remove_buttons(state), None)
        if command == "newlist":
            if arg:
                return self.new_list(state, arg), review_menu(), None
            state["awaiting"][who] = "newlist"
            return ("Send your new list, with items separated by commas or new lines, like eggs, milk, 3 lemons.\n"
                    "This replaces your current list.", cancel_menu(), None)
        if command == "shop":
            if not state["items"]:
                return "Your list is empty. Add a few groceries first.", review_menu(), None
            # For now Shop just shows the list with the current date and time.
            # (Later this is where an Instacart connection could go.)
            items = "\n".join("• " + item_text(x) for x in state["items"])
            return f"Shopping list 🛒\n{pretty(stamp(now), self.settings)}\n\n{items}", menu(), None
        if command in ("done", "ordered"):
            cancel_reminders(state)
            state["snoozed_until"] = None
            state["next_checkin"] = stamp(next_weekly(now, self.settings))
            state["cooldown_until"] = state["next_checkin"]
            return "All set 🌱 I'll check in next week. Your list stays saved for the next trip.", menu(), None
        if command in ("stockmenu", "stock", "stocked", "essentials", "staples", "staple", "forget", "seed", "confirm"):
            return "We've simplified things. Use Groceries to add or remove items, or Shop for your list.", menu(), None
        if command == "apply":
            pending = state["pending"]
            if not pending or pending["id"] != arg or parse(pending["expires"]) < now:
                return "That suggestion has expired or the list changed. Send the request again.", menu(), None
            messages = []
            for action in pending["actions"]:
                messages.append(self.apply_action(state, action, now))
            state["pending"] = None
            return "\n".join(messages) + "\n\n" + list_text(state), review_menu(), None
        if command == "cancel":
            state["pending"] = None
            return "Cancelled. Your list and reminder timing are unchanged.", menu(), None
        if not text or text.startswith("/") or callback:
            return "Use /help for commands. Receipt uploads aren't supported in this first version.", None, None
        if not self.settings.openai_key:
            return "Tap Groceries to add or remove items, or Shop to see your list.", menu(), None
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
                summaries.append({"add": "Add " + item_text({"name": a["item"], "quantity": a["quantity"]}),
                                  "remove": f"Remove {a['item']} from your list",
                                  "snooze": f"Send an extra reminder in {a['days']} days"}[a["type"]])
            return "Apply these edits?\n" + "\n".join("• " + x for x in summaries), [[button("Apply", "apply:" + token), button("Cancel", "cancel")]], None
        except Exception as exc:
            log.warning("Language parsing failed (%s)", type(exc).__name__)
            return "I couldn't interpret that just now. Your groceries are unchanged. Tap Groceries to edit your list.", menu(), None
