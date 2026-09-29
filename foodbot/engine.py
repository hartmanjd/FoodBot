import copy
import json
import logging
import re
import secrets
from datetime import timedelta

from .assistant import UNITS, interpret
from .clients import shopping_link
from .schedule import stamp, parse, local_slot, next_weekly, next_daily, pretty, is_quiet

log = logging.getLogger(__name__)

HELP = """One small step at a time 🌱
/list — review groceries
/add coffee | 2 | package — add or set quantity
/skip milk — skip this shopping cycle
/stocked eggs | 5 — ask about eggs again in 5 days
/snooze 3 — pause every automatic nudge for 3 days
/essentials — keep only staples marked essential
/shop — make an Instacart link (or copyable list)
/ordered — review and confirm what you bought
/staples — see your usual groceries
/staple eggs | 12 | each | 10 | yes — repeat every 10 days; essential
/forget eggs — stop suggesting this staple
/pause — stop automatic check-ins
/resume — turn check-ins back on
/status — see the next reminder
/checkin — try a check-in now

Names may include a brand. Supported units: each, package, gallon, liter, oz, lb, can, bunch.
You can also say “snooze for 3 days”. With an OpenAI key, casual list edits are proposed for your review.
Receipt images/PDFs and automatic receipt learning are planned for later."""


def button(text, data):
    return {"text": text, "callback_data": data}


def menu():
    return [[button("Review groceries", "list"), button("Shop", "shop")],
            [button("Tomorrow", "snooze:1"), button("In 3 days", "snooze:3"), button("In 7 days", "snooze:7")],
            [button("Still stocked", "stockmenu"), button("Just essentials", "essentials")],
            [button("I ordered", "ordered")]]


