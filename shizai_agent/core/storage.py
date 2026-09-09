"""Read-only access to the local Shizai project index and JSON files."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .paths import ShizaiPaths


def read_json(path: Path) -> Any:
    """Read UTF-8 JSON from ``path`` with a clear error for malformed data."""
    return json.loads(path.read_text(encoding="utf-8"))


def iter_application_dirs(paths: ShizaiPaths) -> Iterable[Path]:
    """Yield application directories in stable order."""
    if not paths.applications_root.is_dir():
        return
    yield from sorted(
        (item for item in paths.applications_root.iterdir() if item.is_dir()),
        key=lambda item: item.name.lower(),
    )


def query_rows(paths: ShizaiPaths, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Execute a read-only SQLite query and return dictionaries."""
    if not paths.sqlite_db.is_file():
        return []
    uri = f"file:{paths.sqlite_db.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]


def package_index(paths: ShizaiPaths) -> dict[str, dict[str, Any]]:
    """Index package metadata by normalized local project directory."""
    rows = query_rows(
        paths,
        """
        SELECT p.*, a.applicationType, a.creatorId, a.creatorName
        FROM application_package AS p
        LEFT JOIN application AS a ON a.applicationId = p.applicationId
        ORDER BY p.id DESC
        """,
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_path = row.get("packageLocalFullPath")
        if raw_path:
            result[str(Path(raw_path)).casefold()] = row
    return result


def package_record_for_path(paths: ShizaiPaths, project_path: str | Path) -> dict[str, Any] | None:
    """Return the newest package row that owns one local project directory.

    The directory name is a local cache key and is not the same identifier as
    ``applicationId`` or ``packageId``.  Runtime calls must use the database
    row matched by the persisted package path.
    """
    target = Path(project_path).expanduser().resolve()
    target_key = str(target).casefold()
    # The package table is large on long-lived designer installations. A
    # status/validation call should not resolve every historical package path
    # through pathlib just to find the current project. Keep the normalized
    # path fallback below for old databases that store a non-canonical path.
    candidates = {str(project_path), str(target)}
    for candidate in candidates:
        matches = query_rows(
            paths,
            """
            SELECT * FROM application_package
            WHERE packageLocalFullPath IS NOT NULL
              AND lower(packageLocalFullPath) = lower(?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (candidate,),
        )
        if matches:
            return matches[0]

    matches = [
        row
        for row in query_rows(
            paths,
            "SELECT * FROM application_package ORDER BY id DESC",
        )
        if row.get("packageLocalFullPath")
        and str(Path(str(row["packageLocalFullPath"])).expanduser().resolve()).casefold() == target_key
    ]
    return matches[0] if matches else None
