"""Verify actual frozen entry points before a Windows release can be packaged.

This is a build/installation audit, not an RPA evaluation. It calls only
initialize, tools/list and browser status against an isolated missing config.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from shizai_agent import __version__

from .browser_bridge import native_read_frame
from .mcp_server import _schemas

MANIFEST_FILE = "release-manifest.json"
REQUIRED_FILES = (
    "shizai-agent.exe", "shizai-agent-mcp.exe", "shizai-agent-browser-host.exe",
    "browser-extension/manifest.json", "browser-extension/service_worker.js",
    "browser-extension/content_script.js", "browser-extension/src/core.js",
    "scripts/register-browser.ps1", "scripts/unregister-browser.ps1",
    "scripts/register-codex.ps1", "scripts/unregister-codex.ps1", "scripts/verify-package.ps1", "README.txt",
)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tool_manifest(tools: list[Any]) -> dict[str, Any]:
    payload = sorted((tool.model_dump(mode="json", exclude_none=True) for tool in tools), key=lambda item: item["name"])
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"count": len(payload), "names": [tool["name"] for tool in payload],
            "schema_sha256": hashlib.sha256(encoded).hexdigest()}


def check_source_versions(root: Path, version: str) -> None:
    pyproject = re.search(r'^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    package = json.loads((root / "npm/shizai-agent-mcp/package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "npm/shizai-agent-mcp/package-lock.json").read_text(encoding="utf-8"))
    versions = [__version__, pyproject.group(1) if pyproject else None,
                package.get("version"), lock.get("version"), lock.get("packages", {}).get("", {}).get("version")]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or any(item != version for item in versions):
        raise ValueError(f"Release/source/npm version mismatch: expected {version}, observed {versions}")


async def probe_mcp(binary: Path, profile: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="shizai-release-probe-") as temporary, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errors:
        params = StdioServerParameters(command=str(binary), args=[], env={
            **os.environ, "SHIZAI_MCP_TOOL_PROFILE": profile,
            "SHIZAI_BROWSER_BRIDGE_CONFIG": str(Path(temporary) / "absent.json"),
        })
        async with stdio_client(params, errlog=errors) as streams:
            async with ClientSession(*streams) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                actual = tool_manifest(listed.tools)
                expected = tool_manifest(_schemas(profile))
                if initialized.serverInfo.version != __version__ or actual != expected:
                    missing = sorted(set(expected["names"]) - set(actual["names"]))
                    raise ValueError(f"{profile}: frozen MCP differs from source; version="
                                     f"{initialized.serverInfo.version}, tools={actual['count']}, missing={missing}")
                status = await session.call_tool("shizai_browser_status", {})
                payload = json.loads(status.content[0].text)
                if status.isError or payload.get("state") != "not_installed":
                    raise ValueError("Frozen browser status did not honor isolated config")
        errors.seek(0)
        if errors.read():
            raise ValueError(f"{profile}: frozen MCP wrote stderr during startup/shutdown")
    return actual


def verify_native_host(binary: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="shizai-host-probe-") as temporary:
        config = Path(temporary) / "config.json"
        config.write_text(json.dumps({"allowedExtensionIds": ["probe"], "installToken": "probe-only",
                                      "bridgePort": 1}), encoding="utf-8")
        result = subprocess.run([str(binary), "--config", str(config), "chrome-extension://probe/"],
                                capture_output=True, timeout=20, check=True)
        payload = native_read_frame(io.BytesIO(result.stdout))
        if result.stderr or not payload or payload.get("type") not in {"bridgeUnavailable", "bootstrap"}:
            raise ValueError("Frozen browser Native Messaging bootstrap failed")


def check_package_tree(root: Path, version: str) -> dict[str, Any]:
    """Verify manifest, required paths and every declared file before installation."""
    root = root.resolve()
    manifest = json.loads((root / MANIFEST_FILE).read_text(encoding="utf-8-sig"))
    if manifest.get("format") != 1 or manifest.get("version") != version:
        raise ValueError("Release manifest format/version mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or not set(REQUIRED_FILES) <= files.keys():
        raise ValueError("Release manifest lacks required executables, extension or setup files")
    for name, expected in files.items():
        path = (root / name).resolve()
        if Path(name).suffix in {".py", ".pyc", ".task"} or {"test", "tests", "__pycache__"} & set(Path(name).parts):
            raise ValueError(f"Release contains development/project data: {name}")
        if not path.is_relative_to(root) or not path.is_file() or file_digest(path) != expected:
            raise ValueError(f"Release file missing, outside package or hash mismatch: {name}")
    for profile in ("full", "rpa-authoring"):
        toolset = manifest.get("profiles", {}).get(profile, {})
        if not isinstance(toolset.get("names"), list) or toolset.get("count") != len(toolset["names"]):
            raise ValueError(f"Release lacks a verified {profile} tool manifest")
        if not {"shizai_browser_status", "shizai_rpa_add_step"} <= set(toolset["names"]):
            raise ValueError(f"Release lacks browser or incremental authoring tools: {profile}")
    return manifest


async def build_manifest(root: Path, version: str) -> dict[str, Any]:
    for name in REQUIRED_FILES:
        if not (root / name).is_file():
            raise ValueError(f"Release missing required file: {name}")
    profiles = {}
    for profile in ("full", "rpa-authoring"):
        profiles[profile] = await asyncio.wait_for(probe_mcp(root / "shizai-agent-mcp.exe", profile), 45)
    cli = await asyncio.to_thread(subprocess.run, [str(root / "shizai-agent.exe"), "--version"],
                                  capture_output=True, text=True, timeout=20, check=True)
    if cli.stderr or not re.search(rf"\b{re.escape(version)}\b", cli.stdout):
        raise ValueError("Frozen CLI version differs from release")
    await asyncio.to_thread(verify_native_host, root / "shizai-agent-browser-host.exe")
    extension = json.loads((root / "browser-extension/manifest.json").read_text(encoding="utf-8"))
    files = {path.relative_to(root).as_posix(): file_digest(path) for path in sorted(root.rglob("*"))
             if path.is_file() and path.name != MANIFEST_FILE}
    manifest = {"format": 1, "version": version, "extension_version": extension["version"],
                "profiles": profiles, "files": files}
    (root / MANIFEST_FILE).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return check_package_tree(root, version)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--package-dir", type=Path)
    args = parser.parse_args()
    check_source_versions(args.source_root, args.version)
    if args.package_dir:
        result = asyncio.run(build_manifest(args.package_dir.resolve(), args.version))
        print(json.dumps({"verified": True, "version": args.version,
                          "tools": {k: v["count"] for k, v in result["profiles"].items()},
                          "extension_version": result["extension_version"]}))


if __name__ == "__main__":
    main()
