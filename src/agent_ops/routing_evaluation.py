"""Development-only three-version routing; no final-test selector or production activation."""
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_ops.ecd_redaction import redact_ecd
from agent_ops.p8_pilot import CLASSIFIER_PROMPT, SILVER_PROMPT
from agent_ops.support import Route, route_message

HYBRID_THRESHOLD = .9


class DevelopmentSample(BaseModel):
    model_config = ConfigDict(extra='forbid',strict=True)
    id: str
    source: Literal['p2_synthetic','ecd_redacted']
    message: str = Field(min_length=1,max_length=2000)
    context: list[str] = Field(max_length=3)
    group_hash: str
    speaker_role: Literal['synthetic_customer','unknown']


class SilverAnswer(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    label: Route
    uncertain: bool
    basis_code: str = Field(pattern=r'^[A-Z_]{1,64}$')


def request_messages(sample: DevelopmentSample, purpose: str) -> list[dict[str,str]]:
    if purpose not in {'classifier','silver'}:
        raise ValueError('UNKNOWN_PURPOSE')
    prompt=CLASSIFIER_PROMPT if purpose=='classifier' else SILVER_PROMPT
    body=json.dumps(sample.model_dump(include={'message','context','speaker_role'}),ensure_ascii=False,sort_keys=True)
    return [{'role':'system','content':prompt},{'role':'user','content':body}]


def hybrid_confidence(message: str) -> float:
    """Predeclared candidate heuristic; calibration awaits the 100 human dev labels."""
    text=message.lower()
    if any(word in text for word in ['不','别','不要','并','同时','如果','ignore',"don't",'do not']):
        return 0
    groups=[any(x in text for x in words) for words in [
        ['人工','投诉','盗刷','隐私','human','fraud','privacy'],
        ['物流','快递','发货','配送','tracking','delivery','shipping'],
        ['退货','退款','换货','refund','return','exchange']]]
    return 1 if sum(groups)==1 else 0


def development_versions(sample: DevelopmentSample, model_route: Route | None,
                         threshold: float = HYBRID_THRESHOLD) -> dict[str,Any]:
    if not 0 <= threshold <= 1:
        raise ValueError('INVALID_THRESHOLD')
    rule=route_message(sample.message)
    confidence=hybrid_confidence(sample.message)
    needs_model=confidence<threshold
    return {'rule':rule,'model':model_route,'hybrid':model_route if needs_model else rule,
            'hybrid_model_used':needs_model,'rule_confidence':confidence,
            'threshold':threshold,'status':'development_candidate_not_frozen'}


def group_digest(turns: list[str]) -> str:
    return hashlib.sha256(json.dumps(turns,ensure_ascii=False).encode()).hexdigest()


def redacted_turns(turns: list[str]) -> tuple[list[str],dict[str,int]]:
    counts:dict[str,int]={}
    clean=[]
    for turn in turns:
        value,hits=redact_ecd(turn)
        clean.append(value)
        for name,n in hits.items():counts[name]=counts.get(name,0)+n
    return clean,counts
