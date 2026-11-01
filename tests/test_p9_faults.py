from __future__ import annotations

import pytest

from agent_ops.bench.faults import FaultProfile, default_matrix


def test_choice_is_deterministic_for_same_request() -> None:
    profile = default_matrix(seed=11).get("mixed_low")
    assert profile.choose("abc") == profile.choose("abc")


def test_ratios_are_respected_over_many_requests() -> None:
    profile = FaultProfile(seed=3, rate_limit=0.2, server_error=0.1)
    kinds = [profile.choose(f"req-{index}") for index in range(4000)]
    assert kinds.count("rate_limit") / len(kinds) == pytest.approx(0.2, abs=0.02)
    assert kinds.count("server_error") / len(kinds) == pytest.approx(0.1, abs=0.02)
    assert kinds.count("none") > 0.65 * len(kinds)


def test_total_ratio_above_one_is_rejected() -> None:
    with pytest.raises(ValueError):
        FaultProfile(rate_limit=0.6, server_error=0.6)


def test_window_gates_faults() -> None:
    profile = FaultProfile(server_error=1.0, window_start_seconds=10, window_end_seconds=20)
    assert profile.active(9) is False
    assert profile.active(15) is True
    assert profile.active(20) is False


def test_slow_tail_extra_delay_in_range() -> None:
    profile = FaultProfile(slow_tail=1.0, slow_tail_extra_ms=(100, 300), seed=5)
    delays = [profile.extra_delay_ms(f"id-{index}") for index in range(100)]
    assert all(100 <= delay <= 300 for delay in delays)
    assert len(set(delays)) > 50
