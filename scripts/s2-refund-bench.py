#!/usr/bin/env python3
"""S2 退款跨实例 HTTP 幂等重放（本地测试密钥 + 本地签名网关，不联网、不调沙箱）。

2026-11-03 按 Codex 评审修正：
- 重放=完全相同 payload/revision（不再是 revision+index 的五个不同修订）；
  顺序与并发两个阶段均重放同一请求。
- 每次结果 http_status 与 body 分开记录；错误响应含业务错误码（PaymentError
  消息，如 REFUND_TICKET_NOT_CLAIMED），不把 kind 类名当业务码。
- 固定端口开工前检查占用（被占用即退出，不接管别人的服务）。
- 三个 systemd 实例的启动全部纳入 try/finally 保护；任何失败也把已完成
  的原始结果落到 .local/s2/（fail 文件），克隆库在 finally 收尾。
- 网关断言按 refund_no：实际 fund_change=Y 恰好 1 次（不只是数据库单行）；
  退款请求次数如实记录。
- 并发 409 如实记录业务错误码与协议局限，不强改生产支付实现假通过。

用法：uv run python scripts/s2-refund-bench.py [--out .local/s2/refund-bench-runN.json]
"""
import argparse
import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_ops.auth import Principal
from agent_ops.commerce_backend import CommerceBackend, PlaceOrder
from agent_ops.payment_gateway import PaymentConfig, canonical
from agent_ops.payments import Payments
from agent_ops.support import SupportRequest
from agent_ops.workbench import WorkbenchService

KEY_DIR = Path(".local/s2/keys")
ENV_FILE = Path(".local/s1/db.env")
PORTS = {"refund_a": 18911, "refund_b": 18912, "gateway": 18913}
UNITS = {name: f"s2rf-{name}-{os.getpid()}" for name in PORTS}
FAILURE_FILE = Path(".local/s2/refund-bench-failure.json")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def assert_ports_free() -> None:
    busy = []
    for name, port in PORTS.items():
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(f"{name}:{port}")
    if busy:
        raise RuntimeError(f"ports busy, refusing to take over: {', '.join(busy)}")


def sh(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)


