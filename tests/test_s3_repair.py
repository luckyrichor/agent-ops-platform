"""Codex 返修边界测试：nearest-rank 分位数、检索标签独立性、分布对比口径。"""
import importlib.util
import json
from pathlib import Path

import pytest

from agent_ops.bench.stats import nearest_rank
from agent_ops.zh_catalog import CATEGORY_ZH


def _module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("values,q,expected", [
    ([1.0], 95, 1.0),                    # 单样本：任何分位=该样本
    ([1.0, 2.0], 95, 2.0),               # n=2 p95 -> rank=ceil(1.9)=2
    ([1.0, 2.0], 50, 1.0),               # n=2 p50 -> rank=1（nearest-rank 取下位）
    (list(range(1, 21)), 95, 19.0),      # n=20 p95 -> 第19个，不再取到最大值
    (list(range(1, 21)), 100, 20.0),     # p100 -> 最大值
    (list(range(1, 21)), 50, 10.0),      # n=20 p50 -> 第10个
])
def test_nearest_rank_boundaries(values, q, expected) -> None:
    assert nearest_rank(values, q) == expected


def test_nearest_rank_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        nearest_rank([], 50)
    with pytest.raises(ValueError):
        nearest_rank([1.0], 0)
    with pytest.raises(ValueError):
        nearest_rank([1.0], 101)


def test_retrieval_bench_label_rule_is_independent_of_segmentation() -> None:
    """标签只由查询集结构化字段 + 类目主词表构成，不 import segment()。"""
    source = Path("scripts/s3-retrieval-bench.py").read_text()
    assert "segment(" not in source.replace("segmentation", "")
    queries = json.loads(Path("evals/s3/zh-queries.json").read_text())["queries"]
    for item in queries:
        heads = {CATEGORY_ZH[c]["head"] for c in item["expected_categories"]}
        assert heads  # 每条查询有独立于实现的主词标签
        if "qualifier" in item:
            assert all(item["qualifier"] in CATEGORY_ZH[c]["features"]
                       for c in item["expected_categories"])


def test_distribution_check_has_no_default_zipf_slope() -> None:
    source = Path("scripts/s3-distribution-check.py").read_text()
    assert "1.1" not in source
    assert "zipf-slope" not in source
    assert "zipf_exponent" in source  # 斜率只来自冻结拟合 manifest


def test_distribution_check_keeps_first_line_as_data() -> None:
    module = _module("s3_dist", "scripts/s3-distribution-check.py")
    source = Path("scripts/s3-distribution-check.py").read_text()
    assert "header is None" not in source  # 无 header：首行即数据
    # 行为校验表完整
    assert set(module.BEHAVIORS) == {"pv", "buy", "cart", "fav"}


def test_lru_definition_matches_between_real_and_synthetic() -> None:
    module = _module("s3_dist", "scripts/s3-distribution-check.py")
    cache = module.Lru(3)
    for key in [1, 2, 3, 4, 2, 5, 2]:
        cache.request(key)
    assert cache.hits == 2 and cache.misses == 5
    report = module.lru_report([1, 1, 2, 3], 10)
    assert report["1pct"]["capacity"] == 1 or report["1pct"]["capacity"] > 0
