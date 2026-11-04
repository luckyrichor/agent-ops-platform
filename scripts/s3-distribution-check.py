#!/usr/bin/env python3
"""S3 前置 c：真实公开源（UserBehavior pv）与合成访问分布的 LRU 命中率对比。

口径（2026-11-03 按 Codex 评审修正）：
- UserBehavior.csv 无 header（verified-fit-v2.json: header=false）：首行是数据，
  逐行结构校验（5 字段、可解析），不丢首行；记录已读原始行数、pv 行数、
  事件时间范围（UTC）。
- 合成生成器使用项目已冻结的全源 MLE 拟合（.local/datasets/userbehavior-v1/
  verified-fit-v2.json：zipf_exponent，pv 支持 4,068,790），禁止默认猜测斜率；
  本脚本不做任何拟合/调参，只做冻结参数下的对照。
- 对比口径：真实/合成统一为"样本支持集内的商品频次 rank 序列"；合成
  support=样本支持集大小（斜率冻结不重估，支持集归一化差异如实披露）；
  容量定义为该支持集的 1% 与 10%（两序列用相同绝对容量）。
- 校准/验证划分冻结：序列前 60% 校准（仅展示）、后 40% holdout 独立验证；
  验收阈值 ≤5pp 只在 holdout 上评估，且本轮无论结果如何均为
  sample_only_not_formal（正式判定需阶段2冻结协议后全量执行）。
- 20 万 bench 目录投影单独报告（真实 item id 与 bench 1..200000 的交集/
  映射损失），不静默截断、不哈希对齐。
- 完整真实请求序列只写私有文件（.local/s3/），公开输出仅聚合统计与
  私有文件 SHA256；不导出原始请求序列。
- 已知限制：冻结拟合生成器不含时序/用户局部性；LRU 差距可能因此不通过，
  如实报告，不靠对齐同一序列或重放真实频率伪造通过。
"""
import argparse
import hashlib
import json
import random
import time
import zipfile
from collections import Counter, OrderedDict
from pathlib import Path

USERBEHAVIOR = Path(".local/datasets/raw/UserBehavior.csv.zip")
FIT_MANIFEST = Path(".local/datasets/userbehavior-v1/verified-fit-v2.json")
PRIVATE_SAMPLE = Path(".local/s3/distribution-sample.json")
BEHAVIORS = {"pv", "buy", "cart", "fav"}


class Lru:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.store: OrderedDict[int, None] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def request(self, key: int) -> None:
        if key in self.store:
            self.hits += 1
            self.store.move_to_end(key)
        else:
            self.misses += 1
            if len(self.store) >= self.capacity:
                self.store.popitem(last=False)
            self.store[key] = None

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


def sample_userbehavior(lines: int) -> dict:
    if not USERBEHAVIOR.exists():
        return {"available": False, "reason": "raw archive missing"}
    item_requests: list[int] = []
    raw_lines = 0
    malformed = 0
    non_pv = 0
    ts_min: int | None = None
    ts_max: int | None = None
    with zipfile.ZipFile(USERBEHAVIOR) as archive:
        name = archive.namelist()[0]
        with archive.open(name) as stream:
            for raw in stream:  # 无 header：首行即数据，逐行校验
                raw_lines += 1
                fields = raw.decode("utf-8").strip().split(",")
                if len(fields) != 5:
                    malformed += 1
                    continue
                _user, item_id, _category, behavior, ts = fields
                if behavior not in BEHAVIORS:
                    malformed += 1
                    continue
                if behavior != "pv":
                    non_pv += 1
                    continue
                try:
                    item = int(item_id)
                    stamp = int(ts)
                except ValueError:
                    malformed += 1
                    continue
                item_requests.append(item)
                ts_min = stamp if ts_min is None else min(ts_min, stamp)
                ts_max = stamp if ts_max is None else max(ts_max, stamp)
                if len(item_requests) >= lines:
                    break
    summary: dict = {
        "available": True,
        "raw_lines_read": raw_lines,
        "malformed_lines": malformed,
        "non_pv_lines": non_pv,
        "pv_lines": len(item_requests),
        "distinct_items": len(set(item_requests)),
        "event_time_range_utc": ([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_min)),
                                  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_max))]
                                 if ts_min is not None else None),
        "sampling": f"文件顺序前 {lines} 条 pv（确定性，无随机抽样）",
    }
    PRIVATE_SAMPLE.parent.mkdir(parents=True, exist_ok=True)
    PRIVATE_SAMPLE.write_text(json.dumps({"item_requests": item_requests}, separators=(",", ":")))
    summary["private_sample_file"] = str(PRIVATE_SAMPLE)
    summary["private_sample_sha256"] = hashlib.sha256(PRIVATE_SAMPLE.read_bytes()).hexdigest()
    return summary


def ranks_for(requests: list[int]) -> list[int]:
    """支持集内频次 rank 序列（1=最热门）；同支持集对比的统一表达。"""
    counts = Counter(requests)
    order = sorted(counts, key=lambda k: (-counts[k], k))
    rank = {item: index + 1 for index, item in enumerate(order)}
    return [rank[item] for item in requests]


