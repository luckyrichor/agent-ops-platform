"""Opt-in DeepSeek evaluation transport; never changes the existing Ark planner."""
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agent_ops.planner import LLMConfig


class EvaluationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DeepSeekConfig:
    token: str = field(repr=False)
    endpoint: str
    model: str
    concurrency: int = 8
    max_attempts: int = 3
    timeout_seconds: float = 30

    def __post_init__(self) -> None:
        LLMConfig(self.token, self.endpoint, self.model, self.timeout_seconds,
                  self.concurrency, self.max_attempts)

    @classmethod
    def from_env(cls) -> 'DeepSeekConfig':
        return cls(os.environ.get('DEEPSEEK_API_KEY', ''),
                   os.environ.get('AGENT_OPS_DEEPSEEK_ENDPOINT', ''),
                   os.environ.get('AGENT_OPS_DEEPSEEK_MODEL', ''),
                   concurrency=int(os.environ.get('AGENT_OPS_LLM_CONCURRENCY', '8')),
                   max_attempts=int(os.environ.get('AGENT_OPS_DEEPSEEK_MAX_ATTEMPTS', '3')),
                   timeout_seconds=float(os.environ.get('AGENT_OPS_LLM_TIMEOUT_SECONDS', '30')))


class DeepSeekEvaluation:
    """One shared instance/semaphore per run, including label and hybrid calls.

    No network by default. Caller must obtain user approval first. Cache keys cover
    dataset digest, exact messages, model, endpoint and generation settings.
    ECD is blocked until a separately reviewed data authorization is implemented.
    """
    def __init__(self, config: DeepSeekConfig, cache: Path, *, allow_network: bool = False,
                 client: httpx.AsyncClient | None = None,
                 slots: asyncio.Semaphore | None = None) -> None:
        root = (Path.cwd()/'.local').resolve()
        if cache.is_symlink() or not cache.resolve().is_relative_to(root):
            raise ValueError('EVALUATION_CACHE_REQUIRES_LOCAL')
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        if cache.stat().st_mode & 0o777 != 0o700:
            raise ValueError('EVALUATION_CACHE_REQUIRES_MODE_700')
        self.config, self.cache, self.allow_network = config, cache, allow_network
        self.slots = slots if slots is not None else asyncio.Semaphore(config.concurrency)
        self._client = client
        self._owned = client is None
        self._locks: dict[str, asyncio.Lock] = {}
        self.network_requests = self.retries = self.rate_limits = self.cache_hits = 0

    async def aclose(self) -> None:
        if self._owned and self._client is not None:
            await self._client.aclose()

    async def complete(self, *, dataset_digest: str, source: str,
                       messages: list[dict[str, str]]) -> dict[str, Any]:
        if source != 'p2_synthetic':
            raise EvaluationUnavailable('ECD_THIRD_PARTY_NOT_AUTHORIZED')
        if not dataset_digest or not messages:
            raise ValueError('EVALUATION_INPUT_REQUIRED')
        body = {'model': self.config.model, 'messages': messages, 'temperature': 0,
                'thinking': {'type': 'disabled'}, 'max_tokens': 512,
                'response_format': {'type': 'json_object'}}
        canonical = json.dumps({'endpoint': self.config.endpoint, 'dataset': dataset_digest,
            'source': source, 'body': body}, ensure_ascii=False, sort_keys=True)
        key = hashlib.sha256(canonical.encode()).hexdigest()
        path = self.cache/(key+'.json')
        async with self._locks.setdefault(key, asyncio.Lock()):
            if path.exists():
                if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
                    raise EvaluationUnavailable('EVALUATION_CACHE_UNSAFE')
                cached: dict[str, Any] = json.loads(path.read_text())
                if cached.get('fingerprint') != key:
                    raise EvaluationUnavailable('EVALUATION_CACHE_MISMATCH')
                self.cache_hits += 1
                return cached
            if not self.allow_network:
                raise EvaluationUnavailable('MODEL_CALL_NOT_APPROVED')
            try:
                async with asyncio.timeout(self.config.timeout_seconds):
                    async with self.slots:
                        result = await self._request(body)
            except TimeoutError:
                raise EvaluationUnavailable('DEEPSEEK_TIMEOUT') from None
            record = {'fingerprint': key, 'dataset_digest': dataset_digest,
                      'input_summary': {'sha256': hashlib.sha256(json.dumps(messages,
                          ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                          'characters': sum(len(m.get('content','')) for m in messages)},
                      'requested_model': self.config.model,
                      'called_at': datetime.now(UTC).isoformat(), **result}
            # Complete response only; never persist credentials or HTTP headers.
            temporary = path.with_suffix('.tmp')
            fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(record, f, ensure_ascii=False)
            temporary.replace(path)
            return record

    async def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout_seconds, trust_env=False,
                follow_redirects=False, limits=httpx.Limits(max_connections=self.config.concurrency))
        for attempt in range(self.config.max_attempts):
            self.network_requests += 1
            delay = float(2**attempt)
            try:
                response = await self._client.post(self.config.endpoint,
                    headers={'Authorization': 'Bearer '+self.config.token}, json=body)
                if response.status_code == 429:
                    self.rate_limits += 1
                if response.status_code in (408, 429) or response.status_code >= 500:
                    try:
                        delay = max(delay, float(response.headers.get('Retry-After','0')))
                    except ValueError:
                        pass
                elif response.status_code != 200:
                    raise EvaluationUnavailable('DEEPSEEK_REQUEST_REJECTED')
                else:
                    if len(response.content) > 65536:
                        raise EvaluationUnavailable('DEEPSEEK_INVALID_RESPONSE')
                    try:
                        data = response.json()
                        content = data['choices'][0]['message']['content']
                        if not isinstance(content, str) or not isinstance(json.loads(content), dict):
                            raise TypeError()
                        usage = data['usage']
                        for name in ('prompt_tokens','completion_tokens'):
                            if type(usage[name]) is not int or usage[name] < 0:
                                raise TypeError()
                        return {'returned_model': data['model'], 'content': content,
                                'usage': usage, 'attempts': attempt+1}
                    except (ValueError, KeyError, IndexError, TypeError):
                        raise EvaluationUnavailable('DEEPSEEK_INVALID_RESPONSE') from None
            except httpx.TransportError:
                pass
            if attempt+1 == self.config.max_attempts:
                raise EvaluationUnavailable('DEEPSEEK_RETRIES_EXHAUSTED')
            self.retries += 1
            await asyncio.sleep(min(max(delay, 0), self.config.timeout_seconds))
        raise EvaluationUnavailable('DEEPSEEK_RETRIES_EXHAUSTED')
