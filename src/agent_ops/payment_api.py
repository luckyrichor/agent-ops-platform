"""Private customer/staff payment API and separate, minimal public notification application."""
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qsl
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from agent_ops.auth import Principal
from agent_ops.payment_gateway import PaymentConfig, PaymentError
from agent_ops.payments import Payments
from agent_ops.support_api import BIGINT, Guard, owner_id


def from_env(url: str) -> PaymentConfig | None:
    folder = os.environ.get('AGENT_OPS_PAYMENT_DIR')
    if not folder:
        return None
    # Notification URL is private, never in checked-in configuration or access logs.
    path = Path(folder) / 'notify_url.txt'
    try:
        if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
            raise PaymentError('PAYMENT_FILE_PERMISSIONS')
        return PaymentConfig.load(Path(folder), path.read_text().strip())
    except OSError:
        raise PaymentError('PAYMENT_CONFIGURATION_INVALID') from None


def payment_router(service: Payments | None, *, customer: Guard, staff: Guard) -> APIRouter:
    router = APIRouter()
    Customer = Annotated[Principal, Depends(customer)]
    Staff = Annotated[Principal, Depends(staff)]
    Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]

    def required() -> Payments:
        if service is None:
            raise HTTPException(503, 'PAYMENTS_NOT_CONFIGURED')
        return service

    @router.get('/payments/availability')
    async def availability(_: Customer) -> dict[str, bool]:
        return {'enabled': service is not None}

    @router.get('/workbench/payments/availability')
    async def staff_availability(_: Staff) -> dict[str, bool]:
        return {'enabled': service is not None}

    @router.post('/orders/{order_id}/payment')
    async def create(order_id: int, principal: Customer, idempotency_key: Key) -> dict[str, Any]:
        if not 0 < order_id <= BIGINT:
            raise HTTPException(422, 'INVALID_ORDER')
        try:
            return await required().create(order_id, owner_id(principal))
        except PaymentError as error:
            raise HTTPException(404 if str(error) == 'ORDER_NOT_FOUND' else 409, str(error)) from None

    @router.post('/workbench/tickets/{ticket_id}/refund')
    async def approve(ticket_id: str, body: 'Approval', principal: Staff, idempotency_key: Key) -> dict[str, Any]:
        try:
            result = await required().approve_refund(ticket_id, body.expected_revision, principal)
            return await required().execute_refund(result['refund_no'])
        except PaymentError as error:
            raise HTTPException(403 if 'FORBIDDEN' in str(error) or 'CLAIMED' in str(error) else 409, str(error)) from None

    return router


class Approval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1, le=BIGINT)


def notification_app(service: Payments, *, manage_lifecycle: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if manage_lifecycle:
                await service.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def trace(request: Request, call_next: Any) -> Any:
        trace_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    @app.post('/payments/notify')
    async def notify(request: Request) -> PlainTextResponse:
        try:
            if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/x-www-form-urlencoded':
                raise PaymentError('PAYMENT_CONTENT_TYPE')
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    raise PaymentError('PAYMENT_BODY_TOO_LARGE')
            pairs = parse_qsl(body.decode('utf-8', errors='strict'), keep_blank_values=True,
                              strict_parsing=True, max_num_fields=100, encoding='utf-8', errors='strict')
            params = dict(pairs)
            if len(params) != len(pairs) or any(len(k) > 128 or len(v) > 8192 for k, v in pairs):
                raise PaymentError('PAYMENT_DUPLICATE_OR_OVERSIZE_FIELDS')
            await service.notify(params)
            return PlainTextResponse('success', headers={'Cache-Control': 'no-store'})
        except (ValueError, UnicodeError):
            await service.reject('PAYMENT_FORM_INVALID')
            return PlainTextResponse('failure', status_code=400)
        except PaymentError as error:
            # notify already audits validation failures; framing failures are audited here.
            if str(error) in {'PAYMENT_CONTENT_TYPE', 'PAYMENT_BODY_TOO_LARGE', 'PAYMENT_DUPLICATE_OR_OVERSIZE_FIELDS'}:
                await service.reject(str(error))
            return PlainTextResponse('failure', status_code=400)
        except Exception:  # noqa: BLE001 -- never expose driver/request/URL diagnostics
            return PlainTextResponse('failure', status_code=503)

    return app


def create_notification_app() -> FastAPI:
    url = os.environ['AGENT_OPS_DATABASE_URL']
    config = from_env(url)
    if config is None:
        raise PaymentError('PAYMENTS_NOT_CONFIGURED')
    return notification_app(Payments(url, config), manage_lifecycle=True)
