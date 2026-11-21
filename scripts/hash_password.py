"""Print a ``DEMO_USERS`` entry for one demo user: ``name[:role]:scrypt$<salt_hex>$<hash_hex>``.

    uv run python scripts/hash_password.py alice                  # role "both" (default)
    uv run python scripts/hash_password.py bob --role approver

The password is read without echo (asked twice), never taken from the command line, and never
printed. Join several entries with commas:
``DEMO_USERS='alice:scrypt$...,bob:approver:scrypt$...'`` (single quotes in ``.env``: the value
contains ``$``; with docker compose write each ``$`` as ``$$``).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.auth import ROLES, hash_password, parse_users  # noqa: E402

MIN_PASSWORD_CHARS = 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("name", help="user name (A-Z a-z 0-9 _ . -)")
    parser.add_argument("--role", choices=ROLES, default="both")
    args = parser.parse_args(argv)
    password = getpass.getpass("Password: ")
    if len(password) < MIN_PASSWORD_CHARS:
        print(f"error: use at least {MIN_PASSWORD_CHARS} characters", file=sys.stderr)
        return 2
    if getpass.getpass("Again: ") != password:
        print("error: the passwords differ", file=sys.stderr)
        return 2
    role = "" if args.role == "both" else f"{args.role}:"
    entry = f"{args.name}:{role}{hash_password(password)}"
    try:
        parse_users(entry)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
