# Set up your grocery assistant

The finished setup is a private Telegram chat on your phone, backed by a small service on Railway. Railway keeps it running when your computer is off. You do not need to write application code; the steps below connect the accounts and run the code already in this folder.

**Your first task: complete section 1 and run the offline preview.** You can come back for the account setup later.

## 1. Prepare your Windows computer

1. Install Python 3.12 or newer from [Python's Windows downloads](https://www.python.org/downloads/windows/). Enable **Add Python to PATH** if the installer offers it.
2. Open **PowerShell** from the Windows Start menu. These instructions use Windows PowerShell, not a WSL/Linux terminal.
3. Paste the following commands one at a time. Your project is already at `C:\ML\Projects\foodbot`.

```powershell
cd C:\ML\Projects\foodbot
py --version
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m foodbot.demo
```

You should see a sample conversation with your five groceries, easy list editing, and a three-day snooze. This preview uses temporary data and contacts no external services. You can safely run it again.

The `.venv` folder is a private copy of Python's packages for this project. We call its Python directly so you don't need to change PowerShell's script execution settings.

If `py` is not recognized, close and reopen PowerShell after installing Python. If your installation offers `python` instead, use `python -m venv .venv` for that command.

**Checkpoint:** the preview prints a conversation and says nothing was sent or ordered.

## 2. Create your Telegram bot

1. Install Telegram on your phone and sign in.
2. Open [the official BotFather](https://t.me/BotFather). Check the exact username `@BotFather`.
3. Send `/newbot`.
4. Choose a display name, such as **My Grocery Buddy**.
5. Choose an available username ending in `bot`, such as `justins_grocery_buddy_bot`.
6. BotFather gives you a bot token. Treat it as a password. It goes in your local `.env` and Railway's Variables, never into this chat or a GitHub commit.
7. Open the link to your new bot and send `/start`. It won't answer until deployment is finished. That is expected.

Telegram documents this flow in [its official bot tutorial](https://core.telegram.org/bots/tutorial).

## 3. Fill in your local settings

In PowerShell, in the project folder:

```powershell
Copy-Item .env.example .env
notepad .env
```

Only copy the file the first time; repeating `Copy-Item` later can overwrite your saved settings.

In Notepad, put your bot token after `TELEGRAM_BOT_TOKEN=`. Save and close. Leave the other values for a moment.

Get your own Telegram numeric ID through your new bot:

```powershell
.\.venv\Scripts\python.exe -m foodbot.manage identify
```

If needed, send `/start` to your bot again while the command waits. The command prints the numeric ID and first name of people who messaged your bot. Choose **your own** ID. This is not the username and not the bot's ID.

Generate a secret for Telegram's connection to your server:

```powershell
.\.venv\Scripts\python.exe -m foodbot.manage secret
```

Open `.env` again:

```powershell
notepad .env
```

Fill in these three required settings, using your actual values:

```dotenv
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_OWNER_ID=your_numeric_user_id
TELEGRAM_WEBHOOK_SECRET=the_random_value_you_just_generated
```

Keep these defaults:

```dotenv
DATABASE_PATH=data/foodbot.sqlite3
TIMEZONE=America/Los_Angeles
CHECKIN_DAY=monday
CHECKIN_TIME=10:00
QUIET_START=21
QUIET_END=9
MAX_FOLLOWUPS=2
```

Leave `OPENAI_API_KEY` empty for now. Save, close Notepad, and check the connection:

```powershell
.\.venv\Scripts\python.exe -m foodbot.manage check
```

**Checkpoint:** it prints “Settings valid” and your bot's username. This verifies Telegram access, not reminder delivery yet.

## 4. Put the code in a private GitHub repository

Railway can deploy directly from GitHub. A repository is simply the saved project code with a history of changes.

1. Create/sign into a [GitHub account](https://github.com/) and install [GitHub Desktop](https://desktop.github.com/).
2. In GitHub Desktop, choose **File → Add local repository** and select `C:\ML\Projects\foodbot`.
3. If Desktop says this is not a Git repository, use its **create a repository here** link. Set the name to `foodbot` and the parent/local path to `C:\ML\Projects`, so the resulting directory is `C:\ML\Projects\foodbot`. Keep the existing files. If it already recognizes the repository, skip creation.
4. Check the changed-file list. It should include `foodbot`, `docs`, `tests`, `Dockerfile`, `railway.toml`, the requirements files, `.env.example`, and `.gitignore`. It must **not** include `.env`, `.venv`, or a database. `.gitignore` already excludes those.
5. Enter **Initial grocery assistant** in the Summary box, then click **Commit to main** (the branch name may differ).
6. Click **Publish repository**, select **Keep this code private**, and publish.

If `.env` appears in the commit list, stop and make sure the supplied `.gitignore` is in this project's root before committing.

**Checkpoint:** your private GitHub repository contains the source files and no API tokens.

## 5. Run it on Railway

1. Sign into [Railway](https://railway.com/) using GitHub. Review the account's current hosting charges and usage limits before enabling a service. Always-on hosting can incur charges.
2. Choose **New Project → Deploy from GitHub repo** and select your private `foodbot` repository. Grant Railway access to that repository if prompted.
3. Railway should detect the supplied `Dockerfile`. It starts the app with one worker and reads its port automatically. The first deployment may fail before you've entered the required settings; finish the next steps, then redeploy.
4. Open the service's **Variables** section. Add the following values. Copy the token, owner ID, and webhook secret from your `.env` exactly.

| Variable | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Your bot token |
| `TELEGRAM_OWNER_ID` | Your own numeric Telegram ID |
| `TELEGRAM_WEBHOOK_SECRET` | The same generated secret used locally |
| `DATABASE_PATH` | `/data/foodbot.sqlite3` |
| `TIMEZONE` | `America/Los_Angeles` |
| `CHECKIN_DAY` | `monday` |
| `CHECKIN_TIME` | `10:00` |
| `QUIET_START` | `21` |
| `QUIET_END` | `9` |
| `MAX_FOLLOWUPS` | `2` |

5. Attach a **Volume** to this service and set its **mount path to `/data`**. A volume keeps the database when Railway restarts or replaces the running container. Depending on the current UI, use the service's volume settings or add a volume from the project canvas. Do this before you start using the bot. See [Railway's volume instructions](https://docs.railway.com/volumes).
6. In deployment settings, keep **one replica** and **Serverless disabled**. Sleeping services cannot reliably initiate scheduled reminders. The Dockerfile already uses one Python worker; do not add more.
7. Under **Settings → Networking**, generate a public domain. If prompted for a target port, use the `PORT` value shown by Railway; the startup command listens on that value, or `8000` if no value is set.
8. Deploy/redeploy the service after the variables and volume are configured.
9. Open `https://YOUR-RAILWAY-DOMAIN/health` in your browser. Replace the placeholder with the domain Railway gave you. You should see `{"status":"ok"}`.

The domain exposes a health check and an authenticated Telegram webhook, not a grocery dashboard. This bot's interface is Telegram.

**Checkpoint:** the deployment is healthy, the database path starts with `/data/`, and a volume is mounted at `/data`.

## 6. Connect Telegram to Railway

Back in your local PowerShell window, replace the example domain below with your real Railway domain:

```powershell
.\.venv\Scripts\python.exe -m foodbot.manage webhook https://YOUR-RAILWAY-DOMAIN
.\.venv\Scripts\python.exe -m foodbot.manage webhook-info
```

The first command tells Telegram where to deliver messages. It also configures the shared secret; this is how the server checks that incoming messages come through your connection. See [Telegram's webhook documentation](https://core.telegram.org/bots/api#setwebhook).

The second command should show your domain ending in `/telegram/webhook`, with no new delivery error. An old `last_error_message` can remain after a repair; verify by sending a new message.

**Checkpoint:** open your Telegram bot and send `/start`. It should answer in a few seconds.

## 7. Try the simple habit loop

Your first page has just three actions:

| Action | What happens |
| --- | --- |
| **Review groceries** | Shows your saved list, with **Add item** and **Remove item** buttons. |
| **Shop** | Makes a copyable shopping list to use in your store app or at the store. |
| **Snooze** | Asks how many days to wait until your next check-in. |

1. Send `/start`. New users start with eggs, hash browns, Greek yogurt, bread, and English muffins. Existing users keep their current list.
2. Tap **Review groceries → Add item**, then type `coffee`. You can add several items with `coffee, apples` or one per line. If you want a specific amount, type `eggs | 12 | each`.
3. Tap **Remove item**, then tap an item's name to remove it. It stays removed until you add it again.
4. Tap **Back** for the three main actions.
5. Tap **Snooze**, then reply `3`. The bot confirms a check-in three days ahead at 10 a.m. Pacific. You can enter any whole number from 1 to 90. **Cancel** leaves the timing unchanged. The question and any saved snooze survive restarts.
6. Send `/status` to check the date. After testing, `/resume` schedules the next 10 a.m. check-in and returns to the normal Monday schedule after that.
7. Tap **Shop** to get a copyable list. It doesn't contact Instacart or place an order.
8. When you finish shopping, send `/done` to stop this week's follow-ups. Your list stays saved for the next trip. This is a habit check-off, with no inventory tracking.
9. Enable Telegram notifications for this chat and pin it if helpful.

The starter amounts are 12 eggs and one package each of the other items. Adjust these through Add item whenever needed. Repeat intervals, still-stocked controls, essentials mode, and automatic replenishment have been removed.

To try an immediate reminder, send `/checkin`. Reminders invite you to review, shop, or snooze; they don't guess what's running out.

## 8. Optional: understand casual replies with OpenAI

Buttons, list editing, snooze questions, and reminders already work without AI. This addition lets you say things like “Remove bread and add two packages of coffee.” The bot shows proposed edits with **Apply** and **Cancel** buttons.

1. Create an API project/key in the [OpenAI API platform](https://platform.openai.com/). Set up any required API billing and review usage controls there.
2. Add these Railway variables:

```dotenv
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=gpt-4o-mini
```

3. Redeploy. Send “Remove bread and add two packages of coffee.” Review the proposal and tap **Apply**.

The integration uses the [Responses API's structured output format](https://developers.openai.com/api/docs/guides/structured-outputs). If that model isn't available to your API project, select a model available to you that supports Responses structured outputs and change `OPENAI_MODEL`. An AI failure falls back to ordinary commands.

With this enabled, your casual message and current list are sent to OpenAI for interpretation. The request uses `store=false`. Telegram holds your chat, and Railway's database holds your grocery records and processed messages. See the providers' current data policies for their retention practices.

## 9. Keep it working

- Enable backups for your Railway volume; keep at least a recent known-good backup. See [operations](OPERATIONS.md) before restoring.
- Use `/status` to inspect reminders. `/pause` stops nudges indefinitely. `/resume` restarts them; `/snooze N` schedules a return and also resumes a paused bot.
- Schedule changes go in Railway Variables. Redeploy after changing them. The next weekly slot is recalculated; an already saved snooze keeps its promised time.
- Edit your saved list through **Review groceries**. Add item can also update an existing quantity. The list is reused until you change it.
- After editing code, commit and push it through GitHub Desktop. Railway deploys the new version. Keep the volume attached.

## If something gets stuck

| Symptom | Try this |
| --- | --- |
| Bot never answers | Open `/health`, then run `webhook-info`. Check the same token and secret are set locally and in Railway. Confirm `TELEGRAM_OWNER_ID` is your own ID. |
| Railway fails at startup | Check deployment logs for missing settings; make sure the volume path and `DATABASE_PATH` match. Never share tokens when sharing logs. |
| `identify` finds nobody | Send `/start` to your new bot and rerun. This helper is for initial setup before a webhook is connected. |
| `identify` says a webhook exists | Your bot is already connected. The helper will not disconnect it. Use the owner ID saved in your settings. |
| No scheduled nudges | Send `/status`; check pause/snooze, quiet hours, notification permissions, and that Railway is running with Serverless off. Two ignored follow-ups intentionally produce a quiet break until next week. |
| List disappeared after deployment | Confirm `/data/foodbot.sqlite3` and the `/data` volume. An ephemeral database cannot survive replacement; restore a backup if needed. |
| Casual messages fail | Commands still work. Check OpenAI key, billing, model availability, and provider status. |
| A button says it is old | The list changed since the button appeared. Open **Review groceries → Remove item** again for current buttons. |

For code checks, run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The tests use fake services and temporary databases, so they don't contact your bot or buy groceries.

## Updating from the original version

After the new commit deploys, send `/start` to see the simplified menu. Old Telegram messages still show their original buttons; inventory buttons on those messages now take you to the current menu without changing groceries. Existing lists, weekly timing, paused state, and saved snoozes are kept. Historical purchase records remain in the database, but the bot no longer uses them or previous staple intervals to suggest items.
