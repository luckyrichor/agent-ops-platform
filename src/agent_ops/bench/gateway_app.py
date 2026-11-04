"""S2 bench gateway：本地可控签名网关（RSA 测试密钥，不联网、不调真实沙箱）。

只实现退款/查单所需方法；同一 out_request_no 重复退款按幂等处理
（首次 fund_change=Y，其后 N），并记录逐次请求供断言。
"""

from __future__ import annotations

import base64
import json
import os
import typing
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

_merchant_public = typing.cast(
    rsa.RSAPublicKey,
    serialization.load_pem_public_key(Path(os.environ["PAY_MERCHANT_PUB"]).read_bytes()),
)
_provider_private = typing.cast(
    rsa.RSAPrivateKey,
    serialization.load_pem_private_key(Path(os.environ["PAY_PROVIDER_PEM"]).read_bytes(), None),
)
# refund_no -> {"count", "fund_change_y", "amount", "out_trade_no", "trade_no"}
_refunds: dict[str, dict[str, Any]] = {}
_calls: list[dict[str, str]] = []


def _sign(obj: dict[str, Any], method: str) -> str:
    raw = json.dumps(obj, separators=(",", ":"))
    return base64.b64encode(
        _provider_private.sign(raw.encode(), padding.PKCS1v15(), hashes.SHA256())
    ).decode()


@app.post("/{path:path}")
async def service(request: Request, path: str) -> JSONResponse:
    body = (await request.body()).decode("utf-8")
    params = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
    method = params.get("method", "")
    _calls.append({"method": method, "out_request_no": "", "body": ""})
    try:
        biz = json.loads(params.get("biz_content", "{}"))
    except ValueError:
        return JSONResponse({"error": "invalid_biz_content"}, status_code=400)
    try:
        _merchant_public.verify(
            base64.b64decode(params.get("sign", ""), validate=True),
            "&".join(f"{k}={v}" for k, v in sorted(params.items())
                     if k not in {"sign"} and v != "").encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception:  # noqa: BLE001 - 网关边界：任何验签失败一律 400，不外泄原因
        return JSONResponse({"error": "invalid_sign"}, status_code=400)
    out_request_no = str(biz.get("out_request_no", ""))
    out_trade_no = str(biz.get("out_trade_no", ""))
    if _calls:
        _calls[-1]["out_request_no"] = out_request_no
    name = method.replace(".", "_") + "_response"
    obj: dict[str, Any]
    if method == "alipay.trade.refund":
        record = _refunds.get(out_request_no)
        if record is None:
            record = {"count": 0, "fund_change_y": 0, "amount": str(biz.get("refund_amount", "")),
                      "out_trade_no": out_trade_no, "trade_no": "T-" + out_trade_no}
            _refunds[out_request_no] = record
        record["count"] += 1
        fund_change = "Y" if record["count"] == 1 else "N"
        if fund_change == "Y":
            record["fund_change_y"] += 1
        obj = {"code": "10000", "msg": "Success", "out_trade_no": out_trade_no,
               "trade_no": record["trade_no"], "out_request_no": out_request_no,
               "refund_fee": record["amount"], "fund_change": fund_change}
    elif method == "alipay.trade.fastpay.refund.query":
        record = _refunds.get(out_request_no)
        if record is None:
            return JSONResponse({"error": "refund_not_found"}, status_code=400)
        obj = {"code": "10000", "msg": "Success", "out_trade_no": out_trade_no,
               "trade_no": record["trade_no"], "out_request_no": out_request_no,
               "refund_amount": record["amount"], "refund_status": "REFUND_SUCCESS"}
    elif method == "alipay.trade.query":
        obj = {"code": "10000", "msg": "Success", "out_trade_no": out_trade_no,
               "trade_no": "T-" + out_trade_no, "trade_status": "TRADE_SUCCESS",
               "total_amount": str(biz.get("total_amount", ""))}
    else:
        return JSONResponse({"error": "unknown_method"}, status_code=400)
    if _calls:
        _calls[-1]["body"] = obj.get("fund_change", "")
    return JSONResponse({name: obj, "sign": _sign(obj, method)})


@app.get("/bench/gateway/_state")
async def state() -> dict[str, Any]:
    """只读观测：调用计数与每笔退款号（out_request_no）的请求/ fund_change=Y 次数。"""
    return {"calls": len(_calls),
            "refunds": {k: {"refund_requests": v["count"], "fund_change_y": v["fund_change_y"],
                            "out_trade_no": v["out_trade_no"]}
                        for k, v in _refunds.items()},
            "methods": sorted({c["method"] for c in _calls})}
