"""Install opt-in loopback P7 services using an already-prepared private environment."""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--env-file',type=Path,required=True)
p.add_argument('--activate',action='store_true')
p.add_argument('--memory-env-file',type=Path)
a = p.parse_args()
root = Path(__file__).resolve().parent.parent
env = a.env_file.resolve()
if a.env_file.is_symlink() or not env.is_relative_to(root/'.local') or env.stat().st_mode & 0o777 != 0o600:
    p.error('private .local environment file with mode 600 required')
if any(c in str(root)+str(env) for c in '\n\r%"\\ '):
    p.error('paths contain unsupported unit characters')
memory_env = a.memory_env_file.resolve() if a.memory_env_file else env
if a.memory_env_file and (a.memory_env_file.is_symlink() or not memory_env.is_relative_to(root/'.local') or memory_env.stat().st_mode & 0o777 != 0o600):
    p.error('private memory environment file with mode 600 required')
if any(c in str(memory_env) for c in '\n\r%"\\ '):
    p.error('unsupported memory environment path')
os.umask(0o077)
units = Path.home()/'.config/systemd/user'
units.mkdir(parents=True,exist_ok=True)
node = shutil.which('node')
if node is None:
    p.error('node is required')
commands = {
    'agent-ops-customer-memory':f'{sys.executable} {root}/scripts/serve-customer-memory.py --env-file {memory_env} --port 18886',
    'agent-ops-p7-api':f'{sys.executable} {root}/scripts/serve.py --env-file {env} --port 18885',
    'agent-ops-p7-web':f'{node} {root}/frontend/node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5175',
}
for name,command in commands.items():
    cwd = root/'frontend' if name.endswith('-web') else root
    (units/(name+'.service')).write_text(f'''[Unit]
Description=Agent Ops P7 loopback component
After=network.target
[Service]
WorkingDirectory={cwd}
ExecStart={command}
Environment=P5_API_TARGET=http://127.0.0.1:18885
UMask=0077
Restart=on-failure
RestartSec=5
StandardOutput=null
StandardError=null
[Install]
WantedBy=default.target
''')
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
if a.activate:
    subprocess.run(['systemctl','--user','enable','--now',*(name+'.service' for name in commands)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    print('P7 loopback services activated; verify health and contracts separately.')
else:
    print('P7 units installed without starting.')
