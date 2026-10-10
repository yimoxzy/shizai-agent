from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from shizai_agent import __version__
from shizai_agent.core import browser_mcp
from shizai_agent.core.release_integrity import (
    MANIFEST_FILE,
    REQUIRED_FILES,
    check_package_tree,
    check_source_versions,
    file_digest,
)

ROOT = Path(__file__).parents[1]


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "package"
    for name in REQUIRED_FILES:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("test payload", encoding="utf-8")
    shutil.copyfile(ROOT / "release/scripts/verify-package.ps1", root / "scripts/verify-package.ps1")
    manifest = {
        "format": 1, "version": __version__, "extension_version": "0.1.9",
        "profiles": {name: {"count": 2, "names": ["shizai_browser_status", "shizai_rpa_add_step"]}
                     for name in ("full", "rpa-authoring")},
        "files": {name: file_digest(root / name) for name in REQUIRED_FILES},
    }
    (root / MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_package_verification_accepts_complete_and_rejects_missing_extension(package):
    assert check_package_tree(package, __version__)["version"] == __version__
    (package / "browser-extension/manifest.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        check_package_tree(package, __version__)


@pytest.mark.parametrize("change", ["version", "binary", "tool", "escape"])
def test_package_verification_rejects_stale_tampered_or_escaping_package(package, change):
    manifest = json.loads((package / MANIFEST_FILE).read_text())
    if change == "version":
        manifest["version"] = "0.0.1"
    elif change == "binary":
        (package / "shizai-agent-mcp.exe").write_text("old executable")
    elif change == "tool":
        manifest["profiles"]["rpa-authoring"]["names"] = ["shizai_rpa_start_design", "shizai_browser_status"]
    else:
        outside = package.parent / "outside.txt"
        outside.write_text("outside")
        manifest["files"]["../outside.txt"] = file_digest(outside)
    (package / MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        check_package_tree(package, __version__)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows package verifier")
def test_powershell_checks_payload_before_install(package):
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(package / "scripts/verify-package.ps1"),
           "-PackageRoot", str(package), "-Version", __version__]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["verified"] is True
    (package / "shizai-agent-browser-host.exe").write_text("wrong host")
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode != 0 and "hash mismatch" in result.stderr


def test_source_and_npm_share_one_release_version():
    check_source_versions(ROOT, __version__)
    with pytest.raises(ValueError, match="mismatch"):
        check_source_versions(ROOT, "0.7.28")


def test_managed_install_with_config_but_no_extension_is_not_ready(tmp_path):
    (tmp_path / "version.json").write_text('{"version":"0.7.28"}')
    config = tmp_path / "browser-bridge.json"
    config.write_text('{"installToken":"private-value"}')
    manager = browser_mcp.BrowserBridgeManager(config)
    result = manager.status()
    assert result["state"] == "installation_incomplete"
    assert set(result["missing_files"]) == {"shizai-agent-browser-host.exe", "browser-extension/manifest.json"}
    assert manager._server is None
    assert "private-value" not in json.dumps(result)


def test_frozen_mcp_uses_its_own_install_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("SHIZAI_BROWSER_BRIDGE_CONFIG", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "shizai-agent-mcp.exe"))
    assert browser_mcp.default_config_path() == tmp_path / "browser-bridge.json"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows browser registration")
def test_browser_registration_preserves_identity_without_touching_live_registry(tmp_path):
    (tmp_path / "shizai-agent-browser-host.exe").touch()
    (tmp_path / "browser-extension").mkdir()
    (tmp_path / "browser-extension/manifest.json").write_text("{}")
    config = tmp_path / "browser-bridge.json"
    config.write_text(json.dumps({"allowedExtensionIds": ["test"], "installToken": "existing-private-token", "bridgePort": 18890}))
    script = tmp_path / "register-test.ps1"
    script.write_text("""param($Registration, $InstallDir)
function New-Item { param($Path, [switch]$Force) if (-not $Path.StartsWith('HKCU:')) { throw 'unexpected write' } }
function Set-Item { param($Path, $Value) if (-not $Path.StartsWith('HKCU:')) { throw 'unexpected write' } }
& $Registration -InstallDir $InstallDir
""", encoding="utf-8")
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
               "-Registration", str(ROOT / "release/scripts/register-browser.ps1"), "-InstallDir", str(tmp_path)]
    for _ in range(2):
        result = subprocess.run(command, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "existing-private-token" not in result.stdout
        assert json.loads(result.stdout)["token_preserved"] is True
        data = json.loads(config.read_text())
        assert data["installToken"] == "existing-private-token" and data["bridgePort"] == 18890
    manifest = json.loads((tmp_path / "com.shizai.agent.browser.json").read_text())
    assert Path(manifest["path"]) == tmp_path / "shizai-agent-browser-host.exe"


@pytest.mark.skipif(shutil.which("node") is None, reason="npm launcher requires node")
def test_npm_status_does_not_claim_old_config_means_extension_installed(tmp_path):
    (tmp_path / "shizai-agent-mcp.exe").touch()
    (tmp_path / "version.json").write_text('{"version":"0.7.28"}')
    (tmp_path / "browser-bridge.json").write_text('{"installToken":"private"}')
    result = subprocess.run([shutil.which("node"), str(ROOT / "npm/shizai-agent-mcp/bin/shizai-agent-mcp.js"), "status"],
                            env={**os.environ, "SHIZAI_AGENT_HOME": str(tmp_path)}, capture_output=True,
                            text=True, encoding="utf-8", check=True)
    data = json.loads(result.stdout)
    assert data["version"] == "0.7.28" and data["installer_version"] == __version__
    assert not data["browser_extension_files_present"] and not data["native_host_present"]
    assert data["browser_extension_loaded"] is None and not data["verification"]["verified"]


@pytest.mark.skipif(shutil.which("node") is None, reason="npm launcher requires node")
def test_mcp_launcher_rejects_stale_binary_without_install_noise(tmp_path):
    (tmp_path / "shizai-agent-mcp.exe").write_text("must not execute")
    (tmp_path / "version.json").write_text('{"version":"0.7.28"}')
    result = subprocess.run([shutil.which("node"), str(ROOT / "npm/shizai-agent-mcp/bin/shizai-agent-mcp.js")],
                            env={**os.environ, "SHIZAI_AGENT_HOME": str(tmp_path)}, capture_output=True,
                            encoding="utf-8")
    assert result.returncode == 1 and result.stdout == ""
    assert f"shizai-agent-mcp@{__version__} install" in result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="npm launcher requires node")
