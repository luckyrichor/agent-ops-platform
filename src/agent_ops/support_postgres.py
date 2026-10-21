"""Transactional support adapter; no request messages or credentials in stored audit/logs."""

import hashlib
import hmac
import json
from typing import Any
from uuid import uuid4

from opentelemetry.trace import Tracer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_ops.support import SupportConflict, SupportRequest, Ticket, route_message
from agent_ops.telemetry import hop

COLUMNS = "ticket_id::text,route,state,revision,reason_code,order_status,history"


class PostgresSupportService:
    def __init__(self, url: str, tracer: Tracer, human_token: str | None = None) -> None:
        self.engine = create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
        self.tracer, self.human_token = tracer, human_token

    async def aclose(self) -> None:
        await self.engine.dispose()

    async def handle(self, request: SupportRequest, key: str) -> Ticket:
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        with hop(self.tracer, "support.route") as span:
            route = route_message(request.message)
            span.set_attribute("support.route", route)
        with hop(self.tracer, "support.persist"):
            async with self.engine.begin() as conn:
                won = (
                    await conn.execute(
                        text("""INSERT INTO support.idempotency_keys
                    (scope,key,request_hash) VALUES ('support.request',:key,:hash)
                    ON CONFLICT DO NOTHING RETURNING key"""),
                        {"key": key, "hash": digest},
                    )
                ).first()
                if won is None:
                    row = (
                        await conn.execute(
                            text("""SELECT request_hash,response_snapshot
                        FROM support.idempotency_keys WHERE scope='support.request' AND key=:key"""),
                            {"key": key},
                        )
                    ).one()
                    if row.request_hash != digest:
                        raise SupportConflict("IDEMPOTENCY_CONFLICT")
                    return Ticket.model_validate(row.response_snapshot)
                status = None
                state = "needs_information"
                reason = "CLARIFY_REQUEST"
                if route == "order_status":
                    with hop(self.tracer, "support.order_lookup"):
                        status = {"demo-100": "shipped", "demo-200": "processing"}.get(
                            request.order_id or ""
                        )
                    if status:
                        state, reason = "resolved", "ORDER_STATUS_FOUND"
                    else:
                        reason = "ORDER_ID_REQUIRED_OR_UNKNOWN"
                elif route in ("human", "return_request"):
                    state, reason = "waiting_human", "HUMAN_APPROVAL_REQUIRED"
                ticket = Ticket.model_validate(
                    {
                        "ticket_id": str(uuid4()),
                        "route": route,
                        "state": state,
                        "reason_code": reason,
                        "order_status": status,
                        "revision": 1,
                        "history": ["created", state],
                    }
                )
                values = ticket.model_dump()
                await conn.execute(
                    text("""INSERT INTO support.tickets
                    (ticket_id,route,state,revision,reason_code,order_status,history)
                    VALUES (:ticket_id,:route,:state,:revision,:reason_code,:order_status,CAST(:history AS jsonb))"""),
                    {**values, "history": json.dumps(values["history"])},
                )
                await conn.execute(
                    text("""INSERT INTO support.ticket_events
                    (ticket_id,revision,type,actor,payload) VALUES (:id,1,'created','system',CAST(:payload AS jsonb))"""),
                    {
                        "id": ticket.ticket_id,
                        "payload": json.dumps({"state": state, "reason_code": reason}),
                    },
                )
                await conn.execute(
                    text("""UPDATE support.idempotency_keys SET
                    ticket_id=:id,response_snapshot=CAST(:snapshot AS jsonb)
                    WHERE scope='support.request' AND key=:key"""),
                    {"id": ticket.ticket_id, "snapshot": ticket.model_dump_json(), "key": key},
                )
                return ticket

    async def get(self, ticket_id: str) -> Ticket:
        # Invalid external IDs remain a not-found response, not a PostgreSQL cast error.
        from uuid import UUID

        try:
            UUID(ticket_id)
        except ValueError:
            raise KeyError(ticket_id) from None
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(f"SELECT {COLUMNS} FROM support.tickets WHERE ticket_id=:id"),
                        {"id": ticket_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise KeyError(ticket_id)
            return Ticket.model_validate(dict(row))

    async def resolve(self, ticket_id: str, expected: int, token: str) -> Ticket:
        if not self.human_token or not hmac.compare_digest(
            token.encode(), self.human_token.encode()
        ):
            raise PermissionError("HUMAN_AUTH_REQUIRED")
        # Reuse UUID validation, including 404 behavior.
        await self.get(ticket_id)
        with hop(self.tracer, "support.human_resolve"):
            async with self.engine.begin() as conn:
                row = (
                    (
                        await conn.execute(
                            text(f"""UPDATE support.tickets SET state='resolved',
                    revision=revision+1,reason_code='HUMAN_RESOLVED',updated_at=now(),
                    history=history || '["human_resolved"]'::jsonb
                    WHERE ticket_id=:id AND state='waiting_human' AND revision=:expected
                    RETURNING {COLUMNS}"""),
                            {"id": ticket_id, "expected": expected},
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise SupportConflict("TICKET_STATE_CONFLICT")
                ticket = Ticket.model_validate(dict(row))
                await conn.execute(
                    text("""INSERT INTO support.ticket_events
                    (ticket_id,revision,type,actor,payload)
                    VALUES (:id,:rev,'human_resolved','shared-human-token',CAST(:payload AS jsonb))"""),
                    {
                        "id": ticket_id,
                        "rev": ticket.revision,
                        "payload": json.dumps({"state": ticket.state}),
                    },
                )
                return ticket

    async def snapshot(self) -> str:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(f"SELECT {COLUMNS} FROM support.tickets ORDER BY ticket_id")
                )
            ).mappings()
            values: dict[str, Any] = {r["ticket_id"]: dict(r) for r in rows}
            return json.dumps(values)
