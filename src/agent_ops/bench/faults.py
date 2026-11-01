"""确定性故障计划：同一 request_id + seed 每次得到同一类故障。

注入率是实验设定，不代表真实供应商故障率；桩绝不代理到真实 API。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Literal

FaultKind = Literal[
    "none",
    "slow_tail",
    "rate_limit",
    "server_error",
    "connection_reset",
    "disconnect_after_headers",
    "truncated_body",
]

# 固定优先顺序，便于复现与解释；比例按此顺序累积。
ORDER: tuple[FaultKind, ...] = (
    "rate_limit",
    "server_error",
    "connection_reset",
    "disconnect_after_headers",
    "truncated_body",
    "slow_tail",
)


@dataclass(frozen=True)
class FaultProfile:
    rate_limit: float = 0.0
    server_error: float = 0.0
    connection_reset: float = 0.0
    disconnect_after_headers: float = 0.0
    truncated_body: float = 0.0
    slow_tail: float = 0.0
    slow_tail_extra_ms: tuple[float, float] = (0.0, 0.0)
    window_start_seconds: float = 0.0
    window_end_seconds: float | None = None
    name: str = "healthy"
    seed: int = 42
    retry_after_seconds: int = 1

    def __post_init__(self) -> None:
        ratios = [
            self.rate_limit,
            self.server_error,
            self.connection_reset,
            self.disconnect_after_headers,
            self.truncated_body,
            self.slow_tail,
        ]
        if any(ratio < 0 or ratio > 1 for ratio in ratios):
            raise ValueError("fault ratios must be within 0..1")
        if sum(ratios) > 1:
            raise ValueError("fault ratios must not exceed 1 in total")
        if self.slow_tail_extra_ms[0] > self.slow_tail_extra_ms[1]:
            raise ValueError("slow tail range inverted")
        if self.window_end_seconds is not None and (
            self.window_end_seconds <= self.window_start_seconds
        ):
            raise ValueError("fault window inverted")

    def active(self, elapsed_seconds: float) -> bool:
        if elapsed_seconds < self.window_start_seconds:
            return False
        return self.window_end_seconds is None or elapsed_seconds < self.window_end_seconds

    def choose(self, request_id: str) -> FaultKind:
        digest = hashlib.sha256(f"{self.seed}:{request_id}".encode()).digest()
        draw = int.from_bytes(digest[:8], "big") / 2**64
        cumulative = 0.0
        for kind in ORDER:
            cumulative += float(getattr(self, kind))
            if draw < cumulative:
                return kind
        return "none"

    def extra_delay_ms(self, request_id: str) -> float:
        low, high = self.slow_tail_extra_ms
        if high <= 0:
            return 0.0
        digest = hashlib.sha256(f"{self.seed}:tail:{request_id}".encode()).digest()
        draw = int.from_bytes(digest[:8], "big") / 2**64
        return low + (high - low) * draw


@dataclass(frozen=True)
class FaultMatrix:
    """按场景命名的故障组；空矩阵只含 healthy。"""

    profiles: dict[str, FaultProfile] = field(default_factory=lambda: {"healthy": FaultProfile()})

    def get(self, name: str) -> FaultProfile:
        if name not in self.profiles:
            raise KeyError(f"unknown fault profile: {name}")
        return self.profiles[name]


def default_matrix(seed: int = 42) -> FaultMatrix:
    """低频故障组用于验证客户端既有超时/重试/取消路径，不当作供应商故障率。"""
    return FaultMatrix(
        {
            "healthy": FaultProfile(name="healthy", seed=seed),
            "mixed_low": FaultProfile(
                name="mixed_low",
                seed=seed,
                rate_limit=0.01,
                server_error=0.01,
                connection_reset=0.005,
                disconnect_after_headers=0.005,
                truncated_body=0.005,
                slow_tail=0.05,
                slow_tail_extra_ms=(200.0, 1500.0),
            ),
        }
    )
