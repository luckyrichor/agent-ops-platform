"""Install an opt-in user service for an approved private notification configuration.

No firewall, main proxy or application database changes. Stop with systemctl --user
stop agent-ops-payment-entry; disable removes boot activation without deleting evidence.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--env-file', type=Path, required=True)
p.add_argument('--payment-dir', type=Path, default=Path('.local/alipay'))
p.add_argument('--runtime-dir', type=Path, default=Path('.local/payment-entry'))
p.add_argument('--rate', type=int, default=5)
p.add_argument('--burst', type=int, default=20)
p.add_argument('--connections', type=int, default=10)
p.add_argument('--activate', action='store_true')
p.add_argument('--public-confirmed', action='store_true')
a = p.parse_args()
if a.activate and not a.public_confirmed:
    p.error('cloud rule confirmation required before activation')
root = Path(__file__).resolve().parent.parent
paths = [a.env_file.resolve(), a.payment_dir.resolve(), a.runtime_dir.resolve()]
if any('.local' not in f.parts or any(c in str(f) for c in '\n\r%"\\ ') for f in paths):
    p.error('use private .local paths without systemd metacharacters')
if a.env_file.is_symlink() or a.env_file.stat().st_mode & 0o777 != 0o600:
    p.error('private environment file must have mode 600')
os.umask(0o077)
command = [sys.executable, str(root / 'scripts/payment-entry.py'), '--env-file', str(paths[0]),
           '--payment-dir', str(paths[1]), '--runtime-dir', str(paths[2]),
           '--rate', str(a.rate), '--burst', str(a.burst), '--connections', str(a.connections)]
subprocess.run(command, check=True)
units = Path.home() / '.config/systemd/user'
units.mkdir(parents=True, exist_ok=True)
unit = units / 'agent-ops-payment-entry.service'
unit.write_text(f'''[Unit]
Description=Agent Ops isolated payment notification entry
After=network-online.target

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={' '.join(command)} --start --public-confirmed
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=20
UMask=0077
NoNewPrivileges=true
StandardOutput=null
StandardError=null

[Install]
WantedBy=default.target
''')
rotation = units / 'agent-ops-payment-logrotate.service'
rotation.write_text(f"""[Unit]
Description=Rotate sanitized payment entry metadata
[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/sbin/logrotate --state {paths[2]}/logrotate.status {paths[2]}/logrotate.conf
StandardOutput=null
StandardError=null
""")
timer = units / 'agent-ops-payment-logrotate.timer'
timer.write_text("""[Unit]
Description=Payment metadata rotation timer
[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
[Install]
WantedBy=timers.target
""")
subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
if a.activate:
    subprocess.run(['systemctl', '--user', 'enable', '--now', unit.name, timer.name], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('Notification service activated; external payment delivery still requires verification.')
else:
    print('Notification user service installed but not enabled or started.')
