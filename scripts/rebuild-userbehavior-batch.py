"""Rebuild only UserBehavior-dependent artifacts; reference frozen other CSVs unchanged."""
import argparse
import bisect
from collections import Counter
import csv
import hashlib
from itertools import accumulate
import json
from pathlib import Path
import random
import time


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


p = argparse.ArgumentParser()
p.add_argument('--fit', type=Path, required=True)
p.add_argument('--base', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--seed', type=int, default=42)
a = p.parse_args()
if a.output.exists() or '.local' not in a.output.resolve().parts:
    p.error('new private batch required')
f = json.loads(a.fit.read_text())
if not f.get('fitted') or not f.get('crc_pass'):
    p.error('verified full UserBehavior source required')
base = json.loads((a.base/'manifest.json').read_text())
started = time.monotonic()
for table, row in base['files'].items():
    if sha(Path(row['path'])) != row['sha256']:
        raise ValueError('frozen base checksum differs: '+table)
a.output.mkdir(parents=True, mode=0o700)
hist = f['user_activity_histogram']
counts = sorted(map(int, hist))
cdf = list(accumulate(hist[str(k)] for k in counts))
rng = random.Random(a.seed+1)
activity = Counter()
with (a.output/'users.csv').open('w', newline='') as out, Path(base['files']['users']['path']).open() as original:
    writer = csv.writer(out, lineterminator='\n')
    n = 0
    for record in csv.reader(original):
        value = counts[bisect.bisect_right(cdf, rng.random()*cdf[-1])]
        writer.writerow([int(record[0]), value])
        activity[value] += 1
        n += 1
assert n == base['files']['users']['rows']
files = {k: {**v, 'reused_frozen': True} for k, v in base['files'].items() if k != 'users'}
files['users'] = {'path': str(a.output/'users.csv'), 'rows': n, 'columns': ['id', 'activity_events'],
                  'bytes': (a.output/'users.csv').stat().st_size, 'sha256': sha(a.output/'users.csv'), 'reused_frozen': False}
metadata = {'seed': a.seed, 'synthetic': True, 'rows': n, 'sha256': files['users']['sha256'],
            'source_archive_sha256': f['archive_sha256'], 'userbehavior_fit_complete': True,
            'method': f['user_activity_method'], 'intent_proxy_applied': False,
            'routing_accuracy_or_evaluation_use': False}
(a.output/'users.provenance.json').write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2)+'\n')
files['users']['provenance_sha256'] = sha(a.output/'users.provenance.json')
# Keep old per-file provenance intact: ECD-derived tables still explicitly carry their proxy note.
result = {**base, 'seed': a.seed, 'base_manifest_sha256': sha(a.base/'manifest.json'),
          'files': files, 'user_activity_fit_complete': True,
          'userbehavior_source_sha256': f['archive_sha256'], 'userbehavior_fit_sha256': sha(a.fit),
          'rebuilt_tables': ['commerce.users.activity_events'],
          'user_assignment': 'existing user IDs and order/conversation foreign keys unchanged; activity weights fitted; order linkage remains historical structural fixture',
          'generation_seconds': time.monotonic()-started,
          'total_csv_bytes': sum(v['bytes'] for v in files.values()),
          'new_csv_bytes': files['users']['bytes'], 'generated_activity_histogram': dict(sorted(activity.items())),
          'raw_source_users_copied': False, 'phase_p2_accepted': False}
(a.output/'manifest.json').write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)+'\n')
combined = json.loads(Path('.local/datasets/fit-v1/fits.json').read_text())
combined['userbehavior'] = f
combined['source_archive_sha256']['userbehavior'] = f['archive_sha256']
(a.output/'fits.json').write_text(json.dumps(combined, ensure_ascii=False, sort_keys=True, indent=2)+'\n')
print(json.dumps({'users': n, 'new_csv_bytes': result['new_csv_bytes'], 'seconds': result['generation_seconds']}))
