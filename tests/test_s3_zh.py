"""S3 前置 a/b 离线测试：中文词表、确定性生成、查询集一致性与 LRU 口径。"""
import json
from pathlib import Path

import pytest

from agent_ops.zh_catalog import CATEGORY_ZH, VOCAB, description_zh, segment, title_zh

QUERIES = json.loads(Path("evals/s3/zh-queries.json").read_text())


def test_generation_is_deterministic_and_category_consistent() -> None:
    for category_id, spec in CATEGORY_ZH.items():
        for product_id in (1, 42, 99999):
            assert title_zh(product_id, category_id) == title_zh(product_id, category_id)
            assert description_zh(product_id, category_id) == description_zh(product_id, category_id)
            assert spec["head"] in title_zh(product_id, category_id)
            description = description_zh(product_id, category_id)
            assert spec["head"] in description
            assert any(feature in description for feature in spec["features"])


def test_titles_differ_across_categories() -> None:
    heads = {spec["head"] for spec in CATEGORY_ZH.values()}
    assert len(heads) == len(CATEGORY_ZH)


def test_segment_terms_are_in_vocab() -> None:
    for item in QUERIES["queries"]:
        terms = segment(item["query"])
        assert terms, item["id"]
        for term in terms:
            assert term in VOCAB, (item["id"], term)


def test_query_labels_are_covered_by_category_specs() -> None:
    for item in QUERIES["queries"]:
        assert item["expected_categories"], item["id"]
        terms = segment(item["query"])
        heads = [term for term in terms if term in {spec["head"] for spec in CATEGORY_ZH.values()}]
        assert heads, item["id"]
        for category_id in item["expected_categories"]:
            assert CATEGORY_ZH[category_id]["head"] in heads, (item["id"], category_id)
        if "qualifier" in item:
            for category_id in item["expected_categories"]:
                assert item["qualifier"] in CATEGORY_ZH[category_id]["features"], item["id"]


def test_unlisted_text_falls_back_without_crash() -> None:
    assert segment("xyz") == ["x", "y", "z"]
    assert segment("") == []


def test_lru_hit_rate_increases_with_capacity() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "s3_distribution_check", Path("scripts/s3-distribution-check.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    small, large = module.Lru(5), module.Lru(50)
    requests = [1 + (i * i) % 500 for i in range(5000)]
    for key in requests:
        small.request(key)
        large.request(key)
    assert large.hit_rate > small.hit_rate


@pytest.mark.parametrize("query,expected", [("304不锈钢保温杯", ["304不锈钢", "保温杯"]),
                                            ("静音无线鼠标", ["静音", "无线鼠标"])])
def test_known_segmentations(query: str, expected: list[str]) -> None:
    assert segment(query) == expected
