"""CLI tool to manage vibr8 users.

Usage:
    uv run python -m server.manage_users add <username>
    uv run python -m server.manage_users remove <username>
    uv run python -m server.manage_users list
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

import bcrypt

USERS_FILE = Path.home() / ".companion" / "users.json"


def load() -> dict:
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text())
    return {"users": {}}


def save(data: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(data, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage vibr8 users")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="List all users")
    add_p = sub.add_parser("add", help="Add a user")
    add_p.add_argument("username")
    rm_p = sub.add_parser("remove", help="Remove a user")
    rm_p.add_argument("username")
    args = parser.parse_args()

    if args.command == "list":
        data = load()
        users = list(data.get("users", {}).keys())
        if users:
            for u in users:
                print(u)
        else:
            print("No users configured")
    elif args.command == "add":
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm: ")
        if password != confirm:
            print("Passwords do not match", file=sys.stderr)
            sys.exit(1)
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        data = load()
        data["users"][args.username] = hashed
        save(data)
        print(f"Added user: {args.username}")
    elif args.command == "remove":
        data = load()
        if args.username in data.get("users", {}):
            del data["users"][args.username]
            save(data)
            print(f"Removed user: {args.username}")
        else:
            print(f"User not found: {args.username}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
