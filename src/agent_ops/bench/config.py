"""读取并校验 P9 验收配置；口径在正式开跑前固定。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).with_name("acceptance-v2.json")
EXPECTED_VERSION = "p9-acceptance-v2"


@dataclass(frozen=True)
class Acceptance:
    raw: dict[str, Any]

    @property
    def preflight_gates(self) -> dict[str, float]:
        return dict(self.raw["preflight"]["gates"])

    @property
    def tiers(self) -> dict[str, Any]:
        return dict(self.raw["tiers"])

    @property
    def calibration(self) -> dict[str, Any]:
        return dict(self.raw["calibration"])

    @property
    def budget(self) -> dict[str, Any]:
        return dict(self.raw["budget"])


def load(path: Path | None = None) -> Acceptance:
    target = path or CONFIG_PATH
    data = json.loads(target.read_text(encoding="utf-8"))
    if data.get("config_version") != EXPECTED_VERSION:
        raise ValueError("unexpected acceptance config version")
    for section in ("preflight", "calibration", "tiers", "budget", "reporting"):
        if section not in data:
            raise ValueError(f"missing config section: {section}")
    calibration = data["calibration"]
    if calibration["fit_samples_min"] < calibration["validation_samples_min"]:
        raise ValueError("fit/validation sample minima inconsistent")
    if calibration["budget_usd_max"] <= 0:
        raise ValueError("budget must be positive")
    return Acceptance(data)


def tier_seconds(tier: dict[str, Any]) -> dict[str, int]:
    return {
        "warmup": int(tier["warmup_seconds"]),
        "ramp": int(tier["ramp_seconds"]),
        "steady": int(tier["steady_seconds"]),
        "drain": int(tier["drain_seconds"]),
        "hold": int(tier["warmup_seconds"])
        + int(tier["ramp_seconds"])
        + int(tier["steady_seconds"])
        + int(tier["drain_seconds"]),
    }
