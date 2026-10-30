# Telegram Channel Transfer Bot

Transfers all content from one private Telegram channel to another.
Controlled entirely through your Telegram bot.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Railway (persistent)                           │
│                                                 │
│  ┌─────────────┐    ┌────────────────────────┐  │
│  │  User Client│    │  Bot Client            │  │
│  │  (Pyrogram) │    │  (Pyrogram)            │  │
│  │             │    │                        │  │
│  │  Reads from │    │  /scan /transfer etc.  │  │
│  │  private    │    │  Progress notifications│  │
│  │  channels   │    │                        │  │
│  └──────┬──────┘    └──────────┬─────────────┘  │
│         │                      │                 │
│  ┌──────▼──────────────────────▼─────────────┐  │
│  │  Transfer Engine  ←→  SQLite DB           │  │
│  │  (asyncio task)       (persistent)        │  │
│  └───────────────────────────────────────────┘  │
│  /app/data/  ← Railway Persistent Volume        │
└─────────────────────────────────────────────────┘
```

---

## Prerequisites

- Python 3.11+
- A Telegram account that is **a member of both channels**
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- API credentials from [my.telegram.org/apps](https://my.telegram.org/apps)

---

## Setup

### Step 1 — Get your credentials

1. Go to [my.telegram.org/apps](https://my.telegram.org/apps)
2. Create an app → note `API_ID` and `API_HASH`
3. Create a bot via [@BotFather](https://t.me/BotFather) → note `BOT_TOKEN`
4. Get your Telegram user ID from [@userinfobot](https://t.me/userinfobot)

### Step 2 — Generate session string (run locally once)

```bash
git clone https://github.com/YOUR_USERNAME/tg-channel-transfer
cd tg-channel-transfer
pip install pyrogram TgCrypto
python session_generator.py
```

Enter your API credentials and phone number when prompted.
Copy the printed session string — you'll need it for Railway.

### Step 3 — Push to GitHub

```bash
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/YOUR_USERNAME/tg-channel-transfer
git push -u origin main
```

### Step 4 — Deploy on Railway

1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
2. Select your repository
3. Go to **Settings → Volumes** → Add volume → Mount at `/app/data`
4. Go to **Variables** → Add all variables from `.env.example`:

| Variable | Value |
|---|---|
| `API_ID` | from my.telegram.org |
| `API_HASH` | from my.telegram.org |
| `BOT_TOKEN` | from @BotFather |
| `SESSION_STRING` | generated in Step 2 |
| `AUTHORIZED_USER_ID` | your Telegram user ID |
| `TRANSFER_INTERVAL` | `10` or `15` |
| `BATCH_SIZE` | `5` |
| `MAX_RETRIES` | `5` |
| `DATA_DIR` | `/app/data` |

5. Railway will build and deploy automatically.

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/setsource` | Set source channel |
| `/setdest` | Set destination channel |
| `/scan` | Scan source channel (required before transfer) |
| `/transfer` | Start copying to destination |
| `/pause` | Pause transfer |
| `/resume` | Resume paused transfer |
| `/stop` | Stop and save progress |
| `/retry` | Retry failed messages |
| `/status` | Show job progress |
| `/setinterval 10` | Set seconds between batches |
| `/newjob` | Reset for a new job |
| `/help` | Command list |

---

## Setting a Channel

After `/setsource` or `/setdest`, send the channel in one of three ways:

1. **Forward any post** from the channel to the bot
2. Send `@channelname` (username)
3. Send the channel ID: `-1001234567890`

The user account must be a member of both channels.

---

## Transfer Flow

```
/setsource  →  /setdest  →  /scan  →  /transfer  →  /status
```

1. `/scan` — iterates all channel history, stores message IDs in SQLite
2. `/transfer` — copies messages in chronological order, `TRANSFER_INTERVAL` seconds between batches
3. Duplicate prevention — already-transferred messages are never re-sent
4. Source disconnection — if you leave the source channel, the bot continues with any content it can still access via cached IDs, and reports which messages couldn't be retrieved
5. After a Railway restart — `/resume` picks up exactly where it stopped

---

## What Gets Transferred

- Text messages
- Photos (with captions)
- Videos (with captions)
- Documents / files
- Audio files
- Voice messages
- Video notes (circles)
- Stickers
- Animations / GIFs
- Polls
- Locations / contacts

Service messages (joins, pins, etc.) are skipped.

---

## Security Notes

- `SESSION_STRING` grants full access to your Telegram account. Store it only in Railway environment variables, never in git.
- The bot only accepts commands from `AUTHORIZED_USER_ID`.
- No credentials are ever logged.

---

## Troubleshooting

**Bot doesn't respond** — check `AUTHORIZED_USER_ID` matches your actual Telegram ID.

**"Cannot access source channel"** — your user account must be a member of the source channel.

**FloodWait errors** — increase `TRANSFER_INTERVAL` to 15s and lower `BATCH_SIZE` to 3.

**Messages failing with FileReferenceExpired** — the bot will automatically try to re-download and re-upload affected files.

**Progress lost after restart** — if Railway volume is mounted at `/app/data`, the SQLite DB persists across restarts. Use `/resume` after a restart.
