"""P3 email/password login, role checks and refresh-token rotation.

Passwords are argon2id hashes only; plaintext never reaches storage, logs, spans or responses.
Access tokens are short-lived signed JWTs checked without a database hop; refresh tokens are
opaque random strings stored as SHA-256 digests and rotated on every use (reuse revokes the
whole login family). Rationale and alternatives: docs/design-decisions.md (P3).
"""

import asyncio
import hashlib
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Protocol
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

Role = Literal["customer", "agent", "admin"]
Status = Literal["active", "disabled"]
ROLES: tuple[Role, ...] = ("customer", "agent", "admin")
STAFF: tuple[Role, ...] = ("agent", "admin")
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=7)
FAMILY_TTL = timedelta(days=30)
ISSUER, AUDIENCE = "agent-ops-platform", "agent-ops-api"
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 12


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not 3 <= len(email) <= 254 or not EMAIL.match(email):
        raise ValueError("invalid email")
    return email


def refresh_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Account:
    id: int
    email: str
    password_hash: str
    role: Role
    customer_user_id: int | None
    status: Status


@dataclass(frozen=True)
class Principal:
    account_id: int
    role: Role
    customer_user_id: int | None
    session_id: str

    @property
    def actor(self) -> str:
        return f"account:{self.account_id}"


@dataclass(frozen=True)
class RefreshRecord:
    token_hash: str
    family_id: str
    account_id: int
    expires_at: datetime
    family_expires_at: datetime
    used_at: datetime | None = None
    revoked_at: datetime | None = None
    issued_at: datetime | None = None


class AuthError(Exception):
    """401 with a stable code; never says whether an email exists."""


class AccountConflict(Exception):
    pass


class AccountStore(Protocol):
    async def by_email(self, email: str) -> Account | None: ...
    async def by_id(self, account_id: int) -> Account | None: ...
    async def create(self, email: str, password_hash: str, role: Role,
                     customer_user_id: int | None) -> Account: ...
    async def set_password_hash(self, account_id: int, password_hash: str) -> None: ...
    async def accounts(self, after: int, limit: int) -> list[Account]: ...
    async def set_status(self, account_id: int, status: Status) -> Account | None: ...
    async def add_refresh(self, record: RefreshRecord) -> None: ...
    async def rotate_refresh(self, token_hash: str, new: Callable[[RefreshRecord], RefreshRecord],
                             now: datetime) -> RefreshRecord: ...
    async def revoke_family(self, family_id: str, account_id: int, now: datetime) -> None: ...
    async def revoke_account(self, account_id: int, now: datetime) -> None: ...


def rotation_outcome(old: RefreshRecord | None, account: Account | None,
                     now: datetime) -> Literal["ok", "invalid", "reuse"]:
    """Shared rule for both stores, so memory and PostgreSQL cannot drift apart."""
    if old is None or old.revoked_at is not None or account is None:
        return "invalid"
    if old.used_at is not None:
        return "reuse"
    if old.expires_at <= now or account.status != "active":
        return "invalid"
    return "ok"