def initial(settings, now):
    return {"schema": 1, "started": False, "paused": False, "snoozed_until": None,
            "next_checkin": stamp(next_weekly(now, settings)), "followup": None,
            "followups": 0, "generation": 0, "revision": 0, "cycle": 0,
            "active": False, "items": [], "staples": [], "skipped": [],
            "link": None, "pending": None, "last_nudge": None,
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
    state["link"] = None
    state["pending"] = None


def cancel_reminders(state):
    state["generation"] += 1
    state["followup"] = None
    state["followups"] = 0


def begin_cycle(state):
    if not state["active"]:
        state["active"] = True
        state["cycle"] += 1
        state["skipped"] = []


def add_item(state, name, quantity=1, unit="package"):
    name = normalized(name)
    if unit not in UNITS or not 0 < quantity <= 100:
        raise ValueError("Quantity must be 0–100 (above zero); see /help for units.")
    begin_cycle(state)
    old = find_item(state["items"], name)
    if not old and len(state["items"]) >= 20:
        raise ValueError("Keep this list to 20 items. Finish this trip before adding more.")
    if old:
        old.update(quantity=quantity, unit=unit)
    else:
        state["items"].append({"name": name, "quantity": quantity, "unit": unit})
    state["skipped"] = [n for n in state["skipped"] if n.casefold() != name.casefold()]
    edit(state)


def suggest(state, now):
    begin_cycle(state)
    for staple in state["staples"]:
        if (parse(staple["due"]) <= now and staple["name"].casefold() not in state["skipped"]
                and not find_item(state["items"], staple["name"]) and len(state["items"]) < 20):
            add_item(state, staple["name"], staple["quantity"], staple["unit"])


def list_text(state):
    if not state["items"]:
        return "Your grocery list is clear. Add something with /add eggs | 12 | each."
    return "Your groceries 🛒\n" + "\n".join(
        f"• {x['name']} — {x['quantity']:g} {x['unit']}" for x in state["items"])


class Engine:
    def __init__(self, database, settings, client):
        self.db, self.settings, self.client = database, settings, client

    def initialize(self, now):
        with self.db.transaction() as db:
            state = self.db.read(db) or initial(self.settings, now)
            schedule = [self.settings.timezone, self.settings.day, self.settings.checkin_time]
            if state.get("schedule") != schedule:
                state["next_checkin"] = stamp(next_weekly(now, self.settings))
                if state["followup"]:
                    state["followup"] = stamp(next_daily(now, self.settings))
                state["generation"] += 1
                state["schedule"] = schedule
            self.db.save(db, state)

    def nudge(self, state, now, followup=False):
        suggest(state, now)
        names = ", ".join(x["name"] for x in state["items"][:3])
        if followup:
            text = "A little grocery nudge 🌱 Your list is here whenever you're ready. One tap is enough."
        elif names:
            text = f"Grocery check-in 🌱 Might you be almost out of {names}? I've started a list for you."
        else:
            text = "Grocery check-in 🌱 Need anything for the next few days? We can start with one item."
        if names:
            text += "\nThese are estimates, so ‘Still stocked’ is always welcome."
        return text

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
            due_item = any(parse(x["due"]) <= now and x["name"].casefold() not in state["skipped"]
                           for x in state["staples"])
            at_time = now >= local_slot(now, self.settings)
            if not (weekly or wake or followup or (not state["active"] and due_item and at_time)):
                return
            if weekly:
                # A fresh weekly cycle reconsiders skips but preserves unpurchased list items.
                state["skipped"] = []
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

    def stocked(self, state, name, days, now):
        if not 1 <= days <= 90:
            raise ValueError("Choose 1–90 days.")
        staple = find_item(state["staples"], name)
        if not staple:
            raise ValueError("I don't know that staple yet. Use the exact name from /staples, or /skip to remove a one-time item.")
        staple["due"] = stamp(local_slot(now, self.settings, days))
        state["items"] = [x for x in state["items"] if x["name"].casefold() != name.casefold()]
        # An item can return when its stocked-until date arrives, including in this cycle.
        state["skipped"] = [n for n in state["skipped"] if n != name.casefold()]
        edit(state)
        if not state["items"]:
            state["active"] = False
            cancel_reminders(state)
        return f"Got it—I'll hold off on suggesting {staple['name']} for {days} days."

    def apply_action(self, state, action, now):
        kind = action["type"]
        if kind == "add":
            add_item(state, action["item"], action["quantity"], action["unit"])
        elif kind == "skip":
            self.skip(state, action["item"])
        elif kind == "stocked":
            self.stocked(state, action["item"], action["days"], now)
        elif kind == "snooze":
            self.snooze(state, action["days"], now)

    def skip(self, state, name):
        name = normalized(name)
        if not find_item(state["items"], name) and not find_item(state["staples"], name):
            raise ValueError("I couldn't find that item. Use the exact name from /list or /staples.")
        state["items"] = [x for x in state["items"] if x["name"].casefold() != name.casefold()]
        if name.casefold() not in state["skipped"]:
            state["skipped"].append(name.casefold())
        edit(state)

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
            reply, buttons, purchase = await self.handle(candidate, text, bool(callback), now)
        except (ValueError, IndexError, KeyError):
            candidate = state
            reply, buttons, purchase = "I couldn't apply that. Check the item name and format in /help, then try again.", None, None
        # External work finishes before the transaction; only one worker mutates state.
        with self.db.transaction() as db:
            if db.execute("SELECT done FROM inbox WHERE id=?", (row["id"],)).fetchone()[0]:
                return True
            self.db.save(db, candidate)
            self.db.queue(db, reply, buttons)
            if purchase is not None:
                db.execute("INSERT INTO purchases(bought_at,items) VALUES (?,?)", (stamp(now), json.dumps(purchase)))
            db.execute("UPDATE inbox SET done=1 WHERE id=?", (row["id"],))
        return True

    async def handle(self, state, text, callback, now):
        purchase = None
        if callback:
            command, _, arg = text.partition(":")
        else:
            text = text.strip()
            match = re.fullmatch(r"(?:snooze|snooze for) (\d+)(?: days?)?", text, re.I)
            if match:
                text = "/snooze " + match[1]
            if text.lower() in ("ordered", "i ordered", "i've ordered"):
                text = "/ordered"
            if text.lower() in ("help", "list", "pause", "resume", "status"):
                text = "/" + text.lower()
            command, _, arg = text.partition(" ")
            command = command.lower().split("@")[0].lstrip("/")
            if not text.startswith("/"):
                command = "__natural__"
        arg = arg.strip()

        if command == "start":
            state["started"] = True
            return ("Hi! I'm Foodbot 🌱 I'll help get groceries moving, one small step at a time.\n"
                    "Tap below to load your five starter groceries. Quantities and timing are editable.\n"
                    f"Your next weekly check-in is {pretty(state['next_checkin'], self.settings)}.\n"
                    "Use /help anytime. Turn on Telegram notifications for this chat.",
                    [[button("Load my usual groceries", "seed"), button("Help", "help")]], None)
        if not state["started"]:
            return "Send /start first so I can begin your grocery check-ins.", None, None
        if command == "seed":
            if state["staples"]:
                return "You already have staples saved. Use /staples to see them or /staple to add another.", None, None
            for name, qty, unit, interval, essential in [
                ("eggs", 12, "each", 10, True), ("hash browns", 1, "package", 14, True),
                ("Greek yogurt", 1, "package", 7, True), ("bread", 1, "package", 7, True),
                ("English muffins", 1, "package", 10, False)]:
                state["staples"].append(dict(name=name, quantity=qty, unit=unit, interval=interval,
                                             essential=essential, due=stamp(now), last_bought=None))
            suggest(state, now)
            return "Your staples are saved—these quantities and repeat intervals are starting estimates.\n\n" + list_text(state), menu(), None
        if command == "help":
            return HELP, None, None
        if command == "list":
            return list_text(state), menu(), None
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
                if not state["active"]:
                    dates.extend(x["due"] for x in state["staples"])
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
            return self.snooze(state, int(arg), now), None, None
        if command == "stockmenu":
            if not state["staples"]:
                return "Add a staple first with /staple. See /help for an example.", None, None
            return ("What do you still have? Tap to wait 7 days, or use /stocked eggs | 3 for a different wait.",
                    [[button(x["name"], f"stock:{i}:{state['revision']}")]
                     for i, x in enumerate(state["staples"])], None)
        if command == "stock":
            index, revision = map(int, arg.split(":"))
            if revision != state["revision"] or index < 0:
                return "That button is from an older list. Tap Still stocked again.", menu(), None
            return self.stocked(state, state["staples"][index]["name"], 7, now), menu(), None
        if command == "stocked":
            parts = [x.strip() for x in arg.split("|")]
            return self.stocked(state, parts[0], int(parts[1]) if len(parts) > 1 else 7, now), menu(), None
        if command == "staples":
            lines = [f"• {x['name']} — {x['quantity']:g} {x['unit']}, every {x['interval']} days"
                     + (", essential" if x["essential"] else "") for x in state["staples"]]
            return "Your usual groceries\n" + ("\n".join(lines) or "None yet. See /help to add one."), None, None
        if command == "staple":
            parts = [x.strip() for x in arg.split("|")]
            if len(parts) != 5:
                return "Try /staple eggs | 12 | each | 10 | yes\nThat's name | amount | unit | repeat days | essential yes/no.", None, None
            name, qty, unit, interval, essential = parts
            name, qty, interval = normalized(name), float(qty), int(interval)
            if unit not in UNITS or not 0 < qty <= 100 or not 1 <= interval <= 90 or essential.lower() not in ("yes", "no"):
                raise ValueError("Invalid staple")
            existing = find_item(state["staples"], name)
            if not existing and len(state["staples"]) >= 20:
                return "You have 20 staples. /forget one before adding another.", None, None
            if existing:
                existing.update(quantity=qty, unit=unit, interval=interval, essential=essential.lower() == "yes")
            else:
                state["staples"].append(dict(name=name, quantity=qty, unit=unit, interval=interval,
                                             essential=essential.lower() == "yes", due=stamp(now), last_bought=None))
            edit(state)
            return f"Saved {name} as a staple. Use /checkin to prepare suggestions now.", menu(), None
        if command == "forget":
            name = normalized(arg)
            if not find_item(state["staples"], name):
                raise ValueError("Unknown staple")
            state["staples"] = [x for x in state["staples"] if x["name"].casefold() != name.casefold()]
            edit(state)
            return f"I'll stop suggesting {name}. Any copy already on your list stays until you /skip it.", None, None
        if command == "add":
            parts = [x.strip() for x in arg.split("|")]
            add_item(state, parts[0], float(parts[1]) if len(parts) > 1 else 1,
                     parts[2] if len(parts) > 2 else "package")
            return list_text(state), menu(), None
        if command == "skip":
            self.skip(state, arg)
            return "Skipped for this cycle. Your staple is still saved.\n\n" + list_text(state), menu(), None
        if command == "essentials":
            essentials = {x["name"].casefold() for x in state["staples"] if x["essential"]}
            if not essentials:
                return "Mark at least one staple essential with /staple first. Your list is unchanged.", menu(), None
            for item in list(state["items"]):
                if item["name"].casefold() not in essentials:
                    self.skip(state, item["name"])
            return "Just the essentials for this trip.\n\n" + list_text(state), menu(), None
        if command == "shop":
            if not state["items"]:
                return "Your list is empty. Use /checkin for suggestions or /add for something specific.", menu(), None
            if not self.settings.instacart_key:
                return "Instacart isn't connected yet. Here's your list to copy into the store app:\n\n" + list_text(state), menu(), None
            if not state["link"]:
                try:
                    state["link"] = await shopping_link(self.client, self.settings, state["items"])
                except Exception as exc:
                    log.warning("Instacart request failed (%s)", type(exc).__name__)
                    return "Instacart couldn't make a link just now. Your list is saved; retry /shop or copy this:\n\n" + list_text(state), menu(), None
            label = "Development test link" if self.settings.instacart_env == "development" else "Open Instacart"
            return ("Your shopping link is ready. Review products and quantities, then check out in Instacart. "
                    "Come back and tap ‘I ordered’ after checkout.",
                    [[{"text": label, "url": state["link"]}], [button("I ordered", "ordered")]], None)
        if command == "ordered":
            if not state["items"]:
                return "There's no list to record yet. Add what you bought, then use /ordered.", None, None
            return (list_text(state) + "\n\nDid you buy ALL of these quantities? Edit the list first if anything changed. "
                    "Confirming records today's purchase and stops this cycle's reminders.",
                    [[button("Yes, record these purchases", f"confirm:{state['cycle']}:{state['revision']}")],
                     [button("Review / edit first", "list")]], None)
        if command == "confirm":
            cycle, revision = map(int, arg.split(":"))
            if not state["items"] or cycle != state["cycle"] or revision != state["revision"]:
                return "That confirmation is for an older list. Use /ordered to review the current one.", menu(), None
            purchase = copy.deepcopy(state["items"])
            for item in purchase:
                staple = find_item(state["staples"], item["name"])
                if staple:
                    staple["last_bought"] = stamp(now)
                    staple["due"] = stamp(local_slot(now, self.settings, staple["interval"]))
            state["items"] = []
            state["active"] = False
            state["skipped"] = []
            state["snoozed_until"] = None
            cancel_reminders(state)
            edit(state)
            state["next_checkin"] = stamp(next_weekly(now, self.settings))
            state["cooldown_until"] = stamp(local_slot(now, self.settings, 1))
            return "All set 🌱 Purchase recorded. I'll keep an eye on the timing and get the next grocery trip started.", None, purchase
        if command == "apply":
            pending = state["pending"]
            if not pending or pending["id"] != arg or parse(pending["expires"]) < now:
                return "That suggestion has expired or the list changed. Send the request again.", menu(), None
            for action in pending["actions"]:
                self.apply_action(state, action, now)
            state["pending"] = None
            return "Done.\n\n" + list_text(state), menu(), None
        if command == "cancel":
            state["pending"] = None
            return "Okay, no suggested edits applied.", menu(), None
        if not text or text.startswith("/") or callback:
            return "Use /help for commands. Receipt uploads aren't supported in this first version.", None, None
        if not self.settings.openai_key:
            return "For now, use /add, /skip, /stocked or /snooze. /help has examples. Casual edits need the optional OpenAI key.", menu(), None
        try:
            proposal = await interpret(self.client, self.settings, text[:2000], state)
            if proposal.clarification or not proposal.actions:
                return proposal.clarification or "What would you like to add or skip?", None, None
            actions = [x.model_dump() for x in proposal.actions]
            test_state = copy.deepcopy(state)
            for action in actions:
                self.apply_action(test_state, action, now)
            token = secrets.token_hex(6)
            state["pending"] = {"id": token, "actions": actions, "expires": stamp(now + timedelta(minutes=30))}
            summaries = []
            for a in actions:
                summaries.append({"add": f"Set {a['item']} to {a['quantity']:g} {a['unit']}",
                                  "skip": f"Skip {a['item']} this cycle",
                                  "stocked": f"Wait {a['days']} days for {a['item']}",
                                  "snooze": f"Snooze all nudges for {a['days']} days"}[a["type"]])
            return "Apply these edits?\n" + "\n".join("• " + x for x in summaries), [[button("Apply", "apply:" + token), button("Cancel", "cancel")]], None
        except Exception as exc:
            log.warning("Language parsing failed (%s)", type(exc).__name__)
            return "I couldn't interpret that just now. Your groceries are unchanged. Try /add, /skip, /stocked or /snooze; /help shows examples.", menu(), None
