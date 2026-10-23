"""Small invented archives validate schema, exact aggregate counts, and repeatable workloads."""

import json
import subprocess
import sys
import zipfile
from pathlib import Path


def test_full_archive_counts_and_seeded_events(tmp_path: Path) -> None:
    archive = tmp_path / 'source.zip'
    rows = [
        '1,10,2,pv,100', '1,10,2,pv,101', '1,20,2,cart,102',
        '2,20,2,pv,103', '2,20,2,buy,104', '2,30,3,fav,-1',
    ]
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('UserBehavior.csv', '\n'.join(rows)+'\n')
    root = tmp_path / '.local'
    fit = root / 'fit.json'
    subprocess.run([sys.executable, 'scripts/verify-fit-userbehavior.py', '--archive', str(archive),
                    '--output', str(fit)], check=True, capture_output=True)
    data = json.loads(fit.read_text())
    assert data['crc_pass'] and data['rows'] == 6
    assert data['users'] == 2 and data['items'] == 3 and data['categories'] == 2
    assert data['user_activity_histogram'] == {'3': 2}
    assert data['behavior_counts'] == {'buy': 1, 'cart': 1, 'fav': 1, 'pv': 3}
    assert data['source_anomalies']['negative_timestamp'] == 1
    assert data['time_filter_applied'] is False
    assert data['item_popularity_pv']['events'] == 3
    assert data['item_popularity_all_behaviors']['events'] == 6
    assert data['archive_size_difference_bytes'] != 0  # report mismatch, do not hide actual input
    combined = root / 'fits.json'
    combined.write_text(json.dumps({'userbehavior': data}))
    users = root / 'users.csv'
    users.write_text('1,1\n2,5\n')
    generated = []
    for i in range(2):
        out = root / f'events-{i}.jsonl'
        subprocess.run([sys.executable, 'scripts/generate-behavior-events.py', '--fit', str(combined),
                        '--users', str(users), '--products', '7', '--events', '1000',
                        '--output', str(out)], check=True, capture_output=True)
        generated.append(out.read_bytes())
    assert generated[0] == generated[1]
    events = [json.loads(line) for line in generated[0].splitlines()]
    assert len(events) == 1000
    assert all(1 <= e['product_id'] <= 7 and e['user_id'] in {1, 2} for e in events)
    assert sum(e['user_id'] == 2 for e in events) > 700


def test_reject_header_and_invalid_behavior(tmp_path: Path) -> None:
    for i, row in enumerate(['user_id,item_id,category_id,behavior,timestamp', '1,1,1,unknown,10']):
        source = tmp_path / f'bad-{i}.zip'
        with zipfile.ZipFile(source, 'w') as z:
            z.writestr('UserBehavior.csv', row+'\n')
        output = tmp_path / '.local' / f'bad-{i}.json'
        result = subprocess.run([sys.executable, 'scripts/verify-fit-userbehavior.py', '--archive',
                                 str(source), '--output', str(output)], capture_output=True, check=False)
        assert result.returncode != 0
        assert not output.exists()


def test_finite_rank_fit_recovers_probability_mass_not_ols_r_squared() -> None:
    import runpy

    calibrate = runpy.run_path('scripts/calibrate-userbehavior-rank.py')['calibrate']
    alpha = 0.65
    fitted = calibrate([10000 * rank ** -alpha for rank in range(1, 3001)])
    assert abs(fitted['zipf_exponent'] - alpha) < 1e-6
    assert fitted['rank_cdf_max_gap'] < 1e-6
    assert abs(fitted['mean_log_rank_residual']) < 1e-6
