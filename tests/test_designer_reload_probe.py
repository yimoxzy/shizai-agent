from pathlib import Path

from scripts.probe_designer_reload import probe_designer_reload
from shizai_agent.core.paths import ShizaiPaths


def _paths(tmp_path: Path) -> ShizaiPaths:
    return ShizaiPaths(
        data_root=tmp_path,
        applications_root=tmp_path / "applications",
        sqlite_db=tmp_path / "sqlite.db",
        run_config=tmp_path / "run.config.json",
        install_root=None,
        executable=None,
    )


def test_reload_probe_previews_without_calling_rpc(tmp_path, monkeypatch):
    root = tmp_path / "applications" / "flow-1"
    root.mkdir(parents=True)
    monkeypatch.setattr("scripts.probe_designer_reload.project_core.resolve_project", lambda *_: root)
    monkeypatch.setattr(
        "scripts.probe_designer_reload.project_core._native_package_summary",
        lambda *_: {"matched": True, "application_id": "app-1", "application_name": "Probe flow"},
    )

    result = probe_designer_reload(_paths(tmp_path), str(root))

    assert result["reload_requested"] is False
    assert result["rpc_arguments"] == [
        {"applicationId": "app-1", "packageUuid": "", "status": "local"}
    ]


def test_reload_probe_calls_same_application_rpc_and_preserves_revision(tmp_path, monkeypatch):
    root = tmp_path / "applications" / "flow-1"
    root.mkdir(parents=True)
    (root / "flow.task").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("scripts.probe_designer_reload.project_core.resolve_project", lambda *_: root)
    monkeypatch.setattr(
        "scripts.probe_designer_reload.project_core._native_package_summary",
        lambda *_: {"matched": True, "application_id": "app-1", "application_name": "Probe flow"},
    )

    calls = []
    monkeypatch.setattr(
        "scripts.probe_designer_reload._reload_through_home_renderer",
        lambda client, request: calls.append((client, request)) or {"isSuccess": True, "data": None},
    )
    client = object()
    result = probe_designer_reload(_paths(tmp_path), str(root), allow_reload=True, client=client)

    assert calls == [(client, {"applicationId": "app-1", "packageUuid": "", "status": "local"})]
    assert result["reload_requested"] is True
    assert result["rpc_acknowledged"] is True
    assert result["persisted_revision_unchanged"] is True
    assert result["canvas_reload_verified"] is False


def test_reload_probe_can_reload_the_active_editor_webview(tmp_path, monkeypatch):
    root = tmp_path / "applications" / "flow-1"
    root.mkdir(parents=True)
    monkeypatch.setattr("scripts.probe_designer_reload.project_core.resolve_project", lambda *_: root)
    monkeypatch.setattr(
        "scripts.probe_designer_reload.project_core._native_package_summary",
        lambda *_: {"matched": True, "application_id": "app-1", "application_name": "Probe flow"},
    )
    calls = []
    monkeypatch.setattr(
        "scripts.probe_designer_reload._reload_active_editor",
        lambda client: calls.append(client) or {"isSuccess": True, "data": None},
    )
    client = object()

    result = probe_designer_reload(
        _paths(tmp_path),
        str(root),
        allow_reload=True,
        strategy="active-editor-reload",
        client=client,
    )

    assert calls == [client]
    assert result["rpc"] == "CurrentActiveEditorClientId/WorkbenchService.reload"
    assert result["rpc_arguments"] == []
    assert result["rpc_acknowledged"] is True
