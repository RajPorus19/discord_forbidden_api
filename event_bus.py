#!/usr/bin/env python3
"""
Discord polling-to-event adapter — event bus daemon.

Converts Discord REST API polling into a local Unix-socket event stream
that external tools can subscribe to. Built to work alongside the existing
``main.py`` syncer and ``app.py`` Flask API in this repo.

Architecture (polling_to_event_driven_message_architecture.md):

    Discord REST API
           │
           │ poll adaptively (5s active / 60s idle)
           ▼
    EventBusPoller
           │
           ▼
    Unix socket (~/.hermes/discord_events.sock)  ←─ external tools subscribe
           │
    ┌──────┼──────┐
    ▼      ▼      ▼
  Tool A  Tool B  Tool C

The polling is isolated at the edge. Everything inside your system
uses events.

Usage
-----

.. code-block:: bash

   # As a standalone daemon (uses this repo's config + channels.json):
   uv run event_bus.py

   # External tools subscribe:
   nc -U ~/.hermes/discord_events.sock

   # Python subscriber:
   python3 -c "
   import asyncio, json
   async def sub():
       r, w = await asyncio.open_unix_connection('$HOME/.hermes/discord_events.sock')
       while True:
           line = await r.readline()
           if not line: break
           print(json.loads(line))
   asyncio.run(sub())
   "

Event format (JSONL)
--------------------
Each event is one JSON line::

    {
      "type": "message.received",
      "id": "123456789",
      "timestamp": "2026-06-16T14:00:00",
      "channel": {"id": "...", "name": "...", "type": "dm|group|thread"},
      "guild": {"id": "...", "name": "..."},
      "author": {"id": "...", "name": "...", "is_bot": false},
      "content": "hello world",
      "attachments": ["file.png"],
      "reply_to": "msg_id"
    }

Enabling / disabling
--------------------
Set in ``.env``:

.. code-block:: bash

   DISCORD_EVENT_BUS_ENABLED=true
   # Optional: override the socket path (default ~/.hermes/discord_events.sock)
   DISCORD_EVENT_BUS_SOCKET=/tmp/discord_events.sock
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Set, Dict

import config  # this repo's config module

logger = logging.getLogger("discord-event-bus")

# ── settings ───────────────────────────────────────────────────────────

DISCORD_API_BASE = config.DISCORD_API_BASE
DEFAULT_POLL_INTERVAL = 5   # seconds (aggressive, per the doc's adaptive pattern)
IDLE_POLL_INTERVAL = 60     # seconds when no recent activity
ACTIVE_WINDOW = 120         # keep aggressive polling for 2 min after last message
MAX_CLIENTS = 32
KEEPALIVE_INTERVAL = 30.0   # PING frame interval

_SOCKET_NAME = os.getenv("DISCORD_EVENT_BUS_SOCKET", "discord_events.sock")
_STATE_FILENAME = ".discord_event_bus_state.json"


def _get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


def _get_socket_path() -> Path:
    return _get_hermes_home() / _SOCKET_NAME


def _get_state_path() -> Path:
    return _get_hermes_home() / _STATE_FILENAME


def _load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _dump_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


# ── Discord HTTP client (reuses repo's auth, works with user tokens) ───

class DiscordHTTPClient:
    """Lightweight async-compatible Discord REST API client.

    Uses the same auth as the rest of this repo (``DISCORD_AUTHORIZATION``
    with browser headers) so it works with user tokens. Falls back to
    ``DISCORD_BOT_TOKEN`` if set.
    """

    def __init__(self):
        self._token = config.get_authorization()
        self._is_bot = False
        if not self._token:
            # Fallback to bot token
            bt = os.getenv("DISCORD_BOT_TOKEN", "").strip()
            if bt:
                self._token = bt
                self._is_bot = True

    def _request(self, method: str, path: str,
                 params: dict | None = None) -> dict | list | None:
        url = f"{DISCORD_API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: str(v) for k, v in params.items() if v is not None}
            )

        if self._is_bot:
            headers = {
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "Hermes-EventBus/1.0",
            }
        else:
            headers = {
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.5",
                "authorization": self._token,
                "priority": "u=1, i",
                "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Linux"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": config.get_user_agent(),
                "x-debug-options": "bugReporterEnabled",
                "x-discord-locale": config.get_locale(),
                "x-discord-timezone": config.get_timezone(),
            }
            sp = config.get_super_properties()
            if sp:
                headers["x-super-properties"] = sp

        req = urllib.request.Request(url, method=method, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 204:
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            logger.warning("API %d on %s %s: %s", e.code, method, path, body[:200])
            return None

    def get_messages(self, channel_id: str, limit: int = 50,
                     after: str | None = None) -> list[dict]:
        params = {"limit": limit}
        if after:
            params["after"] = after
        result = self._request("GET", f"/channels/{channel_id}/messages", params)
        return result if isinstance(result, list) else []

    def get_channel(self, channel_id: str) -> dict | None:
        result = self._request("GET", f"/channels/{channel_id}")
        return result if isinstance(result, dict) else None

    def get_guild(self, guild_id: str) -> dict | None:
        result = self._request("GET", f"/guilds/{guild_id}")
        return result if isinstance(result, dict) else None


# ── event builder ──────────────────────────────────────────────────────

def build_event(msg: dict, channel: dict | None, guild: dict | None) -> dict:
    """Convert a Discord API message object into a standardised event."""
    ctype_raw = channel.get("type", 1) if channel else 1
    if ctype_raw in (0, 5, 15):
        channel_type = "group"
    elif ctype_raw in (10, 11, 12):
        channel_type = "thread"
    else:
        channel_type = "dm"

    channel_name = "DM"
    if channel and channel.get("name"):
        channel_name = channel["name"]

    event = {
        "type": "message.received",
        "id": msg.get("id", ""),
        "timestamp": msg.get("timestamp", ""),
        "channel": {
            "id": msg.get("channel_id", ""),
            "name": channel_name,
            "type": channel_type,
        },
        "author": {
            "id": msg.get("author", {}).get("id", ""),
            "name": msg.get("author", {}).get("username", "unknown"),
            "is_bot": msg.get("author", {}).get("bot", False),
        },
        "content": msg.get("content", ""),
    }

    if guild:
        event["guild"] = {"id": guild.get("id", ""), "name": guild.get("name", "unknown")}

    thread = msg.get("thread")
    if thread:
        event["thread_id"] = thread.get("id")

    ref = msg.get("message_reference")
    if ref:
        event["reply_to"] = ref.get("message_id")

    attachments = msg.get("attachments", [])
    if attachments:
        event["attachments"] = [a.get("filename", "unknown") for a in attachments]

    mentions = msg.get("mentions", [])
    if mentions:
        event["mentions"] = [m.get("id", "") for m in mentions]

    return event


# ── event bus server ───────────────────────────────────────────────────

class EventBus:
    """Unix-socket event bus. Subscribers receive JSON-lines events."""

    def __init__(self, socket_path: Path):
        self._socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._clients: Set[asyncio.StreamWriter] = set()
        self._lock = asyncio.Lock()
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        try:
            self._socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path), limit=MAX_CLIENTS,
        )
        os.chmod(str(self._socket_path), 0o666)
        self._running = True
        logger.info("Event bus listening on %s", self._socket_path)

    async def stop(self) -> None:
        self._running = False
        async with self._lock:
            for w in list(self._clients):
                try:
                    w.close()
                except Exception:
                    pass
            self._clients.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        try:
            self._socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("Event bus stopped")

    async def publish(self, event: dict) -> None:
        if not self._running:
            return
        line = json.dumps(event, ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        async with self._lock:
            dead = []
            for w in self._clients:
                try:
                    w.write(data)
                except Exception:
                    dead.append(w)
            for w in dead:
                self._clients.discard(w)

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername", "unknown")
        logger.info("Subscriber connected: %s", peer)
        async with self._lock:
            self._clients.add(writer)
        try:
            welcome = json.dumps({
                "type": "connected", "version": "1.0.0",
                "server": "discord-event-bus", "timestamp": time.time(),
            }) + "\n"
            writer.write(welcome.encode("utf-8"))
            while self._running:
                try:
                    data = await asyncio.wait_for(
                        reader.read(4096), timeout=KEEPALIVE_INTERVAL)
                    if not data:
                        break
                except asyncio.TimeoutError:
                    try:
                        writer.write(
                            json.dumps({"type": "ping", "ts": time.time()}).encode()
                            + b"\n")
                    except Exception:
                        break
        except Exception:
            pass
        finally:
            async with self._lock:
                self._clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("Subscriber disconnected: %s", peer)


# ── poller ─────────────────────────────────────────────────────────────

class EventBusPoller:
    """Polls Discord HTTP API and publishes new messages to the event bus.

    Uses the same channels.json as main.py for channel discovery.
    Implements adaptive polling: aggressive (5s) when active, idle (60s)
    when quiet.
    """

    def __init__(self, client: DiscordHTTPClient, bus: EventBus,
                 state_path: Path):
        self._client = client
        self._bus = bus
        self._state_path = state_path
        self._last_ids: Dict[str, str] = {}
        self._running = False
        self._last_activity = 0.0
        self._guild_cache: Dict[str, dict] = {}
        self._channel_cache: Dict[str, dict] = {}

    def _load_state(self) -> None:
        state = _load_json(self._state_path, {})
        self._last_ids = state.get("last_ids", {})

    def _save_state(self) -> None:
        _dump_json(self._state_path, {
            "last_ids": self._last_ids, "updated": time.time(),
        })

    def get_channels(self) -> Dict[str, str]:
        """Load channels from channels.json (same file as main.py uses)."""
        return config.load_channels()

    async def _get_channel_info(self, channel_id: str) -> dict | None:
        if channel_id not in self._channel_cache:
            info = self._client.get_channel(channel_id)
            if info:
                self._channel_cache[channel_id] = info
        return self._channel_cache.get(channel_id)

    async def _get_guild_info(self, guild_id: str) -> dict | None:
        if guild_id not in self._guild_cache:
            info = self._client.get_guild(guild_id)
            if info:
                self._guild_cache[guild_id] = info
        return self._guild_cache.get(guild_id)

    async def poll_channel(self, channel_id: str) -> int:
        """Poll one channel, return count of new messages published."""
        after = self._last_ids.get(channel_id)
        messages = self._client.get_messages(channel_id, limit=50, after=after)
        if not messages:
            return 0

        # Discord returns newest-first — reverse for chronological order
        messages = list(reversed(messages))

        channel = await self._get_channel_info(channel_id)
        guild_id = channel.get("guild_id") if channel else None
        guild = await self._get_guild_info(guild_id) if guild_id else None

        count = 0
        for msg in messages:
            msg_id = msg.get("id", "")
            if after and msg_id <= after:
                continue
            event = build_event(msg, channel, guild)
            await self._bus.publish(event)
            self._last_ids[channel_id] = msg_id
            count += 1
        return count

    async def run(self) -> None:
        self._load_state()
        self._running = True
        channels = self.get_channels()

        if not channels:
            logger.warning(
                "No channels in channels.json. Create one (copy channels.example.json) "
                "to start monitoring."
            )
            while self._running:
                await asyncio.sleep(10)
            return

        channel_list = list(channels.values())
        logger.info("Monitoring %d channel(s) from channels.json", len(channel_list))

        while self._running:
            total_new = 0
            for ch_id in channel_list:
                try:
                    new = await self.poll_channel(ch_id)
                    total_new += new
                    if new:
                        logger.info("Channel %s: %d new message(s)", ch_id, new)
                except Exception as e:
                    logger.warning("Error polling channel %s: %s", ch_id, e)

            self._save_state()

            now = time.time()
            if total_new > 0:
                self._last_activity = now
            interval = (
                DEFAULT_POLL_INTERVAL
                if now - self._last_activity < ACTIVE_WINDOW
                else IDLE_POLL_INTERVAL
            )
            await asyncio.sleep(interval)

    async def shutdown(self):
        self._running = False
        self._save_state()


# ── main ───────────────────────────────────────────────────────────────

async def _async_main():
    parser = argparse.ArgumentParser(
        description="Discord polling-to-event adapter — event bus daemon")
    parser.add_argument("--subscribe", action="store_true",
                        help="Connect to the event bus and print events (testing)")
    args = parser.parse_args()

    if args.subscribe:
        # Quick subscriber for testing
        sock = _get_socket_path()
        for attempt in range(10):
            try:
                reader, writer = await asyncio.open_unix_connection(str(sock))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if attempt == 9:
                    logger.error("Cannot connect to %s", sock)
                    return
                await asyncio.sleep(1)
        logger.info("Connected — Ctrl+C to exit")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    ev = json.loads(line.decode())
                except json.JSONDecodeError:
                    continue
                if ev.get("type") in ("ping",):
                    continue
                sys.stdout.write(json.dumps(ev, indent=2) + "\n")
                sys.stdout.flush()
        finally:
            writer.close()
            await writer.wait_closed()
        return

    # ── daemon mode ──
    client = DiscordHTTPClient()
    bus = EventBus(_get_socket_path())
    poller = EventBusPoller(client, bus, _get_state_path())

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _on_signal():
        logger.info("Shutting down...")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    await bus.start()
    poll_task = asyncio.create_task(poller.run())

    await shutdown_event.wait()
    await poller.shutdown()
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    await bus.stop()
    logger.info("Goodbye.")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