def synthetic_rank_draws(exponent: float, support: int, count: int, seed: int) -> list[int]:
    """冻结指数 Zipf 抽样，返回 rank（1..support）。指数来自冻结拟合，不重估。"""
    rng = random.Random(seed)
    weights = [1.0 / (rank + 1) ** exponent for rank in range(support)]
    total = sum(weights)
    cumulative: list[float] = []
    acc = 0.0
    for weight in weights:
        acc += weight / total
        cumulative.append(acc)
    draws: list[int] = []
    for _ in range(count):
        draw = rng.random()
        low, high = 0, support - 1
        while low < high:
            mid = (low + high) // 2
            if cumulative[mid] < draw:
                low = mid + 1
            else:
                high = mid
        draws.append(low + 1)
    return draws


def lru_report(ranks: list[int], support: int) -> dict:
    result: dict = {"support_size": support}
    for fraction in (0.01, 0.10):
        capacity = max(1, int(support * fraction))
        cache = Lru(capacity)
        for key in ranks:
            cache.request(key)
        result[f"{int(fraction * 100)}pct"] = {"capacity": capacity,
                                               "hit_rate": round(cache.hit_rate, 4)}
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lines", type=int, default=200000, help="pv 抽样行数（本轮 ≤200k，不扫 500 万）")
    parser.add_argument("--seed", type=int, default=20261103)
    parser.add_argument("--calibration-fraction", type=float, default=0.6,
                        help="冻结划分：前段校准占比（仅展示），其余为 holdout")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    if not 0 < args.calibration_fraction < 1:
        raise SystemExit("--calibration-fraction must be in (0,1)")
    if not FIT_MANIFEST.exists():
        raise SystemExit(f"frozen fit manifest missing: {FIT_MANIFEST}")
    fit = json.loads(FIT_MANIFEST.read_text())
    exponent = fit["zipf_exponent"]
    started = time.perf_counter()
    real = sample_userbehavior(args.lines)
    report: dict = {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "real": {"source": "UserBehavior.csv.zip (只读流式抽样，无 header，逐行校验)",
                 "frozen_fit": {"manifest": str(FIT_MANIFEST),
                                "manifest_sha256": hashlib.sha256(FIT_MANIFEST.read_bytes()).hexdigest(),
                                "zipf_exponent": exponent,
                                "fit_method": fit["fit_method"],
                                "fit_support_items_pv": fit["item_popularity_pv"]["items"],
                                "calibration_reason": fit["calibration_reason"]},
                 "ecd_note": "ECD 是对话文本，不是浏览日志，不作为访问分布源",
                 **real},
        "protocol": {"calibration_fraction": args.calibration_fraction,
                     "holdout_evaluated_only": True,
                     "tuning_on_sample": "禁止：本脚本不拟合不调参，指数为冻结值",
                     "support_set": "真实样本 distinct items；合成以同支持集频次 rank 表达",
                     "capacity_definition": "支持集大小的 1% 与 10%（两序列相同绝对容量）",
                     "formal_status": "sample_only_not_formal（本轮不做行数自动达标）"},
        "limitations": [
            "冻结拟合指数来自全源 MLE（支持 4.07M 商品），本对比支持集归一化到样本 distinct 数，归一化差异已披露",
            "生成器不含时序/用户局部性；真实序列有时序局部性，LRU 差距可能因此不通过，须如实报告",
            "样本为文件顺序前 N 条 pv，非随机窗口；不代表全时段",
            "真实 item id 与合成目录无语义映射，不做投影类指标",
        ],
    }
    if real.get("available"):
        requests = json.loads(PRIVATE_SAMPLE.read_text())["item_requests"]
        support = real["distinct_items"]
        split = int(len(requests) * args.calibration_fraction)
        calibration, holdout = requests[:split], requests[split:]
        real_holdout_ranks = ranks_for(holdout)
        synthetic_holdout_ranks = synthetic_rank_draws(exponent, support, len(holdout), args.seed)
        report["calibration_segment"] = {
            "lines": len(calibration), "note": "仅作暖机/窗口展示，不参与验收阈值"}
        real_lru = lru_report(real_holdout_ranks, support)
        synth_lru = lru_report(synthetic_holdout_ranks, support)
        report["holdout_lru"] = {"real": real_lru, "synthetic_frozen_fit": synth_lru}
        report["holdout_gap_pp"] = {
            tier: round(abs(real_lru[tier]["hit_rate"] - synth_lru[tier]["hit_rate"]) * 100, 2)
            for tier in real_lru if tier != "support_size"}
        report["holdout_verdict"] = ("pass_sample_only" if all(g <= 5 for g in report["holdout_gap_pp"].values())
                                     else "fail_reported_not_faked")
        # 2026-11-04：删除数值 ID 交集/"映射损失"指标——真实 item id 与合成 1..200000
        # 无语义映射，数值交集没有语义意义。正式口径见 docs/optimizations/
        # S3-distribution-protocol.md（support 用各自目录规模，禁止样本 distinct 口径）。
        report["projection_to_bench_catalog"] = {
            "removed": True,
            "note": "无语义映射指标已按冻结协议移除；不宣称真实 id 与合成目录可投影"}
    report["status_note"] = "sample_only_and_superseded：本脚本为阶段1探索口径，已被 S3-distribution-protocol.md 正式协议替代"
    report["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    text_out = json.dumps(report, ensure_ascii=False, indent=1)
    print(text_out)
    if args.out:
        Path(args.out).write_text(text_out + "\n")


if __name__ == "__main__":
    main()
