"""Configurable paths for the local Shizai installation and user data."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


@dataclass(frozen=True)
class ShizaiPaths:
    """Resolved paths used by read-only local inspection commands."""

    data_root: Path
    applications_root: Path
    sqlite_db: Path
    run_config: Path
    install_root: Path | None
    executable: Path | None

    @classmethod
    def from_environment(cls) -> "ShizaiPaths":
        """Resolve paths from environment variables and the normal Windows layout."""
        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        data_root = _env_path("SHIZAI_DATA_ROOT", appdata / "Z-Factory")
        run_config = data_root / "run.config.json"
        install_root: Path | None = None
        executable: Path | None = None

        configured_install = os.environ.get("SHIZAI_INSTALL_ROOT")
        if configured_install:
            install_root = Path(configured_install).expanduser()
        elif run_config.is_file():
            try:
                config = json.loads(run_config.read_text(encoding="utf-8"))
                raw = config.get("appInstallPath")
                if raw:
                    install_root = Path(raw)
            except (OSError, ValueError):
                pass

        if install_root:
            candidate = install_root / "实在设计器.exe"
            executable = candidate if candidate.is_file() else None

        return cls(
            data_root=data_root,
            applications_root=data_root / "applications",
            sqlite_db=data_root / "sqlite.db",
            run_config=run_config,
            install_root=install_root,
            executable=executable,
        )

    def as_dict(self) -> dict[str, str | None]:
        """Return JSON-friendly path information."""
        return {
            "data_root": str(self.data_root),
            "applications_root": str(self.applications_root),
            "sqlite_db": str(self.sqlite_db),
            "run_config": str(self.run_config),
            "install_root": str(self.install_root) if self.install_root else None,
            "executable": str(self.executable) if self.executable else None,
        }
