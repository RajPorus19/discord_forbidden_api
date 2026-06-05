# discord-forbidden-api

Fetch messages from Discord channels, store them locally as JSON lines, and
serve them over a small Flask API. Also supports sending messages and replying
to a specific message.

> **Warning:** This talks to Discord using a user account token, which is
> against Discord's Terms of Service for automation. Use it only on accounts
> and servers you own, at your own risk. No secrets are committed to this repo —
> you supply your own via `.env`.

## Setup

This project uses [uv](https://docs.astral.sh/uv/). Install uv first if you
don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

1. Install dependencies (uv creates a virtualenv and syncs from `uv.lock`):

   ```bash
   uv sync
   ```

2. Configure secrets:

   ```bash
   cp .env.example .env
   # edit .env and set DISCORD_AUTHORIZATION (your token)
   ```

3. Configure the channels to sync:

   ```bash
   cp channels.example.json channels.json
   # edit channels.json: { "channel-slug": "channel-id", ... }
   ```

## Syncing messages locally

```bash
uv run main.py
```

This loops over every channel in `channels.json` and writes
`chatlogs/messages_in_<slug>.jsonl`, appending only new messages each pass.

### Limiting the first-run backfill

The **first** time a channel is synced there is no local history, so the tool
would otherwise page all the way back to the channel's very first message. To
avoid pulling years of history, the backfill is capped by
`DISCORD_MAX_OLD_MESSAGES` in `.env` (**default: `1000`**). Set it to `0` (or
leave it empty) to fetch the entire channel history instead.

This cap mainly affects the first run; on later passes the sync stops as soon as
it reaches messages already stored locally, so it only fetches what's new.

### Sync frequency

`main.py` runs continuously, syncing every channel and then sleeping before the
next pass. The wait is controlled by `DISCORD_SYNC_INTERVAL_SECONDS` in `.env`
(**default: `300`**, i.e. 5 minutes).

## Serving the stored messages as an API

```bash
uv run app.py            # or: uv run flask --app app run
```

Endpoints:

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/health` | Health check |
| GET | `/channels` | Channels that have a local log |
| GET | `/channels/<slug>/messages` | Stored messages as JSON |
| POST | `/channels/<slug>/send` | Send a message or reply |

`GET /channels/<slug>/messages` supports query params: `limit` (most recent N),
`before_id`, `after_id`, `author_id`, and `q` (substring search).

```bash
curl "http://127.0.0.1:5000/channels/general/messages?limit=20&q=hello"
```

## Sending messages and replies

Via the API:

```bash
# Plain message
curl -X POST http://127.0.0.1:5000/channels/general/send \
  -H 'Content-Type: application/json' \
  -d '{"content": "hello channel"}'

# Reply to a specific message (optionally without pinging the author)
curl -X POST http://127.0.0.1:5000/channels/general/send \
  -H 'Content-Type: application/json' \
  -d '{"content": "replying!", "reply_to": "123456789012345678", "mention_author": false}'
```

Via the CLI:

```bash
uv run discord_send_messages.py "hello channel" --channel-id 123456789012345678
uv run discord_send_messages.py "replying!" --channel-id 123 --reply-to 456 --no-mention
```
