#!/usr/bin/env python3
"""S1 汇总：A/A 噪声、A/B 配对比值、消融、两步演示、W1 分位。

只读测量 JSON，不重跑、不改原始文件。
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = ("1", "100", "1000", "2500")


def load(directory: Path) -> dict[str, dict]:
    return {path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json")) if path.stem != "summary"}


def p95(document: dict, page: str) -> float | None:
    return document["page_latency_ms"].get(page, {}).get("p95")


def spread(values: list[float]) -> float:
    return (max(values) - min(values)) / statistics.mean(values) * 100


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, default=ROOT / "docs" / "measurements" / "2026-11-03-s1")
    args = parser.parse_args()
    data = load(args.dir)

    aa_runs = [doc for label, doc in data.items() if label.startswith("aa-")]
    aa_throughput = [doc["throughput_per_second"] for doc in aa_runs]
    aa_noise = spread(aa_throughput) if len(aa_throughput) >= 2 else 0.0

    ab = {}
    for round_index in range(1, 6):
        base = data.get(f"ab-{round_index}-baseline")
        opt = data.get(f"ab-{round_index}-optimized")
        if not base or not opt:
            continue
        ab[round_index] = {
            "baseline": base["throughput_per_second"],
            "optimized": opt["throughput_per_second"],
            "throughput_ratio": (opt["throughput_per_second"] / base["throughput_per_second"]
                                 if base["throughput_per_second"] else None),
            "claim_p95_ms": {"baseline": base["claim_latency_ms"]["p95"],
                             "optimized": opt["claim_latency_ms"]["p95"]},
            "page_p95_ms": {page: {"baseline": p95(base, page), "optimized": p95(opt, page)}
                            for page in PAGES},
            "duplicates": {"baseline": base["duplicates"], "optimized": opt["duplicates"]},
        }

    ratios = [item["throughput_ratio"] for item in ab.values() if item["throughput_ratio"]]
    deep_ratios = [item["page_p95_ms"]["2500"]["optimized"] / item["page_p95_ms"]["2500"]["baseline"]
                   for item in ab.values()
                   if item["page_p95_ms"]["2500"]["optimized"] and item["page_p95_ms"]["2500"]["baseline"]]

    def group(label: str, suffix: str = "") -> dict[str, float | int | None]:
        runs = [doc for key, doc in data.items()
                if key.startswith(label) and key.endswith(suffix)]
        if not runs:
            return {}
        return {
            "runs": len(runs),
            "throughput_median": statistics.median(d["throughput_per_second"] for d in runs),
            "claim_p95_median": statistics.median(d["claim_latency_ms"]["p95"] for d in runs),
            "page2500_p95_median": statistics.median(
                value for value in (p95(d, "2500") for d in runs) if value is not None),
            "duplicates_total": sum(d["duplicates"] for d in runs),
        }

    summary = {
        "aa": {"runs": len(aa_runs), "throughput_spread_percent": round(aa_noise, 3),
               "throughput_median": statistics.median(aa_throughput) if aa_throughput else None},
        "threshold_percent": max(10.0, round(aa_noise, 3)),
        "ab": ab,
        "throughput_ratio_median": statistics.median(ratios) if ratios else None,
        "deep_page_ratio_median": statistics.median(deep_ratios) if deep_ratios else None,
        "groups": {
            "baseline_A": group("ab-", "-baseline"),
            "optimized_B": group("ab-", "-optimized"),
            "only_keyset": group("abl-keyset-"),
            "only_index": group("abl-index-"),
            "only_skip_locked": group("abl-skip-"),
            "naive_two_step": group("naive-"),
            "w1_baseline_A": group("w1-a-"),
            "w1_optimized_B": group("w1-b-"),
        },
        "naive_duplicates_total": sum(d["duplicates"] for key, d in data.items()
                                     if key.startswith("naive-")),
        "w1": {
            "a_claim_p50_median": statistics.median(
                d["claim_latency_ms"]["p50"] for key, d in data.items() if key.startswith("w1-a-"))
            if any(key.startswith("w1-a-") for key in data) else None,
            "b_claim_p50_median": statistics.median(
                d["claim_latency_ms"]["p50"] for key, d in data.items() if key.startswith("w1-b-"))
            if any(key.startswith("w1-b-") for key in data) else None,
            "a_page2500_p95": [p95(d, "2500") for key, d in data.items() if key.startswith("w1-a-")],
            "b_page2500_p95": [p95(d, "2500") for key, d in data.items() if key.startswith("w1-b-")],
        },
    }
    (args.dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                           encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False)[:2500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
