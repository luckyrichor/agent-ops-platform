"""Render/start an isolated notification entry. Public startup requires explicit confirmation.

Private values stay in ignored files; no existing proxy, firewall or route is modified.
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--env-file', type=Path, required=True)
parser.add_argument('--payment-dir', type=Path, default=Path('.local/alipay'))
parser.add_argument('--runtime-dir', type=Path, default=Path('.local/payment-entry'))
parser.add_argument('--rate', type=int, default=5)
parser.add_argument('--burst', type=int, default=20)
parser.add_argument('--connections', type=int, default=10)
parser.add_argument('--start', action='store_true')
parser.add_argument('--public-confirmed', action='store_true')
a = parser.parse_args()
os.umask(0o077)
for p in (a.env_file, a.payment_dir, a.runtime_dir):
    if '.local' not in p.resolve().parts:
        parser.error('private paths must be in ignored .local')
if not (1 <= a.rate <= 1000 and 1 <= a.burst <= 1000 and 1 <= a.connections <= 100):
    parser.error('invalid rate configuration')
if a.start and not a.public_confirmed:
    parser.error('confirm cloud rule has been reopened before starting')
runtime = a.runtime_dir.resolve()
runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
config = f'''env TZ=UTC;
pid {runtime}/nginx.pid;
error_log /dev/null crit;
worker_processes 1;
daemon off;
events {{ worker_connections 256; }}
http {{
 map $http_content_type $body_type {{
  default "other";
  ~*^application/x-www-form-urlencoded "form";
 }}
 log_format safe escape=json '{{"time":"$time_iso8601","path":"$uri","method":"$request_method","status":$status,"bytes":$body_bytes_sent,"seconds":$request_time,"trace_id":"$request_id","request_bytes":$request_length,"body_type":"$body_type","upstream_status":"$upstream_status"}}';
 access_log {runtime}/access.jsonl safe;
 client_body_temp_path {runtime}/body-temp;
 proxy_temp_path {runtime}/proxy-temp;
 limit_req_zone $binary_remote_addr zone=notify_rate:1m rate={a.rate}r/s;
 limit_conn_zone $binary_remote_addr zone=notify_conn:1m;
 limit_req_status 429;
 server {{
  listen 0.0.0.0:18080;
  server_name _;
  client_max_body_size 64k;
  client_body_buffer_size 64k;
  client_header_timeout 10s;
  client_body_timeout 15s;
  location = /payments/notify {{
   if ($request_method != POST) {{ return 405; }}
   limit_req zone=notify_rate burst={a.burst} nodelay;
   limit_conn notify_conn {a.connections};
   proxy_pass http://127.0.0.1:18081;
   proxy_set_header Authorization "";
   proxy_set_header Cookie "";
   proxy_set_header X-Forwarded-For "";
   proxy_set_header X-Request-Id $request_id;
   proxy_request_buffering off;
   proxy_buffering off;
   proxy_connect_timeout 3s;
   proxy_read_timeout 10s;
   proxy_send_timeout 10s;
  }}
  location / {{ access_log off; return 404; }}
 }}
}}
'''
(runtime / 'nginx.conf').write_text(config)
# Explicit file policy for logrotate; no network or firewall commands.
(runtime / 'logrotate.conf').write_text(f'''{runtime}/access.jsonl {{
 daily
 rotate 7
 size 1M
 missingok
 notifempty
 copytruncate
 create 0600
}}
''')
check = subprocess.run(['/usr/sbin/nginx', '-t', '-p', str(runtime) + '/', '-c', str(runtime / 'nginx.conf')], capture_output=True, check=False)
if check.returncode:
    parser.error('private proxy configuration validation failed; diagnostics suppressed')
print('Private proxy configuration validated; capacity limits are unvalidated.', flush=True)
if not a.start:
    sys.exit(0)
if a.env_file.stat().st_mode & 0o777 != 0o600:
    parser.error('environment file must have mode 600')
values = dict(line.split('=', 1) for line in a.env_file.read_text().splitlines() if line and not line.startswith('#'))
env = {**os.environ, **{k: v for k, v in values.items() if k.startswith('AGENT_OPS_')},
       'AGENT_OPS_PAYMENT_DIR': str(a.payment_dir.resolve())}
processes = []

def stop(signum, frame):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
failed = False
try:
    import socket
    # A successful TCP connect can belong to an old worker. Refuse occupied ports
    # before spawning, instead of interpreting somebody else's listener as readiness.
    for host, port in [('127.0.0.1', 18081), ('0.0.0.0', 18080)]:
        with socket.socket() as available:
            available.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            available.bind((host, port))
    processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn',
        'agent_ops.payment_api:create_notification_app', '--factory', '--host', '127.0.0.1',
        '--port', '18081', '--no-access-log', '--log-level', 'critical'], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    # No public listener before the private process is alive; service readiness is checked below.
    import socket
    for _ in range(100):
        if processes[0].poll() is not None:
            raise RuntimeError('private notification process failed')
        try:
            with socket.create_connection(('127.0.0.1', 18081), timeout=.2):
                break
        except OSError:
            time.sleep(.1)
    else:
        raise RuntimeError('private notification process not ready')
    processes.append(subprocess.Popen(['/usr/sbin/nginx', '-p', str(runtime) + '/',
        '-c', str(runtime / 'nginx.conf')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    print('Notification entry running; no raw request/exception logging.', flush=True)
    while all(p.poll() is None for p in processes):
        time.sleep(1)
    raise RuntimeError('notification child exited')
except KeyboardInterrupt:
    pass
except (OSError, RuntimeError):
    failed = True
    print('Notification entry failed; private diagnostics suppressed.', flush=True)
finally:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    print('Notification entry stopped.', flush=True)

if failed:
    sys.exit(1)
