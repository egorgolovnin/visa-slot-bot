"""Shell runner for the in-account probe, so it can be run without Telegram:

    docker compose exec bot python -m app._probe_run aut

Logs in via the normal path (Capsolver + OTP from the mailbox) and prints the probe result.
"""
import asyncio
import re
import sys

from . import config, mailbox
from .slot_probe import probe_account
from .vfs_auth import live_session


async def main() -> None:
    mission = (sys.argv[1] if len(sys.argv) > 1 else "aut").strip().lower()
    settings = config.load()

    async def wait(**kw):
        return await mailbox.wait_for(**kw) if mailbox.enabled() else None

    print(f"[probe] logging in to VFS cyp/{mission} …")
    sess = await live_session("cyp", mission, settings.proxy_url, wait)
    if not (sess and sess.alive):
        print("[probe] login FAILED — no live session (check Capsolver / OTP mailbox)")
        return
    print("[probe] login ok, hitting in-account endpoints …\n")
    out = await probe_account(sess)
    print(re.sub(r"</?(b|code)>", "", out))  # strip Telegram tags for the terminal


if __name__ == "__main__":
    asyncio.run(main())
