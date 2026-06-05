"""
Centralized configuration for the Discord client.

All secrets (account token, cookies, fingerprint properties) are read from the
environment so that nothing sensitive is committed to the repository. Copy
`.env.example` to `.env` and fill in your own values.
"""

import json
import os
from typing import Dict, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional at runtime
    pass


DISCORD_API_BASE = os.getenv("DISCORD_API_BASE", "https://discord.com/api/v9")

# Default base64 of {"location":"chat_input"} - not a secret, just request context.
DEFAULT_X_CONTEXT_PROPERTIES = "eyJsb2NhdGlvbiI6ImNoYXRfaW5wdXQifQ=="

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36"
)

# Where channel name -> id mappings live, and where logs are written.
CHANNELS_FILE = os.getenv("DISCORD_CHANNELS_FILE", "channels.json")
CHATLOGS_DIR = os.getenv("DISCORD_CHATLOGS_DIR", "chatlogs")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def require_env(name: str) -> str:
    """Return the environment variable or raise a helpful error."""
    value = os.getenv(name)
    if not value:
        raise ConfigError(
            f"Missing required environment variable: {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


def get_authorization() -> str:
    """The Discord account token (required for all requests)."""
    return require_env("DISCORD_AUTHORIZATION")


def get_cookies() -> Dict[str, str]:
    """
    Optional cookies used to look like a real browser session. Only the ones
    that are set in the environment are included.
    """
    mapping = {
        "__dcfduid": "DISCORD_COOKIE_DCFDUID",
        "__sdcfduid": "DISCORD_COOKIE_SDCFDUID",
        "locale": "DISCORD_COOKIE_LOCALE",
        "_cfuvid": "DISCORD_COOKIE_CFUVID",
        "cf_clearance": "DISCORD_COOKIE_CF_CLEARANCE",
    }
    cookies: Dict[str, str] = {}
    for cookie_name, env_name in mapping.items():
        value = os.getenv(env_name)
        if value:
            cookies[cookie_name] = value
    return cookies


def get_super_properties() -> Optional[str]:
    """Optional x-super-properties browser fingerprint (base64)."""
    return os.getenv("DISCORD_X_SUPER_PROPERTIES")


def get_context_properties() -> str:
    """x-context-properties used when sending a message."""
    return os.getenv("DISCORD_X_CONTEXT_PROPERTIES", DEFAULT_X_CONTEXT_PROPERTIES)


def get_user_agent() -> str:
    return os.getenv("DISCORD_USER_AGENT", DEFAULT_USER_AGENT)


def get_locale() -> str:
    return os.getenv("DISCORD_LOCALE", "en-US")


def get_timezone() -> str:
    return os.getenv("DISCORD_TIMEZONE", "Europe/Paris")


def default_channel_id() -> Optional[str]:
    """A single channel id, if the user prefers env config over channels.json."""
    return os.getenv("DISCORD_CHANNEL_ID")


def get_max_old_messages() -> Optional[int]:
    """
    Cap on how many old messages to pull when backfilling a channel (mostly
    relevant on the first run). Returns None (unlimited) if set to 0 or empty.
    """
    raw = os.getenv("DISCORD_MAX_OLD_MESSAGES", "1000")
    try:
        value = int(raw)
    except ValueError:
        return 1000
    return value if value > 0 else None


def get_sync_interval_seconds() -> int:
    """How long to wait between full sync passes over all channels."""
    raw = os.getenv("DISCORD_SYNC_INTERVAL_SECONDS", "300")
    try:
        value = int(raw)
    except ValueError:
        return 300
    return value if value > 0 else 300


def load_channels() -> Dict[str, str]:
    """
    Load the {channel_slug: channel_id} mapping from CHANNELS_FILE.

    Returns an empty dict if the file does not exist.
    """
    if not os.path.exists(CHANNELS_FILE):
        return {}
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ConfigError(f"{CHANNELS_FILE} must contain a JSON object of name -> id.")
    return {str(name): str(channel_id) for name, channel_id in data.items()}
