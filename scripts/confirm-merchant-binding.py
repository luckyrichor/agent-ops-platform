"""Record an explicit operator check; never infer a merchant binding from credentials."""
import argparse
import json
import os
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from agent_ops.payment_gateway import MerchantBinding

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--payment-dir', type=Path, default=Path('.local/alipay'))
p.add_argument('--verified-on', type=date.fromisoformat, required=True)
p.add_argument('--confirm-manual-check', action='store_true', required=True)
a = p.parse_args()
os.umask(0o077)
try:
    root = (Path.cwd()/'.local').resolve()
    folder = a.payment_dir.resolve()
    today = datetime.now(timezone(timedelta(hours=8))).date()
    if not folder.is_relative_to(root) or a.verified_on > today:
        raise ValueError
    identities = []
    for name in ('app_id.txt', 'seller_id.txt'):
        path = folder/name
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError
        value = path.read_text().strip().lstrip('\ufeff')
        if not value.isdigit():
            raise ValueError
        identities.append(value)
    binding = MerchantBinding.confirmed(*identities, a.verified_on)
    # Exclusive creation also refuses symlinks. Retain old attestations before rechecking.
    with (folder/'merchant_binding.json').open('x') as output:
        json.dump({'verified':True, 'verified_on':binding.verified_on.isoformat(),
                   'app_digest':binding.app_digest, 'seller_digest':binding.seller_digest}, output)
        output.write('\n')
    print('Manual merchant binding recorded; private file only.')
except (OSError, ValueError):
    p.exit(1, 'Binding not recorded; check private file permissions, date and existing attestation.\n')
