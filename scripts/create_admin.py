"""Create the first user (no self-signup). Run from the project root:

    python -m scripts.create_admin --username admin --role admin
"""
import argparse
import asyncio
import getpass

import aiosqlite

from app.auth import ROLES, hash_password
from app.db import init_db
from app.settings import get_settings


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", required=True)
    ap.add_argument("--role", choices=ROLES, default="admin")
    args = ap.parse_args()
    password = getpass.getpass("Password (min 10 chars): ")
    if len(password) < 10:
        raise SystemExit("Password too short")
    await init_db()
    async with aiosqlite.connect(get_settings().db_path) as db:
        await db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (args.username, hash_password(password), args.role),
        )
        await db.commit()
    print(f"Created {args.role} '{args.username}'")


if __name__ == "__main__":
    asyncio.run(main())
