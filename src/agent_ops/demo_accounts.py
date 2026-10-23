"""Create a handful of demo logins; the 1M seeded commerce.users stay credential-less.

Usage (on the server, never commit the roster):
    uv run python -m agent_ops.demo_accounts --init          # write .local/demo-accounts.json
    uv run python -m agent_ops.demo_accounts                 # create the accounts in PostgreSQL

Roster source: AGENT_OPS_DEMO_ACCOUNTS (JSON text) or --roster (default .local/demo-accounts.json).
Database: AGENT_OPS_DATABASE_URL or --env-file (default .local/application.env).
Output lists email/role/linked user id/action only; passwords and hashes are never printed.
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text

from agent_ops.auth import (
    MIN_PASSWORD,
    AccountConflict,
    AuthService,
    Role,
    TokenCodec,
    normalize_email,
)
from agent_ops.auth_postgres import PostgresAccountStore

DEFAULT_ROSTER = [("customer", 3), ("agent", 2), ("admin", 1)]


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    password: str = Field(min_length=MIN_PASSWORD, max_length=256, repr=False)
    role: Role
    customer_user_id: int | None = Field(default=None, gt=0)


class Roster(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accounts: list[Entry] = Field(min_length=1, max_length=20)


def init_roster(path: Path) -> None:
    if path.exists():
        raise SystemExit(f"{path} already exists; not overwriting")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    accounts = [{"email": f"{role}{n}@demo.invalid", "role": role,
                 "password": secrets.token_urlsafe(18)}
                for role, count in DEFAULT_ROSTER for n in range(1, count + 1)]
    # Create 0600 before writing so the secrets are never world-readable, even briefly.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"accounts": accounts}, stream, indent=2)
    print(json.dumps({"roster": str(path), "accounts": len(accounts),
                      "note": "passwords written only to this file"}))


def load_roster(path: Path) -> Roster:
    raw = os.environ.get("AGENT_OPS_DEMO_ACCOUNTS")
    if raw is None:
        if not path.exists():
            raise SystemExit(f"no roster: set AGENT_OPS_DEMO_ACCOUNTS or create {path} (--init)")
        raw = path.read_text()
    try:
        return Roster.model_validate_json(raw)
    except ValidationError as error:
        # Report field locations only; pydantic's default message would echo the password.
        fields = sorted({".".join(map(str, e["loc"])) for e in error.errors()})
        raise SystemExit("invalid roster fields: " + ", ".join(fields)) from None


def database_url(env_file: Path) -> str:
    if url := os.environ.get("AGENT_OPS_DATABASE_URL"):
        return url
    if env_file.exists():
        values = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
        if url := values.get("AGENT_OPS_DATABASE_URL"):
            return url
    raise SystemExit("AGENT_OPS_DATABASE_URL not set and not found in env file")


async def free_customers(store: PostgresAccountStore, count: int) -> list[int]:
    """Lowest-id seeded users that have orders and no login yet (deterministic, few rows)."""
    async with store.engine.connect() as c:
        rows = await c.execute(text("""SELECT u.id FROM commerce.users u
            WHERE EXISTS(SELECT 1 FROM commerce.orders o WHERE o.user_id=u.id)
              AND NOT EXISTS(SELECT 1 FROM auth.accounts a WHERE a.customer_user_id=u.id)
            ORDER BY u.id LIMIT :n"""), {"n": count})
        return [int(r.id) for r in rows]


async def provision(url: str, roster: Roster, reset_passwords: bool) -> list[dict[str, Any]]:
    store = PostgresAccountStore(url)
    # Only hashing is used here; the signing key is irrelevant and never persisted.
    auth = AuthService(store, TokenCodec(secrets.token_urlsafe(48)))
    report: list[dict[str, Any]] = []
    if any(e.role != "customer" and e.customer_user_id is not None for e in roster.accounts):
        raise SystemExit("customer_user_id is only valid for customer accounts")
    try:
        wanted = [e for e in roster.accounts if e.role == "customer" and e.customer_user_id is None
                  and await store.by_email(normalize_email(e.email)) is None]
        pool = iter(await free_customers(store, len(wanted)))
        for entry in roster.accounts:
            email = normalize_email(entry.email)
            existing = await store.by_email(email)
            action: Literal["created", "exists", "password_reset"]
            if existing is not None:
                action = "exists"
                if reset_passwords:
                    await store.set_password_hash(existing.id,
                                                  await auth.hash_password(entry.password))
                    await store.revoke_account(existing.id, datetime.now(UTC))
                    action = "password_reset"
                account = existing
            else:
                cid = entry.customer_user_id
                if entry.role == "customer" and cid is None:
                    cid = next(pool, None)
                    if cid is None:
                        raise SystemExit("not enough seeded users with orders to link customers")
                try:
                    account = await auth.create_account(email, entry.password, entry.role, cid)
                except AccountConflict:
                    raise SystemExit(f"cannot create {email}: email/user already linked "
                                     "or commerce user missing") from None
                action = "created"
            report.append({"email": account.email, "role": account.role,
                           "customer_user_id": account.customer_user_id, "action": action})
    finally:
        await store.aclose()
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--roster", type=Path, default=Path(".local/demo-accounts.json"))
    parser.add_argument("--env-file", type=Path, default=Path(".local/application.env"))
    parser.add_argument("--init", action="store_true", help="write a random-password roster file")
    parser.add_argument("--reset-passwords", action="store_true",
                        help="re-hash passwords of existing roster accounts and end their sessions")
    args = parser.parse_args(argv)
    if args.init:
        init_roster(args.roster)
        return
    report = asyncio.run(provision(database_url(args.env_file), load_roster(args.roster),
                                   args.reset_passwords))
    for row in report:
        print(json.dumps(row), file=sys.stdout)


if __name__ == "__main__":
    main()
