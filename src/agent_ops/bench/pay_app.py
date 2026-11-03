"""S2 bench：接收本地签名支付通知，验证跨进程只入账一次。"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request

from agent_ops.payment_gateway import PaymentConfig
from agent_ops.payments import Payments

app = FastAPI()
_provider_public = serialization.load_pem_public_key(
    Path(os.environ["PAY_PROVIDER_PUB"]).read_bytes()
)
_merchant = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_config = PaymentConfig("bench-app", "bench-seller", "https://gateway.invalid/service",
                        "http://notify.invalid/payments/notify", _merchant,
                        _provider_public, 0.5, 1)
_payments = Payments(os.environ["S1_DATABASE_URL"], _config)


@app.post("/bench/notify")
async def notify(request: Request) -> dict[str, str]:
    form = await request.form()
    await _payments.notify({key: str(value) for key, value in form.items()})
    return {"status": "ok"}
