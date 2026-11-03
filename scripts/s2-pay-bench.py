#!/usr/bin/env python3
"""跨进程支付回调：同一 notify_id 并发 5 次打两个实例，只入账一次。"""
import asyncio
import base64
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_ops.commerce_backend import CommerceBackend, PlaceOrder
from agent_ops.payment_api import from_env  # noqa: F401  (import side-effect check)
from agent_ops.payment_gateway import PaymentConfig, canonical
from agent_ops.payments import Payments

KEY_DIR = Path(".local/s2/keys")
URL = os.environ["S1_DATABASE_URL"]
SYNC_URL = URL.replace("postgresql+psycopg://", "postgresql://")


def sign(params: dict[str, str]) -> str:
    key = serialization.load_pem_private_key((KEY_DIR / "provider_private.pem").read_bytes(), None)
    return base64.b64encode(key.sign(canonical(params, notification=True), padding.PKCS1v15(),
                                     hashes.SHA256())).decode()


async def main() -> None:
    merchant = serialization.load_pem_private_key((KEY_DIR / "merchant_private.pem").read_bytes(), None)
    provider = serialization.load_pem_public_key((KEY_DIR / "provider_public.pem").read_bytes())
    config = PaymentConfig("bench-app", "bench-seller", "https://gateway.invalid/service",
                           "http://notify.invalid/payments/notify", merchant, provider, 0.5, 1)
    commerce = CommerceBackend(URL)
    payments = Payments(URL, config)
    order = await commerce.place_order(PlaceOrder(user_id=1, lines=[{"product_id": 801, "quantity": 1}]),
                                       f"s2pay-{datetime.now(UTC).timestamp()}")
    await payments.create(order["order_id"], 1)
    with psycopg.connect(SYNC_URL) as c, c.cursor() as cur:
        cur.execute("SELECT merchant_no,amount_cents FROM payments.payments WHERE order_id=%s", (order["order_id"],))
        merchant_no, amount = cur.fetchone()
    notify_id = f"s2-notify-{merchant_no}"
    params = {"app_id": "bench-app", "seller_id": "bench-seller",
              "out_trade_no": merchant_no, "trade_status": "TRADE_SUCCESS",
              "trade_no": f"s2-trade-{merchant_no}", "total_amount": f"{amount/100:.2f}",
              "notify_id": notify_id, "sign_type": "RSA2"}
    params["sign"] = sign(params)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        async def one(port: int, index: int) -> int:
            response = await client.post(f"http://127.0.0.1:{port}/bench/notify", data=params)
            return response.status_code
        results = await asyncio.gather(*(one(18911 if i % 2 == 0 else 18912, i) for i in range(5)))
    with psycopg.connect(SYNC_URL) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM payments.audit WHERE order_id=%s AND code='PAYMENT_CAPTURED'", (order["order_id"],))
        captured = cur.fetchone()[0]
        cur.execute("SELECT state FROM payments.payments WHERE order_id=%s", (order["order_id"],))
        state = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM payments.notifications WHERE notification_id=%s", (notify_id,))
        notes = cur.fetchone()[0]
    print(json.dumps({"statuses": results, "captured_audits": captured, "payment_state": state,
                      "notifications": notes, "order_id": order["order_id"]}))
    await commerce.aclose(); await payments.aclose()


if __name__ == "__main__":
    asyncio.run(main())
