from __future__ import annotations

import json
import threading
import time

import pytest

from shizai_agent.core import debug_session
from shizai_agent.core.paths import ShizaiPaths


class FakeDebugClient:
    def __init__(self, line_count: int = 1):
        self.calls = []
        self.line = 1
        self.line_count = line_count

    def call_unlocked(self, service, method, args):
        self.calls.append((service, method, args))
        if service == "PackageScriptService":
            return {"code": "generated"}
        if service == "AppRunService" and method == "run":
            return {"state": "running"}
        if service == "AppRunService" and method == "stop":
            return {"state": "stopped"}
        if service == "DebugAdapterService" and method == "send":
            command = args[0]
            if command == "threads":
                return {"body": {"threads": [{"id": 7, "name": "MainThread"}]}}
            if command == "stackTrace":
                return {
                    "body": {
                        "stackFrames": [
                            {
                                "id": 1,
                                "name": "main",
                                "line": self.line,
                                "column": 1,
                                "source": {"path": "C:/flow_modules/io5cd.py"},
                            }
                        ]
                    }
                }
            if command in {"next", "continue"}:
                self.line = min(self.line + 1, self.line_count)
            return {"body": {}}
        return {"data": None}


class NoExistingAdapterDebugClient(FakeDebugClient):
    """Model the normal path where MCP must create the DAP connection."""

    def __init__(self, line_count: int = 1):
        super().__init__(line_count=line_count)
        self.connected = False

    def call_unlocked(self, service, method, args):
        if service == "DebugAdapterService" and method == "connect":
            self.connected = True
            self.calls.append((service, method, args))
            return {"data": None}
        if service == "DebugAdapterService" and method == "send" and not self.connected:
            self.calls.append((service, method, args))
            return {"data": None}
        return super().call_unlocked(service, method, args)


class DesignerAttachesAfterRunDebugClient(NoExistingAdapterDebugClient):
    """Model the installed designer attaching after AppRunService publishes debugpy."""

    def __init__(self, line_count: int = 1):
        super().__init__(line_count=line_count)
        self.run_started_at = None

    def call_unlocked(self, service, method, args):
        if service == "AppRunService" and method == "run":
            self.run_started_at = time.monotonic()
            self.calls.append((service, method, args))
            return {"state": "running"}
        if service == "DebugAdapterService" and method == "send" and args[0] == "threads":
            if self.run_started_at is not None and time.monotonic() - self.run_started_at >= 0.8:
                self.connected = True
                self.calls.append((service, method, args))
                return {"body": {"threads": [{"id": 7, "name": "MainThread"}]}}
        return super().call_unlocked(service, method, args)


class BlockingAttachDebugClient(FakeDebugClient):
    """Model debugpy's attach request waiting for configurationDone."""

    def __init__(self, line_count: int = 1):
        super().__init__(line_count=line_count)
        self.attach_started = threading.Event()
        self.release_attach = threading.Event()
        self.events = []
        self.connected = False

    def call_unlocked(self, service, method, args):
        if service == "DebugAdapterService" and method == "connect":
            self.connected = True
            self.calls.append((service, method, args))
            return {"data": None}
        if service == "DebugAdapterService" and method == "send":
            command = args[0]
            if not self.connected:
                self.calls.append((service, method, args))
                return {"data": None}
            if command == "attach":
                self.events.append("attach_started")
                self.attach_started.set()
                self.release_attach.wait(2)
                self.events.append("attach_finished")
            elif command == "configurationDone":
                assert self.attach_started.is_set()
                self.events.append("configuration_done")
                self.release_attach.set()
        return super().call_unlocked(service, method, args)


