import os
import time

import config
from discord_messages import DiscordMessages


def main() -> None:
    # Channels to sync are read from channels.json (see channels.example.json).
    channel_id_dict = config.load_channels()
    if not channel_id_dict:
        raise SystemExit(
            f"No channels configured. Copy channels.example.json to "
            f"{config.CHANNELS_FILE} and add your channel name -> id mappings."
        )

    limit = 50                          # Messages per API request
    delay = 5.0                         # Seconds to wait before each request
    refresh_time = config.get_sync_interval_seconds()  # Wait between sync passes
    force_verify = False                # True = refetch entire channel and fill any gaps
    max_old_messages = config.get_max_old_messages()  # First-run backfill cap

    while True:
        for channel_slug, channel_id in channel_id_dict.items():
            filepath = os.path.join(
                config.CHATLOGS_DIR, f"messages_in_{channel_slug}.jsonl"
            )
            client = DiscordMessages(channel_id=channel_id)
            added = client.fetch_channel(
                filepath,
                limit=limit,
                delay=delay,
                force_verify=force_verify,
                max_old_messages=max_old_messages,
            )
            print(
                f"Sync complete: {added} message(s) added for channel {channel_id} -> {filepath}"
            )
        print(f"Waiting {refresh_time} seconds before next sync...")
        time.sleep(refresh_time)


if __name__ == "__main__":
    main()
