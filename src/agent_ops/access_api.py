"""P3 HTTP surface: /auth (public login/refresh), /orders (customer-owned), /admin (accounts)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from agent_ops.auth import (
    AccountView,
    AuthError,
    AuthService,
    Credentials,
    Principal,
    RefreshBody,
    StatusBody,
    TokenPair,
    unauthorized,
)
from agent_ops.catalog_search import CatalogQuery, CatalogSearch
from agent_ops.commerce_backend import CommerceBackend, CommerceConflict, OrderLine, PlaceOrder
from agent_ops.support_api import BIGINT, Guard, Page, commerce_required, owner_id


class OrderBody(BaseModel):
    """The owner comes from the access token; a client-supplied user_id is rejected."""
    model_config = ConfigDict(extra="forbid")
    lines: list[OrderLine] = Field(min_length=1, max_length=100)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, le=BIGINT)


def auth_router(auth: AuthService, *, signed_in: Guard) -> APIRouter:
    router = APIRouter(prefix="/auth")

    @router.post("/login", response_model=TokenPair)
    async def login(body: Credentials) -> TokenPair:
        try:
            return await auth.login(body)
        except AuthError as error:
            raise unauthorized(str(error)) from None

    @router.post("/refresh", response_model=TokenPair)
    async def refresh(body: RefreshBody) -> TokenPair:
        try:
            return await auth.refresh(body)
        except AuthError as error:
            raise unauthorized(str(error)) from None

    @router.post("/logout", status_code=204)
    async def logout(principal: Annotated[Principal, Depends(signed_in)]) -> Response:
        await auth.logout(principal)
        return Response(status_code=204)

    @router.get("/me", response_model=AccountView)
    async def me(principal: Annotated[Principal, Depends(signed_in)]) -> AccountView:
        account = await auth.store.by_id(principal.account_id)
        if account is None:
            raise unauthorized("ACCESS_TOKEN_INVALID")
        return AccountView.of(account)

    return router


def admin_router(auth: AuthService, *, admin: Guard) -> APIRouter:
    router = APIRouter(prefix="/admin")
    Admin = Annotated[Principal, Depends(admin)]

    @router.get("/accounts", response_model=Page)
    async def accounts(_: Admin, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                       cursor: Annotated[int, Query(ge=0, le=BIGINT)] = 0) -> Page:
        items = [AccountView.of(a) for a in await auth.store.accounts(cursor, limit)]
        return Page(items=items, next_cursor=str(items[-1].id) if len(items) == limit else None)

    @router.post("/accounts/{account_id}/status", response_model=AccountView)
    async def status(principal: Admin, body: StatusBody,
                     account_id: Annotated[int, Path(gt=0, le=BIGINT)]) -> AccountView:
        if account_id == principal.account_id and body.status == "disabled":
            raise HTTPException(409, "CANNOT_DISABLE_SELF")
        account = await auth.set_status(account_id, body.status)
        if account is None:
            raise HTTPException(404, "ACCOUNT_NOT_FOUND")
        return AccountView.of(account)

    return router


def orders_router(commerce: CommerceBackend | None, *, customer: Guard) -> APIRouter:
    """Every query is filtered by the token's user id; other users' orders read as 404."""
    router = APIRouter(prefix="/orders")
    Customer = Annotated[Principal, Depends(customer)]
    OrderId = Annotated[int, Path(gt=0, le=BIGINT)]

    @router.get("", response_model=Page)
    async def orders(principal: Customer, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                     cursor: Annotated[int | None, Query(gt=0, le=BIGINT)] = None) -> Page:
        items = await commerce_required(commerce).orders(owner_id(principal), after=cursor,
                                                           limit=limit)
        return Page(items=items,
                    next_cursor=str(items[-1]["order_id"]) if len(items) == limit else None)

    @router.get("/{order_id}")
    async def order(principal: Customer, order_id: OrderId) -> dict[str, Any]:
        try:
            return await commerce_required(commerce).order(order_id, owner_id(principal))
        except KeyError as error:
            raise HTTPException(404, "ORDER_NOT_FOUND") from error

    @router.post("")
    async def place(principal: Customer, body: OrderBody,
                    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]
                    ) -> dict[str, Any]:
        request = PlaceOrder(user_id=owner_id(principal), lines=body.lines)
        try:
            return await commerce_required(commerce).place_order(request, idempotency_key)
        except CommerceConflict as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(422, "INVALID_ORDER") from error

    @router.post("/{order_id}/cancel")
    async def cancel(principal: Customer, order_id: OrderId, body: CancelBody) -> dict[str, Any]:
        backend = commerce_required(commerce)
        try:
            await backend.order(order_id, owner_id(principal))
        except KeyError as error:
            raise HTTPException(404, "ORDER_NOT_FOUND") from error
        try:
            return await backend.cancel_order(order_id, owner_id(principal),
                                              body.expected_revision)
        except CommerceConflict as error:
            raise HTTPException(409, str(error)) from error

    return router


def products_router(commerce: CommerceBackend | None, catalog: CatalogSearch | None = None) -> APIRouter:
    """P5 read-only exposure of the existing real PG catalog; no semantic search."""
    router = APIRouter(prefix="/products")

    @router.get("", response_model=Page)
    async def products(limit: Annotated[int, Query(ge=1, le=100)] = 24,
                       cursor: Annotated[int, Query(ge=0, le=BIGINT)] = 0) -> Page:
        items = await commerce_required(commerce).products(after=cursor, limit=limit)
        return Page(items=items, next_cursor=str(items[-1]["id"]) if len(items) == limit else None)

    @router.get("/search", response_model=Page)
    async def search(query: str = "", category_id: int | None = None,
                     min_price_cent: int = 0, max_price_cent: int = 2**63-1,
                     available_only: bool = True, limit: int = 24, offset: int = 0) -> Page:
        from pydantic import ValidationError
        if catalog is None:
            raise HTTPException(503, "CATALOG_NOT_CONFIGURED")
        try:
            filters = CatalogQuery(query=query, category_id=category_id, min_price_cent=min_price_cent,
                max_price_cent=max_price_cent, available_only=available_only, limit=limit, offset=offset)
        except ValidationError:
            raise HTTPException(422, "INVALID_SEARCH_FILTERS") from None
        items = await catalog.search(filters)
        return Page(items=items, next_cursor=str(offset+limit) if len(items)==limit else None)

    @router.get("/{product_id}")
    async def product(product_id: Annotated[int, Path(gt=0, le=BIGINT)]) -> dict[str, Any]:
        try:
            return await commerce_required(commerce).product(product_id)
        except KeyError:
            raise HTTPException(404, "PRODUCT_NOT_FOUND") from None

    return router
