# Operations and limits

## Storage and delivery

Run one service replica with one Uvicorn worker. The one background loop processes inbox events before scheduling or delivering outgoing messages. SQLite transactions save state, outgoing replies, and each processed update together. A repeated Telegram update ID cannot apply an edit twice.

The outbox retries failed deliveries with a delay. Telegram does not provide an idempotency key for sending a message: if a send succeeds but the process crashes before saving that success, a message can be delivered twice after restart. Grocery edits remain deduplicated. Old reminder generations are discarded after list changes, snoozes, pauses, or completed shopping check-ins.

The health endpoint verifies database connectivity and that the worker is making progress. It does not prove Telegram or other providers are reachable. Delivery failures are logged by exception type without request URLs, since Telegram URLs contain the bot token.

## Habit and reminder behavior

- `/start` starts check-ins and shows only Groceries and Shop. Snooze appears only on automatic check-in messages. New users get the five starter groceries once; existing lists are preserved.
- Groceries offers Add item, Remove item, and New list. Add accepts names (`eggs`), comma/newline-separated names, and an optional number first (`3 lemons`). Items are shown exactly like that, with no default units. Remove accepts several comma/newline-separated names; if one isn't on the list, nothing is removed. Remove buttons stay open for more taps and carry the list revision so an old button cannot remove a different item. New list replaces the whole list.
- The list is reused until edited. Removed items never return automatically.
- Shop shows the list with the current date and time. It does not contact a shopping provider, mark anything bought, or stop follow-ups.
- Snooze asks for 1–90 whole days and schedules one extra reminder at the check-in time that many days ahead. The regular weekly check-in is unchanged. Snoozing cancels this week's remaining follow-ups, and the extra reminder has no follow-ups of its own. Snoozing again replaces the extra reminder; `/done` and `/resume` clear it. The pending question is persisted. Invalid replies ask again; Cancel leaves the previous schedule unchanged. Another explicit command exits the prompt.
- Weekly check-in: Monday at 10 a.m. Pacific, configurable through environment variables. There are no inventory-based triggers between weekly check-ins.
- Up to two daily follow-ups, then a quiet break until the next weekly check-in. Reviewing or editing the list doesn't mean shopping is finished.
- At least 20 hours between automatic nudges, except an extra reminder you asked for. No automatic messages during quiet hours. Explicit replies can arrive whenever requested.
- `/done` ends follow-ups until next week and keeps the saved list. `/pause` stops check-ins indefinitely; `/resume` restarts them at the next daily slot.
- A missed check-in after downtime arrives at the next eligible worker check outside quiet hours.

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

The database contains shopping lists, legacy staple preferences, historical purchase records, Telegram input events, and outgoing messages. There is no automatic data retention cleanup yet. It is a local SQLite file without application-level encryption. Restrict access to your hosting project and backups. `.env`, databases, and backups are excluded from source control and Docker build context.

Only the configured owner in a private Telegram chat, or anyone in the optional group set by `TELEGRAM_GROUP_ID`, can submit actions. Other users and groups are ignored; an ignored group's ID is logged to help setup. Replies go to the chat that asked; automatic reminders go only to the owner's private chat. Each person has their own pending question, so one person's message can't answer another's prompt. The webhook secret is checked before parsing the body. There is no public endpoint to read groceries. Receipt attachments are not downloaded or interpreted in this version.

## Compatibility and deferred features

Schema 2 preserves the existing list, pause state, weekly schedule, snooze, and historical purchases. Retired inventory fields are retained under `legacy_inventory` for recovery, not used to suggest groceries. Old queued UI messages are discarded during migration, and old inventory callbacks safely return the new menu. `/skip` remains an alias for remove; `/ordered` remains an alias for `/done`. Old purchase confirmation buttons cannot record a purchase.

Instacart shopping links, inventory tracking, essentials mode, purchase capture, receipt scanning, adaptive learning, email ingestion, voice notes, and automatic checkout are outside the current habit-focused version. Old Instacart environment variables can be removed; they are no longer used.

Live Telegram, optional OpenAI, and Railway access require your accounts. Tests use fake services and temporary databases.
