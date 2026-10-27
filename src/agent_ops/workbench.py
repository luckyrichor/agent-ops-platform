"""P4 transactional workflow. Product claim is a single CAS UPDATE, never SELECT then UPDATE."""

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from agent_ops.auth import Principal
from agent_ops.support import (
    SupportConflict,
    SupportRequest,
    Ticket,
    idempotency_scope,
    route_message,
)
from agent_ops.support_postgres import PostgresSupportService

FIELDS = "ticket_id::text,route,state,revision,reason_code,order_status,history,customer_user_id,order_id,assigned_account_id,lease_expires_at,priority,sla_due_at,sla_warned,sla_overdue"


class WorkflowDenied(Exception):
    pass


class WorkbenchService(PostgresSupportService):
    async def _row(self, c: AsyncConnection, ticket_id: str, *, lock: bool = False) -> Ticket:
        try:
            UUID(ticket_id)
        except ValueError:
            raise KeyError(ticket_id) from None
        row = (
            (
                await c.execute(
                    text(
                        f"SELECT {FIELDS} FROM support.tickets WHERE ticket_id=:id"
                        + (" FOR UPDATE" if lock else "")
                    ),
                    {"id": ticket_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise KeyError(ticket_id)
        return Ticket.model_validate(dict(row))

    async def get(self, ticket_id: str) -> Ticket:
        async with self.engine.connect() as c:
            return await self._row(c, ticket_id)

    @staticmethod
    def _owner(ticket: Ticket, principal: Principal) -> None:
        if principal.role == "customer" and ticket.customer_user_id != principal.customer_user_id:
            raise KeyError(ticket.ticket_id)

    async def _reserve(self, c: AsyncConnection, scope: str, key: str, data: Any) -> Any:
        digest = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
        row = (
            await c.execute(
                text("""INSERT INTO support.operations(scope,key,request_hash)
            VALUES (:s,:k,:h) ON CONFLICT DO NOTHING RETURNING key"""),
                {"s": scope, "k": key, "h": digest},
            )
        ).first()
        if row is not None:
            return None
        old = (
            await c.execute(
                text(
                    "SELECT request_hash,response FROM support.operations WHERE scope=:s AND key=:k"
                ),
                {"s": scope, "k": key},
            )
        ).one()
        if old.request_hash != digest:
            raise SupportConflict("IDEMPOTENCY_CONFLICT")
        return old.response

    async def _finish(self, c: AsyncConnection, scope: str, key: str, result: Any) -> None:
        await c.execute(
            text(
                "UPDATE support.operations SET response=CAST(:r AS jsonb) WHERE scope=:s AND key=:k"
            ),
            {"s": scope, "k": key, "r": json.dumps(jsonable_encoder(result))},
        )

    async def _push(
        self,
        c: AsyncConnection,
        t: Ticket,
        kind: str,
        *,
        staff: bool,
        customer: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        # This transactional row lock serializes sequence allocation AND commit order.
        seq: int = (
            await c.execute(
                text("UPDATE support.push_clock SET value=value+1 WHERE singleton RETURNING value")
            )
        ).scalar_one()
        payload = {
            "ticket_id": t.ticket_id,
            "state": t.state,
            "revision": t.revision,
            "assigned_account_id": t.assigned_account_id,
            "lease_expires_at": t.lease_expires_at.isoformat() if t.lease_expires_at else None,
            **(extra or {}),
        }
        await c.execute(
            text("""INSERT INTO support.push_events(sequence,ticket_id,kind,staff,customer_user_id,payload)
            VALUES (:seq,:id,:kind,:staff,:customer,CAST(:payload AS jsonb))"""),
            {
                "seq": seq,
                "id": t.ticket_id,
                "kind": kind,
                "staff": staff,
                "customer": customer,
                "payload": json.dumps(payload),
            },
        )

    async def _event(
        self,
        c: AsyncConnection,
        t: Ticket,
        kind: str,
        actor: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "state": t.state,
            "reason_code": t.reason_code,
            "order_id": t.order_id,
            "assigned_account_id": t.assigned_account_id,
            "lease_expires_at": t.lease_expires_at.isoformat() if t.lease_expires_at else None,
            **(extra or {}),
        }
        await c.execute(
            text("""INSERT INTO support.ticket_events(ticket_id,revision,type,actor,payload)
            VALUES (:id,:rev,:kind,:actor,CAST(:payload AS jsonb))"""),
            {
                "id": t.ticket_id,
                "rev": t.revision,
                "kind": kind,
                "actor": actor,
                "payload": json.dumps(payload),
            },
        )
        names = {
            "created": "ticket_created",
            "claimed": "ticket_claimed",
            "released": "ticket_released",
            "resolved": "ticket_resolved",
            "sla_warning": "ticket_sla_warning",
            "sla_overdue": "ticket_sla_overdue",
            "message_created": "message_created",
        }
        await self._push(
            c,
            t,
            names.get(kind, "ticket_updated"),
            staff=True,
            customer=t.customer_user_id if kind == "message_created" else None,
            extra=extra,
        )
        if (
            kind in {"created", "claimed", "released", "resolved", "information_supplied"}
            and t.customer_user_id is not None
        ):
            await self._push(
                c, t, "ticket_status_changed", staff=False, customer=t.customer_user_id
            )

    async def _message(
        self, c: AsyncConnection, t: Ticket, role: str, actor: str, content: str
    ) -> int:
        return int(
            (
                await c.execute(
                    text("""INSERT INTO support.messages(ticket_id,sender_role,actor,content)
            VALUES (:id,:role,:actor,:content) RETURNING id"""),
                    {"id": t.ticket_id, "role": role, "actor": actor, "content": content},
                )
            ).scalar_one()
        )

    async def _lookup(
        self, c: AsyncConnection, order_id: str | int | None, customer: int | None
    ) -> dict[str, Any] | None:
        try:
            oid = int(order_id) if order_id is not None else 0
        except ValueError:
            return None
        if not 0 < oid <= 2**63 - 1 or customer is None:
            return None
        row = (
            (
                await c.execute(
                    text("SELECT id,status FROM commerce.orders WHERE id=:id AND user_id=:user"),
                    {"id": oid, "user": customer},
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None

    async def handle(
        self, request: SupportRequest, key: str, customer_user_id: int | None = None
    ) -> Ticket:
        return await self.create(request, key, customer_user_id)

    async def create(
        self,
        request: SupportRequest,
        key: str,
        customer: int | None,
        *,
        return_request: bool = False,
    ) -> Ticket:
        route = "return_request" if return_request else route_message(request.message)
        scope = idempotency_scope(customer)
        if return_request:
            scope += ":return"
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        async with self.engine.begin() as c:
            won = (
                await c.execute(
                    text("""INSERT INTO support.idempotency_keys(scope,key,request_hash)
                VALUES (:s,:k,:h) ON CONFLICT DO NOTHING RETURNING key"""),
                    {"s": scope, "k": key, "h": digest},
                )
            ).first()
            if won is None:
                old = (
                    await c.execute(
                        text(
                            "SELECT request_hash,response_snapshot FROM support.idempotency_keys WHERE scope=:s AND key=:k"
                        ),
                        {"s": scope, "k": key},
                    )
                ).one()
                if old.request_hash != digest:
                    raise SupportConflict("IDEMPOTENCY_CONFLICT")
                return Ticket.model_validate(old.response_snapshot)
            order = await self._lookup(c, request.order_id, customer)
            state, reason = "needs_information", "CLARIFY_REQUEST"
            if route == "order_status":
                if order:
                    state, reason = "resolved", "ORDER_STATUS_FOUND"
                else:
                    reason = "ORDER_ID_REQUIRED_OR_UNKNOWN"
            elif route in {"human", "return_request"}:
                state, reason = "waiting_human", "HUMAN_APPROVAL_REQUIRED"
            priority = 2 if route == "human" else 1
            hours = {0: 48, 1: 24, 2: 1}[priority]
            row = (
                (
                    await c.execute(
                        text(f"""INSERT INTO support.tickets(ticket_id,route,state,revision,reason_code,
                history,customer_user_id,order_id,order_status,priority,sla_due_at)
                VALUES (:id,:route,:state,1,:reason,CAST(:history AS jsonb),:user,:order,:status,:priority,
                        now()+make_interval(hours=>:hours)) RETURNING {FIELDS}"""),
                        {
                            "id": str(uuid4()),
                            "route": route,
                            "state": state,
                            "reason": reason,
                            "history": json.dumps(["created", state]),
                            "user": customer,
                            "order": order["id"] if order else None,
                            "status": order["status"] if order else None,
                            "priority": priority,
                            "hours": hours,
                        },
                    )
                )
                .mappings()
                .one()
            )
            ticket = Ticket.model_validate(dict(row))
            customer_message = await self._message(
                c, ticket, "customer", f"customer:{customer}", request.message
            )
            system_message = await self._message(c, ticket, "system", "system", reason)
            await self._event(
                c, ticket, "created", "system", {"message_ids": [customer_message, system_message]}
            )
            await c.execute(
                text("""UPDATE support.idempotency_keys SET ticket_id=:id,response_snapshot=CAST(:r AS jsonb)
                WHERE scope=:s AND key=:k"""),
                {"id": ticket.ticket_id, "r": ticket.model_dump_json(), "s": scope, "k": key},
            )
            return ticket

    async def change(
        self,
        ticket_id: str,
        operation: str,
        expected: int,
        principal: Principal,
        key: str,
        *,
        message: str | None = None,
        order_id: int | None = None,
    ) -> Ticket:
        try:
            UUID(ticket_id)
        except ValueError:
            raise KeyError(ticket_id) from None
        scope = f"p4.{operation}:{principal.actor}:{ticket_id}"
        async with self.engine.begin() as c:
            prior = await self._reserve(c, scope, key, [expected, message, order_id])
            if prior is not None:
                return Ticket.model_validate(prior)
            if operation == "claim":
                # No pre-read of the ticket. Racing seats compete on this one versioned UPDATE.
                row = (
                    (
                        await c.execute(
                            text(f"""UPDATE support.tickets SET state='claimed',revision=revision+1,
                    assigned_account_id=:actor,lease_expires_at=now()+interval '10 minutes',
                    reason_code='HUMAN_CLAIMED',updated_at=now(),history=history || '["claimed"]'::jsonb
                    WHERE ticket_id=:id AND state='waiting_human' AND revision=:rev RETURNING {FIELDS}"""),
                            {"id": ticket_id, "rev": expected, "actor": principal.account_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    await self._row(c, ticket_id)
                    raise SupportConflict("TICKET_STATE_CONFLICT")
                ticket = Ticket.model_validate(dict(row))
                kind = "claimed"
                extra: dict[str, Any] = {}
            else:
                ticket = await self._row(c, ticket_id, lock=True)
                self._owner(ticket, principal)
                if ticket.revision != expected or ticket.state == "resolved":
                    raise SupportConflict("TICKET_STATE_CONFLICT")
                now: datetime = (await c.execute(text("SELECT now()"))).scalar_one()
                if principal.role != "customer":
                    if (
                        ticket.state != "claimed"
                        or ticket.assigned_account_id != principal.account_id
                    ):
                        raise WorkflowDenied("CLAIM_REQUIRED")
                    if ticket.lease_expires_at is None or ticket.lease_expires_at <= now:
                        raise SupportConflict("CLAIM_LEASE_EXPIRED")
                kind, extra = "message_created", {}
                if operation == "information":
                    if ticket.state not in {"needs_information", "waiting_human", "claimed"}:
                        raise SupportConflict("TICKET_STATE_CONFLICT")
                    if ticket.order_id is not None:
                        raise SupportConflict("ORDER_ALREADY_LINKED")
                    order = await self._lookup(c, order_id, principal.customer_user_id)
                    if order is None:
                        raise KeyError("ORDER_NOT_FOUND")
                    ticket.order_id, ticket.order_status = order["id"], order["status"]
                    if ticket.state == "needs_information":
                        ticket.state = (
                            "resolved" if ticket.route == "order_status" else "waiting_human"
                        )
                        ticket.reason_code = (
                            "ORDER_STATUS_FOUND"
                            if ticket.state == "resolved"
                            else "HUMAN_APPROVAL_REQUIRED"
                        )
                    # Metadata supplementation while queued/claimed does not change state or ownership.
                    kind = "resolved" if ticket.state == "resolved" else "information_supplied"
                    await self._message(
                        c, ticket, "customer", principal.actor, message or "补充订单信息"
                    )
                    await self._message(c, ticket, "system", "system", "ORDER_INFORMATION_SUPPLIED")
                elif operation in {"resolve", "release"}:
                    ticket.state = "resolved" if operation == "resolve" else "waiting_human"
                    ticket.reason_code = (
                        "HUMAN_RESOLVED" if operation == "resolve" else "HUMAN_RELEASED"
                    )
                    ticket.assigned_account_id, ticket.lease_expires_at = None, None
                    kind = "resolved" if operation == "resolve" else "released"
                    await self._message(c, ticket, "system", "system", ticket.reason_code)
                elif operation in {"reply", "message"}:
                    if not message:
                        raise SupportConflict("MESSAGE_REQUIRED")
                    role = "customer" if principal.role == "customer" else "agent"
                    mid = await self._message(c, ticket, role, principal.actor, message)
                    extra = {"message_id": mid, "sender_role": role}
                else:
                    raise SupportConflict("UNKNOWN_OPERATION")
                ticket.revision += 1
                ticket.history.append(kind)
                if ticket.state == "claimed" and principal.role != "customer":
                    ticket.lease_expires_at = (
                        await c.execute(text("SELECT now()+interval '10 minutes'"))
                    ).scalar_one()
                await c.execute(
                    text("""UPDATE support.tickets SET state=:state,revision=:revision,
                    order_id=:order_id,order_status=:order_status,assigned_account_id=:assigned_account_id,
                    lease_expires_at=:lease_expires_at,reason_code=:reason_code,history=CAST(:h AS jsonb),updated_at=now()
                    WHERE ticket_id=:ticket_id"""),
                    {**ticket.model_dump(), "h": json.dumps(ticket.history)},
                )
            await self._event(c, ticket, kind, principal.actor, extra)
            await self._finish(c, scope, key, ticket.model_dump(mode="json"))
            return ticket

    async def customer_page(self, customer: int, limit: int, offset: int) -> list[Ticket]:
        async with self.engine.connect() as c:
            rows = (
                await c.execute(
                    text(f"""SELECT {FIELDS} FROM support.tickets WHERE customer_user_id=:user
                ORDER BY created_at DESC,ticket_id DESC LIMIT :limit OFFSET :offset"""),
                    {"user": customer, "limit": limit, "offset": offset},
                )
            ).mappings()
            return [Ticket.model_validate(dict(r)) for r in rows]

    async def queue(self, limit: int, offset: int) -> list[Ticket]:
        async with self.engine.connect() as c:
            rows = (
                await c.execute(
                    text(f"""SELECT {FIELDS} FROM support.tickets WHERE state='waiting_human'
                ORDER BY priority DESC,created_at,ticket_id LIMIT :limit OFFSET :offset"""),
                    {"limit": limit, "offset": offset},
                )
            ).mappings()
            return [Ticket.model_validate(dict(r)) for r in rows]

    async def messages(
        self, ticket_id: str, principal: Principal, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        async with self.engine.connect() as c:
            ticket = await self._row(c, ticket_id)
            self._owner(ticket, principal)
            rows = (
                await c.execute(
                    text("""SELECT * FROM (
                SELECT 'live' AS origin,id,NULL::bigint AS conversation_id,sender_role,actor,content,created_at
                  FROM support.messages WHERE ticket_id=:id
                UNION ALL
                SELECT 'seed',m.seq,m.conversation_id,m.sender,'seed',m.content,m.created_at
                  FROM commerce.conversations v JOIN commerce.messages m ON m.conversation_id=v.id
                  WHERE v.ticket_id=:id
                ) messages ORDER BY created_at,origin,conversation_id,id LIMIT :limit OFFSET :offset"""),
                    {"id": ticket_id, "limit": limit, "offset": offset},
                )
            ).mappings()
            return [dict(r) for r in rows]

    async def audit(self, ticket_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        async with self.engine.connect() as c:
            await self._row(c, ticket_id)
            return [
                dict(r)
                for r in (
                    await c.execute(
                        text("""SELECT revision,type,actor,payload,created_at
                FROM support.ticket_events WHERE ticket_id=:id ORDER BY revision LIMIT :n OFFSET :off"""),
                        {"id": ticket_id, "n": limit, "off": offset},
                    )
                ).mappings()
            ]

    async def view_order(self, ticket_id: str, principal: Principal) -> dict[str, Any]:
        async with self.engine.begin() as c:
            ticket = await self._row(c, ticket_id, lock=True)
            row = (
                (
                    await c.execute(
                        text("SELECT * FROM commerce.orders WHERE id=:id AND user_id=:user"),
                        {"id": ticket.order_id, "user": ticket.customer_user_id},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise KeyError("ORDER_NOT_FOUND")
            order = dict(row)
            order["items"] = [
                dict(r)
                for r in (
                    await c.execute(
                        text(
                            "SELECT * FROM commerce.order_items WHERE order_id=:id ORDER BY line_no"
                        ),
                        {"id": ticket.order_id},
                    )
                ).mappings()
            ]
            shipment = (
                (
                    await c.execute(
                        text("SELECT * FROM commerce.shipments WHERE order_id=:id"),
                        {"id": ticket.order_id},
                    )
                )
                .mappings()
                .first()
            )
            order["shipment"] = dict(shipment) if shipment else None
            ticket = Ticket.model_validate(
                dict(
                    (
                        await c.execute(
                            text(f"""UPDATE support.tickets SET revision=revision+1,
                updated_at=now(),lease_expires_at=CASE WHEN assigned_account_id=:actor AND lease_expires_at>now()
                 THEN now()+interval '10 minutes' ELSE lease_expires_at END WHERE ticket_id=:id RETURNING {FIELDS}"""),
                            {"id": ticket_id, "actor": principal.account_id},
                        )
                    )
                    .mappings()
                    .one()
                )
            )
            await self._event(
                c, ticket, "order_viewed", principal.actor, {"order_id": ticket.order_id}
            )
            return {"ticket_revision": ticket.revision, "order": order}

    async def inspect(self, ticket_id: str, principal: Principal) -> Ticket:
        async with self.engine.begin() as c:
            ticket = await self._row(c, ticket_id, lock=True)
            now: datetime = (await c.execute(text("SELECT now()"))).scalar_one()
            if (
                ticket.state == "claimed"
                and ticket.assigned_account_id == principal.account_id
                and ticket.lease_expires_at
                and ticket.lease_expires_at > now
            ):
                row = (
                    (
                        await c.execute(
                            text(f"""UPDATE support.tickets SET revision=revision+1,
                    lease_expires_at=now()+interval '10 minutes',updated_at=now() WHERE ticket_id=:id RETURNING {FIELDS}"""),
                            {"id": ticket_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                ticket = Ticket.model_validate(dict(row))
                await self._event(c, ticket, "lease_renewed", principal.actor)
            return ticket

    async def stats(self) -> dict[str, Any]:
        async with self.engine.connect() as c:
            summary = dict(
                (
                    await c.execute(
                        text("""SELECT count(*) FILTER(WHERE state='waiting_human') AS queue_length,
                coalesce(avg(extract(epoch FROM now()-created_at)) FILTER(WHERE state='waiting_human'),0)::float AS average_wait_seconds,
                count(*) FILTER(WHERE sla_overdue) AS sla_overdue FROM support.tickets""")
                    )
                )
                .mappings()
                .one()
            )
            summary["agents"] = [
                dict(r)
                for r in (
                    await c.execute(
                        text("""SELECT actor,count(*) AS resolved
                FROM support.ticket_events WHERE type='resolved' AND actor LIKE 'account:%' GROUP BY actor ORDER BY actor""")
                    )
                ).mappings()
            ]
            return summary

    async def maintain(self) -> None:
        async with self.engine.connect() as c:
            ids: list[str] = list(
                (
                    await c.execute(
                        text("""SELECT ticket_id::text FROM support.tickets WHERE
                (state='claimed' AND lease_expires_at<=now()) OR (state<>'resolved' AND
                 ((NOT sla_warned AND sla_due_at<=now()+interval '5 minutes') OR (NOT sla_overdue AND sla_due_at<=now())))
                ORDER BY ticket_id LIMIT 100""")
                    )
                ).scalars()
            )
        for tid in ids:
            for operation, condition, assignment in [
                (
                    "released",
                    "state='claimed' AND lease_expires_at<=now()",
                    "state='waiting_human',assigned_account_id=NULL,lease_expires_at=NULL,reason_code='CLAIM_LEASE_EXPIRED'",
                ),
                (
                    "sla_warning",
                    "state<>'resolved' AND NOT sla_warned AND sla_due_at<=now()+interval '5 minutes'",
                    "sla_warned=true",
                ),
                (
                    "sla_overdue",
                    "state<>'resolved' AND NOT sla_overdue AND sla_due_at<=now()",
                    "sla_overdue=true",
                ),
            ]:
                async with self.engine.begin() as c:
                    row = (
                        (
                            await c.execute(
                                text(f"""UPDATE support.tickets SET {assignment},revision=revision+1,updated_at=now(),history=history || jsonb_build_array(CAST(:kind AS text))
                        WHERE ticket_id=:id AND {condition} RETURNING {FIELDS}"""),
                                {"id": tid, "kind": operation},
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if row:
                        ticket = Ticket.model_validate(dict(row))
                        await self._event(c, ticket, operation, "system")

    async def events(self, principal: Principal, after: int) -> list[dict[str, Any]]:
        async with self.engine.connect() as c:
            rows = (
                await c.execute(
                    text("""SELECT sequence,kind,payload,created_at FROM support.push_events
                WHERE sequence>:after AND ((:staff AND staff) OR customer_user_id=:user)
                ORDER BY sequence LIMIT 100"""),
                    {
                        "after": after,
                        "staff": principal.role != "customer",
                        "user": principal.customer_user_id,
                    },
                )
            ).mappings()
            return [dict(r) for r in rows]

    async def last_sequence(self) -> int:
        async with self.engine.connect() as c:
            return int(
                (
                    await c.execute(text("SELECT value FROM support.push_clock WHERE singleton"))
                ).scalar_one()
            )
