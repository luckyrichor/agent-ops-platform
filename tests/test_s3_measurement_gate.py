"""S3 短测量闸门脚本边界测试：确定性均匀抽样、分位数口径、DSN 守卫、导购请求体。"""
import importlib.util
import subprocess
import sys
from pathlib import Path

from agent_ops.zh_catalog import CATEGORY_ZH

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "s3-measurement-gate.py"


def _module():
    spec = importlib.util.spec_from_file_location("s3_measurement_gate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deterministic_ids_uniform_and_unique() -> None:
    gate = _module()
    ids = list(range(200000))
    # 300 计时样本 + 30 warmup：count ≤ N 且步长与 N 互素 → 同一 offset 内不重复
    chosen = gate.deterministic_ids(ids, 300, 0)
    warmup = gate.deterministic_ids(ids, 30, 300 * gate.ID_STRIDE)
    assert len(chosen) == 300 and len(set(chosen)) == 300
    assert len(warmup) == 30 and len(set(warmup)) == 30
    assert set(chosen).isdisjoint(set(warmup))


def test_deterministic_ids_reproducible() -> None:
    gate = _module()
    ids = list(range(200000))
    assert gate.deterministic_ids(ids, 100, 12345) == gate.deterministic_ids(ids, 100, 12345)


def test_latency_stats_matches_nearest_rank() -> None:
    gate = _module()
    latencies = [float(i) for i in range(1, 101)]
    stats = gate.latency_stats(latencies)
    assert stats["p50"] == 50.0 and stats["p95"] == 95.0 and stats["p99"] == 99.0
    assert stats["max"] == 100.0


def test_with_session_guard_appends_options() -> None:
    gate = _module()
    guarded = gate.with_session_guard("postgresql+psycopg://u@h/db?x=1")
    assert "max_parallel_workers_per_gather%3D0" in guarded
    assert "statement_timeout%3D5000" in guarded
    assert guarded.startswith("postgresql+psycopg://u@h/db?x=1&")
    plain = gate.with_session_guard("postgresql+psycopg://u@h/db")
    assert plain.startswith("postgresql+psycopg://u@h/db?")


def test_shopping_body_uses_real_category_heads() -> None:
    gate = _module()
    item = {"query": "304不锈钢保温杯", "expected_categories": [74]}
    body = gate.shopping_body(item)
    assert body.category == CATEGORY_ZH[74]["head"]
    assert body.request == "304不锈钢保温杯"
    assert body.budget_cent == gate.SHOPPING_BUDGET_CENT
    assert body.run_id is not None  # 每请求独立 run_id（幂等口径与真实链路一致）


def test_cli_rejects_invalid_phases_before_database_access() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT), "--phases", "invalid", "--out", "unused.json"], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "invalid or duplicate phases" in result.stderr

def test_cli_preserves_existing_evidence(tmp_path) -> None:
    evidence = tmp_path / "existing.json"
    evidence.write_text("retained")
    result = subprocess.run([sys.executable, str(SCRIPT), "--out", str(evidence)], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert evidence.read_text() == "retained"
    assert "output exists" in result.stderr
