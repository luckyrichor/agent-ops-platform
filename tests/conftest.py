"""Offline gates must never call a paid provider using the shell's live credentials."""

import pytest


@pytest.fixture(autouse=True)
def offline_planner_default(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_PLANNER", "deterministic")
    monkeypatch.setenv("AGENT_OPS_TRACE_EXPORTER", "none")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