class ConnectedWithoutPauseDebugClient(FakeDebugClient):
    """Expose one DAP connection whose runtime never emits a stopped frame."""

    def __init__(self, line_count: int = 1):
        super().__init__(line_count=line_count)
        self.connected = False
        self.connect_count = 0

    def call_unlocked(self, service, method, args):
        if service == "DebugAdapterService" and method == "connect":
            self.connected = True
            self.connect_count += 1
            self.calls.append((service, method, args))
            return {"data": None}
        if service == "DebugAdapterService" and method == "send":
            command = args[0]
            if command == "threads" and not self.connected:
                self.calls.append((service, method, args))
                return {"data": None}
            if command == "threads" and self.connected:
                self.calls.append((service, method, args))
                return {"body": {"threads": [{"id": 7, "name": "MainThread"}]}}
            if command == "stackTrace" and self.connected:
                self.calls.append((service, method, args))
                return {"body": {"stackFrames": []}}
        return super().call_unlocked(service, method, args)


class InternalComponentFrameDebugClient(FakeDebugClient):
    """Return a component implementation frame while the runtime is running."""

    def call_unlocked(self, service, method, args):
        if service == "DebugAdapterService" and method == "send" and args[0] == "stackTrace":
            self.calls.append((service, method, args))
            return {
                "body": {
                    "stackFrames": [
                        {
                            "id": 13,
                            "name": "CreateV3",
                            "line": 1050,
                            "column": 1,
                            "source": {"path": "E:/rpa-engine/plugins/WebBrowser/exports.py"},
                        }
                    ]
                }
            }
        return super().call_unlocked(service, method, args)


class DisconnectedAfterContinueDebugClient(FakeDebugClient):
    """Expose the DAP failure shape seen after a RuntimeHost component error."""

    def __init__(self, line_count: int = 1):
        super().__init__(line_count=line_count)
        self.disconnected = False

    def call_unlocked(self, service, method, args):
        if service == "DebugAdapterService" and method == "send":
            command = args[0]
            if command == "continue":
                self.disconnected = True
                self.calls.append((service, method, args))
                return {"body": {"allThreadsContinued": True}}
            if command == "stackTrace" and self.disconnected:
                self.calls.append((service, method, args))
                return {"success": False, "message": "Server[1] disconnected unexpectedly"}
        return super().call_unlocked(service, method, args)


class TransientStackTimeoutDebugClient(FakeDebugClient):
    """Raise one transport timeout before returning the next valid stop."""

    def __init__(self, line_count: int = 1):
        super().__init__(line_count=line_count)
        self.stack_failures = 1

    def call_unlocked(self, service, method, args):
        if service == "DebugAdapterService" and method == "send" and args[0] == "stackTrace" and self.stack_failures:
            self.stack_failures -= 1
            raise RuntimeError("Connection timed out")
        return super().call_unlocked(service, method, args)


def _paths(tmp_path):
    return ShizaiPaths(
        data_root=tmp_path,
        applications_root=tmp_path / "applications",
        sqlite_db=tmp_path / "sqlite.db",
        run_config=tmp_path / "run.config.json",
        install_root=None,
        executable=None,
    )


def _project(tmp_path):
    root = tmp_path / "applications" / "debug-flow"
    (root / "projects" / "flow_modules").mkdir(parents=True)
    task = {
        "componentModelEntities": {
            "MAIN_COMPONENT": {
                "args0": [{"name": "statements", "value": ["open", "input", "collect"]}],
            },
            "open": {"id": "open", "type": "WebBrowser_CreateV3", "label": "打开浏览器", "args0": []},
            "input": {"id": "input", "type": "Element_SetValue_V6", "label": "输入关键词", "args0": []},
            "collect": {"id": "collect", "type": "WebBrowser_DataCollectionV5", "label": "采集商品", "args0": []},
        },
    }
    (root / "io5cd.task").write_text(json.dumps(task), encoding="utf-8")
    (root / "edit-state.json").write_text(json.dumps({"currentEditorId": "io5cd-flowBlock"}), encoding="utf-8")
    (root / "projects" / "flow_modules" / "io5cd.py").write_text(
        '    WebBrowser.CreateV3(_position=("io5cd", "open", "流程块1", "打开浏览器", "1", "WebBrowser.CreateV3"))\n'
        '    Element.SetValue_V6(_position=("io5cd", "input", "流程块1", "输入关键词", "2", "Element.SetValue_V6"))\n'
        '    WebBrowser.DataCollectionV5(_position=("io5cd", "collect", "流程块1", "采集商品", "3", "WebBrowser.DataCollectionV5"))\n',
        encoding="utf-8",
    )
    return root


