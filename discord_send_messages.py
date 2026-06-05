"""
Send a message to a Discord channel via the API.

Supports plain messages and replies to a specific message.
"""

import random
import time
from http import HTTPStatus
from typing import Any, Dict, Optional

import requests

import config


DISCORD_API_BASE = config.DISCORD_API_BASE


class DiscordSendMessage:
    """
    Send messages to a Discord channel.

    All credentials are read from the environment via the `config` module.
    See `.env.example` for the variables used.
    """

    def __init__(self, channel_id: Optional[str] = None) -> None:
        resolved = channel_id or config.default_channel_id()
        if not resolved:
            raise config.ConfigError(
                "No channel_id provided and DISCORD_CHANNEL_ID is not set."
            )
        self.channel_id: str = resolved

    def _headers(self) -> Dict[str, str]:
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.5",
            "authorization": config.get_authorization(),
            "content-type": "application/json",
            "origin": "https://discord.com",
            "priority": "u=1, i",
            "referer": f"https://discord.com/channels/@me/{self.channel_id}",
            "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "sec-gpc": "1",
            "user-agent": config.get_user_agent(),
            "x-context-properties": config.get_context_properties(),
            "x-debug-options": "bugReporterEnabled",
            "x-discord-locale": config.get_locale(),
            "x-discord-timezone": config.get_timezone(),
        }
        super_properties = config.get_super_properties()
        if super_properties:
            headers["x-super-properties"] = super_properties
        return headers

    def _cookies(self) -> Dict[str, str]:
        return config.get_cookies()

    def send_message(
        self,
        content: str,
        tts: bool = False,
        nonce: Optional[str] = None,
        mobile_network_type: str = "unknown",
        flags: int = 0,
        reply_to: Optional[str] = None,
        reply_channel_id: Optional[str] = None,
        mention_author: bool = True,
    ) -> Dict[str, Any]:
        """
        POST a message to the channel. Returns the API response JSON.

        - content: message text (required).
        - tts: text-to-speech (default False).
        - nonce: unique string for the request (default: generated).
        - mobile_network_type, flags: passed through to the API.
        - reply_to: if set, the message id this message replies to. The reply
          appears threaded under that message in Discord.
        - reply_channel_id: channel id the replied-to message lives in
          (defaults to this client's channel).
        - mention_author: whether to ping the author of the replied-to message
          (only relevant when reply_to is set).
        """
        if nonce is None:
            nonce = str((int(time.time() * 1000) << 22) + random.randint(0, 2**22 - 1))

        url = f"{DISCORD_API_BASE}/channels/{self.channel_id}/messages"
        payload: Dict[str, Any] = {
            "mobile_network_type": mobile_network_type,
            "content": content,
            "nonce": nonce,
            "tts": tts,
            "flags": flags,
        }

        if reply_to:
            payload["message_reference"] = {
                "channel_id": reply_channel_id or self.channel_id,
                "message_id": str(reply_to),
            }
            if not mention_author:
                # Suppress the ping but keep the visual reply.
                payload["allowed_mentions"] = {
                    "parse": ["users", "roles", "everyone"],
                    "replied_user": False,
                }

        response = requests.post(
            url,
            headers=self._headers(),
            cookies=self._cookies(),
            json=payload,
            timeout=15,
        )

        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(
                f"Discord API error: {response.status_code} - {response.text}"
            )

        return response.json()

    def reply_to_message(
        self,
        message_id: str,
        content: str,
        mention_author: bool = True,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Convenience wrapper to reply to a specific message in this channel."""
        return self.send_message(
            content,
            reply_to=message_id,
            mention_author=mention_author,
            **kwargs,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Send a Discord message.")
    parser.add_argument("content", help="Message text to send.")
    parser.add_argument("--channel-id", help="Target channel id.")
    parser.add_argument("--reply-to", help="Message id to reply to.")
    parser.add_argument(
        "--no-mention",
        action="store_true",
        help="When replying, do not ping the original author.",
    )
    args = parser.parse_args()

    client = DiscordSendMessage(channel_id=args.channel_id)
    result = client.send_message(
        args.content,
        reply_to=args.reply_to,
        mention_author=not args.no_mention,
    )
    print("Sent message:", result.get("id"), result.get("content"))