def start_units(base_url: str) -> None:
    common = ["--setenv=S1_DATABASE_URL=" + base_url, "--setenv=PYTHONPATH=" + os.getcwd() + "/src",
              "--setenv=PAY_MERCHANT_PEM=" + str(KEY_DIR / "merchant_private.pem"),
              "--setenv=PAY_PROVIDER_PUB=" + str(KEY_DIR / "provider_public.pem"),
              "--setenv=PAY_MERCHANT_PUB=" + str(KEY_DIR / "merchant_public.pem"),
              "--setenv=PAY_PROVIDER_PEM=" + str(KEY_DIR / "provider_private.pem"),
              "--setenv=PAY_GATEWAY_URL=http://127.0.0.1:18913/gateway"]
    specs = [("refund_a", "agent_ops.bench.refund_app:app", PORTS["refund_a"]),
             ("refund_b", "agent_ops.bench.refund_app:app", PORTS["refund_b"]),
             ("gateway", "agent_ops.bench.gateway_app:app", PORTS["gateway"])]
    for name, target, port in specs:
        sh(["systemd-run", "--user", "--unit=" + UNITS[name], "--collect",
            "-p", "CPUQuota=100%", "-p", "MemoryMax=512M", "-p", "MemorySwapMax=0",
            "--working-directory=" + os.getcwd(), *common,
            os.getcwd() + "/.venv/bin/python", "-m", "uvicorn", target,
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"])


def stop_units() -> None:
    for unit in UNITS.values():
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, check=False)


async def wait_ready(client: httpx.AsyncClient, name: str, path: str) -> None:
    port = PORTS[name]
    for _ in range(60):
        try:
            response = await client.get(f"http://127.0.0.1:{port}{path}")
            if response.status_code in (200, 404, 405):
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError(f"{name} not ready on {port}")


def notify_params(merchant_no: str, amount: int, trade_no: str) -> dict[str, str]:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key((KEY_DIR / "provider_private.pem").read_bytes(), None)
    params = {"app_id": "bench-app", "seller_id": "bench-seller", "out_trade_no": merchant_no,
              "trade_status": "TRADE_SUCCESS", "trade_no": trade_no,
              "total_amount": f"{amount / 100:.2f}", "notify_id": uuid.uuid4().hex, "sign_type": "RSA2"}
    params["sign"] = base64.b64encode(key.sign(canonical(params, notification=True),
                                               padding.PKCS1v15(), hashes.SHA256())).decode()
    return params


def seed(sync_url: str) -> None:
    """模板库 commerce 为空：补最小夹具（用户/类目/商品/库存），与 pytest 夹具同构。"""
    with psycopg.connect(sync_url) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO commerce.users(id) VALUES(1) ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO commerce.categories(id,name) VALUES(801,'s2-refund-fixture') ON CONFLICT DO NOTHING")
        cur.execute("""INSERT INTO commerce.products(id,sku,title,category_id,price_cents,freight_cents,status)
            VALUES(801,'S2-RF','RefundFixture',801,100,0,'active') ON CONFLICT DO NOTHING""")
        cur.execute("INSERT INTO commerce.inventory(product_id,stock,reserved) VALUES(801,1000,0) ON CONFLICT DO NOTHING")
        cur.execute("SELECT setval(pg_get_serial_sequence('commerce.orders','id'), GREATEST((SELECT coalesce(max(id),1) FROM commerce.orders),1000))")
        conn.commit()


def serialization_merchant():
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key((KEY_DIR / "merchant_private.pem").read_bytes(), None)


def serialization_provider_public():
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_public_key((KEY_DIR / "provider_public.pem").read_bytes())


async def setup_order(base_url: str, sync_url: str, product_id: int) -> dict:
    seed(sync_url)
    commerce = CommerceBackend(base_url)
    support = WorkbenchService(base_url, None)
    order = await commerce.place_order(PlaceOrder(user_id=1, lines=[{"product_id": product_id, "quantity": 1}]),
                                       "s2rf-" + uuid.uuid4().hex)
    merchant = serialization_merchant()
    provider = serialization_provider_public()
    config = PaymentConfig("bench-app", "bench-seller", "http://127.0.0.1:18913/gateway",
                           "http://notify.invalid/payments/notify", merchant, provider, 0.5, 1)
    payments = Payments(base_url, config)
    await payments.create(order["order_id"], 1)
    with psycopg.connect(sync_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT merchant_no, amount_cents FROM payments.payments WHERE order_id=%s", (order["order_id"],))
        merchant_no, amount = cur.fetchone()
    params = notify_params(merchant_no, amount, "T-" + merchant_no)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        response = await client.post("http://127.0.0.1:18911/bench/notify", data=params)
        assert response.status_code == 200, response.text
    ticket = await support.create(SupportRequest(message="申请退货", order_id=str(order["order_id"])),
                                  "s2rf-" + uuid.uuid4().hex, 1)
    claimed = await support.change(ticket.ticket_id, "claim", ticket.revision,
                                   Principal(3, "agent", None, "s2rf-bench"), uuid.uuid4().hex)
    await commerce.aclose(); await payments.aclose(); await support.aclose()
    return {"order_id": order["order_id"], "ticket_id": claimed.ticket_id,
            "revision": claimed.revision}


async def ledger(sync_url: str, order_id: int) -> dict:
    with psycopg.connect(sync_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*), count(*) FILTER (WHERE state='succeeded'), "
                    "count(DISTINCT refund_no) FROM payments.refunds WHERE order_id=%s", (order_id,))
        refunds, succeeded, distinct_no = cur.fetchone()
        cur.execute("SELECT count(*) FROM payments.audit WHERE order_id=%s AND code='REFUND_SUCCEEDED'", (order_id,))
        audits = cur.fetchone()[0]
        cur.execute("SELECT status FROM commerce.orders WHERE id=%s", (order_id,))
        status = cur.fetchone()[0]
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        state = (await client.get("http://127.0.0.1:18913/bench/gateway/_state")).json()
    return {"refund_rows": refunds, "refund_succeeded": succeeded,
            "distinct_refund_no": distinct_no, "refund_succeeded_audits": audits,
            "order_status": status, "gateway": state}


async def run(out_path: str) -> dict:
    env = load_env()
    base = env["S1_DATABASE_URL"]
    base_url = base.rsplit("/", 1)[0]
    assert_ports_free()
    round_name = f"s2rf{int(time.time())}"
    db_created = False
    units_started = False
    result: dict = {"round_db": round_name, "replay_mode": "identical payload+revision (fixed)",
                    "phases": {}}
    try:
        sh(["docker", "exec", "-e", "PGPASSWORD=" + env["S1_PG_PASSWORD"], "s1-postgres",
            "psql", "-U", "ops_owner", "-d", "postgres", "-q", "-c",
            f"CREATE DATABASE {round_name} TEMPLATE s2_template"])
        db_created = True
        round_url = f"{base_url}/{round_name}"
        sync_url = round_url.replace("postgresql+psycopg://", "postgresql://")
        os.environ["S1_DATABASE_URL"] = round_url
        start_units(round_url)
        units_started = True
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            await wait_ready(client, "gateway", "/bench/gateway/_state")
            await wait_ready(client, "refund_a", "/bench/refund")
            await wait_ready(client, "refund_b", "/bench/refund")
        for phase in ("sequential", "concurrent"):
            setup = await setup_order(round_url, sync_url, 801)
            payload = {"order_id": str(setup["order_id"]), "ticket_id": setup["ticket_id"],
                       "revision": str(setup["revision"]), "account_id": "3"}
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                async def call(index: int, payload: dict = payload) -> dict:
                    port = PORTS["refund_a"] if index % 2 == 0 else PORTS["refund_b"]
                    response = await client.post(f"http://127.0.0.1:{port}/bench/refund", data=payload)
                    return {"http_status": response.status_code, "body": response.json()}
                if phase == "sequential":
                    outcomes = [await call(i) for i in range(5)]
                else:
                    outcomes = list(await asyncio.gather(*(call(i) for i in range(5))))
            book = await ledger(sync_url, setup["order_id"])
            refund_nos = {entry["body"].get("refund_no") for entry in outcomes
                          if entry["body"].get("refund_no")}
            gateway_fund_y = {no: info["fund_change_y"] for no, info in book["gateway"]["refunds"].items()}
            checks = {
                "all_replays_http_200": all(o["http_status"] == 200 for o in outcomes),
                "all_replays_same_refund_no": (len(refund_nos) == 1
                                               and all(o["body"].get("refund_no") for o in outcomes)),
                "ledger_single_refund_row": book["refund_rows"] == 1,
                "ledger_single_succeeded": book["refund_succeeded"] == 1,
                "ledger_single_succeeded_audit": book["refund_succeeded_audits"] == 1,
                "order_refunded": book["order_status"] == "refunded",
                "gateway_fund_change_y_exactly_once_per_refund_no":
                    len(refund_nos) == 1
                    and all(book["gateway"]["refunds"].get(no, {}).get("fund_change_y") == 1
                            for no in refund_nos)
                    # 网关 _state 跨阶段累计：任一 refund_no 的 Y 次数都不得 >1
                    and all(v == 1 for v in gateway_fund_y.values()),
                "gateway_request_counts_recorded": {
                    no: book["gateway"]["refunds"].get(no, {}).get("refund_requests") for no in refund_nos},
            }
            result["phases"][phase] = {"setup": {"order_id": setup["order_id"]},
                                       "payload": payload,
                                       "outcomes": outcomes,
                                       "ledger": book,
                                       "gateway_fund_change_y_by_refund_no": gateway_fund_y,
                                       "checks": checks}
        # 汇总判定：硬断言（账本唯一性、重放全 200、同 refund_no、网关 fund_change=Y 恰 1）
        for phase, data in result["phases"].items():
            failed = [k for k, v in data["checks"].items() if v is False]
            assert not failed, f"{phase} checks failed: {failed}"
        result["notes"] = ("重放为完全相同 payload/revision；硬断言含：全部重放 HTTP 200、同一 refund_no、"
                           "每阶段网关 fund_change=Y 恰 1 次/号；409（若有）为当前协议真实行为，"
                           "业务码见 outcomes[].body.code。")
    finally:
        if sys.exc_info()[0] is not None and result.get("phases"):
            # 失败也保留已完成的原始阶段结果（私有），不覆盖既有 run 文件
            FAILURE_FILE.parent.mkdir(parents=True, exist_ok=True)
            FAILURE_FILE.write_text(json.dumps({"partial_result": result,
                                                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                                               ensure_ascii=False, indent=1))
        if units_started:
            stop_units()
        if db_created:
            sh(["docker", "exec", "-e", "PGPASSWORD=" + env["S1_PG_PASSWORD"], "s1-postgres",
                "psql", "-U", "ops_owner", "-d", "postgres", "-q", "-c",
                f"DROP DATABASE {round_name} WITH (FORCE)"])
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=f".local/s2/refund-bench-run3-{int(time.time())}.json")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.out))
        print(json.dumps({"ok": True, "out": args.out,
                          "phases": {p: d["checks"] for p, d in result["phases"].items()}},
                         ensure_ascii=False, indent=1))
    except Exception as error:  # noqa: BLE001 - 失败也要保留原始证据
        FAILURE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 不覆盖 finally 已写入的 partial_result：读取后仅补充 error 字段
        existing: dict = {}
        if FAILURE_FILE.exists():
            try:
                existing = json.loads(FAILURE_FILE.read_text())
            except (ValueError, OSError):
                existing = {}
        if "partial_result" not in existing:
            existing["partial_result"] = result
        existing["error"] = type(error).__name__
        existing["detail"] = str(error)
        existing["error_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        FAILURE_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=1))
        print(f"FAILED: {type(error).__name__}: {error}; evidence at {FAILURE_FILE}", file=sys.stderr)
        raise SystemExit(1)
