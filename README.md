# Telegram Channel Transfer Bot

Button-driven Telegram channel history scanner and transfer tool. It uses a logged-in Telegram **user session** to discover channels and perform copying, while a Telegram bot provides the control panel.

## Features

- `/start` opens an inline-button control panel.
- SOURCE list is built from channels visible in the logged-in account's Telegram dialogs.
- DESTINATION list is restricted to channels where that account is an administrator/owner and has posting permission (where Telegram exposes that permission).
- Channel lists are cached for 2 minutes; refresh is available in the UI.
- Full available channel history scan, newest-to-oldest discovery and oldest-to-newest transfer order.
- Batched SQLite inserts to reduce scan overhead.
- Text and Telegram-supported message/media types are copied with `copy_message`; expired media references get a download/re-upload fallback.
- Pause, resume, stop, retry failed messages, job status and progress notifications.
- FloodWait is waited out and retried; the bot does not attempt to bypass Telegram limits.
- Existing SQLite tables are retained; new code adds methods without dropping old jobs/messages.

## Requirements

Python 3.11+ and the packages in `requirements.txt`.

## Railway environment variables

Set these in Railway Variables (never commit real credentials):

| Variable | Required | Default |
|---|---:|---:|
| `API_ID` | Yes | — |
| `API_HASH` | Yes | — |
| `BOT_TOKEN` | Yes | — |
| `SESSION_STRING` | Yes | — |
| `AUTHORIZED_USER_ID` | Yes | — |
| `TRANSFER_INTERVAL` | No | `2` |
| `BATCH_SIZE` | No | `10` |
| `PER_MESSAGE_DELAY` | No | `0.15` |
| `SCAN_BATCH_SIZE` | No | `100` |
| `MAX_RETRIES` | No | `5` |
| `FLOOD_SLEEP_THRESHOLD` | No | `120` |
| `DATA_DIR` | No | `/app/data` |

Mount a Railway Volume at `/app/data` so SQLite state and temporary media survive redeploys.

## First run

1. Deploy this repository to Railway using the included Dockerfile / `railway.toml`.
2. Add the required variables above.
3. Open your Telegram bot and send `/start`.
4. Press **SOURCE CHANNEL** and choose a channel from the logged-in user account's joined channel list.
5. Press **DESTINATION** and choose a channel where that user account is an admin/owner with permission to post.
6. Press **SCAN FULL HISTORY**. Wait for the scan-complete message.
7. Press **START TRANSFER**.

The bot also supports `/scan`, `/transfer`, `/status`, `/setsource`, `/setdest`, and `/help`.

## Speed and Telegram limits

The included fast preset uses batches of 10, a 2-second wait between batches, and a short per-message delay. Actual throughput depends on Telegram's current limits, channel size, media size, Railway resources, and Telegram FloodWait responses. When Telegram asks the account to wait, the engine waits and retries; it cannot guarantee a fixed messages-per-second rate.

## Important limitations

- The user session can only scan history Telegram makes available to that account. It cannot access channels the account has not joined or content Telegram restricts.
- Albums are copied message-by-message; the individual media and captions are retained, but original album grouping may not be retained.
- Service events (for example, join/pin notifications) are intentionally skipped.
- Expired-reference fallback downloads and re-uploads file media. Very large files still depend on Telegram account/API limits and available Railway volume storage.
- A full real-Telegram integration test requires the owner's Railway environment and Telegram credentials. The included offline tests use fake Telegram/DB clients and do not claim to verify live Telegram access.

## Security

`SESSION_STRING` grants access to the Telegram user account. Store it only in Railway Variables. Do not share it or commit it to GitHub. Keep `AUTHORIZED_USER_ID` set to your own Telegram numeric user ID.
