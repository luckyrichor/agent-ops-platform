#!/usr/bin/env python3
"""S1 汇总：A/A 噪声、A/B 配对、消融、留一法、复合索引、W3 队首抢占、两步演示、W1。

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


def med(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


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

    ab2 = {}
    for round_index in range(1, 6):
        base = data.get(f"ab2-{round_index}-baseline")
        opt = data.get(f"ab2-{round_index}-optimized")
        if not base or not opt:
            continue
        ab2[round_index] = {
            "baseline": base["throughput_per_second"],
            "optimized": opt["throughput_per_second"],
            "throughput_ratio": (opt["throughput_per_second"] / base["throughput_per_second"]
                                 if base["throughput_per_second"] else None),
            "claim_p95_ms": {"baseline": base["claim_latency_ms"]["p95"],
                             "optimized": opt["claim_latency_ms"]["p95"]},
            "page2500_p95_ms": {"baseline": p95(base, "2500"), "optimized": p95(opt, "2500")},
            "duplicates": {"baseline": base["duplicates"], "optimized": opt["duplicates"]},
        }

    ratios = [item["throughput_ratio"] for item in ab.values() if item["throughput_ratio"]]
    deep_ratios = [item["page_p95_ms"]["2500"]["optimized"] / item["page_p95_ms"]["2500"]["baseline"]
                   for item in ab.values()
                   if item["page_p95_ms"]["2500"]["optimized"] and item["page_p95_ms"]["2500"]["baseline"]]

    def prefixed(prefix: str, suffix: str = "") -> list[dict]:
        return [doc for key, doc in data.items()
                if key.startswith(prefix) and key.endswith(suffix)]

    def group(prefix: str, suffix: str = "") -> dict[str, float | int | None]:
        runs = prefixed(prefix, suffix)
        if not runs:
            return {}
        return {
            "runs": len(runs),
            "throughput_median": med([d["throughput_per_second"] for d in runs]),
            "claim_p95_median": med([d["claim_latency_ms"]["p95"] for d in runs]),
            "page2500_p95_median": med([v for v in (p95(d, "2500") for d in runs)
                                        if v is not None]),
            "duplicates_total": sum(d["duplicates"] for d in runs),
            "conflicts_total": sum(d["conflicts"] for d in runs),
            "skip_locked_empty_total": sum(d.get("skip_locked_empty", 0) for d in runs),
        }

    loo = {}
    for round_index in range(1, 4):
        full = data.get(f"loo-B-{round_index}")
        if not full:
            continue
        entry: dict[str, object] = {"full_B": full["throughput_per_second"]}
        for name, label in (("no_index", "loo-noindex"), ("no_cursor", "loo-nocursor"),
                            ("no_skip_locked", "loo-noskip")):
            variant = data.get(f"{label}-{round_index}")
            if variant:
                entry[name] = {
                    "throughput": variant["throughput_per_second"],
                    "ratio_to_full": (variant["throughput_per_second"]
                                      / full["throughput_per_second"]),
                    "claim_p95_ms": variant["claim_latency_ms"]["p95"],
                    "page2500_p95_ms": p95(variant, "2500"),
                    "duplicates": variant["duplicates"],
                    "conflicts": variant["conflicts"],
                    "skip_locked_empty": variant.get("skip_locked_empty", 0),
                }
        loo[round_index] = entry

    w3 = {}
    for round_index in range(1, 6):
        entry = {}
        for key in ("a", "k", "b"):
            doc = data.get(f"w3-{key}-{round_index}")
            if doc:
                entry[key] = {
                    "throughput": doc["throughput_per_second"],
                    "claim_p95_ms": doc["claim_latency_ms"]["p95"],
                    "conflicts": doc["conflicts"],
                    "skip_locked_empty": doc.get("skip_locked_empty", 0),
                    "duplicates": doc["duplicates"],
                }
        if entry:
            w3[round_index] = entry

    summary = {
        "aa": {"runs": len(aa_runs), "throughput_spread_percent": round(aa_noise, 3),
               "throughput_median": med(aa_throughput)},
        "threshold_percent": max(10.0, round(aa_noise, 3)),
        "ab": ab,
        "ab2": ab2,
        "ab2_ratio_median": med([item["throughput_ratio"] for item in ab2.values()
                                 if item["throughput_ratio"]]),
        "correctness_no_refill": {
            "A_random": group("corr-a-"),
            "K_head_cas": group("corr-k-"),
            "B_head_skip": group("corr-b-"),
        },
        "throughput_ratio_median": med(ratios),
        "deep_page_ratio_median": med(deep_ratios),
        "groups": {
            "baseline_A": group("ab-", "-baseline"),
            "optimized_B": group("ab-", "-optimized"),
            "only_keyset": group("abl-keyset-"),
            "only_index": group("abl-index-"),
            "only_skip_locked": group("abl-skip-"),
            "naive_two_step": group("naive-"),
            "w1_baseline_A": group("w1-a-"),
            "w1_optimized_B": group("w1-b-"),
            "loo_full_B": group("loo-B-"),
            "loo_no_index": group("loo-noindex-"),
            "loo_no_cursor": group("loo-nocursor-"),
            "loo_no_skip_locked": group("loo-noskip-"),
            "composite_index": group("comp-"),
            "keyset_only_no_index": group("ablk2-"),
            "index_cursor_cas_random": group("ablk3-"),
            "w3_baseline_A": group("w3-a-"),
            "w3_index_cursor_cas": group("w3-k-"),
            "w3_full_B": group("w3-b-"),
        },
        "loo": loo,
        "composite": {
            "throughput_median": group("comp-")["throughput_median"],
            "claim_p95_median": group("comp-")["claim_p95_median"],
            "page2500_p95_median": group("comp-")["page2500_p95_median"],
        },
        "w3": w3,
        "naive_duplicates_total": sum(d["duplicates"] for key, d in data.items()
                                     if key.startswith("naive-")),
        "w1": {
            "a_claim_p50_median": med([d["claim_latency_ms"]["p50"] for d in prefixed("w1-a-")]),
            "b_claim_p50_median": med([d["claim_latency_ms"]["p50"] for d in prefixed("w1-b-")]),
            "a_page2500_p95": [p95(d, "2500") for d in prefixed("w1-a-")],
            "b_page2500_p95": [p95(d, "2500") for d in prefixed("w1-b-")],
        },
        "indexes": [json.loads(path.read_text(encoding="utf-8"))
                    for path in sorted(args.dir.glob("idx-*.json"))],
    }
    (args.dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                           encoding="utf-8")
    print(json.dumps({key: summary[key] for key in
                      ("aa", "threshold_percent", "throughput_ratio_median",
                       "deep_page_ratio_median")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