def test_native_debug_reuses_designer_dap_session_without_starting_another_runtime(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    (paths.data_root / "debugpyPort.json").write_text(json.dumps({"Port": 65432}), encoding="utf-8")
    client = FakeDebugClient(line_count=3)
    client.line = 2

    first = debug_session.debug_start(paths, str(project), start_from=2, start_timeout=2, client=client)
    assert first["status"] == "paused"
    assert first["current"]["component_id"] == "input"
    assert not any(item[:2] == ("AppRunService", "run") for item in client.calls)
    assert first["adapter_mode"] == "designer_reused"
    assert first["dap_session_reused"] is True
    assert first["run_owned"] is False
    breakpoint_call = next(
        item for item in client.calls if item[:2] == ("DebugAdapterService", "send") and item[2][0] == "setBreakpoints"
    )
    assert breakpoint_call[2][1]["lines"] == [2, 3]
    stack_call = next(
        item for item in client.calls if item[:2] == ("DebugAdapterService", "send") and item[2][0] == "stackTrace"
    )
    assert stack_call[2][1] == {"threadId": 7, "startFrame": 0, "levels": 20}

    stepped = debug_session.debug_command(paths, str(project), "next", wait_seconds=1)
    assert stepped["session_reused"] is True
    assert stepped["current"]["component_id"] == "collect"
    assert any(item[:2] == ("DebugAdapterService", "send") and item[2][0] == "next" for item in client.calls)
    assert not any(item[:2] == ("DebugAdapterService", "send") and item[2][0] == "continue" for item in client.calls)

    stopped = debug_session.debug_stop(paths, str(project))
    assert stopped["stopped"] is True
    assert any(item[:2] == ("DebugAdapterService", "send") and item[2][0] == "disconnect" for item in client.calls)
    assert not any(item[:2] == ("AppRunService", "stop") for item in client.calls)


def test_native_debug_reconnects_persisted_session_across_cli_processes(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    first_client = FakeDebugClient(line_count=3)

    started = debug_session.debug_start(paths, str(project), start_from=1, start_timeout=2, client=first_client)
    assert started["status"] == "paused"
    assert started["current"]["component_id"] == "open"

    # A normal one-shot CLI invocation exits here, so only the JSON session
    # marker remains. The next invocation must rebuild its session object.
    debug_session._SESSIONS.clear()
    second_client = FakeDebugClient(line_count=3)
    stepped = debug_session.debug_command(
        paths,
        str(project),
        "next",
        wait_seconds=1,
        client=second_client,
    )

    assert stepped["status"] == "paused"
    assert stepped["current"]["component_id"] == "input"
    assert stepped["adapter_mode"] == "designer_reused"
    assert not any(item[:2] == ("AppRunService", "run") for item in second_client.calls)
    assert any(item[:2] == ("DebugAdapterService", "send") and item[2][0] == "threads" for item in second_client.calls)

    stopped = debug_session.debug_stop(paths, str(project), client=second_client)
    assert stopped["stopped"] is True


def test_native_debug_status_does_not_reconnect_or_rewind_persisted_stop(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    first_client = FakeDebugClient(line_count=3)

    started = debug_session.debug_start(paths, str(project), start_from=1, start_timeout=2, client=first_client)
    assert started["current"]["component_id"] == "open"
    stepped = debug_session.debug_command(paths, str(project), "next", wait_seconds=1, client=first_client)
    assert stepped["current"]["component_id"] == "input"

    class NoReconnectClient:
        def call(self, *_args, **_kwargs):
            raise AssertionError("status must not reconnect the DAP adapter")

        call_unlocked = call

    debug_session._SESSIONS.clear()
    status = debug_session.debug_status(paths, str(project), client=NoReconnectClient())

    assert status["status"] == "paused"
    assert status["current"]["component_id"] == "input"


def test_native_debug_does_not_treat_accepted_run_result_as_completion(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    client = FakeDebugClient(line_count=3)
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=client,
        state_file=tmp_path / "session.json",
        thread_id=7,
        status="paused",
        current={"component_id": "open", "component_sequence": 1},
        last_line=1,
        run_result={"state": "running"},
    )
    session.run_thread = threading.Thread(target=lambda: None)
    session.run_thread.start()
    session.run_thread.join()

    result = session._wait_after_command("next", 0.2)

    assert result["status"] == "paused"
    assert result["status"] != "succeeded"
    assert result["run_state"] == "running"


def test_native_debug_clears_transient_timeout_after_valid_component_stop(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    client = TransientStackTimeoutDebugClient(line_count=3)
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=client,
        state_file=tmp_path / "session.json",
        thread_id=7,
        status="paused",
        current={"component_id": "open", "component_sequence": 1},
        last_line=1,
    )

    result = session.command("next", wait_seconds=1)

    assert result["status"] == "paused"
    assert result["current"]["component_id"] == "input"
    assert result["error"] is None


def test_native_debug_next_ignores_intermediate_flow_lines_until_exact_component_boundary(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    module = project / "projects" / "flow_modules" / "io5cd.py"
    module.write_text(
        'WebBrowser.CreateV3(_position=("io5cd", "open", "流程块1", "打开网页", "1", "WebBrowser.CreateV3"))\n'
        "if True:\n"
        "    pass\n"
        'Element.SetValue_V6(_position=("io5cd", "input", "流程块1", "输入关键词", "2", "Element.SetValue_V6"))\n'
        'WebBrowser.DataCollectionV5(_position=("io5cd", "collect", "流程块1", "采集商品", "3", "WebBrowser.DataCollectionV5"))\n',
        encoding="utf-8",
    )
    client = FakeDebugClient(line_count=5)
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=client,
        state_file=tmp_path / "session.json",
        thread_id=7,
        status="paused",
        current={"component_id": "open", "component_sequence": 1, "component_boundary": True},
        last_line=1,
    )

    result = session.command("next", wait_seconds=1)

    assert result["status"] == "paused"
    assert result["current"]["line"] == 4
    assert result["current"]["component_id"] == "input"
    assert result["current"]["component_boundary"] is True
    assert result["runtime_verification"] == {
        "verified": True,
        "component_executed": True,
        "component_id": "open",
        "component_sequence": 1,
        "evidence_kind": "dap_component_boundary",
    }
    dap_steps = [item for item in client.calls if item[:2] == ("DebugAdapterService", "send") and item[2][0] == "next"]
    assert len(dap_steps) == 3


def test_native_debug_next_completes_last_component_at_flow_tail_without_extra_step(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    module = project / "projects" / "flow_modules" / "io5cd.py"
    module.write_text(
        'WebBrowser.CreateV3(_position=("io5cd", "open", "流程块1", "打开网页", "1", "WebBrowser.CreateV3"))\n'
        'Element.SetValue_V6(_position=("io5cd", "input", "流程块1", "输入关键词", "2", "Element.SetValue_V6"))\n'
        'WebBrowser.DataCollectionV5(_position=("io5cd", "collect", "流程块1", "采集商品", "3", "WebBrowser.DataCollectionV5"))\n'
        "if True:\n"
        "    pass\n",
        encoding="utf-8",
    )
    client = FakeDebugClient(line_count=5)
    client.line = 3
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=client,
        state_file=tmp_path / "session.json",
        thread_id=7,
        status="paused",
        current={"component_id": "collect", "component_sequence": 3, "component_boundary": True},
        last_line=3,
    )

    result = session.command("next", wait_seconds=1)

    assert result["status"] == "paused"
    assert result["current"]["line"] == 4
    assert result["component_completed"] is True
    assert result["completed_component_id"] == "collect"
    assert result["flow_tail_pending"] is True
    dap_steps = [item for item in client.calls if item[:2] == ("DebugAdapterService", "send") and item[2][0] == "next"]
    assert len(dap_steps) == 1


def test_native_debug_continue_freezes_runtime_error_after_dap_disconnect(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    (paths.data_root / "debugpyPort.json").write_text(json.dumps({"Port": 65432}), encoding="utf-8")
    client = DisconnectedAfterContinueDebugClient(line_count=3)

    started = debug_session.debug_start(paths, str(project), start_from=1, start_timeout=2, client=client)
    result = debug_session.debug_command(paths, str(project), "continue", wait_seconds=1)

    assert started["status"] == "paused"
    assert result["status"] == "runtime_error"
    assert "disconnected unexpectedly" in result["error"]
    assert result["runtime_error"]["message"] == result["error"]
    continue_calls = [
        item for item in client.calls if item[:2] == ("DebugAdapterService", "send") and item[2][0] == "continue"
    ]
    assert len(continue_calls) == 1
    assert not any(item[:2] == ("DebugAdapterService", "send") and item[2][0] == "disconnect" for item in client.calls)


def test_native_debug_preflight_blocks_element_execution_without_sending_continue(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    client = FakeDebugClient(line_count=3)
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=client,
        state_file=tmp_path / "session.json",
        thread_id=7,
        status="paused",
        current={"component_id": "input", "component_sequence": 2},
        last_line=2,
    )
    monkeypatch.setattr(
        debug_session,
        "runtime_preflight",
        lambda *args, **kwargs: {
            "kind": "rpa_runtime_preflight",
            "valid": False,
            "reason": "page_challenge",
            "message": "当前页面进入验证页，请先处理后重试。",
        },
    )

    result = session.command("next", wait_seconds=0.2)

    assert result["status"] == "paused"
    assert result["preflight_blocked"] is True
    assert result["preflight"]["reason"] == "page_challenge"
    assert not any(item[:2] == ("DebugAdapterService", "send") and item[2][0] == "continue" for item in client.calls)


def test_native_debug_capabilities_reports_component_breakpoint_and_reuse_contract(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)

    result = debug_session.debug_capabilities(paths, str(project))

    assert result["ready"] is True
    assert result["component_count"] == 3
    assert result["generated_position_count"] == 3
    assert result["active_session"] is False
    assert result["capabilities"]["component_next"]["supported"] is True
    assert result["capabilities"]["browser_session_reuse"]["supported"] is True


def test_native_debug_creates_dap_session_when_designer_has_none(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    (paths.data_root / "debugpyPort.json").write_text(json.dumps({"Port": 65432}), encoding="utf-8")
    client = NoExistingAdapterDebugClient(line_count=3)
    probe_calls = 0

    def listener_probe(_port):
        nonlocal probe_calls
        probe_calls += 1
        return {101} if probe_calls == 1 else {202}

    result = debug_session.debug_start(
        paths,
        str(project),
        start_from=2,
        start_timeout=4,
        client=client,
        listener_probe=listener_probe,
    )

    assert result["status"] == "paused"
    assert result["current"]["component_id"] == "input"
    assert result["adapter_mode"] == "mcp_created"
    assert result["dap_session_reused"] is False
    assert result["run_owned"] is True
    run_call = next(item for item in client.calls if item[:2] == ("AppRunService", "run"))
    run_request = run_call[2][0]
    assert run_request["isDebug"] == 1
    assert run_request["root"] == str(project)
    assert "class RPAProject" in run_request["code"]
    assert any(item[:2] == ("DebugAdapterService", "connect") for item in client.calls)
    assert any(item[:2] == ("DebugAdapterService", "send") and item[2][0] == "attach" for item in client.calls)

    debug_session.debug_stop(paths, str(project))
    assert any(item[:2] == ("AppRunService", "stop") for item in client.calls)


def test_native_debug_rejects_incomplete_persisted_component_plan(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    (project / "szrpa.json").write_text(
        json.dumps(
            {
                "mcpComponentPlan": [
                    {"id": "open", "component": "WebBrowser_CreateV3"},
                    {"id": "input", "component": "Element_SetValue_V6"},
                    {"id": "collect", "component": "WebBrowser_DataCollectionV5"},
                    {"id": "write", "component": "Excel_Cs_WriteRange"},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"complete component graph.*planned=4, written=3"):
        debug_session.debug_start(paths, str(project), client=FakeDebugClient())


def test_native_debug_waits_for_designer_attach_after_runtime_start(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    (paths.data_root / "debugpyPort.json").write_text(json.dumps({"Port": 65432}), encoding="utf-8")
    client = DesignerAttachesAfterRunDebugClient(line_count=3)
    probe_calls = 0

    def listener_probe(_port):
        nonlocal probe_calls
        probe_calls += 1
        return {101} if probe_calls == 1 else {202}

    result = debug_session.debug_start(
        paths,
        str(project),
        start_from=2,
        start_timeout=4,
        client=client,
        listener_probe=listener_probe,
    )

    assert result["status"] == "paused"
    assert result["current"]["component_id"] == "input"
    assert result["adapter_mode"] == "designer_reused"
    assert result["dap_session_reused"] is True
    assert not any(item[:2] == ("DebugAdapterService", "connect") for item in client.calls)
    debug_session.debug_stop(paths, str(project))


def test_native_debug_configures_dap_while_attach_is_pending(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    (paths.data_root / "debugpyPort.json").write_text(json.dumps({"Port": 65432}), encoding="utf-8")
    client = BlockingAttachDebugClient(line_count=3)
    client.line = 1
    probe_calls = 0

    def listener_probe(_port):
        nonlocal probe_calls
        probe_calls += 1
        return {101} if probe_calls == 1 else {202}

    result = debug_session.debug_start(
        paths,
        str(project),
        start_timeout=4,
        client=client,
        listener_probe=listener_probe,
    )

    assert result["status"] == "paused"
    assert client.events.index("configuration_done") < client.events.index("attach_finished")
    assert result["run_owned"] is True
    debug_session.debug_stop(paths, str(project))


def test_native_debug_does_not_reconnect_after_dap_connected_without_pause(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    (paths.data_root / "debugpyPort.json").write_text(json.dumps({"Port": 65432}), encoding="utf-8")
    client = ConnectedWithoutPauseDebugClient(line_count=3)
    probe_calls = 0

    def listener_probe(_port):
        nonlocal probe_calls
        probe_calls += 1
        return {101} if probe_calls == 1 else {202}

    try:
        debug_session.debug_start(
            paths,
            str(project),
            start_timeout=4,
            client=client,
            listener_probe=listener_probe,
        )
    except Exception as exc:
        assert "DAP 已连接" in str(exc)
        assert "停靠" in str(exc)
    else:
        raise AssertionError("debug_start must fail when no stopped frame is received")

    assert client.connect_count == 1
    assert sum(item[:2] == ("DebugAdapterService", "send") and item[2][0] == "disconnect" for item in client.calls) == 1


def test_native_debug_stop_treats_app_run_3001_as_already_stopped(tmp_path):
    class AlreadyStoppedClient(FakeDebugClient):
        def call_unlocked(self, service, method, args):
            if service == "AppRunService" and method == "stop":
                self.calls.append((service, method, args))
                raise RuntimeError("current state cannot stop (code=3001)")
            return super().call_unlocked(service, method, args)

    paths = _paths(tmp_path)
    project = _project(tmp_path)
    client = AlreadyStoppedClient(line_count=3)
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=client,
        state_file=tmp_path / "session.json",
        port=5680,
        status="paused",
        run_owned=True,
    )

    result = session.stop()

    assert result["stopped"] is True
    assert result["cleanup_complete"] is True
    assert result["errors"] == []
    assert result["stop_attempted"] is True
    assert result["stop_acknowledged"] is True
    assert result["disconnect_attempted"] is True
    stop_index = next(index for index, item in enumerate(client.calls) if item[:2] == ("AppRunService", "stop"))
    disconnect_index = next(
        index
        for index, item in enumerate(client.calls)
        if item[:2] == ("DebugAdapterService", "send") and item[2][0] == "disconnect"
    )
    assert stop_index < disconnect_index
    assert any(item[:2] == ("AppRunService", "stop") for item in client.calls)


def test_native_debug_stop_persists_cleanup_timeout_and_retry_action(tmp_path):
    class StopTimeoutClient(FakeDebugClient):
        def call_unlocked(self, service, method, args):
            if service == "AppRunService" and method == "stop":
                self.calls.append((service, method, args))
                raise RuntimeError("AppRunService.stop timed out")
            return super().call_unlocked(service, method, args)

    paths = _paths(tmp_path)
    project = _project(tmp_path)
    client = StopTimeoutClient(line_count=3)
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=client,
        state_file=tmp_path / "session.json",
        port=5680,
        status="paused",
        run_owned=True,
    )

    result = session.stop()
    persisted = json.loads((tmp_path / "session.json").read_text(encoding="utf-8"))

    assert result["status"] == "cleanup_timeout"
    assert result["cleanup_complete"] is False
    assert result["retryable"] is True
    assert result["next_tool"] == "shizai_rpa_debug_stop"
    assert persisted["status"] == "cleanup_timeout"
    assert persisted["run_owned"] is True
    assert any(item[:2] == ("DebugAdapterService", "send") and item[2][0] == "disconnect" for item in client.calls)


def test_native_debug_stop_persists_terminal_state_after_cross_process_fallback(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    state_file = debug_session._state_path(paths, project)
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "status": "start_timeout",
                "run_owned": True,
                "project": str(project),
                "port": 5680,
            }
        ),
        encoding="utf-8",
    )

    class FallbackClient:
        def call(self, service, method, args):
            assert (service, method, args) == ("AppRunService", "stop", [{}])
            raise RuntimeError("当前状态不可停止 (code=3001)")

    result = debug_session.debug_stop(paths, str(project), client=FallbackClient())
    persisted = json.loads(state_file.read_text(encoding="utf-8"))

    assert result["status"] == "stopped"
    assert result["cleanup_complete"] is True
    assert persisted["status"] == "stopped"
    assert persisted["cleanup_status"] == "complete"


def test_native_debug_skips_stale_adapter_after_terminal_persisted_session(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    (paths.data_root / "debugpyPort.json").write_text(json.dumps({"Port": 65432}), encoding="utf-8")
    state_file = paths.data_root / "mcp-debug-sessions" / (debug_session._session_key(project) + ".json")
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
    client = NoExistingAdapterDebugClient(line_count=3)
    probe_calls = 0

    def listener_probe(_port):
        nonlocal probe_calls
        probe_calls += 1
        return {101} if probe_calls == 1 else {202}

    result = debug_session.debug_start(
        paths,
        str(project),
        start_from=1,
        start_timeout=4,
        client=client,
        listener_probe=listener_probe,
    )

    assert result["status"] == "paused"
    assert result["current"]["component_id"] == "open"
    assert any(item[:2] == ("AppRunService", "run") for item in client.calls)
    assert result["adapter_mode"] == "mcp_created"
    debug_session.debug_stop(paths, str(project))


def test_native_debug_does_not_report_component_implementation_frame_as_paused(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    client = InternalComponentFrameDebugClient(line_count=3)
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=client,
        state_file=tmp_path / "session.json",
        thread_id=7,
        status="connected",
    )

    result = session._snapshot(wait_seconds=1)

    assert result["status"] == "running"
    assert result["current"] is None
    assert "组件实现内部" in result["message"]


def test_native_debug_adapter_identity_rejects_old_listener_and_accepts_new_listener(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    task_path = project / "io5cd.task"
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=task_path,
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=object(),
        state_file=tmp_path / "session.json",
        port_before=65432,
        port_mtime_before=10,
        listener_pids_before=[101],
        listener_probe_before=True,
    )

    assert not session._is_current_adapter_port(65432, 10, listener_pids={101})
    assert session._is_current_adapter_port(65432, 10, listener_pids={202})
    assert session._is_current_adapter_port(65433, 11, listener_pids={101})


def test_runtime_error_evidence_ignores_old_project_with_reused_component_id(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    log_dir = paths.data_root / "logs" / "2026-08-29" / "output-logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "old-flow.jsonl"
    log_path.write_text(
        json.dumps(
            {
                "data": {
                    "text": "运行错误：[010400100020029][输入关键词]元素查找失败",
                    "errorCode": "010400100020029",
                    "errorTraceback": "C:\\\\Users\\\\old\\\\applications\\\\old-flow\\\\projects\\\\flow_modules\\\\io5cd.py",
                }
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=object(),
        state_file=tmp_path / "session.json",
        created_at=time.time(),
        run_started_at=time.time(),
    )

    assert session._runtime_error_from_logs() is None


def test_runtime_error_evidence_accepts_current_project_log(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    log_dir = paths.data_root / "logs" / "2026-08-29" / "user_logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / f"{project.name}.log"
    log_path.write_text(
        "运行错误：[010400100020029][输入关键词]元素查找失败\n",
        encoding="utf-8",
    )
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=object(),
        state_file=tmp_path / "session.json",
        created_at=time.time(),
        run_started_at=time.time(),
    )

    evidence = session._runtime_error_from_logs()
    assert evidence is not None
    assert evidence["log_path"] == str(log_path)


def test_runtime_success_log_takes_precedence_over_normal_dap_disconnect(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    log_dir = paths.data_root / "logs" / "2026-08-29" / "user_logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / f"{project.name}.log"
    log_path.write_text(
        "开始运行\n运行结束\nsuccess\n",
        encoding="utf-8",
    )
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=object(),
        state_file=tmp_path / "session.json",
        created_at=time.time(),
        run_started_at=time.time(),
        status="running",
        current={"component_id": "input", "component_sequence": 2},
    )

    assert session._mark_terminal_if_needed("Server[1] disconnected unexpectedly") is True
    assert session.status == "succeeded"
    assert session.error is None


def test_runtime_success_log_ignores_historical_success_marker(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)
    log_dir = paths.data_root / "logs" / "2026-08-29" / "user_logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / f"{project.name}.log"
    log_path.write_text(
        "2026-08-29 10:00:00,000 log_util.py[line: 52][pid:1] INFO: success\n",
        encoding="utf-8",
    )
    run_time = time.mktime((2026, 8, 29, 12, 0, 0, 0, 0, -1))
    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=object(),
        state_file=tmp_path / "session.json",
        created_at=run_time,
        run_started_at=run_time,
    )

    assert session._runtime_success_from_logs() is None


def test_empty_dap_threads_require_explicit_completion_evidence(tmp_path):
    paths = _paths(tmp_path)
    project = _project(tmp_path)

    class EmptyThreadsClient:
        def call_unlocked(self, service, method, args):
            if service == "DebugAdapterService" and method == "send":
                if args[0] == "threads":
                    return {"body": {"threads": []}}
                return {"body": {}}
            return {"state": "running"}

    session = debug_session.NativeDebugSession(
        paths=paths,
        project_dir=project,
        task_path=project / "io5cd.task",
        arrangement_id="io5cd-flowBlock",
        component_ids=["open", "input", "collect"],
        start_from=1,
        client=EmptyThreadsClient(),
        state_file=tmp_path / "session.json",
        thread_id=7,
        status="paused",
        current={"component_id": "open", "component_sequence": 1},
        last_line=1,
        run_result={"state": "running"},
    )
    session.run_thread = threading.Thread(target=lambda: None)
    session.run_thread.start()
    session.run_thread.join()

    result = session._wait_after_command("next", 0.2)

    assert result["status"] != "succeeded"
