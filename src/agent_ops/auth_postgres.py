"""PostgreSQL account store: unique email, hashed refresh tokens, row-locked rotation."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agent_ops.auth import (
    Account,
    AccountConflict,
    AuthError,
    RefreshRecord,
    Role,
    Status,
    rotation_outcome,
)

ACCOUNT = "id,email,password_hash,role,customer_user_id,status"
TOKEN = "token_hash,family_id::text,account_id,expires_at,family_expires_at,used_at,revoked_at"


def account(row: Any) -> Account:
    return Account(int(row.id), row.email, row.password_hash, row.role,
                   row.customer_user_id, row.status)


def record(row: Any) -> RefreshRecord:
    return RefreshRecord(row.token_hash, row.family_id, int(row.account_id), row.expires_at,
                         row.family_expires_at, row.used_at, row.revoked_at)


class PostgresAccountStore:
    def __init__(self, url: str | AsyncEngine) -> None:
        self.owned = isinstance(url, str)
        self.engine = (create_async_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=3)
                       if isinstance(url, str) else url)

    async def aclose(self) -> None:
        if self.owned:
            await self.engine.dispose()

    async def _one(self, sql: str, **params: Any) -> Account | None:
        async with self.engine.connect() as c:
            row = (await c.execute(text(sql), params)).first()
            return account(row) if row else None

    async def by_email(self, email: str) -> Account | None:
        return await self._one(f"SELECT {ACCOUNT} FROM auth.accounts WHERE email=:e", e=email)

    async def by_id(self, account_id: int) -> Account | None:
        return await self._one(f"SELECT {ACCOUNT} FROM auth.accounts WHERE id=:i", i=account_id)

    async def create(self, email: str, password_hash: str, role: Role,
                     customer_user_id: int | None) -> Account:
        try:
            async with self.engine.begin() as c:
                row = (await c.execute(text(f"""INSERT INTO auth.accounts
                    (email,password_hash,role,customer_user_id) VALUES (:e,:h,:r,:c)
                    RETURNING {ACCOUNT}"""),
                    {"e": email, "h": password_hash, "r": role, "c": customer_user_id})).one()
                return account(row)
        except IntegrityError as error:
            # Unique email/customer link or a missing commerce user; no row is created.
            raise AccountConflict("ACCOUNT_EXISTS_OR_USER_UNKNOWN") from error

    async def set_password_hash(self, account_id: int, password_hash: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(text("""UPDATE auth.accounts SET password_hash=:h,updated_at=now()
                WHERE id=:i"""), {"h": password_hash, "i": account_id})

    async def accounts(self, after: int, limit: int) -> list[Account]:
        async with self.engine.connect() as c:
            rows = await c.execute(text(f"""SELECT {ACCOUNT} FROM auth.accounts WHERE id>:a
                ORDER BY id LIMIT :n"""), {"a": after, "n": limit})
            return [account(r) for r in rows]

    async def set_status(self, account_id: int, status: Status) -> Account | None:
        async with self.engine.begin() as c:
            row = (await c.execute(text(f"""UPDATE auth.accounts SET status=:s,updated_at=now()
                WHERE id=:i RETURNING {ACCOUNT}"""), {"s": status, "i": account_id})).first()
            return account(row) if row else None

    async def add_refresh(self, item: RefreshRecord) -> None:
        async with self.engine.begin() as c:
            await c.execute(text("""INSERT INTO auth.refresh_tokens
                (token_hash,family_id,account_id,issued_at,expires_at,family_expires_at)
                VALUES (:h,CAST(:f AS uuid),:a,:i,:e,:fe)"""),
                {"h": item.token_hash, "f": item.family_id, "a": item.account_id,
                 "i": item.issued_at, "e": item.expires_at, "fe": item.family_expires_at})

    async def rotate_refresh(self, token_hash: str, new: Callable[[RefreshRecord], RefreshRecord],
                             now: datetime) -> RefreshRecord:
        async with self.engine.begin() as c:
            # Row lock serializes concurrent use of one token: exactly one rotation wins.
            row = (await c.execute(text(f"""SELECT {TOKEN} FROM auth.refresh_tokens
                WHERE token_hash=:h FOR UPDATE"""), {"h": token_hash})).first()
            old = record(row) if row else None
            owner = None
            if old is not None:
                found = (await c.execute(text(f"SELECT {ACCOUNT} FROM auth.accounts WHERE id=:i"),
                                         {"i": old.account_id})).first()
                owner = account(found) if found else None
            outcome = rotation_outcome(old, owner, now)
            if old is not None and outcome == "reuse":
                await c.execute(text("""UPDATE auth.refresh_tokens SET revoked_at=:now
                    WHERE family_id=CAST(:f AS uuid) AND revoked_at IS NULL"""),
                    {"now": now, "f": old.family_id})
            elif old is not None and outcome == "ok":
                await c.execute(text("""UPDATE auth.refresh_tokens SET used_at=:now
                    WHERE token_hash=:h"""), {"now": now, "h": token_hash})
                item = new(old)
                await c.execute(text("""INSERT INTO auth.refresh_tokens
                    (token_hash,family_id,account_id,issued_at,expires_at,family_expires_at)
                    VALUES (:h,CAST(:f AS uuid),:a,:now,:e,:fe)"""),
                    {"h": item.token_hash, "f": item.family_id, "a": item.account_id,
                     "now": item.issued_at, "e": item.expires_at, "fe": item.family_expires_at})
                return item
        # Raised after commit so a detected reuse keeps the family revoked.
        raise AuthError("REFRESH_TOKEN_REUSED" if outcome == "reuse" else "REFRESH_TOKEN_INVALID")

    async def revoke_family(self, family_id: str, account_id: int, now: datetime) -> None:
        async with self.engine.begin() as c:
            await c.execute(text("""UPDATE auth.refresh_tokens SET revoked_at=:now
                WHERE family_id=CAST(:f AS uuid) AND account_id=:a AND revoked_at IS NULL"""),
                {"now": now, "f": family_id, "a": account_id})

    async def revoke_account(self, account_id: int, now: datetime) -> None:
        async with self.engine.begin() as c:
            await c.execute(text("""UPDATE auth.refresh_tokens SET revoked_at=:now
                WHERE account_id=:a AND revoked_at IS NULL"""), {"now": now, "a": account_id})