def test_npm_refuses_unrelated_install_and_uninstall_directory(tmp_path):
    marker = tmp_path / "user-data.txt"
    marker.write_text("keep me")
    for command in ("install", "uninstall"):
        result = subprocess.run([shutil.which("node"), str(ROOT / "npm/shizai-agent-mcp/bin/shizai-agent-mcp.js"), command],
                                env={**os.environ, "SHIZAI_AGENT_HOME": str(tmp_path)}, capture_output=True,
                                encoding="utf-8", timeout=10)
        assert result.returncode == 1 and "非 Shizai Agent" in result.stderr
        assert marker.read_text() == "keep me"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["user-data.txt"]


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("node") is None, reason="Windows npm download fallback")
def test_npm_install_without_curl_uses_windows_powershell(package, tmp_path):
    asset = tmp_path / f"shizai-agent-v{__version__}-windows-x64.zip"
    with zipfile.ZipFile(asset, "w") as archive:
        for file in package.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(package).as_posix())
    Path(str(asset) + ".sha256").write_text(hashlib.sha256(asset.read_bytes()).hexdigest())

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    destination = tmp_path / "安装目录"
    try:
        result = subprocess.run([shutil.which("node"), str(ROOT / "npm/shizai-agent-mcp/bin/shizai-agent-mcp.js"),
                                 "install", "--skip-register", "--without-browser-extension"],
                                env={**os.environ, "SHIZAI_AGENT_HOME": str(destination),
                                     "SHIZAI_AGENT_CURL": str(tmp_path / "no-curl.exe"),
                                     "SHIZAI_AGENT_RELEASE_BASE_URL": f"http://127.0.0.1:{server.server_port}"},
                                capture_output=True, encoding="utf-8", timeout=90)
        assert result.returncode == 0, result.stderr
        assert check_package_tree(destination, __version__)["version"] == __version__
        assert json.loads((destination / "version.json").read_text())["version"] == __version__
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
