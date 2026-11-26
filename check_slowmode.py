import asyncio
import os
from datetime import datetime, timezone

import discord

CHANNEL_ID = 1543748327152029756


class CheckClient(discord.Client):
    async def on_ready(self):
        try:
            channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
            slowmode = getattr(channel, "slowmode_delay", 0) or 0
            me = self.user

            last = None
            async for msg in channel.history(limit=200):
                if msg.author.id == me.id:
                    last = msg
                    break

            now = datetime.now(timezone.utc)
            print(f"Channel: {CHANNEL_ID}")
            print(f"Slowmode: {slowmode} seconds")

            if last is None:
                print("No recent message from you found in last 200 messages.")
                print("You can likely send (unless another restriction applies).")
                return

            elapsed = (now - last.created_at).total_seconds()
            remaining = slowmode - elapsed
            print(f"Your last message: {last.created_at.isoformat()} ({last.id})")
            print(f"Elapsed: {int(elapsed)} seconds")

            if remaining <= 0:
                print("Slowmode is over. You can send now.")
            else:
                mins, secs = divmod(int(remaining), 60)
                hrs, mins = divmod(mins, 60)
                print(f"Slowmode remaining: {hrs}h {mins}m {secs}s")
        finally:
            await self.close()


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN first.")
    client = CheckClient()
    client.run(token)


if __name__ == "__main__":
    main()
