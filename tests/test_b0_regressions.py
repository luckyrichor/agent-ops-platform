"""Regressions fixed before B0; no paid providers or network calls."""
import httpx
import pytest
from agent_memory.sdk import MemoryAPIError, MemoryClient

from agent_ops.agents import demo_agents
from agent_ops.api import create_app
from agent_ops.orchestrator import Dispatcher
from agent_ops.support import route_message


@pytest.mark.parametrize('message', ['humanitarian aid', 'inhuman behavior', 'humanoid robot'])
def test_human_keywords_do_not_match_inside_unrelated_english_words(message):
    assert route_message(message) == 'clarify'


@pytest.mark.parametrize('message', ['HUMAN please', 'fraud!', 'privacy concern'])
def test_explicit_english_handoff_is_preserved(message):
    assert route_message(message) == 'human'


async def test_internal_memory_client_ignores_shell_proxy(monkeypatch):
    # A broken shell proxy must not prevent constructing the internal HTTP client.
    monkeypatch.setenv('AGENT_MEMORY_URL', 'http://memory.invalid')
    for name in ('ALL_PROXY', 'all_proxy', 'HTTP_PROXY', 'http_proxy'):
        monkeypatch.setenv(name, 'ftp://invalid-proxy.invalid')
    monkeypatch.delenv('NO_PROXY', raising=False)
    monkeypatch.delenv('no_proxy', raising=False)

    async def unavailable(self, *args, **kwargs):
        raise MemoryAPIError(503, 'MEMORY_UNAVAILABLE')

    monkeypatch.setattr(MemoryClient, 'search', unavailable)
    monkeypatch.setattr(MemoryClient, 'remember', unavailable)
    dispatch = Dispatcher(demo_agents())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(dispatch)),
                                    base_url='http://test', trust_env=False) as client:
            response = await client.post('/v1/commerce/runs',
                headers={'Authorization': 'Bearer fixture'}, json={
                    'request': 'fixture', 'category': 'kettle',
                    'budget_cent': 5000, 'region': '上海'})
        assert response.status_code == 200
        assert response.json()['memory_status'] == 'read_write_degraded'
        assert response.json()['recommendation']['selected']['id'] == 'kettle-basic'
    finally:
        await dispatch.aclose()
