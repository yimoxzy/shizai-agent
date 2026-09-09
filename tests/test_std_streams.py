"""Regression tests for frozen stdio shutdown behavior."""

from __future__ import annotations

import asyncio

from shizai_agent.core import mcp_server
from shizai_agent.core.std_streams import ShutdownSafeStderr


class _ClosedStream:
    def write(self, _value: str) -> int:
        raise ValueError("I/O operation on closed file")

    def flush(self) -> None:
        raise ValueError("I/O operation on closed file")


def test_shutdown_safe_stderr_swallows_late_closed_stream_writes():
    stream = ShutdownSafeStderr(_ClosedStream())

    assert stream.write("late traceback") == len("late traceback")
    stream.flush()
    stream.close()


def test_mcp_main_uses_clean_frozen_exit_after_stdio_runner(monkeypatch):
    events = []

    async def fake_run_stdio():
        events.append("stdio")

    def fake_anyio_run(function):
        asyncio.run(function())

    monkeypatch.setattr(mcp_server, "_run_stdio", fake_run_stdio)
    monkeypatch.setattr(mcp_server.anyio, "run", fake_anyio_run)
    monkeypatch.setattr(mcp_server, "install_shutdown_safe_stderr", lambda: events.append("stderr"))
    monkeypatch.setattr(mcp_server.sys, "frozen", True, raising=False)
    monkeypatch.setattr(mcp_server.os, "_exit", lambda code: events.append(("exit", code)))

    mcp_server.main()

    assert events == ["stderr", "stdio", ("exit", 0)]


def test_mcp_main_keeps_normal_source_mode_exit(monkeypatch):
    events = []

    async def fake_run_stdio():
        events.append("stdio")

    def fake_anyio_run(function):
        asyncio.run(function())

    monkeypatch.setattr(mcp_server, "_run_stdio", fake_run_stdio)
    monkeypatch.setattr(mcp_server.anyio, "run", fake_anyio_run)
    monkeypatch.setattr(mcp_server, "install_shutdown_safe_stderr", lambda: events.append("stderr"))
    monkeypatch.setattr(mcp_server.sys, "frozen", False, raising=False)
    monkeypatch.setattr(mcp_server.os, "_exit", lambda code: events.append(("exit", code)))

    mcp_server.main()

    assert events == ["stderr", "stdio"]
