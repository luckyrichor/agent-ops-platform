"""Run the version-pinned customer-only memory API on loopback, without workers."""
import argparse
import os
from pathlib import Path

import uvicorn
from agent_memory.api.app import create_app
from agent_memory.config import Settings

p = argparse.ArgumentParser()
p.add_argument('--env-file',type=Path,required=True)
p.add_argument('--port',type=int,default=18886)
a = p.parse_args()
if a.env_file.is_symlink() or a.env_file.stat().st_mode & 0o777 != 0o600:
    p.error('private environment file must be mode 600')
config = dict(line.split('=',1) for line in a.env_file.read_text().splitlines() if line)
os.environ['MEMORY_TRACE_EXPORTER'] = 'none'
settings = Settings(database_url=config['AGENT_OPS_CUSTOMER_MEMORY_DATABASE_URL'],
    jwt_public_key=Path(config['AGENT_OPS_CUSTOMER_MEMORY_PUBLIC_KEY']).read_text(),
    jwt_issuer=config['AGENT_OPS_CUSTOMER_MEMORY_ISSUER'],
    jwt_audience=config['AGENT_OPS_CUSTOMER_MEMORY_AUDIENCE'],trace_sample_ratio=0,
    trace_exporter='none',metrics_exporter='none')
uvicorn.run(create_app(settings),host='127.0.0.1',port=a.port,access_log=False,log_level='warning')
