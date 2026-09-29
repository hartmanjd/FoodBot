# Operations and limits

## Storage and delivery

Run one service replica with one Uvicorn worker. The one background loop processes inbox events before scheduling or delivering outgoing messages. SQLite transactions save state, purchase records, outgoing replies, and each processed update together. A repeated Telegram update ID cannot apply an edit twice.

The outbox retries failed deliveries with a delay. Telegram does not provide an idempotency key for sending a message: if a send succeeds but the process crashes before saving that success, a message can be delivered twice after restart. Grocery changes and purchases remain deduplicated. Old reminder generations are discarded after list changes, snoozes, pauses, or completed orders.

The health endpoint verifies database connectivity and that the worker is making progress. It does not prove Telegram or other providers are reachable. Delivery failures are logged by exception type without request URLs, since Telegram URLs contain the bot token.

## Reminder behavior

- The owner must send `/start` before automatic reminders begin.
- Weekly check-in: Monday at 10 a.m. Pacific, configurable through environment variables.
- A due staple can begin a new cycle between weekly check-ins after a completed purchase or when all items were marked stocked.
- During an active cycle, each nudge refreshes due suggestions. There aren't separate notifications for every item.
- Up to two daily follow-ups, followed by a quiet break until the next weekly check-in. Reviewing or editing the list doesn't mean shopping is finished; it remains eligible for these bounded follow-ups.
- At least 20 hours between automatic nudges, except an explicit snooze return, with no automatic messages during quiet hours. Explicit command replies can arrive whenever you ask.
- Snooze suppresses every automatic reminder until the chosen local date/time, including weekly ones. A long snooze results in one return check-in rather than a backlog.
- “Still stocked” delays the named staple and removes it from the list. If other groceries remain, their cycle can continue. A weekly check-in can still ask whether anything else is needed.
- Skip removes an item for the current cycle. The next weekly cycle or a completed purchase clears skips. Forget removes it from staples permanently but leaves any current-list copy for explicit review.
- “Just essentials” keeps only list items whose staples are marked essential. Other items stay skipped for this cycle.
- `/ordered` shows a confirmation for the exact list revision. Confirmed quantities are recorded as bought today. A stale confirmation cannot record a changed list.
- A missed check-in after downtime is delivered at the next eligible worker check outside quiet hours. It does not replay every missed day.

Dates are saved in UTC and calculated using the configured IANA timezone. The default 10 a.m. schedule avoids ambiguous daylight saving transition hours. Tests cover spring and fall changes.

## Backups

Use Railway volume backups for the hosted database. Follow the current options in the [Railway volumes guide](https://docs.railway.com/volumes). Take a backup before upgrades and practice restoring one before relying on it.

For a local database (or from a Railway service shell with access to the mounted volume), the helper uses SQLite's online backup API so the WAL is included consistently:

```bash
python -m foodbot.manage backup backups/foodbot-before-upgrade.sqlite3
```

Choose a new destination each time; existing backups are not overwritten. A backup stored only on the same volume is not protection against losing that volume. Keep a copy somewhere private and independent.

To restore manually, stop the application, preserve the current database and its `-wal` and `-shm` sidecars together in a separate directory, and place the clean SQLite backup at `DATABASE_PATH`. Do not leave sidecars from the old database beside the restored file. Restart one instance, check `/health`, and inspect `/list` and `/status`. A restored backup also restores old outbox/inbox state; an old pending message may be delivered again. Volume snapshots should be restored through Railway's documented flow.

## Data and secrets

The database contains shopping lists, staple preferences, purchase records, Telegram input events, and outgoing messages. There is no automatic data retention cleanup yet. It is a local SQLite file without application-level encryption. Restrict access to your hosting project and backups. `.env`, databases, and backups are excluded from source control and Docker build context.

Only the configured owner in a private Telegram chat can submit actions. Unknown users and groups are ignored. The webhook secret is checked before parsing the body. There is no public endpoint to read groceries. Receipt attachments are not downloaded or interpreted in this version.

## What is intentionally deferred

Receipt OCR/PDF parsing, receipt deduplication, substitutions/refunds, email ingestion, automatic purchase-history import, adaptive interval learning, quantity-based consumption estimates, voice notes, multiple users, and automatic checkout are not implemented. Purchase history is stored for future learning, but current timing uses explicit staple intervals and your still-stocked feedback.

The live Telegram, OpenAI, Instacart, and Railway integrations require your accounts and keys. Automated tests exercise their boundaries with mocked HTTP responses; account permissions and real provider behavior must be checked during setup.
