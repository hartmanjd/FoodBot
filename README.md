# Foodbot 🌱

A personal Telegram grocery companion that starts the conversation for you.

**Start here: [beginner setup guide](docs/SETUP.md).** It walks through Windows setup, Telegram, Railway, and a first-use checklist. You can stop after any numbered section and come back later.

Your defaults are **Monday at 10 a.m., America/Los_Angeles**, with eggs, hash browns, Greek yogurt, bread, and English muffins. Load them with one button after `/start`. Quantities, essential flags, and repeat intervals are editable starting assumptions.

## What is ready

- Weekly check-ins, plus replenishment estimates after confirmed purchases.
- A saved grocery list with quick review, essentials, still-stocked, and shopping buttons.
- “Snooze for 3 days” or `/snooze 14`; all automatic nudges wait until the selected date.
- Up to two daily follow-ups, then a break until the next weekly check-in. Quiet hours are 9 p.m.–9 a.m.
- Item-specific delays: `/stocked eggs | 5` removes eggs from this list and delays their next suggestion.
- Optional Instacart shopping links, with a copyable list available without an API key.
- Optional OpenAI conversational edits, shown for confirmation before they change your groceries.
- Purchase confirmation that stops the current reminder cycle and updates staple estimates.
- Private-chat owner restriction, webhook authentication, SQLite persistence, input deduplication, and durable outgoing messages.

## Try the conversation without accounts

After installing dependencies as described in the setup guide:

```powershell
.\.venv\Scripts\python.exe -m foodbot.demo
```

This prints a sample conversation using a temporary database. It sends nothing and makes no API calls.

## How it works

Telegram → authenticated FastAPI webhook → SQLite inbox → single background worker → grocery state and outgoing replies → Telegram.

Reminder timing is ordinary Python code. OpenAI is optional and only proposes list edits; it doesn't control reminders or mark purchases. Instacart creates a shopping link, and you complete checkout there. No receipt extraction, automatic checkout, email access, or adaptive learning is included in this milestone.

The database stores configured repeat intervals and confirmed purchase dates. It estimates when you may need something; it cannot see what's left in your kitchen. Buying extra does not automatically change the interval yet—use `/stocked` to delay the next suggestion.

## Development

Python 3.12+; dependencies are constrained to the versions in `requirements.lock`.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m foodbot.demo
```

With a completed `.env`, start the server:

```bash
python -m uvicorn foodbot.app:app --host 127.0.0.1 --port 8000 --workers 1
```

The local server alone does not connect Telegram. The hosted setup guide registers an HTTPS webhook. Use **one replica and one worker**: state changes and outgoing calls are intentionally serialized for this personal bot.

| File | Responsibility |
| --- | --- |
| `foodbot/app.py` | Webhook authorization, durable intake, health endpoint |
| `foodbot/engine.py` | Commands, buttons, grocery actions, reminder policy |
| `foodbot/database.py` | SQLite transactions, inbox, outbox, purchase ledger |
| `foodbot/schedule.py` | Local scheduling with UTC storage and daylight saving handling |
| `foodbot/worker.py` | Event processing, reminder checks, delivery retries |
| `foodbot/assistant.py` | Optional structured natural-language proposals |
| `foodbot/clients.py` | Telegram and Instacart API clients |
| `foodbot/manage.py` | Setup, connection checks, and database backup helpers |

See [operations and limitations](docs/OPERATIONS.md) for backups, updates, and failure behavior.
