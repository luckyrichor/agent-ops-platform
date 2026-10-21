from typing import Annotated, Protocol

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agent_ops.support import (
    SupportCapacity,
    SupportConflict,
    SupportRequest,
    Ticket,
)


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class SupportBackend(Protocol):
    async def handle(self, request: SupportRequest, key: str) -> Ticket: ...
    async def get(self, ticket_id: str) -> Ticket: ...
    async def resolve(self, ticket_id: str, expected: int, token: str) -> Ticket: ...


def support_router(service: SupportBackend) -> APIRouter:
    router = APIRouter(prefix="/support")

    @router.post("/requests", response_model=Ticket)
    async def handle(body: SupportRequest,
                     idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]) -> Ticket:
        try:
            return await service.handle(body, idempotency_key)
        except SupportConflict as error:
            raise HTTPException(409, str(error)) from error
        except SupportCapacity as error:
            raise HTTPException(503, str(error)) from error

    @router.get("/tickets/{ticket_id}", response_model=Ticket)
    async def get(ticket_id: str) -> Ticket:
        try:
            return await service.get(ticket_id)
        except KeyError as error:
            raise HTTPException(404, "TICKET_NOT_FOUND") from error

    @router.post("/tickets/{ticket_id}/resolve", response_model=Ticket)
    async def resolve(ticket_id: str, body: ResolveRequest,
                      authorization: Annotated[str, Header()] = "") -> Ticket:
        try:
            token = authorization[7:] if authorization.startswith("Bearer ") else ""
            return await service.resolve(ticket_id, body.expected_revision, token)
        except PermissionError as error:
            raise HTTPException(403, "HUMAN_AUTH_REQUIRED") from error
        except KeyError as error:
            raise HTTPException(404, "TICKET_NOT_FOUND") from error
        except SupportConflict as error:
            raise HTTPException(409, str(error)) from error

    return router
