"""P4 authenticated HTTP workflows and replayable WebSocket stream. No frontend or refunds."""

import asyncio
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from agent_ops.auth import AuthError, AuthService, Principal
from agent_ops.support import SupportConflict, SupportRequest
from agent_ops.support_api import BIGINT, Guard
from agent_ops.workbench import WorkbenchService, WorkflowDenied


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, le=BIGINT)


class Reply(Action):
    message: str = Field(min_length=1, max_length=2000)


class Information(Action):
    order_id: int = Field(gt=0, le=BIGINT)
    message: str = Field(default="补充订单信息", min_length=1, max_length=2000)


def workbench_router(
    service: WorkbenchService | None,
    auth: AuthService,
    *,
    customer: Guard,
    staff: Guard,
    admin: Guard,
    signed_in: Guard,
) -> APIRouter:
    router = APIRouter()
    Customer = Annotated[Principal, Depends(customer)]
    Staff = Annotated[Principal, Depends(staff)]
    Admin = Annotated[Principal, Depends(admin)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Offset = Annotated[int, Query(ge=0, le=2**31 - 1)]

    def required() -> WorkbenchService:
        if service is None:
            raise HTTPException(503, "WORKBENCH_BACKEND_UNAVAILABLE")
        return service

    async def action(tid: str, op: str, body: Action, principal: Principal, key: str) -> Any:
        try:
            return await required().change(
                tid,
                op,
                body.expected_revision,
                principal,
                key,
                message=body.message if isinstance(body, (Reply, Information)) else None,
                order_id=body.order_id if isinstance(body, Information) else None,
            )
        except KeyError:
            raise HTTPException(404, "TICKET_OR_ORDER_NOT_FOUND") from None
        except WorkflowDenied as error:
            raise HTTPException(403, str(error)) from None
        except SupportConflict as error:
            raise HTTPException(409, str(error)) from None

    @router.get("/workbench/tickets")
    async def queue(principal: Staff, limit: Limit = 20, offset: Offset = 0,
                    state: Literal["waiting_human", "claimed"] = "waiting_human", mine: bool = False) -> Any:
        return {"items": await required().queue(limit, offset, state=state,
                assigned=principal.account_id if mine else None), "offset": offset, "limit": limit}

    @router.get("/workbench/tickets/{ticket_id}")
    async def detail(ticket_id: str, principal: Staff) -> Any:
        try:
            ticket = await required().inspect(ticket_id, principal)
            return {
                "ticket": ticket,
                "messages": await required().messages(ticket_id, principal, 50, 0),
                "events": await required().audit(ticket_id, 50, 0),
            }
        except KeyError:
            raise HTTPException(404, "TICKET_NOT_FOUND") from None

    @router.get("/workbench/tickets/{ticket_id}/order")
    async def order(ticket_id: str, principal: Staff) -> Any:
        try:
            return await required().view_order(ticket_id, principal)
        except KeyError:
            raise HTTPException(404, "ORDER_NOT_FOUND") from None

    @router.get("/workbench/tickets/{ticket_id}/events")
    async def audit(ticket_id: str, principal: Staff, limit: Limit = 50, offset: Offset = 0) -> Any:
        try:
            ticket = await required().inspect(ticket_id, principal)
            return {
                "items": await required().audit(ticket_id, limit, offset),
                "ticket_revision": ticket.revision,
                "offset": offset,
                "limit": limit,
            }
        except KeyError:
            raise HTTPException(404, "TICKET_NOT_FOUND") from None

    @router.get("/support/tickets/{ticket_id}/messages")
    async def customer_messages(
        ticket_id: str, principal: Customer, limit: Limit = 50, offset: Offset = 0
    ) -> Any:
        try:
            return {
                "items": await required().messages(ticket_id, principal, limit, offset),
                "offset": offset,
                "limit": limit,
            }
        except KeyError:
            raise HTTPException(404, "TICKET_NOT_FOUND") from None

    @router.get("/workbench/tickets/{ticket_id}/messages")
    async def staff_messages(
        ticket_id: str, principal: Staff, limit: Limit = 50, offset: Offset = 0
    ) -> Any:
        try:
            ticket = await required().inspect(ticket_id, principal)
            return {
                "items": await required().messages(ticket_id, principal, limit, offset),
                "ticket_revision": ticket.revision,
                "offset": offset,
                "limit": limit,
            }
        except KeyError:
            raise HTTPException(404, "TICKET_NOT_FOUND") from None

    @router.post("/workbench/tickets/{ticket_id}/claim")
    async def claim(ticket_id: str, body: Action, principal: Staff, idempotency_key: Key) -> Any:
        return await action(ticket_id, "claim", body, principal, idempotency_key)

    @router.post("/workbench/tickets/{ticket_id}/reply")
    async def reply(ticket_id: str, body: Reply, principal: Staff, idempotency_key: Key) -> Any:
        return await action(ticket_id, "reply", body, principal, idempotency_key)

    @router.post("/workbench/tickets/{ticket_id}/release")
    async def release(ticket_id: str, body: Action, principal: Staff, idempotency_key: Key) -> Any:
        return await action(ticket_id, "release", body, principal, idempotency_key)

    async def resolve(ticket_id: str, body: Action, principal: Staff, idempotency_key: Key) -> Any:
        return await action(ticket_id, "resolve", body, principal, idempotency_key)

    async def legacy_resolve(
        ticket_id: str, body: Action, principal: Staff, idempotency_key: Key
    ) -> Any:
        return await action(ticket_id, "resolve", body, principal, idempotency_key)

    @router.post("/support/tickets/{ticket_id}/information")
    async def information(
        ticket_id: str, body: Information, principal: Customer, idempotency_key: Key
    ) -> Any:
        return await action(ticket_id, "information", body, principal, idempotency_key)

    @router.post("/support/tickets/{ticket_id}/messages")
    async def message(
        ticket_id: str, body: Reply, principal: Customer, idempotency_key: Key
    ) -> Any:
        return await action(ticket_id, "message", body, principal, idempotency_key)

    @router.post("/support/returns")
    async def returns(body: SupportRequest, principal: Customer, idempotency_key: Key) -> Any:
        try:
            return await required().create(
                body, idempotency_key, principal.customer_user_id, return_request=True
            )
        except SupportConflict as error:
            raise HTTPException(409, str(error)) from None

    @router.get("/admin/support-stats")
    async def stats(_: Admin) -> Any:
        return await required().stats()

    # A login-only status endpoint is deliberately unnecessary; roles are rechecked on WS.
    @router.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=5)
            if not isinstance(first, dict) or set(first) - {"access_token", "after"}:
                await ws.close(code=4400)
                return
            token = first.get("access_token")
            after = first.get("after", 0)
            if not isinstance(token, str) or type(after) is not int or not 0 <= after <= BIGINT:
                await ws.close(code=4400)
                return
            if service is None:
                await ws.close(code=1013)
                return
            auth.codec.decode(token)
            if after > await service.last_sequence():
                await ws.close(code=4400)
                return
            while True:
                principal = auth.codec.decode(token)
                account = await auth.store.by_id(principal.account_id)
                if account is None or account.status != "active":
                    await ws.close(code=4401)
                    return
                for event in await service.events(principal, after):
                    await ws.send_json(jsonable_encoder(event))
                    after = event["sequence"]
                try:
                    data = await asyncio.wait_for(ws.receive_text(), timeout=0.1)
                    if data != "ping":
                        await ws.close(code=4400)
                        return
                    await ws.send_json({"type": "pong"})
                except TimeoutError:
                    pass
        except AuthError:
            await ws.close(code=4401)
        except (ValueError, TypeError, json.JSONDecodeError):
            await ws.close(code=4400)
        except TimeoutError:
            await ws.close(code=4408)
        except WebSocketDisconnect:
            pass

    if service is not None:
        router.add_api_route("/workbench/tickets/{ticket_id}/resolve", resolve, methods=["POST"])
        router.add_api_route(
            "/support/tickets/{ticket_id}/resolve", legacy_resolve, methods=["POST"]
        )
    return router
