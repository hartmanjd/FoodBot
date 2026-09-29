# Foodbot 🌱

A personal Telegram grocery companion that starts the conversation for you.

**Start here: [beginner setup guide](docs/SETUP.md).** It walks through Windows setup, Telegram, Railway, and a first-use checklist. You can stop after any numbered section and come back later.

Your defaults are **Monday at 10 a.m., America/Los_Angeles**, with eggs, hash browns, Greek yogurt, bread, and English muffins. New users get these groceries automatically on `/start`. Existing saved lists are preserved. Items are plain names; type a number first (like `3 lemons`) only when you want an amount.

## Three simple actions

- **Review groceries** — see the saved list, tap **Add item** and type names, or tap **Remove item** and choose what to remove.
- **Shop** — get a copyable list for your store app or trip to the store.
- **Snooze** — the bot asks how many days until your next check-in. Reply with a number from 1 to 90.

The focus is building the grocery habit. Lists stay saved between trips. There are no inventory estimates, still-stocked controls, essentials mode, or Instacart calls. Monday check-ins, two bounded follow-ups, quiet hours, and durable snoozes keep the habit moving. `/done` ends the week's follow-ups while keeping the list.

Optional OpenAI conversational edits still ask for confirmation. Every button flow works without an AI key. Private-chat owner restriction, webhook authentication, SQLite persistence, input deduplication, and durable outgoing messages remain in place.

## Try the conversation without accounts

After installing dependencies as described in the setup guide:

```powershell
.\.venv\Scripts\python.exe -m foodbot.demo
```

This prints a sample conversation using a temporary database. It sends nothing and makes no API calls.

## How it works

Telegram → authenticated FastAPI webhook → SQLite inbox → single background worker → grocery state and outgoing replies → Telegram.

Reminder timing is ordinary Python code. OpenAI is optional and proposes only additions, removals, or snoozes. Shop formats your saved list without external calls or purchase tracking. Existing inventory data and purchase history are preserved during the upgrade but are no longer used.

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
| `foodbot/clients.py` | Telegram API client |
| `foodbot/manage.py` | Setup, connection checks, and database backup helpers |

See [operations and limitations](docs/OPERATIONS.md) for backups, updates, and failure behavior.
