from __future__ import annotations

from pathlib import Path

import pytest

from agent_ops.bench import schema


def record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "experiment_id": "p9-test",
        "run_index": 1,
        "scenario": {
            "kind": "business",
            "connections": 100,
            "slow_reader_ratio": 0.0,
            "seed": 42,
            "timings": {"warmup": 1, "ramp": 1, "steady": 1, "drain": 1, "hold": 4},
            "profile": "stub",
        },
        "source": {"commit": "abc", "dirty": False, "generator": "mac"},
        "started_at": "2026-11-01T00:00:00+00:00",
        "status": "completed",
        "target_connections": 100,
        "achieved_connections": 100,
        "healthy_connections": 100,
        "latency_ms": {"connect": {"p95": 1.0}, "ttfb": {"p95": 2.0},
                       "first_event": {"p95": 3.0, "p99": 4.0}, "full": {"p95": 5.0}},
        "errors": {},
        "throughput": {"completed": 1000, "steady_seconds": 180, "per_second": 5.5},
        "resources": {"peak_memory_mib": 200.0},
        "protection": {"aborted": False, "reasons": []},
        "completed_requests": 1000,
    }


def test_valid_record_passes() -> None:
    schema.validate_run(record())


def test_public_record_rejects_host_details() -> None:
    document = record()
    document["scenario"]["profile"] = "http://10.0.0.15:18901"  # type: ignore[index]
    with pytest.raises(ValueError):
        schema.validate_run(document, public=True)


def test_completed_requires_reaching_target() -> None:
    document = record()
    document["achieved_connections"] = 90
    with pytest.raises(ValueError):
        schema.validate_run(document)


def test_write_once_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "run.json"
    schema.write_once(target, record())
    with pytest.raises(FileExistsError):
        schema.write_once(target, record())


def test_aggregate_flags_unstable_tier() -> None:
    first, second, third = record(), record(), record()
    second["throughput"] = {"completed": 1, "steady_seconds": 180, "per_second": 1.0}
    verdict = schema.aggregate([first, second, third], spread_max_percent=10)
    assert verdict["passed"] is False
    assert "throughput_per_second" in verdict["unstable_metrics"]


def test_aggregate_requires_three_runs() -> None:
    verdict = schema.aggregate([record()], spread_max_percent=10)
    assert verdict == {"passed": False, "reason": "THREE_RUNS_REQUIRED", "runs": 1}
