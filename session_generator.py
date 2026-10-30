"""
session_generator.py
────────────────────
Run this ONCE locally to generate your Pyrogram SESSION_STRING.
The string is then stored as an environment variable on Railway —
no session file ever touches the server.

Usage:
    python session_generator.py

You will be prompted for your phone number and Telegram OTP.
Copy the printed string into Railway → Variables → SESSION_STRING.
"""

import asyncio
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded


async def generate():
    print("=" * 60)
    print("  Telegram Session String Generator")
    print("=" * 60)
    print()

    api_id  = int(input("Enter your API_ID  : ").strip())
    api_hash = input("Enter your API_HASH : ").strip()

    print()
    print("Logging in… You'll receive a Telegram OTP.")
    print()

    async with Client(
        ":memory:",
        api_id=api_id,
        api_hash=api_hash,
    ) as client:
        session_string = await client.export_session_string()

    print()
    print("=" * 60)
    print("  ✅  SESSION STRING (copy the entire line below)")
    print("=" * 60)
    print()
    print(session_string)
    print()
    print("=" * 60)
    print("Add this as SESSION_STRING in your Railway environment variables.")
    print("Keep it secret — it grants full access to your Telegram account.")


if __name__ == "__main__":
    asyncio.run(generate())
