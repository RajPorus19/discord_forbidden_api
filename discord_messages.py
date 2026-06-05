import json
import os
import re
import time
from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple

import requests

import config


DISCORD_API_BASE = config.DISCORD_API_BASE


class DiscordMessages:
    """
    Small helper class to fetch and post-process Discord messages.

    All credentials are read from the environment via the `config` module, so
    nothing sensitive is hardcoded. See `.env.example` for the variables used.
    """

    def __init__(self, channel_id: Optional[str] = None) -> None:
        """
        Create a new DiscordMessages helper.

        - channel_id: the channel to read. If None, falls back to the
          DISCORD_CHANNEL_ID environment variable.
        """
        resolved = channel_id or config.default_channel_id()
        if not resolved:
            raise config.ConfigError(
                "No channel_id provided and DISCORD_CHANNEL_ID is not set."
            )
        self.channel_id: str = resolved

    # ---------- Internal helpers ----------

    def _build_request(
        self, limit: int = 50, before_id: Optional[str] = None
    ) -> Tuple[str, Dict[str, str], Dict[str, str], Dict[str, str]]:
        """Build URL, headers, cookies, and params for a messages request."""
        channel_id = self.channel_id

        url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"

        params: Dict[str, str] = {"limit": str(limit)}
        if before_id:
            params["before"] = str(before_id)

        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.5",
            "authorization": config.get_authorization(),
            "priority": "u=1, i",
            "referer": f"https://discord.com/channels/@me/{channel_id}",
            "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sec-gpc": "1",
            "user-agent": config.get_user_agent(),
            "x-debug-options": "bugReporterEnabled",
            "x-discord-locale": config.get_locale(),
            "x-discord-timezone": config.get_timezone(),
        }
        super_properties = config.get_super_properties()
        if super_properties:
            headers["x-super-properties"] = super_properties

        cookies = config.get_cookies()

        return url, headers, cookies, params

    def _fetch_raw_messages(
        self, limit: int = 50, before_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Low-level fetch: returns the raw list of message dicts from Discord."""
        url, headers, cookies, params = self._build_request(
            limit=limit, before_id=before_id
        )

        response = requests.get(
            url,
            headers=headers,
            cookies=cookies,
            params=params,
            timeout=15,
        )

        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(
                f"Discord API error: {response.status_code} - {response.text}"
            )

        data = response.json()
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected response format: {data!r}")

        return data

    # ---------- Public API ----------

    @staticmethod
    def _replace_mentions_with_usernames(
        content: str, mentions: List[Dict[str, Any]]
    ) -> str:
        """
        Replace occurrences of <@123> / <@!123> in the message content
        with @username, using the message's "mentions" array.
        """
        if not content:
            return content

        id_to_username = {
            str(user.get("id")): user.get("username", "unknown")
            for user in (mentions or [])
        }

        pattern = re.compile(r"<@!?(\d+)>")

        def _repl(match: re.Match) -> str:
            user_id = match.group(1)
            username = id_to_username.get(user_id)
            if username:
                return f"@{username}"
            return match.group(0)

        return pattern.sub(_repl, content)

    def _build_log_entry(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build a normalized dict for logging, with:
          - author_id and author_username
          - content_resolved where all <@id> mentions are replaced by @username
        """
        author = msg.get("author", {}) or {}
        content_original = msg.get("content", "") or ""
        content_resolved = self._replace_mentions_with_usernames(
            content_original,
            msg.get("mentions", []),
        )

        return {
            "id": str(msg.get("id", "")),
            "timestamp": msg.get("timestamp", ""),
            "author_username": author.get("username", ""),
            "author_id": str(author.get("id", "")),
            "content": content_original,
            "content_resolved": content_resolved,
            "channel_id": self.channel_id,
            "raw": msg,
        }

    def get_messages(
        self, limit: int = 50, before_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        High-level reusable helper to fetch messages.

        - Uses env / default channel ID configured on the instance.
        - Returns the list of message dicts, each enriched with a
          'content_resolved' field where <@id> mentions are replaced
          by @username.
        - Messages are returned oldest-first (reverse of the API order).
        """
        messages = self._fetch_raw_messages(limit=limit, before_id=before_id)
        messages.reverse()  # oldest first

        for msg in messages:
            msg["content_resolved"] = self._build_log_entry(msg)["content_resolved"]

        return messages

    def append_messages_to_csv(self, filepath: str, limit: int = 50) -> int:
        """
        Deprecated: prefer append_messages_to_json.

        Kept for backward compatibility.
        """
        return self.append_messages_to_json(filepath + ".deprecated.csv", limit=limit)

    def append_messages_to_json(
        self,
        filepath: str,
        limit: int = 50,
        before_id: Optional[str] = None,
        ensure_sorted: bool = True,
    ) -> int:
        """
        Fetch messages and append them to a JSON-lines file (one JSON object per line).

        Each object has, at minimum:
          - id
          - timestamp
          - author_username
          - author_id
          - content
          - content_resolved
          - channel_id
          - raw (the full original Discord message object)

        - If the file exists, only new messages (by id) are appended.
        - Returns the number of messages appended.
        """
        messages = self.get_messages(limit=limit, before_id=before_id)

        # Ensure parent directory exists (if any)
        directory = os.path.dirname(filepath)
        if directory:
            os.makedirs(directory, exist_ok=True)

        # Load existing entries if any
        existing_entries: Dict[str, Dict[str, Any]] = {}
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg_id = obj.get("id")
                    if msg_id is not None:
                        existing_entries[str(msg_id)] = obj

        # Build entries for newly fetched messages
        new_count = 0
        for msg in messages:
            msg_id = str(msg.get("id", ""))
            if not msg_id or msg_id in existing_entries:
                continue
            existing_entries[msg_id] = self._build_log_entry(msg)
            new_count += 1

        if new_count == 0:
            return 0

        # Optionally re-sort and rewrite file from scratch
        entries_list = list(existing_entries.values())
        if ensure_sorted:
            entries_list.sort(key=lambda e: int(e.get("id", 0)))

        with open(filepath, "w", encoding="utf-8") as f:
            for obj in entries_list:
                json.dump(obj, f, ensure_ascii=False)
                f.write("\n")

        return new_count

    def append_messages_to_csv(self, filepath: str, limit: int = 50) -> int:
        """
        Fetch messages and append them to a semicolon-separated CSV-like file.

        Columns:
          message_id ; datetime_iso ; author_username ; author_id ; content ; channel_id

        - If the file exists, only new messages (by message_id) are appended.
        - Returns the number of messages appended.
        """
        messages = self.get_messages(limit=limit)

        # Collect already-logged IDs if file exists
        existing_ids: set[str] = set()
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("message_id"):
                        continue
                    parts = line.split(";", 1)
                    if parts and parts[0]:
                        existing_ids.add(parts[0])

        # Filter to new messages only
        new_messages = [m for m in messages if str(m.get("id")) not in existing_ids]

        # Nothing new to append
        if not new_messages:
            return 0

        # Ensure deterministic order: oldest first by ID (Discord snowflakes increase over time)
        new_messages.sort(key=lambda m: int(m.get("id", 0)))

        file_exists = os.path.exists(filepath)
        with open(filepath, "a", encoding="utf-8") as f:
            # Write header only if creating the file
            if not file_exists:
                f.write(
                    "message_id;datetime_iso;author_username;author_id;content;channel_id\n"
                )

            for msg in new_messages:
                message_id = str(msg.get("id", ""))
                timestamp = msg.get("timestamp", "")
                author = msg.get("author", {}) or {}
                author_username = author.get("username", "")
                author_id = str(author.get("id", ""))
                content = msg.get("content_resolved", msg.get("content", "")) or ""

                # Basic sanitization for newlines / semicolons to keep format stable
                safe_content = (
                    content.replace("\n", "\\n").replace("\r", "\\r").replace(";", ",")
                )

                line = (
                    f"{message_id};{timestamp};{author_username};"
                    f"{author_id};{safe_content};{self.channel_id}\n"
                )
                f.write(line)

        return len(new_messages)

    def append_older_messages_to_json(
        self,
        filepath: str,
        limit: int = 50,
    ) -> int:
        """
        Fetch the next page of older messages and merge into the JSONL log,
        keeping it sorted from oldest to newest.

        Strategy:
          - If the log file is empty or missing, behaves like append_messages_to_json.
          - Otherwise, finds the smallest (oldest) message id in the log and
            requests messages "before" that id.
        """
        oldest_id: Optional[str] = None
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg_id = obj.get("id")
                    if msg_id is None:
                        continue
                    msg_id_str = str(msg_id)
                    if oldest_id is None or int(msg_id_str) < int(oldest_id):
                        oldest_id = msg_id_str

        # If we have no existing messages, just fetch the latest and sort
        if oldest_id is None:
            return self.append_messages_to_json(
                filepath, limit=limit, before_id=None, ensure_sorted=True
            )

        # Otherwise, request messages older than the current oldest
        return self.append_messages_to_json(
            filepath, limit=limit, before_id=oldest_id, ensure_sorted=True
        )

    def fetch_all_messages_to_json(
        self,
        filepath: str,
        limit: int = 50,
        delay: float = 5.0,
    ) -> int:
        """
        Backfill the channel: fetch all messages from newest to oldest,
        page by page, and write them to a JSONL file sorted first (oldest) to last (newest).

        - delay: seconds to wait before each API request (default 5). Waits once before
          the first query, then before each next page until no more older messages.
        - After each query, the file gains more lines (until the start of the channel).
        - Returns the total number of messages in the file when done.
        """
        request_num = 0
        while True:
            request_num += 1
            print(f"Request #{request_num}: waiting {delay}s before query...")
            time.sleep(delay)
            added = self.append_older_messages_to_json(filepath, limit=limit)
            if added == 0:
                print(f"Request #{request_num}: no older messages; backfill complete.")
                break
            with open(filepath, encoding="utf-8") as f:
                total = sum(1 for line in f if line.strip())
            print(f"Request #{request_num}: added {added} messages (total in file: {total})")

        if not os.path.exists(filepath):
            return 0
        count = 0
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count

    def _load_existing_entries(self, filepath: str) -> Dict[str, Dict[str, Any]]:
        """Load id -> parsed object from JSONL file. Returns {} if file missing or empty."""
        existing: Dict[str, Dict[str, Any]] = {}
        if not os.path.exists(filepath):
            return existing
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_id = obj.get("id")
                if msg_id is not None:
                    existing[str(msg_id)] = obj
        return existing

    def _fetch_all_raw_messages(
        self,
        limit: int = 50,
        delay: float = 5.0,
        max_messages: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch the channel from latest going back, up to `max_messages`
        (None = the entire channel).
        Waits `delay` seconds before each request and prints progress.
        Returns list of raw message dicts (newest to oldest order).
        """
        collected: List[Dict[str, Any]] = []
        before_id: Optional[str] = None
        request_num = 0

        while True:
            request_num += 1
            print(f"Request #{request_num}: waiting {delay}s before query...")
            time.sleep(delay)
            messages = self.get_messages(limit=limit, before_id=before_id)
            if not messages:
                print(f"Request #{request_num}: no messages; reached start of channel.")
                break
            collected.extend(messages)
            if max_messages is not None and len(collected) >= max_messages:
                print(f"Request #{request_num}: reached cap ({max_messages}); stopping.")
                break
            oldest_in_batch = min((int(m["id"]) for m in messages if m.get("id")), default=None)
            if oldest_in_batch is None:
                break
            before_id = str(oldest_in_batch)
            print(f"Request #{request_num}: fetched {len(messages)} (total so far: {len(collected)}).")

        return collected

    def fetch_channel(
        self,
        filepath: str,
        limit: int = 50,
        delay: float = 5.0,
        force_verify: bool = False,
        max_old_messages: Optional[int] = None,
    ) -> int:
        """
        Single entry point to sync this channel to a JSONL file.

        - If no messages are known for this channel (file missing or empty):
          fetches messages from latest going back, up to `max_old_messages`
          (a first-run backfill cap; None = no cap).

        - If messages are already known (file exists with data):
          fetches the latest messages until arriving at the last known message,
          and appends only new ones.

        - If force_verify=True (default False):
          fetches all messages in the channel, verifies none are missing,
          and appends any lost messages in the correct timespace (sorted by id).
        """
        directory = os.path.dirname(filepath)
        if directory:
            os.makedirs(directory, exist_ok=True)

        if force_verify:
            print("Force verify: fetching all messages and filling any gaps.")
            existing_entries = self._load_existing_entries(filepath)
            all_raw = self._fetch_all_raw_messages(
                limit=limit, delay=delay, max_messages=max_old_messages
            )
            added = 0
            for msg in all_raw:
                msg_id = str(msg.get("id", ""))
                if not msg_id:
                    continue
                if msg_id not in existing_entries:
                    existing_entries[msg_id] = self._build_log_entry(msg)
                    added += 1
            entries_list = list(existing_entries.values())
            entries_list.sort(key=lambda e: int(e.get("id", 0)))
            with open(filepath, "w", encoding="utf-8") as f:
                for obj in entries_list:
                    json.dump(obj, f, ensure_ascii=False)
                    f.write("\n")
            print(f"Force verify: added {added} missing message(s) (total in file: {len(entries_list)}).")
            return added

        return self.refresh_new_messages_to_json(
            filepath, limit=limit, delay=delay, max_old_messages=max_old_messages
        )

    def refresh_new_messages_to_json(
        self,
        filepath: str,
        limit: int = 50,
        delay: float = 5.0,
        max_old_messages: Optional[int] = None,
    ) -> int:
        """
        Fetch new messages and append only those not already in the JSONL file.

        Strategy:
          - Request the 50 latest messages, then keep requesting older pages
          (before_id = oldest in current batch) until a batch contains at least
          one message already present in the file (overlap). Then merge: add
          only messages that don't exist yet, sort by id, rewrite the file.

        - delay: seconds to wait before each API request.
        - Prints progress for each request.
        - Returns the number of new messages added.
        """
        directory = os.path.dirname(filepath)
        if directory:
            os.makedirs(directory, exist_ok=True)

        existing_entries = self._load_existing_entries(filepath)
        existing_ids = set(existing_entries.keys())

        collected: List[Dict[str, Any]] = []  # raw message dicts we've fetched
        before_id: Optional[str] = None
        request_num = 0

        while True:
            request_num += 1
            print(f"Refresh request #{request_num}: waiting {delay}s before query...")
            time.sleep(delay)

            messages = self.get_messages(limit=limit, before_id=before_id)
            if not messages:
                print(f"Refresh request #{request_num}: no messages; stopping.")
                break

            n_before = len(existing_entries)
            batch_ids = {str(m.get("id")) for m in messages if m.get("id") is not None}
            overlap = batch_ids & existing_ids

            for msg in messages:
                msg_id = str(msg.get("id", ""))
                if msg_id and msg_id not in existing_entries:
                    collected.append(msg)
                    existing_entries[msg_id] = self._build_log_entry(msg)
                    existing_ids.add(msg_id)

            # Write file after each query cycle when we have new messages (creates file if missing)
            if len(existing_entries) > n_before:
                entries_list = list(existing_entries.values())
                entries_list.sort(key=lambda e: int(e.get("id", 0)))
                with open(filepath, "w", encoding="utf-8") as f:
                    for obj in entries_list:
                        json.dump(obj, f, ensure_ascii=False)
                        f.write("\n")
                print(
                    f"Refresh request #{request_num}: wrote {len(entries_list)} message(s) to file."
                )

            if overlap:
                print(
                    f"Refresh request #{request_num}: found {len(overlap)} message(s) "
                    "already in file; stopping."
                )
                break

            if max_old_messages is not None and len(collected) >= max_old_messages:
                print(
                    f"Refresh request #{request_num}: reached old-message cap "
                    f"({max_old_messages}); stopping backfill."
                )
                break

            oldest_in_batch = min((int(m["id"]) for m in messages if m.get("id")), default=None)
            if oldest_in_batch is None:
                break
            before_id = str(oldest_in_batch)
            print(f"Refresh request #{request_num}: no overlap yet; fetched {len(messages)} (going older).")

        added = len(collected)
        if added == 0:
            print("Refresh: no new messages to add.")
            return 0

        print(f"Refresh: added {added} new message(s) (total in file: {len(existing_entries)}).")
        return added

