"""S2 bench：退款跨实例 HTTP 幂等重放应用。

与 s2_app 不同，商户私钥从 PAY_MERCHANT_PEM 路径加载（本地测试密钥），
保证网关可用对应商户公钥验签。/bench/refund 幂等：同一订单重复调用
复用同一退款号并执行退款；网关侧同号重复退款按幂等处理。
"""

from __future__ import annotations

import os
import typing
from pathlib import Path
from urllib.parse import parse_qs

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.trace import get_tracer

from agent_ops.auth import Principal
from agent_ops.payment_gateway import PaymentConfig, PaymentError
from agent_ops.payments import Payments
from agent_ops.workbench import WorkbenchService

app = FastAPI()

_merchant = typing.cast(
    rsa.RSAPrivateKey,
    serialization.load_pem_private_key(Path(os.environ["PAY_MERCHANT_PEM"]).read_bytes(), None),
)
_provider_public = typing.cast(
    rsa.RSAPublicKey,
    serialization.load_pem_public_key(Path(os.environ["PAY_PROVIDER_PUB"]).read_bytes()),
)
_config = PaymentConfig("bench-app", "bench-seller",
                        os.environ.get("PAY_GATEWAY_URL", "http://127.0.0.1:18913/gateway"),
                        "http://notify.invalid/payments/notify", _merchant,
                        _provider_public, 0.5, 1)
_support = WorkbenchService(os.environ["S1_DATABASE_URL"], get_tracer("s2-refund-bench"))
_payments = Payments(os.environ["S1_DATABASE_URL"], _config, support=_support)


@app.post("/bench/notify")
async def notify(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    form = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
    await _payments.notify(form)
    return {"status": "ok"}


@app.post("/bench/refund")
async def refund(request: Request) -> JSONResponse:
    body = (await request.body()).decode("utf-8")
    form = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
    try:
        int(form["order_id"])  # 仅类型校验；bench 边界，非生产 API 校验
    except (KeyError, ValueError):
        return JSONResponse({"status": "error", "kind": "BadRequest", "code": "ORDER_ID_INVALID"},
                            status_code=400)
    try:
        approved = await _payments.approve_refund(
            form["ticket_id"], int(form["revision"]), _principal(form))
        result = await _payments.execute_refund(approved["refund_no"])
        return JSONResponse({"status": "ok", **result, "refund_no": approved["refund_no"]})
    except Exception as error:  # noqa: BLE001 - bench 边界：如实返回错误类别与业务码
        code = str(error) if isinstance(error, PaymentError) else ""
        return JSONResponse({"status": "error", "kind": type(error).__name__, "code": code},
                            status_code=409)


def _principal(form: dict[str, str]) -> Principal:
    return Principal(int(form.get("account_id", "3")), "agent", None, "s2-bench")
