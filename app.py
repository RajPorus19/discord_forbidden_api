"""
Flask API exposing the locally stored Discord messages, plus a send endpoint.

Run with:
    flask --app app run            # or: python app.py

Endpoints:
    GET  /health
    GET  /channels
        -> list of channel slugs that have a local log file
    GET  /channels/<slug>/messages?limit=&before_id=&after_id=&author_id=&q=
        -> stored messages for a channel as JSON
    POST /channels/<slug>/send
        body: {"content": "...", "reply_to": "<message_id>" (optional),
               "mention_author": true (optional)}
        -> sends a message (or reply) to the channel
"""

import glob
import json
import os
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request

import config
from discord_send_messages import DiscordSendMessage


app = Flask(__name__)

LOG_PREFIX = "messages_in_"
LOG_SUFFIX = ".jsonl"


def _log_path(slug: str) -> str:
    return os.path.join(config.CHATLOGS_DIR, f"{LOG_PREFIX}{slug}{LOG_SUFFIX}")


def _list_channel_slugs() -> List[str]:
    """Return slugs for every chatlog file that exists on disk."""
    pattern = os.path.join(config.CHATLOGS_DIR, f"{LOG_PREFIX}*{LOG_SUFFIX}")
    slugs = []
    for path in sorted(glob.glob(pattern)):
        name = os.path.basename(path)
        slug = name[len(LOG_PREFIX) : -len(LOG_SUFFIX)]
        slugs.append(slug)
    return slugs


def _resolve_channel_id(slug: str) -> Optional[str]:
    """Map a slug to its channel id using channels.json, if available."""
    return config.load_channels().get(slug)


def _load_messages(slug: str) -> List[Dict[str, Any]]:
    """Read all stored messages for a channel slug (oldest first)."""
    path = _log_path(slug)
    if not os.path.exists(path):
        return []
    messages: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return messages


@app.get("/health")
def health() -> Any:
    return jsonify({"status": "ok"})


@app.get("/channels")
def channels() -> Any:
    slugs = _list_channel_slugs()
    known = config.load_channels()
    return jsonify(
        {
            "channels": [
                {"slug": slug, "channel_id": known.get(slug)} for slug in slugs
            ]
        }
    )


@app.get("/channels/<slug>/messages")
def channel_messages(slug: str) -> Any:
    messages = _load_messages(slug)
    if not messages and not os.path.exists(_log_path(slug)):
        return jsonify({"error": f"No log found for channel '{slug}'."}), 404

    # Optional filters.
    author_id = request.args.get("author_id")
    query = request.args.get("q")
    before_id = request.args.get("before_id")
    after_id = request.args.get("after_id")
    limit = request.args.get("limit", type=int)

    def keep(msg: Dict[str, Any]) -> bool:
        if author_id and str(msg.get("author_id")) != str(author_id):
            return False
        if before_id and int(msg.get("id", 0)) >= int(before_id):
            return False
        if after_id and int(msg.get("id", 0)) <= int(after_id):
            return False
        if query:
            haystack = (
                msg.get("content_resolved") or msg.get("content") or ""
            ).lower()
            if query.lower() not in haystack:
                return False
        return True

    filtered = [m for m in messages if keep(m)]

    # limit returns the most recent N (filtered) messages, still oldest-first.
    if limit is not None and limit >= 0:
        filtered = filtered[-limit:]

    return jsonify(
        {
            "slug": slug,
            "channel_id": _resolve_channel_id(slug),
            "count": len(filtered),
            "messages": filtered,
        }
    )


@app.post("/channels/<slug>/send")
def send_to_channel(slug: str) -> Any:
    body = request.get_json(silent=True) or {}
    content = body.get("content")
    if not content:
        return jsonify({"error": "'content' is required."}), 400

    channel_id = _resolve_channel_id(slug) or body.get("channel_id")
    if not channel_id:
        return (
            jsonify(
                {
                    "error": (
                        f"Unknown channel '{slug}'. Add it to "
                        f"{config.CHANNELS_FILE} or pass 'channel_id' in the body."
                    )
                }
            ),
            400,
        )

    reply_to = body.get("reply_to")
    mention_author = bool(body.get("mention_author", True))

    try:
        client = DiscordSendMessage(channel_id=channel_id)
        result = client.send_message(
            content,
            reply_to=reply_to,
            mention_author=mention_author,
        )
    except Exception as exc:  # surface Discord/config errors to the caller
        return jsonify({"error": str(exc)}), 502

    return jsonify(
        {
            "sent": True,
            "channel_id": channel_id,
            "reply_to": reply_to,
            "message": {"id": result.get("id"), "content": result.get("content")},
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=port, debug=True)
