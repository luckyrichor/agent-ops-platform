from typing import cast

from agent_ops.models import CommerceRequest
from agent_ops.orchestrator import AgentHandler

PRODUCTS = [
    {"id": "kettle-basic", "category": "kettle", "title": "基础水壶", "price_cent": 3990,
     "stock": 10},
    {"id": "kettle-premium", "category": "kettle", "title": "温控水壶", "price_cent": 7990,
     "stock": 5},
    {"id": "kettle-soldout", "category": "kettle", "title": "缺货水壶", "price_cent": 2990,
     "stock": 0},
]


async def catalog(request: CommerceRequest, inputs: dict[str, dict[str, object]]) -> dict[str, object]:
    return {"items": [dict(p) for p in PRODUCTS if p["category"] == request.category and
                      int(cast(int, p["stock"])) > 0], "source": "local-demo-catalog"}


async def pricing(request: CommerceRequest, inputs: dict[str, dict[str, object]]) -> dict[str, object]:
    products = cast(list[dict[str, object]], inputs["catalog"]["items"])
    return {"quotes": [{"id": p["id"], "title": p["title"],
                        "price_cent": int(cast(int, p["price_cent"]))} for p in products]}


async def shipping(request: CommerceRequest, inputs: dict[str, dict[str, object]]) -> dict[str, object]:
    products = cast(list[dict[str, object]], inputs["catalog"]["items"])
    fee = 0 if request.region in {"上海", "北京"} else 1000
    return {"fees": {str(p["id"]): fee for p in products}, "region": request.region}


async def recommendation(request: CommerceRequest,
                         inputs: dict[str, dict[str, object]]) -> dict[str, object]:
    quotes = cast(list[dict[str, object]], inputs["pricing"]["quotes"])
    fees = cast(dict[str, int], inputs["shipping"]["fees"])
    choices = [{"id": q["id"], "title": q["title"],
                "total_cent": int(cast(int, q["price_cent"])) + fees[str(q["id"])]} for q in quotes]
    eligible = [q for q in choices if int(cast(int, q["total_cent"])) <= request.budget_cent]
    eligible.sort(key=lambda q: (int(cast(int, q["total_cent"])), str(q["id"])))
    return {"selected": eligible[0] if eligible else None,
            "reason_code": "LOWEST_TOTAL_IN_BUDGET" if eligible else "NO_MATCH_IN_BUDGET",
            "budget_cent": request.budget_cent}


def demo_agents() -> dict[str, AgentHandler]:
    return {"catalog": catalog, "pricing": pricing, "shipping": shipping,
            "recommendation": recommendation}