class InMemoryAccountStore:
    """Baseline-switch companion of the in-memory support backend; lost on restart."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.rows: dict[int, Account] = {}
        self.tokens: dict[str, RefreshRecord] = {}

    async def by_email(self, email: str) -> Account | None:
        return next((a for a in self.rows.values() if a.email == email), None)

    async def by_id(self, account_id: int) -> Account | None:
        return self.rows.get(account_id)

    async def create(self, email: str, password_hash: str, role: Role,
                     customer_user_id: int | None) -> Account:
        async with self.lock:
            if any(a.email == email or (customer_user_id is not None
                                        and a.customer_user_id == customer_user_id)
                   for a in self.rows.values()):
                raise AccountConflict("ACCOUNT_EXISTS")
            account = Account(len(self.rows) + 1, email, password_hash, role,
                              customer_user_id, "active")
            self.rows[account.id] = account
            return account

    async def set_password_hash(self, account_id: int, password_hash: str) -> None:
        async with self.lock:
            self.rows[account_id] = replace(self.rows[account_id], password_hash=password_hash)

    async def accounts(self, after: int, limit: int) -> list[Account]:
        return [self.rows[k] for k in sorted(self.rows) if k > after][:limit]

    async def set_status(self, account_id: int, status: Status) -> Account | None:
        async with self.lock:
            if account_id not in self.rows:
                return None
            self.rows[account_id] = replace(self.rows[account_id], status=status)
            return self.rows[account_id]

    async def add_refresh(self, record: RefreshRecord) -> None:
        async with self.lock:
            self.tokens[record.token_hash] = record

    async def rotate_refresh(self, token_hash: str, new: Callable[[RefreshRecord], RefreshRecord],
                             now: datetime) -> RefreshRecord:
        async with self.lock:
            old = self.tokens.get(token_hash)
            owner = self.rows.get(old.account_id) if old else None
            outcome = rotation_outcome(old, owner, now)
            if old is None or outcome == "invalid":
                raise AuthError("REFRESH_TOKEN_INVALID")
            if outcome == "reuse":
                self._revoke(lambda r: r.family_id == old.family_id, now)
                raise AuthError("REFRESH_TOKEN_REUSED")
            self.tokens[token_hash] = replace(old, used_at=now)
            record = new(old)
            self.tokens[record.token_hash] = record
            return record

    def _revoke(self, match: Callable[[RefreshRecord], bool], now: datetime) -> None:
        for key, record in self.tokens.items():
            if match(record) and record.revoked_at is None:
                self.tokens[key] = replace(record, revoked_at=now)

    async def revoke_family(self, family_id: str, account_id: int, now: datetime) -> None:
        async with self.lock:
            self._revoke(lambda r: r.family_id == family_id and r.account_id == account_id, now)

    async def revoke_account(self, account_id: int, now: datetime) -> None:
        async with self.lock:
            self._revoke(lambda r: r.account_id == account_id, now)


class TokenCodec:
    def __init__(self, secret: str, access_ttl: timedelta = ACCESS_TTL) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("AGENT_OPS_AUTH_SECRET must be at least 32 bytes")
        self.secret, self.access_ttl = secret, access_ttl

    def issue(self, principal: Principal, now: datetime) -> str:
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": str(principal.account_id),
                  "role": principal.role, "cid": principal.customer_user_id,
                  "sid": principal.session_id, "typ": "access",
                  "iat": int(now.timestamp()), "exp": int((now + self.access_ttl).timestamp())}
        return jwt.encode(claims, self.secret, algorithm="HS256")

    def decode(self, token: str) -> Principal:
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"], audience=AUDIENCE,
                                issuer=ISSUER, leeway=5,
                                options={"require": ["exp", "iat", "sub", "iss", "aud"]})
        except jwt.ExpiredSignatureError:
            raise AuthError("ACCESS_TOKEN_EXPIRED") from None
        except jwt.PyJWTError:
            raise AuthError("ACCESS_TOKEN_INVALID") from None
        role, cid = claims.get("role"), claims.get("cid")
        if (claims.get("typ") != "access" or role not in ROLES or not isinstance(claims.get("sid"), str)
                or (role == "customer") != isinstance(cid, int)):
            raise AuthError("ACCESS_TOKEN_INVALID")
        try:
            account_id = int(claims["sub"])
        except ValueError:
            raise AuthError("ACCESS_TOKEN_INVALID") from None
        return Principal(account_id, role, cid, claims["sid"])


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    # Bounded so a huge body cannot pin argon2 CPU; validation errors never echo the value.
    password: str = Field(min_length=1, max_length=256, repr=False)


class RefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=16, max_length=256, repr=False)


class AccountView(BaseModel):
    id: int
    email: str
    role: Role
    customer_user_id: int | None
    status: Status

    @classmethod
    def of(cls, account: Account) -> "AccountView":
        return cls(id=account.id, email=account.email, role=account.role,
                   customer_user_id=account.customer_user_id, status=account.status)


class TokenPair(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    refresh_token: str
    refresh_expires_at: datetime
    account: AccountView


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Status


class AuthService:
    def __init__(self, store: AccountStore, codec: TokenCodec,
                 hasher: PasswordHasher | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 hash_slots: int = 4) -> None:
        self.store, self.codec, self.clock = store, codec, clock
        self.hasher = hasher or PasswordHasher()
        # argon2id defaults use 64 MiB per hash; bound concurrent hashing on the 7.5 GiB host.
        self.slots = asyncio.Semaphore(hash_slots)
        self._dummy: str | None = None

    async def hash_password(self, password: str) -> str:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        async with self.slots:
            return await asyncio.to_thread(self.hasher.hash, password)

    async def _verify(self, stored: str, password: str) -> bool:
        async with self.slots:
            try:
                return await asyncio.to_thread(self.hasher.verify, stored, password)
            except (VerificationError, InvalidHashError):
                return False

    async def create_account(self, email: str, password: str, role: Role,
                             customer_user_id: int | None) -> Account:
        if (role == "customer") != (customer_user_id is not None):
            raise ValueError("customer accounts need exactly one linked commerce user")
        return await self.store.create(normalize_email(email), await self.hash_password(password),
                                       role, customer_user_id)

    def _pair(self, account: Account, record: RefreshRecord, token: str,
              now: datetime) -> TokenPair:
        principal = Principal(account.id, account.role, account.customer_user_id, record.family_id)
        return TokenPair(access_token=self.codec.issue(principal, now),
                         expires_in=int(self.codec.access_ttl.total_seconds()),
                         refresh_token=token, refresh_expires_at=record.expires_at,
                         account=AccountView.of(account))

    async def login(self, credentials: Credentials) -> TokenPair:
        try:
            email = normalize_email(credentials.email)
        except ValueError:
            email = ""
        account = await self.store.by_email(email) if email else None
        if account is None:
            # Same argon2 cost for unknown emails, so timing does not reveal registered accounts.
            if self._dummy is None:
                self._dummy = await asyncio.to_thread(self.hasher.hash, secrets.token_hex(16))
            await self._verify(self._dummy, credentials.password)
            raise AuthError("INVALID_CREDENTIALS")
        if not await self._verify(account.password_hash, credentials.password) \
                or account.status != "active":
            raise AuthError("INVALID_CREDENTIALS")
        if self.hasher.check_needs_rehash(account.password_hash):
            await self.store.set_password_hash(account.id,
                                               await self.hash_password(credentials.password))
        now = self.clock()
        token = secrets.token_urlsafe(32)
        record = RefreshRecord(refresh_digest(token), str(uuid4()), account.id,
                               now + REFRESH_TTL, now + FAMILY_TTL, issued_at=now)
        await self.store.add_refresh(record)
        return self._pair(account, record, token, now)

    async def refresh(self, body: RefreshBody) -> TokenPair:
        now = self.clock()
        token = secrets.token_urlsafe(32)

        def successor(old: RefreshRecord) -> RefreshRecord:
            return RefreshRecord(refresh_digest(token), old.family_id, old.account_id,
                                 min(now + REFRESH_TTL, old.family_expires_at),
                                 old.family_expires_at, issued_at=now)

        record = await self.store.rotate_refresh(refresh_digest(body.refresh_token), successor, now)
        account = await self.store.by_id(record.account_id)
        if account is None:
            raise AuthError("REFRESH_TOKEN_INVALID")
        return self._pair(account, record, token, now)

    async def logout(self, principal: Principal) -> None:
        await self.store.revoke_family(principal.session_id, principal.account_id, self.clock())

    async def set_status(self, account_id: int, status: Status) -> Account | None:
        account = await self.store.set_status(account_id, status)
        if account is not None and status == "disabled":
            await self.store.revoke_account(account_id, self.clock())
        return account


def unauthorized(code: str) -> HTTPException:
    return HTTPException(401, code, headers={"WWW-Authenticate": "Bearer"})


def principal_dependency(codec: TokenCodec) -> Callable[..., Awaitable[Principal]]:
    async def current(authorization: Annotated[str | None, Header()] = None) -> Principal:
        if not authorization:
            raise unauthorized("AUTH_REQUIRED")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise unauthorized("AUTH_REQUIRED")
        try:
            return codec.decode(token.strip())
        except AuthError as error:
            raise unauthorized(str(error)) from None
    return current


def role_guard(current: Callable[..., Awaitable[Principal]],
               *allowed: Role) -> Callable[..., Awaitable[Principal]]:
    async def guard(principal: Annotated[Principal, Depends(current)]) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(403, "ROLE_FORBIDDEN")
        return principal
    return guard
