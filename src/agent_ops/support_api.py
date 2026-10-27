from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from agent_ops.auth import Principal
from agent_ops.commerce_backend import CommerceBackend
from agent_ops.support import (
    SupportCapacity,
    SupportConflict,
    SupportRequest,
    Ticket,
)
from agent_ops.workbench import WorkbenchService

Guard = Callable[..., Awaitable[Principal]]
BIGINT = 2**63 - 1


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class Page(BaseModel):
    items: list[Any]
    next_cursor: str | None


class OffsetPage(BaseModel):
    items: list[Any]
    offset: int
    limit: int


class SupportBackend(Protocol):
    async def handle(self, request: SupportRequest, key: str,
                     customer_user_id: int | None = None) -> Ticket: ...
    async def get(self, ticket_id: str) -> Ticket: ...
    async def resolve(self, ticket_id: str, expected: int, actor: str) -> Ticket: ...
    async def customer_tickets(self, customer_user_id: int, after: str | None,
                               limit: int) -> list[Ticket]: ...


def owner_id(principal: Principal) -> int:
    if principal.customer_user_id is None:  # guaranteed by token validation for customers
        raise HTTPException(403, "ROLE_FORBIDDEN")
    return principal.customer_user_id


def commerce_required(commerce: CommerceBackend | None) -> CommerceBackend:
    if commerce is None:
        raise HTTPException(503, "COMMERCE_BACKEND_UNAVAILABLE")
    return commerce


def support_router(service: SupportBackend, commerce: CommerceBackend | None, *,
                   customer: Guard, staff: Guard, signed_in: Guard, enable_resolve: bool = True) -> APIRouter:
    """Customer-facing /support plus the staff-only resolve action (/workbench and legacy path)."""
    router = APIRouter()
    Customer = Annotated[Principal, Depends(customer)]
    Staff = Annotated[Principal, Depends(staff)]
    Anyone = Annotated[Principal, Depends(signed_in)]
    Limit = Annotated[int, Query(ge=1, le=100)]

    @router.post("/support/requests", response_model=Ticket)
    async def handle(body: SupportRequest, principal: Customer,
                     idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]) -> Ticket:
        try:
            return await service.handle(body, idempotency_key, owner_id(principal))
        except SupportConflict as error:
            raise HTTPException(409, str(error)) from error
        except SupportCapacity as error:
            raise HTTPException(503, str(error)) from error

    @router.get("/support/tickets", response_model=Page | OffsetPage)
    async def own_tickets(principal: Customer, limit: Limit = 20,
                          cursor: Annotated[str | None, Query(max_length=64)] = None,
                          offset: Annotated[int, Query(ge=0, le=2**31-1)] = 0) -> Page | OffsetPage:
        if isinstance(service, WorkbenchService):
            if cursor is not None:
                raise HTTPException(422, "P4_USES_OFFSET")
            return OffsetPage(items=await service.customer_page(owner_id(principal), limit, offset), offset=offset, limit=limit)
        items = await service.customer_tickets(owner_id(principal), cursor, limit)
        return Page(items=items, next_cursor=items[-1].ticket_id if len(items) == limit else None)

    @router.get("/support/tickets/{ticket_id}", response_model=Ticket)
    async def get(ticket_id: str, principal: Anyone) -> Ticket:
        try:
            ticket = await service.get(ticket_id)
        except KeyError as error:
            raise HTTPException(404, "TICKET_NOT_FOUND") from error
        # Staff see every ticket (B1); a customer only their own, others look nonexistent.
        if principal.role == "customer" and ticket.customer_user_id != principal.customer_user_id:
            raise HTTPException(404, "TICKET_NOT_FOUND")
        return ticket

    async def resolve(ticket_id: str, body: ResolveRequest, principal: Principal) -> Ticket:
        try:
            return await service.resolve(ticket_id, body.expected_revision, principal.actor)
        except KeyError as error:
            raise HTTPException(404, "TICKET_NOT_FOUND") from error
        except SupportConflict as error:
            raise HTTPException(409, str(error)) from error

    async def workbench_resolve(ticket_id: str, body: ResolveRequest, principal: Staff) -> Ticket:
        return await resolve(ticket_id, body, principal)

    async def legacy_resolve(ticket_id: str, body: ResolveRequest, principal: Staff) -> Ticket:
        # Pre-P3 path kept for compatibility; now an agent/admin access token, not a shared secret.
        return await resolve(ticket_id, body, principal)

    @router.get("/support/conversations", response_model=Page)
    async def conversations(principal: Customer, limit: Limit = 20,
                            cursor: Annotated[int | None, Query(ge=0, le=BIGINT)] = None) -> Page:
        items = await commerce_required(commerce).conversations(
            owner_id(principal), after=cursor, limit=limit)
        last = str(items[-1]["conversation_id"]) if len(items) == limit else None
        return Page(items=items, next_cursor=last)

    @router.get("/support/conversations/{conversation_id}/messages", response_model=Page)
    async def messages(principal: Customer,
                       conversation_id: Annotated[int, Path(gt=0, le=BIGINT)],
                       limit: Limit = 50,
                       cursor: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0) -> Page:
        try:
            items = await commerce_required(commerce).messages(
                conversation_id, owner_id(principal), after=cursor, limit=limit)
        except KeyError as error:
            raise HTTPException(404, "CONVERSATION_NOT_FOUND") from error
        return Page(items=items, next_cursor=str(items[-1]["seq"]) if len(items) == limit else None)

    if enable_resolve:
        router.add_api_route("/workbench/tickets/{ticket_id}/resolve", workbench_resolve, methods=["POST"], response_model=Ticket)
        router.add_api_route("/support/tickets/{ticket_id}/resolve", legacy_resolve, methods=["POST"], response_model=Ticket)
    return router
