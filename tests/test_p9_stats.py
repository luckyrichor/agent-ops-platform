from __future__ import annotations

import pytest

from agent_ops.bench import stats


def test_percentile_linear_interpolation() -> None:
    assert stats.percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert stats.percentile([10], 99) == 10
    assert stats.percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)


def test_percentile_rejects_empty_and_bad_q() -> None:
    with pytest.raises(ValueError):
        stats.percentile([], 50)
    with pytest.raises(ValueError):
        stats.percentile([1], 101)


def test_summarize_reports_distribution_not_only_mean() -> None:
    summary = stats.summarize([1, 2, 3, 100])
    assert summary["count"] == 4
    assert summary["p99"] > summary["p95"] > summary["p50"]
    assert summary["max"] == 100


def test_spread_matches_formula_and_guards_zero_mean() -> None:
    assert stats.spread_percent([9, 10, 11]) == pytest.approx(20)
    with pytest.raises(ValueError):
        stats.spread_percent([0, 0, 0])


def test_wilson_interval_bounds() -> None:
    low, high = stats.wilson_interval(0, 10)
    assert low == 0 and 0 < high < 0.35
    low, high = stats.wilson_interval(10, 10)
    assert 0.65 < low < 1 and high == 1


def test_bootstrap_ratio_is_deterministic() -> None:
    left = [100 + index for index in range(200)]
    right = [100 + index * 2 for index in range(200)]
    first = stats.bootstrap_ratio_ci(left, right, q=50, resamples=200, seed=7)
    second = stats.bootstrap_ratio_ci(left, right, q=50, resamples=200, seed=7)
    assert first == second
    assert first["low"] <= first["point"] <= first["high"]
    assert first["point"] < 1


def test_cdf_distance_extremes() -> None:
    assert stats.cdf_max_distance([1, 2, 3], [1, 2, 3]) == 0
    assert stats.cdf_max_distance([1, 1, 1], [10, 10, 10]) == 1


def test_within_checks_interval_not_point() -> None:
    assert stats.within({"low": 0.95, "high": 1.05}, [0.9, 1.1])
    assert not stats.within({"low": 0.8, "high": 1.05}, [0.9, 1.1])
