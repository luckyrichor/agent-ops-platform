"""P3 login, token lifecycle and the role matrix for every HTTP route (in-memory backend).

Ownership of PostgreSQL-backed orders/conversations is covered in test_auth_postgres.py.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_ops.agents import demo_agents
from agent_ops.api import create_app
from agent_ops.auth import AUDIENCE, ISSUER, Principal
from agent_ops.orchestrator import Dispatcher

SECRET = "fixture-signing-secret-" + "s" * 32
PASSWORDS = {
    "c1": "fixture-customer-one",
    "c2": "fixture-customer-two",
    "agent": "fixture-agent-password",
    "admin": "fixture-admin-password",
}
ROLES = {"c1": "customer", "c2": "customer", "agent": "agent", "admin": "admin"}
CUSTOMER_IDS = {"c1": 1, "c2": 2}
TICKET = "00000000-0000-0000-0000-000000000000"

# (method, path, json body, extra headers, roles allowed). Every non-public route must appear.
MATRIX = [
    ("POST", "/v1/commerce/runs", {"request": "fixture", "category": "kettle",
      "budget_cent": 5000, "region": "上海"}, {}, {"customer"}),
    ("POST", "/v1/commerce/stream", {"request": "fixture", "category": "kettle",
      "budget_cent": 5000, "region": "上海"}, {}, {"customer"}),
    ("POST", "/auth/logout", None, {}, {"customer", "agent", "admin"}),
    ("GET", "/auth/me", None, {}, {"customer", "agent", "admin"}),
    ("POST", "/support/requests", {"message": "转人工"}, {"Idempotency-Key": "matrix"},
     {"customer"}),
    ("GET", "/support/tickets", None, {}, {"customer"}),
    ("GET", f"/support/tickets/{TICKET}", None, {}, {"customer", "agent", "admin"}),
    ("POST", f"/support/tickets/{TICKET}/resolve", {"expected_revision": 1}, {},
     {"agent", "admin"}),
    ("POST", f"/workbench/tickets/{TICKET}/resolve", {"expected_revision": 1}, {},
     {"agent", "admin"}),
    ("GET", "/support/conversations", None, {}, {"customer"}),
    ("GET", "/support/conversations/1/messages", None, {}, {"customer"}),
    ("GET", "/orders", None, {}, {"customer"}),
    ("GET", "/orders/1", None, {}, {"customer"}),
    ("POST", "/orders", {"lines": [{"product_id": 1, "quantity": 1}]},
     {"Idempotency-Key": "matrix"}, {"customer"}),
    ("POST", "/orders/1/cancel", {"expected_revision": 1}, {}, {"customer"}),
    ("GET", "/admin/accounts", None, {}, {"admin"}),
    ("POST", "/admin/accounts/999/status", {"status": "active"}, {}, {"admin"}),
]
PUBLIC = {("POST", "/auth/login"), ("POST", "/auth/refresh")}


@pytest.fixture
async def env(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_AUTH_SECRET", SECRET)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    dispatch = Dispatcher(demo_agents(), provider.get_tracer("auth-test"))
    app = create_app(dispatch)
    for name, password in PASSWORDS.items():
        await app.state.auth.create_account(f"{name}@fixture.invalid", password, ROLES[name],
                                             CUSTOMER_IDS.get(name))
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield app, client, exporter
    await dispatch.aclose()
    provider.shutdown()


async def login(client, name, password=None):
    return await client.post("/auth/login", json={"email": f"{name}@fixture.invalid",
                                                  "password": password or PASSWORDS[name]})


async def bearer(client, name):
    response = await login(client, name)
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def routes(app):
    # The OpenAPI schema lists every mounted operation (included routers are not in app.routes).
    return {(method.upper(), path) for path, item in app.openapi()["paths"].items()
            for method in item}


def template(path):
    return (path.replace(TICKET, "{ticket_id}").replace("/orders/1", "/orders/{order_id}")
            .replace("/conversations/1/", "/conversations/{conversation_id}/")
            .replace("/accounts/999/", "/accounts/{account_id}/"))


async def test_every_route_is_public_by_decision_or_in_the_role_matrix(env):
    app, _, _ = env
    covered = {(method, template(path)) for method, path, *_ in MATRIX}
    assert routes(app) == covered | PUBLIC


@pytest.mark.parametrize(("method", "path", "body", "headers", "allowed"), MATRIX,
                         ids=[f"{m} {p}" for m, p, *_ in MATRIX])
async def test_role_matrix(env, method, path, body, headers, allowed):
    _, client, _ = env
    anonymous = await client.request(method, path, json=body, headers=headers)
    assert anonymous.status_code == 401
    assert anonymous.json()["detail"] == "AUTH_REQUIRED"
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert anonymous.json()["trace_id"] and anonymous.headers["x-trace-id"]
    for name in ("c1", "agent", "admin"):
        response = await client.request(method, path, json=body,
                                         headers={**headers, **await bearer(client, name)})
        if ROLES[name] in allowed:
            # Past the guard: business outcome (200/204/404/409/503 in memory mode), never 401/403.
            assert response.status_code not in (401, 403), (name, response.text)
        else:
            assert response.status_code == 403, (name, response.text)
            assert response.json()["detail"] == "ROLE_FORBIDDEN"


async def test_login_failures_are_indistinguishable(env):
    app, client, _ = env
    wrong = await login(client, "c1", "not-the-password")
    unknown = await client.post("/auth/login", json={"email": "nobody@fixture.invalid",
                                                     "password": "whatever-password"})
    malformed = await client.post("/auth/login", json={"email": "not-an-email",
                                                       "password": "whatever-password"})
    await app.state.auth.set_status(2, "disabled")
    disabled = await login(client, "c2")
    for response in (wrong, unknown, malformed, disabled):
        assert response.status_code == 401
        assert response.json()["detail"] == "INVALID_CREDENTIALS"
    # Email is case/space-insensitive.
    upper = await client.post("/auth/login", json={"email": "  C1@Fixture.INVALID ",
                                                   "password": PASSWORDS["c1"]})
    assert upper.status_code == 200
    body = upper.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 900
    assert body["account"] == {"id": 1, "email": "c1@fixture.invalid", "role": "customer",
                               "customer_user_id": 1, "status": "active"}
    claims = jwt.decode(body["access_token"], SECRET, algorithms=["HS256"], audience=AUDIENCE)
    assert claims["exp"] - claims["iat"] == 900 and claims["role"] == "customer"
    assert "password" not in json.dumps(claims) and "email" not in claims


async def test_refresh_rotation_reuse_detection_and_logout(env):
    _, client, _ = env
    first = (await login(client, "c1")).json()
    rotated = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]
    me = await client.get("/auth/me",
                          headers={"Authorization": "Bearer " + second["access_token"]})
    assert me.json()["email"] == "c1@fixture.invalid"
    # Replaying a rotated token is treated as theft: the whole login family is revoked.
    replay = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401 and replay.json()["detail"] == "REFRESH_TOKEN_REUSED"
    after = await client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert after.status_code == 401 and after.json()["detail"] == "REFRESH_TOKEN_INVALID"
    # Logout ends only its own session; another login of the same account keeps working.
    one, two = (await login(client, "c1")).json(), (await login(client, "c1")).json()
    out = await client.post("/auth/logout",
                            headers={"Authorization": "Bearer " + one["access_token"]})
    assert out.status_code == 204
    dead = await client.post("/auth/refresh", json={"refresh_token": one["refresh_token"]})
    alive = await client.post("/auth/refresh", json={"refresh_token": two["refresh_token"]})
    assert dead.status_code == 401 and alive.status_code == 200
    garbage = await client.post("/auth/refresh", json={"refresh_token": "x" * 43})
    assert garbage.status_code == 401


async def test_refresh_expiry_and_family_cap(env):
    app, client, _ = env
    auth = app.state.auth
    start = datetime(2026, 1, 1, tzinfo=UTC)
    auth.clock = lambda: start
    token = (await login(client, "c1")).json()["refresh_token"]
    auth.clock = lambda: start + timedelta(days=7, seconds=1)
    expired = await client.post("/auth/refresh", json={"refresh_token": token})
    assert expired.status_code == 401 and expired.json()["detail"] == "REFRESH_TOKEN_INVALID"
    # Rotating daily cannot extend a login past 30 days.
    auth.clock = lambda: start
    token = (await login(client, "c1")).json()["refresh_token"]
    for day in range(1, 31):
        auth.clock = lambda day=day: start + timedelta(days=day, seconds=-1)
        response = await client.post("/auth/refresh", json={"refresh_token": token})
        assert response.status_code == 200, day
        token = response.json()["refresh_token"]
        assert datetime.fromisoformat(response.json()["refresh_expires_at"]) <= \
            start + timedelta(days=30)
    auth.clock = lambda: start + timedelta(days=30, seconds=1)
    assert (await client.post("/auth/refresh", json={"refresh_token": token})).status_code == 401


async def test_rejected_access_tokens(env):
    app, client, _ = env
    codec = app.state.auth.codec
    principal = Principal(1, "customer", 1, "00000000-0000-0000-0000-000000000001")
    now = datetime.now(UTC)
    valid = codec.issue(principal, now)
    claims = jwt.decode(valid, SECRET, algorithms=["HS256"], audience=AUDIENCE)
    refresh = (await login(client, "c1")).json()["refresh_token"]
    cases = {
        "ACCESS_TOKEN_EXPIRED": [codec.issue(principal, now - timedelta(minutes=16))],
        "ACCESS_TOKEN_INVALID": [
            valid[:-2] + ("AA" if not valid.endswith("AA") else "BB"),   # bad signature
            jwt.encode(claims, "another-secret-" + "x" * 32, algorithm="HS256"),
            jwt.encode(claims, None, algorithm="none"),
            jwt.encode({**claims, "role": "admin", "cid": None}, "wrong-key-" + "y" * 32,
                       algorithm="HS256"),
            jwt.encode({**claims, "aud": "someone-else"}, SECRET, algorithm="HS256"),
            jwt.encode({**claims, "iss": "someone-else"}, SECRET, algorithm="HS256"),
            jwt.encode({**claims, "typ": "refresh"}, SECRET, algorithm="HS256"),
            jwt.encode({**claims, "role": "root"}, SECRET, algorithm="HS256"),
            jwt.encode({**claims, "cid": None}, SECRET, algorithm="HS256"),   # customer w/o owner
            refresh,                                                         # wrong token kind
        ],
    }
    for code, tokens in cases.items():
        for token in tokens:
            response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401, token
            assert response.json()["detail"] == code
    for header in ("Basic abc", "Bearer", "Token " + valid, valid):
        response = await client.get("/auth/me", headers={"Authorization": header})
        assert response.status_code == 401 and response.json()["detail"] == "AUTH_REQUIRED"
    assert (await client.get("/auth/me", headers={"Authorization": "bearer " + valid})
            ).status_code == 200
    assert ISSUER == claims["iss"]


async def test_customer_ticket_isolation_and_staff_resolution(env):
    _, client, _ = env
    c1, c2 = await bearer(client, "c1"), await bearer(client, "c2")
    agent, admin = await bearer(client, "agent"), await bearer(client, "admin")
    body = {"message": "我要退款"}
    mine = await client.post("/support/requests", json=body,
                             headers={"Idempotency-Key": "same", **c1})
    assert mine.status_code == 200 and mine.json()["customer_user_id"] == 1
    tid = mine.json()["ticket_id"]
    # Same key and body from another customer is a different ticket, never a replay of c1's.
    other = await client.post("/support/requests", json=body,
                              headers={"Idempotency-Key": "same", **c2})
    assert other.status_code == 200 and other.json()["ticket_id"] != tid
    assert (await client.get(f"/support/tickets/{tid}", headers=c1)).status_code == 200
    hidden = await client.get(f"/support/tickets/{tid}", headers=c2)
    assert hidden.status_code == 404 and hidden.json()["detail"] == "TICKET_NOT_FOUND"
    assert [t["ticket_id"] for t in (await client.get("/support/tickets", headers=c1)
                                     ).json()["items"]] == [tid]
    assert tid not in json.dumps((await client.get("/support/tickets", headers=c2)).json())
    for staff in (agent, admin):
        assert (await client.get(f"/support/tickets/{tid}", headers=staff)).status_code == 200
    assert (await client.post(f"/workbench/tickets/{tid}/resolve", json={"expected_revision": 1},
                              headers=c1)).status_code == 403
    done = await client.post(f"/workbench/tickets/{tid}/resolve", json={"expected_revision": 1},
                             headers=agent)
    assert done.status_code == 200 and done.json()["revision"] == 2
    stale = await client.post(f"/support/tickets/{tid}/resolve", json={"expected_revision": 1},
                              headers=admin)
    assert stale.status_code == 409
    assert (await client.get(f"/support/tickets/{tid}", headers=c1)).json()["state"] == "resolved"


async def test_customer_ticket_pagination(env):
    _, client, _ = env
    c1 = await bearer(client, "c1")
    ids = [(await client.post("/support/requests", json={"message": f"转人工 {n}"},
                              headers={"Idempotency-Key": f"page-{n}", **c1})).json()["ticket_id"]
           for n in range(5)]
    first = (await client.get("/support/tickets?limit=2", headers=c1)).json()
    second = (await client.get(f"/support/tickets?limit=2&cursor={first['next_cursor']}",
                               headers=c1)).json()
    third = (await client.get(f"/support/tickets?limit=2&cursor={second['next_cursor']}",
                              headers=c1)).json()
    seen = [t["ticket_id"] for page in (first, second, third) for t in page["items"]]
    assert seen == list(reversed(ids)) and third["next_cursor"] is None


async def test_admin_account_management(env):
    _, client, _ = env
    admin = await bearer(client, "admin")
    listed = (await client.get("/admin/accounts?limit=3", headers=admin)).json()
    assert [a["id"] for a in listed["items"]] == [1, 2, 3] and listed["next_cursor"] == "3"
    assert "password" not in json.dumps(listed) and "argon2" not in json.dumps(listed)
    session = (await login(client, "c1")).json()
    disabled = await client.post("/admin/accounts/1/status", json={"status": "disabled"},
                                 headers=admin)
    assert disabled.status_code == 200 and disabled.json()["status"] == "disabled"
    # Disabling ends refresh sessions and blocks login; access tokens expire within 15 minutes.
    assert (await client.post("/auth/refresh", json={"refresh_token": session["refresh_token"]})
            ).status_code == 401
    assert (await login(client, "c1")).status_code == 401
    assert (await client.post("/admin/accounts/1/status", json={"status": "active"},
                              headers=admin)).status_code == 200
    assert (await login(client, "c1")).status_code == 200
    own = (await client.get("/auth/me", headers=admin)).json()["id"]
    assert (await client.post(f"/admin/accounts/{own}/status", json={"status": "disabled"},
                              headers=admin)).status_code == 409
    assert (await client.post("/admin/accounts/999/status", json={"status": "disabled"},
                              headers=admin)).status_code == 404
    assert (await client.post("/admin/accounts/1/status", json={"status": "deleted"},
                              headers=admin)).status_code == 422


async def test_memory_mode_has_no_commerce_data(env):
    _, client, _ = env
    response = await client.get("/orders", headers=await bearer(client, "c1"))
    assert response.status_code == 503
    assert response.json()["detail"] == "COMMERCE_BACKEND_UNAVAILABLE"


async def test_passwords_never_reach_logs_spans_responses_or_storage(env, caplog):
    app, client, exporter = env
    caplog.set_level(logging.DEBUG)
    probe = "probe-password-should-never-leak"
    responses = [
        await login(client, "c1"),
        await login(client, "c1", probe),
        await client.post("/auth/login", json={"email": "x@fixture.invalid", "password": probe}),
        await client.post("/auth/login", json={"email": "c1@fixture.invalid",
                                               "password": probe * 20}),          # too long
        await client.post("/auth/login", json={"email": "c1@fixture.invalid",
                                               "password": probe, "extra": probe}),
        await client.post("/auth/login", json={"email": probe, "password": 123}),
    ]
    assert [r.status_code for r in responses] == [200, 401, 401, 422, 422, 422]
    secrets = [probe, *PASSWORDS.values()]
    spans = json.dumps([dict(s.attributes or {}) for s in exporter.get_finished_spans()])
    for text in [caplog.text, spans, *(r.text for r in responses)]:
        assert not any(secret in text for secret in secrets)
    for account in app.state.auth.store.rows.values():
        assert account.password_hash.startswith("$argon2id$")
        assert not any(secret in account.password_hash for secret in secrets)
    assert {s.name for s in exporter.get_finished_spans()} >= {"auth.http"}


async def test_account_creation_rules(env, monkeypatch):
    app, _, _ = env
    auth = app.state.auth
    with pytest.raises(ValueError, match="at least 12"):
        await auth.create_account("short@fixture.invalid", "short", "agent", None)
    with pytest.raises(ValueError, match="exactly one"):
        await auth.create_account("c3@fixture.invalid", "long-enough-password", "customer", None)
    with pytest.raises(ValueError, match="exactly one"):
        await auth.create_account("a3@fixture.invalid", "long-enough-password", "agent", 3)
    with pytest.raises(ValueError, match="invalid email"):
        await auth.create_account("no-at-sign", "long-enough-password", "agent", None)
    monkeypatch.setenv("AGENT_OPS_SUPPORT_BACKEND", "postgres")
    monkeypatch.setenv("AGENT_OPS_DATABASE_URL", "postgresql+psycopg://unused@127.0.0.1:1/x")
    monkeypatch.delenv("AGENT_OPS_AUTH_SECRET")
    with pytest.raises(ValueError, match="AGENT_OPS_AUTH_SECRET"):
        create_app()
    monkeypatch.setenv("AGENT_OPS_AUTH_SECRET", "too-short")
    with pytest.raises(ValueError, match="32 bytes"):
        create_app()


@pytest.mark.parametrize('path', ['/v1/commerce/runs', '/v1/commerce/stream'])
async def test_platform_token_never_reaches_memory_even_when_url_is_configured(env, monkeypatch, path):
    from agent_memory.sdk import MemoryClient

    _, client, _ = env
    monkeypatch.setenv('AGENT_MEMORY_URL', 'http://memory.invalid')
    def forbidden(*args, **kwargs):
        pytest.fail('platform HTTP path must not construct an agent-memory client')
    monkeypatch.setattr(MemoryClient, '__init__', forbidden)
    response = await client.post(path, headers=await bearer(client, 'c1'), json={
        'request': 'fixture', 'category': 'kettle', 'budget_cent': 5000, 'region': '上海',
        'memory_id': '00000000-0000-0000-0000-000000000001'})
    assert response.status_code == 200
    if path.endswith('stream'):
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        result = frames[-1]
    else:
        result = response.json()
    assert result['outcome'] == 'succeeded'
    assert result['memory_status'] == result['memory_read_status'] == result['memory_write_status'] == 'not_configured'
    assert result['memory_id'] is None
