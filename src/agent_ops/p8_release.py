"""Release guards. Frozen policy cannot change after final selection; final run is unique."""
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

POLICY_FILES=(
 'src/agent_ops/routing_policy.py','src/agent_ops/routing_evaluation.py','src/agent_ops/support.py',
 'src/agent_ops/p8_pilot.py','src/agent_ops/deepseek.py','src/agent_ops/ecd_redaction.py',
 'src/agent_ops/routing_metrics.py','src/agent_ops/p8_release.py',
 'scripts/prepare-p8-final.py','scripts/evaluate-p8-final.py','pyproject.toml','uv.lock',
)


def register_round(path: Path, number: int, policy_hash: str) -> None:
    data: dict[str,Any]=json.loads(path.read_text()) if path.exists() else {'max_rounds':3,'rounds':{}}
    if number not in (1,2,3):raise ValueError('DEVELOPMENT_ROUND_LIMIT')
    old=data['rounds'].get(str(number))
    if old and old!=policy_hash:raise ValueError('ROUND_POLICY_ALREADY_RECORDED')
    data['rounds'][str(number)]=policy_hash
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2));path.chmod(0o600)


def verify_freeze(tag: str, manifest: Path) -> dict[str,Any]:
    if subprocess.check_output(['git','cat-file','-t',tag],text=True).strip()!='tag':
        raise ValueError('ANNOTATED_FREEZE_TAG_REQUIRED')
    commit=subprocess.check_output(['git','rev-parse',tag+'^{commit}'],text=True).strip()
    frozen=json.loads(subprocess.check_output(['git','show',f'{commit}:{manifest.as_posix()}']))
    if frozen.get('stage')!='frozen_before_final_selection':raise ValueError('NOT_A_FROZEN_RELEASE')
    for name,digest in frozen['files'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=digest:
            raise ValueError('FROZEN_SOURCE_CHANGED')
    return {**frozen,'commit':commit,'tag':tag}


def reserve_evaluation(path: Path, dataset_hash: str, annotation_hash: str) -> None:
    if path.exists():raise ValueError('FINAL_EVALUATION_ALREADY_RESERVED_OR_COMPLETE')
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as f:json.dump({'stage':'reserved','dataset_sha256':dataset_hash,'annotation_sha256':annotation_hash},f)
    path.chmod(0o600)
