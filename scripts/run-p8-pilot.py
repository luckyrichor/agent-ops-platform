"""Opt-in approved 50-item pilot; holds an exclusive process lock, never runs full P8."""
import argparse
import asyncio
import fcntl
import json
import os
import shlex
from pathlib import Path

from agent_ops.deepseek import DeepSeekConfig, DeepSeekEvaluation
from agent_ops.p8_pilot import PilotSample, run_pilot


def main() -> None:
    p=argparse.ArgumentParser(__doc__)
    p.add_argument('--samples',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--key-file',type=Path)
    p.add_argument('--cache',type=Path)
    p.add_argument('--allow-network',action='store_true')
    a=p.parse_args()
    os.umask(0o077)
    root=(Path.cwd()/'.local').resolve()
    for path in [a.samples,a.output]:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            p.error('samples/output must be private .local paths')
    if a.samples.stat().st_mode & 0o777 != 0o600:
        p.error('samples must have mode 600')
    if a.key_file:
        if a.key_file.is_symlink() or a.key_file.stat().st_mode & 0o777 != 0o600:
            p.error('private key environment file must have mode 600')
        for line in a.key_file.read_text().splitlines():
            words=shlex.split(line,comments=True)
            if words and words[0]=='export':words=words[1:]
            for word in words:
                if word.startswith('DEEPSEEK_API_KEY='):
                    os.environ['DEEPSEEK_API_KEY']=word.split('=',1)[1]
    samples=[PilotSample.model_validate(row) for row in json.loads(a.samples.read_text())['samples']]
    a.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (a.output/'run.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        async def run() -> None:
            service=DeepSeekEvaluation(DeepSeekConfig.from_env(),a.cache or a.output/'cache',allow_network=a.allow_network)
            try:
                result=await run_pilot(samples,service,a.output)
                print(json.dumps(result))  # Aggregate measurements only, never request bodies/keys.
            finally:
                await service.aclose()
        asyncio.run(run())


if __name__=='__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001 -- redact all terminal exception text
        # No exception text/traceback: URLs, response text and credentials stay private.
        print(json.dumps({'pilot_failed_type':type(e).__name__}))
        raise SystemExit(1) from None
