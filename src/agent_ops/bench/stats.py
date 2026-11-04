"""统计口径：分位数、三次波动、Wilson 区间、配对 bootstrap 比值与 CDF 距离。

纯函数，无第三方依赖；输入为已排序或未排序的浮点样本列表。
"""

from __future__ import annotations

import hashlib
import math
import random
from statistics import mean


def percentile(values: list[float], q: float) -> float:
    """线性插值分位数；q 取 0–100。空样本抛 ValueError。"""
    if not values:
        raise ValueError("empty sample")
    if not 0 <= q <= 100:
        raise ValueError("q must be within 0..100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def nearest_rank(values: list[float], q: float) -> float:
    """Nearest-rank 分位数（probe/检索基准统一口径）；q 取 (0,100]。

    rank = ceil(q/100*N) 截到 [1,N]，返回升序第 rank 个（0 基索引 rank-1）。
    n=20、q=95 时取第 19 个（index 18），不再出现 int(n*.95) 取到最大值的错位。
    """
    if not values:
        raise ValueError("empty sample")
    if not 0 < q <= 100:
        raise ValueError("q must be within (0,100]")
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(q / 100 * len(ordered))))
    return ordered[rank - 1]


def summarize(values: list[float]) -> dict[str, float | int]:
    if not values:
        raise ValueError("empty sample")
    return {
        "count": len(values),
        "mean": mean(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "min": min(values),
        "max": max(values),
    }


def spread_percent(values: list[float]) -> float:
    """spread=(max-min)/mean；均值为 0 时按定义无法判定，抛 ValueError。"""
    average = mean(values)
    if average == 0:
        raise ValueError("zero mean: spread undefined")
    return (max(values) - min(values)) / average * 100


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total <= 0 or not 0 <= successes <= total:
        raise ValueError("invalid counts")
    phat = successes / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _seed_for(seed: int, label: str) -> int:
    digest = hashlib.sha256(f"{seed}:{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def bootstrap_ratio_ci(numerator: list[float], denominator: list[float], *,
                       q: float = 50, resamples: int = 2000, seed: int = 42,
                       confidence: float = 0.95) -> dict[str, float]:
    """独立组样本，分别重采样后计算分位数比值区间；两组不配对。"""
    if not numerator or not denominator or resamples < 100:
        raise ValueError("insufficient samples")
    rng = random.Random(_seed_for(seed, f"ratio-{q}"))
    ratios: list[float] = []
    for _ in range(resamples):
        num = [numerator[rng.randrange(len(numerator))] for _ in range(len(numerator))]
        den = [denominator[rng.randrange(len(denominator))] for _ in range(len(denominator))]
        denominator_q = percentile(den, q)
        if denominator_q <= 0:
            continue
        ratios.append(percentile(num, q) / denominator_q)
    if not ratios:
        raise ValueError("degenerate bootstrap")
    alpha = (1 - confidence) / 2
    return {
        "point": percentile(numerator, q) / percentile(denominator, q),
        "low": percentile(ratios, alpha * 100),
        "high": percentile(ratios, (1 - alpha) * 100),
        "q": q,
        "resamples": float(len(ratios)),
    }


def cdf_max_distance(left: list[float], right: list[float]) -> float:
    """两样本经验 CDF 的上确界距离。"""
    if not left or not right:
        raise ValueError("empty sample")
    a, b = sorted(left), sorted(right)
    i = j = 0
    distance = 0.0
    while i < len(a) and j < len(b):
        if a[i] <= b[j]:
            value = a[i]
        else:
            value = b[j]
        while i < len(a) and a[i] <= value:
            i += 1
        while j < len(b) and b[j] <= value:
            j += 1
        distance = max(distance, abs(i / len(a) - j / len(b)))
    return distance


def within(interval: tuple[float, float] | dict[str, float], bounds: list[float]) -> bool:
    low, high = (interval["low"], interval["high"]) if isinstance(interval, dict) else interval
    return bounds[0] <= low and high <= bounds[1]
