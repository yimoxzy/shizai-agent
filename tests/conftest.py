"""Shared pytest isolation for desktop-affecting MCP code."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shizai_agent.core import mcp_server


class _IsolatedDesktopClient:
    """Fail closed if a unit test forgets to replace a native desktop dependency."""

    config = SimpleNamespace(timeout=0.01, client_id="pytest-isolated", agent_id="agent-brain")

    def _connect(self, _timeout: float):
        raise OSError("native desktop access is isolated during unit tests")


@pytest.fixture(autouse=True)
def isolate_mcp_desktop(monkeypatch):
    """Keep MCP unit tests from opening windows in the real Shizai client."""
    client = _IsolatedDesktopClient()
    monkeypatch.setattr(mcp_server, "_DESKTOP_CLIENT", None)
    monkeypatch.setattr(mcp_server, "_desktop_client", lambda: client)
    monkeypatch.setattr(mcp_server, "_RPA_SMALL_CARD_OPEN", False)
    mcp_server._ACTIVE_RPA_DESIGN_UI.clear()
    monkeypatch.setattr(mcp_server, "_LATEST_RPA_DESIGN_REF", None)
    yield
    mcp_server._ACTIVE_RPA_DESIGN_UI.clear()
