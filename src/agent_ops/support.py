"""Local customer-service routing, read-only order tools and controlled human takeover."""
import asyncio
import hashlib
import json
import re
from datetime import datetime
from typing import Literal
from uuid import uuid4

from opentelemetry.trace import Tracer
from pydantic import BaseModel, ConfigDict, Field

from agent_ops.telemetry import hop

Route = Literal["order_status", "return_request", "human", "clarify"]


class SupportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2000)
    order_id: str | None = Field(default=None, min_length=1, max_length=100)


class Ticket(BaseModel):
    ticket_id: str
    route: Route
    state: Literal["resolved", "waiting_human", "needs_information", "claimed"]
    revision: int = 1
    reason_code: str
    order_status: str | None = None
    history: list[str]
    # Owning customer (commerce.users id); None for pre-P3/synthetic tickets, staff-only.
    customer_user_id: int | None = None
    order_id: int | None = None
    assigned_account_id: int | None = None
    lease_expires_at: datetime | None = None
    priority: int = 1
    sla_due_at: datetime | None = None
    sla_warned: bool = False
    sla_overdue: bool = False


def route_message(message: str) -> Route:
    text = message.lower()
    if any(x in text for x in ("人工", "客服人员", "投诉", "盗刷", "隐私")) or bool(
        re.search(r"\b(human|fraud|privacy)\b", text)):
        return "human"
    # Denial/ambiguous instructions are not treated as positive requests.
    if any(x in text for x in ("不要", "不想", "don't", "do not", "ignore previous", "忽略指令")):
        return "clarify"
    delivery = any(x in text for x in ("物流", "快递", "发货", "到货", "送达", "配送")) or bool(
        re.search(r"\b(shipping|delivery|tracking|shipped)\b", text))
    returns = any(x in text for x in ("退款", "退货", "换货", "退钱")) or bool(
        re.search(r"\b(refund|return|exchange)\b", text))
    if delivery and returns:
        return "clarify"
    if delivery:
        return "order_status"
    if returns:
        return "return_request"
    return "clarify"


class SupportConflict(Exception):
    pass


class SupportCapacity(Exception):
    pass


def idempotency_scope(customer_user_id: int | None) -> str:
    """Keys are per customer: another customer's same key must never replay this ticket."""
    return "support.request" if customer_user_id is None else f"support.request:{customer_user_id}"


class SupportService:
    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer
        self.lock = asyncio.Lock()
        self.tickets: dict[str, Ticket] = {}
        self.keys: dict[tuple[str, str], tuple[str, str, Ticket]] = {}
        self.orders = {"demo-100": "shipped", "demo-200": "processing"}

    async def handle(self, request: SupportRequest, key: str,
                     customer_user_id: int | None = None) -> Ticket:
        scoped = idempotency_scope(customer_user_id), key
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        with hop(self.tracer, "support.route") as span:
            route = route_message(request.message)
            span.set_attribute("support.route", route)
        async with self.lock:
            if scoped in self.keys:
                previous, _, original = self.keys[scoped]
                if previous != digest:
                    raise SupportConflict("IDEMPOTENCY_CONFLICT")
                return original.model_copy(deep=True)
            if len(self.tickets) >= 256:
                raise SupportCapacity("SUPPORT_CAPACITY")
            state: Literal["resolved", "waiting_human", "needs_information", "claimed"] = "needs_information"
            reason = "CLARIFY_REQUEST"
            status = None
            if route == "order_status":
                with hop(self.tracer, "support.order_lookup"):
                    status = self.orders.get(request.order_id or "")
                if status is not None:
                    state, reason = "resolved", "ORDER_STATUS_FOUND"
                else:
                    reason = "ORDER_ID_REQUIRED_OR_UNKNOWN"
            elif route in ("human", "return_request"):
                state, reason = "waiting_human", "HUMAN_APPROVAL_REQUIRED"
            ticket = Ticket(ticket_id=str(uuid4()), route=route, state=state,
                            reason_code=reason, order_status=status,
                            history=["created", state], customer_user_id=customer_user_id)
            self.tickets[ticket.ticket_id] = ticket
            self.keys[scoped] = digest, ticket.ticket_id, ticket.model_copy(deep=True)
            return ticket.model_copy(deep=True)

    async def resolve(self, ticket_id: str, expected: int, actor: str) -> Ticket:
        # Authorization (agent/admin role) is enforced at the HTTP boundary; actor is audit only.
        async with self.lock:
            ticket = self.tickets[ticket_id]
            if ticket.revision != expected or ticket.state != "waiting_human":
                raise SupportConflict("TICKET_STATE_CONFLICT")
            with hop(self.tracer, "support.human_resolve"):
                updated = ticket.model_copy(deep=True)
                updated.state = "resolved"
                updated.revision += 1
                updated.reason_code = "HUMAN_RESOLVED"
                updated.history.append("human_resolved")
                self.tickets[ticket_id] = updated
                return updated.model_copy(deep=True)

    async def get(self, ticket_id: str) -> Ticket:
        async with self.lock:
            return self.tickets[ticket_id].model_copy(deep=True)

    async def customer_tickets(self, customer_user_id: int, after: str | None,
                               limit: int) -> list[Ticket]:
        # Insertion order is creation order; newest first, cursor is the last seen ticket_id.
        async with self.lock:
            own = [t for t in reversed(self.tickets.values())
                   if t.customer_user_id == customer_user_id]
        if after is not None:
            ids = [t.ticket_id for t in own]
            own = own[ids.index(after) + 1:] if after in ids else []
        return [t.model_copy(deep=True) for t in own[:limit]]

    async def snapshot(self) -> str:
        async with self.lock:
            return json.dumps({k: v.model_dump() for k, v in self.tickets.items()})
