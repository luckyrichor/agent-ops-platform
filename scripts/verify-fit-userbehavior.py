"""Verify the complete owner-provided archive and fit aggregate workload distributions.

Raw IDs stay private. No sampling, guessed fallback, login, or third party download.
"""
import argparse
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import time
import zipfile


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def rank_fit(counts):
    frequencies = sorted(counts.values(), reverse=True)
    n, total = len(frequencies), sum(frequencies)
    if n < 2:
        raise ValueError('at least two observed items required for rank fit')
    mx = math.fsum(math.log(i) for i in range(1, n + 1)) / n
    my = math.fsum(math.log(f) for f in frequencies) / n
    xx = math.fsum((math.log(i) - mx) ** 2 for i in range(1, n + 1))
    yy = math.fsum((math.log(f) - my) ** 2 for f in frequencies)
    xy = math.fsum((math.log(i) - mx) * (math.log(f) - my)
                   for i, f in enumerate(frequencies, 1))
    slope = xy / xx
    exponent = -slope
    norm = math.fsum(i ** -exponent for i in range(1, n + 1))
    actual = predicted = max_cdf_gap = 0.0
    for rank, count in enumerate(frequencies, 1):
        actual += count / total
        predicted += rank ** -exponent / norm
        max_cdf_gap = max(max_cdf_gap, abs(actual - predicted))
    return {
        'items': n, 'events': total, 'zipf_exponent': exponent,
        'log_rank_intercept': my - slope * mx,
        'log_rank_r_squared': xy * xy / (xx * yy) if yy else None,
        'rank_cdf_max_gap': max_cdf_gap,
        'top_10_share': sum(frequencies[:10]) / total,
        'top_1_percent_share': sum(frequencies[:max(1, math.ceil(n * .01))]) / total,
        'top_10_percent_share': sum(frequencies[:max(1, math.ceil(n * .1))]) / total,
        'method': 'OLS log(count) versus log(rank), full observed support, ranks start at 1; diagnostic only, not a power-law goodness-of-fit test',
        'limitations': 'truncated power rank approximation; head/tail residuals remain; synthetic ranks have no semantic mapping to Olist products',
    }


def inspect_fit(path, output):
    started = time.monotonic()
    users, items, pv_items, categories, behaviors = (Counter() for _ in range(5))
    rows = 0
    anomalies = Counter()
    lo = hi = None
    members = []
    with zipfile.ZipFile(path) as archive:
        names = [i for i in archive.infolist() if not i.is_dir()]
        csvs = [i for i in names if i.filename.lower().endswith('.csv')]
        if len(csvs) != 1:
            raise ValueError('expected one headerless CSV')
        for info in names:
            digest = hashlib.sha256()
            with archive.open(info) as stream:
                if info == csvs[0]:
                    for raw in stream:
                        digest.update(raw)
                        fields = raw.decode('utf-8-sig').rstrip('\r\n').split(',')
                        if len(fields) != 5 or fields[3] not in {'pv', 'cart', 'buy', 'fav'}:
                            raise ValueError(f'invalid headerless schema at row {rows + 1}')
                        uid, item, category = map(int, fields[:3])
                        stamp = int(fields[4])
                        for field, value in [('user_id', uid), ('item_id', item), ('category_id', category), ('timestamp', stamp)]:
                            if value < 0:
                                anomalies['negative_'+field] += 1
                        if not 1511539200 <= stamp < 1512316800:
                            anomalies['outside_reference_2017_11_25_to_12_03_asia_shanghai'] += 1
                        users[uid] += 1
                        items[item] += 1
                        categories[category] += 1
                        behaviors[fields[3]] += 1
                        if fields[3] == 'pv':
                            pv_items[item] += 1
                        lo = stamp if lo is None else min(lo, stamp)
                        hi = stamp if hi is None else max(hi, stamp)
                        rows += 1
                        if rows % 10_000_000 == 0:
                            print(json.dumps({'verified_rows': rows}), flush=True)
                else:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
            # Reading every byte also verifies each member's CRC via ZipExtFile.
            members.append({'name': info.filename, 'uncompressed_bytes': info.file_size,
                            'compressed_bytes': info.compress_size, 'sha256': digest.hexdigest(),
                            'rows': rows if info == csvs[0] else None, 'crc_pass': True})
    if not rows or not pv_items:
        raise ValueError('nonempty complete source with page views required')
    activity = Counter(users.values())
    rank_pv = rank_fit(pv_items)
    rank_all = rank_fit(items)
    archive_hash = sha(path)
    result = {
        'generated_at': datetime.now(UTC).isoformat(), 'status': 'available', 'fitted': True,
        'path': str(path), 'archive_bytes': path.stat().st_size, 'archive_sha256': archive_hash,
        'expected_archive_bytes': 949805174, 'archive_size_difference_bytes': path.stat().st_size - 949805174,
        'expected_size_source': 'owner plan Appendix C historical page metadata',
        'source_page': 'https://tianchi.aliyun.com/dataset/649',
        'license': 'CC BY-NC-SA 4.0 (owner plan page metadata; no independent legal reassessment)',
        'members': members, 'crc_pass': True, 'header': False,
        'columns': ['user_id', 'item_id', 'category_id', 'behavior', 'timestamp'],
        'rows': rows, 'users': len(users), 'items': len(items), 'categories': len(categories),
        'behavior_counts': dict(sorted(behaviors.items())),
        'behavior_proportions': {k: v / rows for k, v in sorted(behaviors.items())},
        'timestamp_min': lo, 'timestamp_max': hi,
        'timestamp_min_utc': datetime.fromtimestamp(lo, UTC).isoformat(),
        'timestamp_max_utc': datetime.fromtimestamp(hi, UTC).isoformat(),
        'time_filter_applied': False, 'source_anomalies': dict(anomalies),
        'reference_window_utc': ['2017-11-24T16:00:00+00:00', '2017-12-03T16:00:00+00:00'],
        'reference_window_note': 'explicit diagnostic window only, not proof of corruption and not a cleaning filter',
        'anomaly_policy': 'all structurally valid rows retained, including source signed IDs or anomalous timestamps; no silent cleaning',
        'user_activity_histogram': dict(sorted(activity.items())),
        'user_activity_method': 'exact full-source per-user event-count histogram; synthetic users independently resampled with seed, not source identities',
        'user_activity_quantiles': {f'p{q}': sorted(users.values())[math.ceil(len(users)*q/100)-1] for q in (50, 95, 99)},
        'item_popularity_pv': rank_pv, 'item_popularity_all_behaviors': rank_all,
        'zipf_exponent': rank_pv['zipf_exponent'], 'popularity_behavior': 'pv',
        'fit_method': rank_pv['method'], 'verification_and_fit_seconds': time.monotonic()-started,
        'original_text_or_ids_exported': False, 'synthetic': False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)+'\n')
    print(json.dumps({k: result[k] for k in ['rows', 'users', 'items', 'archive_bytes', 'archive_sha256', 'verification_and_fit_seconds']}), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or '.local' not in args.output.resolve().parts:
        parser.error('fresh private output required')
    inspect_fit(args.archive, args.output)
