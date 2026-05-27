# frontdesk-watch

Polls the Aabenraa civil-wedding booking page (FrontDesk Suite) for open
appointment slots and broadcasts changes to Telegram subscribers.

## Prerequisites

- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- One of:
  - Docker + Docker Compose (recommended), or
  - Python 3.10+ and [`uv`](https://docs.astral.sh/uv/)

## Setup (Docker — recommended)

1. Clone the repo and `cd` into it.
2. Create your env file:
   ```bash
   cp .env.example .env
   ```
3. Edit `.env`. At minimum set `TELEGRAM_BOT_TOKEN`. Optionally set
   `TELEGRAM_CHAT_ID` to auto-subscribe one chat at startup.
4. Build and start:
   ```bash
   docker compose up -d --build
   ```
5. Tail logs:
   ```bash
   docker compose logs -f
   ```
6. Stop:
   ```bash
   docker compose down
   ```

State (`subscribers.json`, `seen_slots.json`) is persisted in the named
volume `frontdesk-state`, so subscribers survive rebuilds.

### Running with CLI flags

Append flags after the service name:

```bash
# Quick smoke-test: 20 fake slots, alert every 10s
docker compose run --rm frontdesk-watch --dummy-mode --interval 10

# Verbose poll output
docker compose run --rm frontdesk-watch --log-level DEBUG
```

## Setup (local, without Docker)

```bash
# Install dependencies + the project itself
uv sync

# Install Chromium for Playwright
uv run playwright install chromium

# Export env vars (or use direnv / a .env loader)
export TELEGRAM_BOT_TOKEN=<your token>
export TELEGRAM_CHAT_ID=<your numeric chat id>   # optional

# Run
uv run frontdesk-watch
```

## Environment variables

| Name | Required | Default | Purpose |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes (for Telegram) | — | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | no | — | Numeric chat id to auto-subscribe at startup |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `STATE_DIR` | no | `.` | Where `subscribers.json` / `seen_slots.json` live (Docker sets `/app/data`) |

## CLI flags

| Flag | Default | Purpose |
|---|---|---|
| `--dummy-mode` | off | Skip Playwright; emit 20 fake slots to test the alert path |
| `--interval N` | `30` | Polling interval in seconds |
| `--log-level LVL` | `INFO` (or `$LOG_LEVEL`) | Same as the env var |

## Telegram commands

Open a chat with your bot. Type `/` to see the menu, or send any of:

| Command | What it does |
|---|---|
| `/start` | Show the command list |
| `/subscribe` | Subscribe this chat to slot alerts |
| `/unsubscribe` | Unsubscribe |
| `/get` | Last scrape result (count + first 20 slots) |
| `/detailed` | Last scrape result with full per-day breakdown |
| `/hello` | Liveness check |

## How to find your chat id

1. Message your bot (e.g. send `hi`).
2. Run:
   ```bash
   curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" | jq
   ```
3. Look for `"chat": {"id": <number>}` — that's your `TELEGRAM_CHAT_ID`.

Or message [@userinfobot](https://t.me/userinfobot) and it tells you directly.
