#!/usr/bin/env python3
"""S3 访问分布正式复核（协议 docs/optimizations/S3-distribution-protocol.md，2026-11-03 冻结）。

阶段：
  A  冻结旧指数 a=0.7831831153、纯 Zipf、support=200000，校准窗口对照；
  B  A 不过时，Zipf+uniform 混合在粗网格+预声明细网格上仅用校准窗口拟合；
  C  选定参数+协议 hash 写不可覆盖冻结文件后，3 seed 验证窗口判定
     （通过=3 seed × 两档 |gap| 全部 ≤5pp）。
  诊断 support=真实 4.07M 只作参考，不替代 support=200000 主闸。

资源：1 CPU / 1GiB（外层 systemd scope）；MemAvailable<2GiB 中止；总计算预算 15 分钟。
隐私：真实请求序列只写 .local/s3/；公开输出仅聚合+hash。无数值 ID 交集指标。
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import random
import sys
import time
import zipfile
from array import array
from collections import OrderedDict
from pathlib import Path

PROTOCOL_DOC = Path("docs/optimizations/S3-distribution-protocol.md")
USERBEHAVIOR = Path(".local/datasets/raw/UserBehavior.csv.zip")
FIT_MANIFEST = Path(".local/datasets/userbehavior-v1/verified-fit-v2.json")
PRIVATE_SAMPLE = Path(".local/s3/distribution-formal-pv1m.json")
FREEZE_FILE = Path(".local/s3/distribution-freeze-v1.json")

PV_TARGET = 1_000_000
WINDOW = 500_000
WARMUP = 100_000
REAL_CATALOG = 4_068_790          # manifest item_popularity_pv.items（冻结）
SYN_CATALOG = 200_000             # 本项目 S3 实际合成 catalog
CAP_FRACTIONS = (0.01, 0.10)
TIER_KEYS = ("1pct", "10pct")
SEED_CALIB = 20261103
SEEDS_VALID = (20261104, 20261105, 20261106)
GRID_A = [round(0.6 + 0.05 * i, 4) for i in range(11)]   # 0.60..1.10
GRID_W = [0.5, 0.75, 1.0]
BUDGET_SECONDS = 900.0
BEHAVIORS = {"pv", "buy", "cart", "fav"}


def mem_available_mib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    return -1.0


def check_budget(started: float) -> None:
    if time.perf_counter() - started > BUDGET_SECONDS:
        raise TimeoutError(f"compute budget {BUDGET_SECONDS:.0f}s exceeded; keeping partial progress")
    if mem_available_mib() >= 0 and mem_available_mib() < 2048:
        raise MemoryError(f"host MemAvailable <2GiB ({mem_available_mib():.0f} MiB); aborting")


def read_first_pv(count: int) -> dict:
    """文件顺序前 count 条 pv，无 header 首行保留，逐行结构校验。"""
    items: list[int] = []
    raw_lines = malformed = non_pv = 0
    ts_min = ts_max = None
    with zipfile.ZipFile(USERBEHAVIOR) as archive, archive.open(archive.namelist()[0]) as stream:
            for raw in stream:
                raw_lines += 1
                fields = raw.decode("utf-8").strip().split(",")
                if len(fields) != 5:
                    malformed += 1
                    continue
                _user, item_id, _cat, behavior, ts = fields
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
                items.append(item)
                ts_min = stamp if ts_min is None else min(ts_min, stamp)
                ts_max = stamp if ts_max is None else max(ts_max, stamp)
                if len(items) >= count:
                    break
    if len(items) < count:
        raise RuntimeError(f"only {len(items)} pv lines available (<{count})")
    PRIVATE_SAMPLE.parent.mkdir(parents=True, exist_ok=True)
    PRIVATE_SAMPLE.write_text(json.dumps({"pv_item_ids_first_1m": items}, separators=(",", ":")))
    return {"raw_lines_read": raw_lines, "malformed_lines": malformed, "non_pv_lines": non_pv,
            "pv_lines": len(items), "first_line_kept": True,
            "event_time_range_utc": [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_min)),
                                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_max))],
            "sampling": f"文件顺序前 {count} 条 pv（确定性，无随机抽样，无 header 首行为数据）",
            "private_sample_file": str(PRIVATE_SAMPLE),
            "private_sample_sha256": hashlib.sha256(PRIVATE_SAMPLE.read_bytes()).hexdigest()}


class Lru:
    __slots__ = ("capacity", "hits", "measured", "store")

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.store: OrderedDict[int, None] = OrderedDict()
        self.hits = 0
        self.measured = 0

    def run(self, keys: list[int], warmup: int) -> float:
        store = self.store
        hits = 0
        measured = 0
        for index, key in enumerate(keys):
            if key in store:
                store.move_to_end(key)
                if index >= warmup:
                    hits += 1
                    measured += 1
            else:
                if index >= warmup:
                    measured += 1
                if len(store) >= self.capacity:
                    store.popitem(last=False)
                store[key] = None
        self.hits = hits
        self.measured = measured
        return hits / measured if measured else 0.0


class ZipfMixture:
    """以概率 w 抽 Zipf(a, 1..support)，否则均匀抽 rank；CDF 一次构建，bisect 抽样。"""

    def __init__(self, exponent: float, support: int, weight_zipf: float) -> None:
        self.a = exponent
        self.support = support
        self.w = weight_zipf
        weights = array("d")
        acc = 0.0
        for rank in range(1, support + 1):
            acc += 1.0 / rank ** exponent
            weights.append(acc)
        total = acc
        self.cdf = array("d", (value / total for value in weights))

    def draw(self, rng: random.Random) -> int:
        if rng.random() < self.w:
            return bisect.bisect_left(self.cdf, rng.random()) + 1
        return rng.randint(1, self.support)

    def draws(self, count: int, seed: int) -> list[int]:
        rng = random.Random(seed)
        cdf = self.cdf
        w = self.w
        support = self.support
        out = []
        append = out.append
        for _ in range(count):
            if rng.random() < w:
                append(bisect.bisect_left(cdf, rng.random()) + 1)
            else:
                append(rng.randint(1, support))
        return out


def real_lru_hits(pv: list[int], offset: int) -> dict:
    """真实窗口：独立冷启动，前 WARMUP warmup，余下测量；容量=真实完整 catalog 的 1%/10%。"""
    window = pv[offset:offset + WINDOW]
    result = {"window_events": len(window), "warmup_events": WARMUP,
              "measured_events": len(window) - WARMUP}
    for fraction in CAP_FRACTIONS:
        capacity = max(1, int(REAL_CATALOG * fraction))
        result[f"{int(fraction * 100)}pct"] = {"capacity": capacity,
                                               "hit_rate": round(Lru(capacity).run(window, WARMUP), 6)}
    return result


def synth_lru_hits(gen: ZipfMixture, seed: int) -> dict:
    events = gen.draws(WINDOW, seed)
    result = {"window_events": len(events), "warmup_events": WARMUP,
              "measured_events": WINDOW - WARMUP, "generator_seed": seed}
    for fraction in CAP_FRACTIONS:
        capacity = max(1, int(SYN_CATALOG * fraction))
        result[f"{int(fraction * 100)}pct"] = {"capacity": capacity,
                                               "hit_rate": round(Lru(capacity).run(events, WARMUP), 6)}
    return result


def gaps(real: dict, synth: dict) -> dict:
    return {tier: round(abs(real[tier]["hit_rate"] - synth[tier]["hit_rate"]) * 100, 4)
            for tier in TIER_KEYS}


def max_gap(gap: dict) -> float:
    return max(gap[t] for t in TIER_KEYS)


def param_key(a: float, w: float) -> tuple[float, float]:
    return (round(a, 6), round(w, 6))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-diagnostic", action="store_true",
                        help="跳过 support=真实4.07M 诊断（省预算）")
    parser.add_argument("--out", default=".local/s3/distribution-formal.json")
    args = parser.parse_args()
    if FREEZE_FILE.exists() or Path(args.out).exists():
        raise SystemExit("existing freeze/result preserved; use an isolated directory for a new protocol")
    started = time.perf_counter()
    if not FIT_MANIFEST.exists() or not PROTOCOL_DOC.exists() or not USERBEHAVIOR.exists():
        raise SystemExit("frozen manifest / protocol doc / source archive missing")
    fit = json.loads(FIT_MANIFEST.read_text())
    frozen_a = fit["zipf_exponent"]
    assert fit["item_popularity_pv"]["items"] == REAL_CATALOG, "manifest support drifted"
    report: dict = {
        "collected_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "collected_at_beijing": time.strftime("%Y-%m-%dT%H:%M:%S+08:00",
                                              time.gmtime(time.time() + 8 * 3600)),
        "protocol": {"doc": str(PROTOCOL_DOC),
                     "doc_sha256": hashlib.sha256(PROTOCOL_DOC.read_bytes()).hexdigest(),
                     "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                     "manifest_sha256": hashlib.sha256(FIT_MANIFEST.read_bytes()).hexdigest()},
        "frozen_fit": {"zipf_exponent": frozen_a, "support_items_pv": REAL_CATALOG,
                       "syn_catalog": SYN_CATALOG,
                       "capacity_rule": "各自完整 catalog 的 1%/10%（不同绝对容量，明确限制）"},
        "no_semantic_mapping": ("真实 item id 与合成 rank/1..200000 无语义映射；"
                                 "不计算数值 ID 交集或映射损失类指标"),
        "stages": {},
    }

    print("[1/4] reading first 1,000,000 pv ...", flush=True)
    sample = read_first_pv(PV_TARGET)
    pv = json.loads(PRIVATE_SAMPLE.read_text())["pv_item_ids_first_1m"]
    report["sampling"] = sample
    check_budget(started)

    real_cal = real_lru_hits(pv, 0)
    print(f"      real calibration {real_cal['1pct']['hit_rate']:.4f}/{real_cal['10pct']['hit_rate']:.4f}", flush=True)
    report["real"] = {"calibration_window": real_cal}

    # ---- 阶段 A：冻结旧指数、support=200000 校准对照 ----
    print("[2/4] stage A: frozen exponent calibration ...", flush=True)
    check_budget(started)
    gen_old = ZipfMixture(frozen_a, SYN_CATALOG, 1.0)
    synth_cal_old = synth_lru_hits(gen_old, SEED_CALIB)
    gap_old = gaps(real_cal, synth_cal_old)
    report["stages"]["A_frozen_calibration"] = {
        "generator": {"exponent": frozen_a, "w_zipf": 1.0, "support": SYN_CATALOG, "seed": SEED_CALIB},
        "synthetic_calibration": synth_cal_old, "gap_pp": gap_old,
        "calibration_pass_le_5pp": all(g <= 5.0 for g in gap_old.values())}

    best_params = (frozen_a, 1.0)
    if not report["stages"]["A_frozen_calibration"]["calibration_pass_le_5pp"]:
        # ---- 阶段 B：Zipf+uniform 混合，仅校准窗口调参 ----
        print("[3/4] stage B: mixture grid on calibration only ...", flush=True)
        candidates: list[tuple[float, float, float]] = []  # (max_gap, a, w)
        for a in GRID_A:
            for w in GRID_W:
                check_budget(started)
                gen = ZipfMixture(a, SYN_CATALOG, w)
                gap = gaps(real_cal, synth_lru_hits(gen, SEED_CALIB))
                candidates.append((max_gap(gap), a, w))
                print(f"      a={a:.2f} w={w:.2f} max_gap={candidates[-1][0]:.2f}pp", flush=True)
        candidates.sort()
        best_a, best_w = candidates[0][1], candidates[0][2]
        # 预声明局部细网格（一次，仅校准）
        fine: list[tuple[float, float, float]] = []
        for a in {round(best_a + d, 4) for d in (-0.025, -0.0125, 0.0, 0.0125, 0.025)
                  if 0.6 <= round(best_a + d, 4) <= 1.1}:
            for w in {round(min(1.0, max(0.5, best_w + d)), 4)
                      for d in (-0.125, 0.0, 0.125)}:
                check_budget(started)
                gen = ZipfMixture(a, SYN_CATALOG, w)
                gap = gaps(real_cal, synth_lru_hits(gen, SEED_CALIB))
                fine.append((max_gap(gap), a, w))
                print(f"      fine a={a:.4f} w={w:.4f} max_gap={fine[-1][0]:.2f}pp", flush=True)
        fine.sort()
        pool = candidates + fine
        pool.sort()
        best_params = (pool[0][1], pool[0][2])
        report["stages"]["B_mixture_calibration"] = {
            "grid": {"exponent": GRID_A, "w": GRID_W, "seed": SEED_CALIB,
                     "objective": "两档绝对gap最大值最小；平局按(a,w)字典序",
                     "fine_grid": "a*±0.025(步0.0125)、w*±0.125 截断到 [0.6,1.1]x[0.5,1.0]；预声明一次，仅校准"},
            "candidates": [{"max_gap_pp": round(m, 4), "a": a, "w": w} for m, a, w in pool],
            "selected": {"exponent": best_params[0], "w_zipf": best_params[1],
                         "calibration_max_gap_pp": round(pool[0][0], 4)},
            "tuning_saw_validation": False}
    else:
        report["stages"]["B_mixture_calibration"] = {
            "skipped": True, "reason": "stage A calibration passed; frozen exponent kept"}

    # ---- 阶段 C：冻结文件（不可覆盖）+ 验证窗口 ----
    print("[4/4] stage C: freeze params, then validation ...", flush=True)
    protocol_hash = hashlib.sha256(PROTOCOL_DOC.read_bytes()).hexdigest()
    freeze_payload = {"selected": {"exponent": best_params[0], "w_zipf": best_params[1],
                                   "support": SYN_CATALOG},
                      "protocol_doc_sha256": protocol_hash,
                      "protocol_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      "frozen_fit_manifest_sha256": hashlib.sha256(FIT_MANIFEST.read_bytes()).hexdigest(),
                      "validation_seeds": list(SEEDS_VALID),
                      "pass_rule": "3 seeds × 两档 |gap| 全部 ≤5pp",
                      "frozen_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        fd = os.open(FREEZE_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(freeze_payload, ensure_ascii=False, indent=1))
    except FileExistsError:
        raise SystemExit(f"freeze file already exists, preserved: {FREEZE_FILE}") from None
    os.chmod(FREEZE_FILE, 0o444)
    report["freeze_file"] = {"path": str(FREEZE_FILE),
                             "sha256": hashlib.sha256(FREEZE_FILE.read_bytes()).hexdigest(),
                             "payload": freeze_payload}

    check_budget(started)
    real_val = real_lru_hits(pv, WINDOW)
    report["real"]["validation_window"] = real_val
    gen_final = ZipfMixture(best_params[0], SYN_CATALOG, best_params[1])
    per_seed = []
    for seed in SEEDS_VALID:
        check_budget(started)
        synth_val = synth_lru_hits(gen_final, seed)
        gap = gaps(real_val, synth_val)
        per_seed.append({"seed": seed, "synthetic_validation": synth_val,
                         "gap_pp": gap, "seed_pass_le_5pp": all(g <= 5.0 for g in gap.values())})
        print(f"      seed={seed} gap={gap['1pct']:.2f}/{gap['10pct']:.2f}pp", flush=True)
    mean_synth = {tier: round(sum(s["synthetic_validation"][tier]["hit_rate"]
                                  for s in per_seed) / len(per_seed), 6) for tier in TIER_KEYS}
    report["stages"]["C_validation"] = {
        "seeds": per_seed,
        "synthetic_mean_hit": mean_synth,
        "real_hit": {tier: real_val[tier]["hit_rate"] for tier in TIER_KEYS},
        "mean_gap_pp": {tier: round(abs(mean_synth[tier] - real_val[tier]["hit_rate"]) * 100, 4)
                        for tier in TIER_KEYS},
        "verdict": ("pass_formal_per_protocol" if all(s["seed_pass_le_5pp"] for s in per_seed)
                    else "fail_reported_not_faked"),
        "next_step_options_if_fail": [
            "接受失败：正式 S3 实验不用该生成器宣称分布一致性，S3-design 标注前置未过",
            "修订协议（需 Codex 批准并重新冻结，例如引入时序/会话局部性），不得看本轮验证结果调参",
            "改用 rank 分布对比等非 LRU 口径（需 Codex 批准）"],
    }

    # ---- 诊断（可选）：support=真实 4.07M 同支持集对照，不替代主闸 ----
    if not args.skip_diagnostic:
        try:
            check_budget(started)
            diag_gen = ZipfMixture(best_params[0], REAL_CATALOG, best_params[1])
            diag_full = {}
            for fraction in CAP_FRACTIONS:
                capacity = max(1, int(REAL_CATALOG * fraction))
                diag_full[f"{int(fraction * 100)}pct"] = {
                    "capacity": capacity,
                    "hit_rate": round(Lru(capacity).run(diag_gen.draws(WINDOW, SEED_CALIB), WARMUP), 6)}
            report["diagnostic_real_support"] = {
                "support": REAL_CATALOG, "note": "仅诊断参考，不替代 support=200000 主闸",
                "synthetic_calibration_support_real": diag_full,
                "gap_pp_vs_real_calibration": {
                    tier: round(abs(diag_full[tier]["hit_rate"] - real_cal[tier]["hit_rate"]) * 100, 4)
                    for tier in TIER_KEYS}}
        except (TimeoutError, MemoryError) as error:
            report["diagnostic_real_support"] = {"skipped": type(error).__name__, "detail": str(error)}

    report["elapsed_seconds"] = round(time.perf_counter() - started, 1)
    report["resource_note"] = ("1 CPU / 1GiB scope；预算 900s 仅计算；PG 未单独限 CPU 的事实不在本脚本范围")
    text_out = json.dumps(report, ensure_ascii=False, indent=1)
    Path(args.out).write_text(text_out + "\n")
    print(text_out)


if __name__ == "__main__":
    try:
        main()
    except TimeoutError as error:
        print(f"BUDGET: {error}", file=sys.stderr)
        raise SystemExit(2)
    except MemoryError as error:
        print(f"MEMORY: {error}", file=sys.stderr)
        raise SystemExit(3)
