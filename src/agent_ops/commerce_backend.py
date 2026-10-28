"""P2 commerce service; P3 exposes it over HTTP with the caller's user_id taken from the token.

Integer cents, atomic stock reservations, request idempotency and revision checks.
Payment capture and customer-support workflow are deliberately separate later stages.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


class OrderLine(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=10000)


class PlaceOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    user_id: int = Field(gt=0)
    lines: list[OrderLine] = Field(min_length=1, max_length=100)


class CommerceConflict(RuntimeError):
    pass


class CommerceBackend:
    def __init__(self, url: str) -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)

    async def aclose(self) -> None:
        await self.engine.dispose()

    async def products(
        self, *, after: int = 0, limit: int = 50, category_id: int | None = None
    ) -> list[dict[str, Any]]:
        if after < 0 or not 1 <= limit <= 100:
            raise ValueError("invalid cursor or page size")
        async with self.engine.connect() as c:
            rows = await c.execute(
                text("""SELECT p.*,i.stock-i.reserved AS available,i.version AS inventory_version
                FROM commerce.products p JOIN commerce.inventory i ON i.product_id=p.id
                WHERE p.id>:after AND (:category=0 OR p.category_id=:category) AND p.status='active'
                ORDER BY p.id LIMIT :limit"""),
                {"after": after, "category": category_id or 0, "limit": limit},
            )
            return [dict(r) for r in rows.mappings()]

    async def product(self, product_id: int) -> dict[str, Any]:
        async with self.engine.connect() as c:
            row = (await c.execute(text("""SELECT p.*,i.stock-i.reserved AS available
                FROM commerce.products p JOIN commerce.inventory i ON i.product_id=p.id
                WHERE p.id=:id AND p.status='active'"""), {"id": product_id})).mappings().first()
            if row is None:
                raise KeyError("PRODUCT_NOT_FOUND")
            return dict(row)

    async def reserve(self, product_id: int, quantity: int, expected_version: int) -> int:
        if min(product_id, quantity, expected_version) <= 0:
            raise ValueError("positive values required")
        async with self.engine.begin() as c:
            version = await c.scalar(
                text("""UPDATE commerce.inventory
                SET reserved=reserved+:quantity,version=version+1
                WHERE product_id=:id AND version=:expected AND stock-reserved>=:quantity
                RETURNING version"""),
                {"quantity": quantity, "id": product_id, "expected": expected_version},
            )
            if version is None:
                raise CommerceConflict("INVENTORY_CONFLICT")
            return int(version)

    async def place_order(self, request: PlaceOrder, key: str) -> dict[str, Any]:
        if not 1 <= len(key) <= 128:
            raise ValueError("valid idempotency key required")
        if len({line.product_id for line in request.lines}) != len(request.lines):
            raise ValueError("duplicate products; combine quantities before submitting")
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        async with self.engine.begin() as c:
            won = await c.scalar(
                text("""INSERT INTO commerce.idempotency_keys(scope,key,request_hash)
                VALUES ('place_order',:key,:hash) ON CONFLICT DO NOTHING RETURNING key"""),
                {"key": key, "hash": digest},
            )
            if won is None:
                row = (
                    await c.execute(
                        text(
                            "SELECT request_hash,response_snapshot FROM commerce.idempotency_keys WHERE scope='place_order' AND key=:key"
                        ),
                        {"key": key},
                    )
                ).one()
                if row.request_hash != digest:
                    raise CommerceConflict("IDEMPOTENCY_CONFLICT")
                return dict(row.response_snapshot)
            if not await c.scalar(
                text("SELECT 1 FROM commerce.users WHERE id=:id"), {"id": request.user_id}
            ):
                raise KeyError("USER_NOT_FOUND")
            total = 0
            lines = []
            # Stable lock order prevents two multi-item orders from deadlocking on opposite baskets.
            for line in sorted(request.lines, key=lambda row: row.product_id):
                reserved_row = (
                    await c.execute(
                        text("""UPDATE commerce.inventory i SET reserved=reserved+:qty,version=version+1
                    FROM commerce.products p WHERE i.product_id=:id AND p.id=i.product_id
                    AND p.status='active' AND i.stock-i.reserved>=:qty
                    RETURNING p.price_cents,p.freight_cents"""),
                        {"id": line.product_id, "qty": line.quantity},
                    )
                ).first()
                if reserved_row is None:
                    raise CommerceConflict("INSUFFICIENT_STOCK_OR_PRODUCT_UNAVAILABLE")
                total += (reserved_row.price_cents + reserved_row.freight_cents) * line.quantity
                lines.append((line, reserved_row.price_cents, reserved_row.freight_cents))
            oid = await c.scalar(
                text("""INSERT INTO commerce.orders(user_id,status,total_cents,created_at)
                VALUES (:user,'pending_payment',:total,:now) RETURNING id"""),
                {"user": request.user_id, "total": total, "now": datetime.now(UTC)},
            )
            for n, (line, price, freight) in enumerate(lines, 1):
                await c.execute(
                    text("""INSERT INTO commerce.order_items
                    (order_id,line_no,product_id,quantity,unit_price_cents,freight_cents)
                    VALUES (:order,:n,:product,:qty,:price,:freight)"""),
                    {
                        "order": oid,
                        "n": n,
                        "product": line.product_id,
                        "qty": line.quantity,
                        "price": price,
                        "freight": freight,
                    },
                )
            result = {
                "order_id": int(oid),
                "user_id": request.user_id,
                "status": "pending_payment",
                "total_cents": total,
                "revision": 1,
            }
            await c.execute(
                text(
                    "UPDATE commerce.idempotency_keys SET response_snapshot=CAST(:result AS jsonb) WHERE scope='place_order' AND key=:key"
                ),
                {"result": json.dumps(result), "key": key},
            )
            return result

    async def cancel_order(
        self, order_id: int, user_id: int, expected_revision: int
    ) -> dict[str, Any]:
        async with self.engine.begin() as c:
            # Serialize with link creation and capture; a submitted payment must be
            # closed at the provider before its stock reservation can be released.
            owned = await c.scalar(text("SELECT id FROM commerce.orders WHERE id=:id AND user_id=:user FOR UPDATE"),
                                   {"id": order_id, "user": user_id})
            if owned is not None and await c.scalar(text("SELECT EXISTS(SELECT 1 FROM payments.payments WHERE order_id=:id AND state='pending')"), {"id": order_id}):
                raise CommerceConflict("PAYMENT_ALREADY_STARTED")
            row = (
                await c.execute(
                    text("""UPDATE commerce.orders SET status='canceled',revision=revision+1
                WHERE id=:id AND user_id=:user AND status='pending_payment' AND revision=:revision
                RETURNING id,revision"""),
                    {"id": order_id, "user": user_id, "revision": expected_revision},
                )
            ).first()
            if row is None:
                raise CommerceConflict("ORDER_STATE_CONFLICT")
            for line in (
                await c.execute(
                    text(
                        "SELECT product_id,quantity FROM commerce.order_items WHERE order_id=:id ORDER BY product_id"
                    ),
                    {"id": order_id},
                )
            ).all():
                await c.execute(
                    text(
                        "UPDATE commerce.inventory SET reserved=reserved-:qty,version=version+1 WHERE product_id=:id"
                    ),
                    {"qty": line.quantity, "id": line.product_id},
                )
            return {"order_id": order_id, "status": "canceled", "revision": int(row.revision)}

    async def order(self, order_id: int, user_id: int) -> dict[str, Any]:
        async with self.engine.connect() as c:
            row = (
                (
                    await c.execute(
                        text("SELECT * FROM commerce.orders WHERE id=:id AND user_id=:user"),
                        {"id": order_id, "user": user_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise KeyError("ORDER_NOT_FOUND")
            result = dict(row)
            result["items"] = [
                dict(r)
                for r in (
                    await c.execute(
                        text(
                            "SELECT * FROM commerce.order_items WHERE order_id=:id ORDER BY line_no"
                        ),
                        {"id": order_id},
                    )
                ).mappings()
            ]
            shipping = (
                (
                    await c.execute(
                        text("SELECT * FROM commerce.shipments WHERE order_id=:id"),
                        {"id": order_id},
                    )
                )
                .mappings()
                .first()
            )
            result["shipment"] = dict(shipping) if shipping else None
            return result

    async def orders(self, user_id: int, *, after: int | None, limit: int) -> list[dict[str, Any]]:
        """Newest first; cursor is the last order id seen (only meaningful for the same user)."""
        async with self.engine.connect() as c:
            rows = await c.execute(
                text("""SELECT id AS order_id,status,total_cents,created_at,revision
                FROM commerce.orders WHERE user_id=:user AND (CAST(:after AS bigint) IS NULL OR
                  (created_at,id) < (SELECT created_at,id FROM commerce.orders
                   WHERE id=CAST(:after AS bigint) AND user_id=:user))
                ORDER BY created_at DESC,id DESC LIMIT :limit"""),
                {"user": user_id, "after": after, "limit": limit},
            )
            return [dict(r) for r in rows.mappings()]

    async def conversations(
        self, user_id: int, *, after: int | None, limit: int
    ) -> list[dict[str, Any]]:
        async with self.engine.connect() as c:
            rows = await c.execute(
                text("""SELECT id AS conversation_id,ticket_id::text FROM commerce.conversations
                WHERE user_id=:user AND id>:after ORDER BY id LIMIT :limit"""),
                {"user": user_id, "after": after or 0, "limit": limit},
            )
            return [dict(r) for r in rows.mappings()]

    async def messages(
        self, conversation_id: int, user_id: int, *, after: int, limit: int
    ) -> list[dict[str, Any]]:
        """Ownership is checked in the same query path; another user's id is simply not found."""
        async with self.engine.connect() as c:
            if not await c.scalar(
                text("SELECT 1 FROM commerce.conversations WHERE id=:id AND user_id=:user"),
                {"id": conversation_id, "user": user_id},
            ):
                raise KeyError("CONVERSATION_NOT_FOUND")
            rows = await c.execute(
                text("""SELECT m.seq,m.sender,m.content,m.created_at FROM commerce.message_keys k
                JOIN commerce.messages m ON m.conversation_id=k.conversation_id AND m.seq=k.seq
                  AND m.created_at=k.created_at
                WHERE k.conversation_id=:id AND k.seq>:after ORDER BY k.seq LIMIT :limit"""),
                {"id": conversation_id, "after": after, "limit": limit},
            )
            return [dict(r) for r in rows.mappings()]
