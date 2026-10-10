"""Discover the native RPA components installed with the Shizai designer.

The designer ships the component definitions separately from the application
projects.  Keeping discovery here makes the MCP useful across designer
versions while the small, strict browser-RPA contract remains in ``project``.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .paths import ShizaiPaths

COMPONENT_SOURCE_ENV = "SHIZAI_RPA_ENGINE_ROOT"
BUSINESS_INFO_ENV = "SHIZAI_BIZ_COMPONENT_INFO"
BUSINESS_CACHE_ENV = "SHIZAI_BIZ_COMPONENT_CACHE"
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
_COMPONENTS_RELATIVE = Path("componentDicts") / "componentsNew.json"
_BUSINESS_INFO_NAME = "bizComponentInfo.json"
_BUSINESS_CACHE_RELATIVE = Path("child-module-packages") / "biz"

# Designer dictionaries occasionally expose a legacy display type while the
# executable template uses the versioned native type.  Publish the canonical
# name so an MCP client can compose a runnable plan directly.
COMPONENT_TYPE_ALIASES: dict[str, str] = {
    "WebBrowser_Create": "WebBrowser_CreateV3",
}

# These two entries are maintained by the designer bundle rather than
# componentsNew.json.  The other entries in the designer's static index are
# also present in the engine dictionary and are merged by type below.
_STATIC_BASE_COMMANDS: tuple[dict[str, Any], ...] = (
    {
        "type": "logic_control_var",
        "label": "设置变量",
        "category": "base_command",
        "description": "设计器专用变量入口。",
        "baseType": "logic_control_var",
        "designer_static": True,
    },
    {
        "type": "base_command_custom",
        "label": "插入代码",
        "category": "base_command",
        "description": "设计器专用代码入口。",
        "baseType": "base_command_custom",
        "designer_static": True,
    },
)
_STATIC_BASE_COMMAND_TYPES = frozenset(
    {
        "logic_control_var",
        "Basic_SetVariable",
        "base_command_custom",
        "Basic_DebugOutput",
        "Log_Info",
        "Log_Error",
        "WebBrowser_RunJsInIeBySelector",
        "Basic_Type",
        "Basic_JudgeDataType",
        "Basic_Delay",
        "Basic_RandomInt",
        "Basic_Rnd",
        "Basic_Dodecryption",
        "Basic_Clone",
    }
)

# Map the engine's internal category values to the visible first-level menu.
# A component can belong to more than one internal category, so the catalog
# returns all matching top-level menus.
_TOP_LEVELS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("mobile", "手机自动化", ("mobile", "element_operation", "anthro_operation", "connect_mobile")),
    (
        "process_control",
        "流程控制",
        ("process_control", "judgment", "loop", "waite", "log", "process", "exception", "mark", "Scenario"),
    ),
    ("base_command", "基础命令", ("base_command",)),
    (
        "interface_element",
        "桌面软件",
        ("interface_element", "soft_element_operation", "image", "window", "get_soft_data"),
    ),
    ("webBrowser", "网页浏览器", ("webBrowser", "web_element_operation", "webBrowser_operate", "get_web_data")),
    ("peripheral_device", "键盘鼠标外设", ("peripheral_device", "keyboard", "mouse")),
    (
        "ai_software_automation",
        "AI智能",
        (
            "ai_software_automation",
            "ai_operation_screen",
            "VerificationCode",
            "OCR",
            "NLP",
            "WisdomText",
            "AIGC",
            "SafeControlInput",
        ),
    ),
    ("excel", "Excel/WPS表格", ("excel", "excel_operator", "excel_fetch_data", "excel_editor", "excel_sheet")),
    ("google_sheets", "Google Sheets", ("google_sheets",)),
    ("word", "Word/WPS文档", ("word", "word_edit", "cursor_set")),
    (
        "data_parse",
        "数据处理",
        (
            "data_parse",
            "data_type_conversion",
            "string",
            "time",
            "List",
            "dict",
            "data_table",
            "collection",
            "math",
            "bivartable",
            "Array",
        ),
    ),
    ("controller", "控制器/智能门户", ("automation_portal", "controller")),
    ("dialog_operator", "对话框通知", ("dialog", "notification")),
    ("system", "系统", ("system", "system_action", "app", "ScreenSaver", "ScreenControl", "inputMethod")),
    ("file_parse", "文件处理", ("file_parse", "file", "CSV", "INI")),
    ("email", "邮件", ("email", "Outlook", "SMTP/POP", "Exchange")),
    ("other", "其他", ("trigger", "network", "ftp", "clipboard", "pdf", "Database", "peripherals", "mechanical_arm")),
)


def _unique_paths(candidates: Iterable[Path]) -> list[Path]:
    """Return existing paths once, preserving preference order."""
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return result


def _as_engine_root(candidate: Path) -> Path | None:
    """Normalize an override or install path to an rpa-engine directory."""
    try:
        candidate = candidate.expanduser().resolve()
    except OSError:
        return None
    if candidate.is_file() and candidate.name.casefold() == "componentsnew.json":
        candidate = candidate.parent.parent
    if (candidate / _COMPONENTS_RELATIVE).is_file():
        return candidate
    if (candidate / "resources" / _COMPONENTS_RELATIVE).is_file():
        return candidate / "resources"
    if candidate.name.casefold() == "rpa-engine" and (candidate / "componentDicts").is_dir():
        return candidate
    return None


def _candidate_engine_roots(paths: ShizaiPaths) -> Iterable[Path]:
    override = os.environ.get(COMPONENT_SOURCE_ENV)
    if override:
        yield Path(override)
    if paths.install_root:
        yield paths.install_root / "resources" / "rpa-engine"
        yield paths.install_root / "rpa-engine"
        yield paths.install_root

    # Keep the fallback narrow and Windows-specific.  It is only used when
    # run.config.json is unavailable or points at an old uninstalled build.
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        raw = os.environ.get(variable)
        if not raw:
            continue
        base = Path(raw) / "Z-Factory"
        if not base.is_dir():
            continue
        try:
            versions = sorted(base.iterdir(), key=lambda item: item.name.casefold(), reverse=True)
        except OSError:
            continue
        for version in versions:
            if version.is_dir():
                yield version / "resources" / "rpa-engine"


def component_source(paths: ShizaiPaths, engine_root: str | None = None) -> dict[str, Any]:
    """Resolve the installed component dictionary without exposing credentials."""
    candidates = [Path(engine_root)] if engine_root else list(_candidate_engine_roots(paths))
    for candidate in _unique_paths(candidates):
        root = _as_engine_root(candidate)
        if root:
            dictionary = root / _COMPONENTS_RELATIVE
            return {
                "available": True,
                "engine_root": str(root),
                "components_file": str(dictionary),
                "source": "override" if engine_root else "designer_installation",
            }
    return {
        "available": False,
        "engine_root": None,
        "components_file": None,
        "source": "not_found",
        "override_env": COMPONENT_SOURCE_ENV,
        "message": "未找到设计器 rpa-engine 组件目录；请启动或安装实在设计器后重试。",
    }


def business_component_source(
    paths: ShizaiPaths,
    info_path: str | None = None,
    cache_root: str | None = None,
) -> dict[str, Any]:
    """Resolve the SaaS business-component index and local package cache."""
    configured_info = os.environ.get(BUSINESS_INFO_ENV)
    info_candidates = (
        [Path(info_path)]
        if info_path
        else ([Path(configured_info)] if configured_info else [paths.data_root / _BUSINESS_INFO_NAME])
    )
    info_file = next((path for path in _unique_paths(info_candidates) if path.is_file()), None)
    configured_cache = os.environ.get(BUSINESS_CACHE_ENV)
    cache_candidates = (
        [Path(cache_root)]
        if cache_root
        else ([Path(configured_cache)] if configured_cache else [paths.data_root / _BUSINESS_CACHE_RELATIVE])
    )
    resolved_cache = next((path for path in _unique_paths(cache_candidates) if path.is_dir()), None)
    if info_file:
        return {
            "available": True,
            "info_file": str(info_file),
            "cache_root": str(resolved_cache) if resolved_cache else None,
            "cache_available": bool(resolved_cache),
            "source": "designer_business_cache",
        }
    return {
        "available": False,
        "info_file": None,
        "cache_root": str(resolved_cache) if resolved_cache else None,
        "cache_available": bool(resolved_cache),
        "source": "not_found",
        "override_env": f"{BUSINESS_INFO_ENV}/{BUSINESS_CACHE_ENV}",
        "message": "未找到业务组件索引；SaaS 业务组件需要在实在设计器登录后加载。",
    }


@lru_cache(maxsize=4)
def _load_records_cached(path: str, modified_ns: int, size: int) -> tuple[dict[str, Any], ...]:
    """Load one dictionary version; stat values invalidate the small cache."""
    del modified_ns, size
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("componentsNew.json must contain an array")
    return tuple(item for item in payload if isinstance(item, dict))


def load_component_records(source: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Read the component dictionary selected by :func:`component_source`."""
    if not source.get("available") or not source.get("components_file"):
        return ()
    path = Path(str(source["components_file"]))
    try:
        stat = path.stat()
    except OSError as exc:
        raise FileNotFoundError(f"component dictionary is not readable: {path}") from exc
    try:
        return _load_records_cached(str(path), stat.st_mtime_ns, stat.st_size)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid component dictionary {path}: {exc}") from exc


def _load_business_payload(source: dict[str, Any]) -> dict[str, Any]:
    """Read the business index and normalize malformed optional payloads."""
    if not source.get("available") or not source.get("info_file"):
        return {}
    path = Path(str(source["info_file"]))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid business component index {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("bizComponentInfo.json must contain an object")
    return payload


def _business_cache_keys(cache_root: str | None) -> set[tuple[str, str]]:
    """Return ``(package_code, version)`` pairs found in the local cache."""
    if not cache_root:
        return set()
    root = Path(cache_root)
    if not root.is_dir():
        return set()
    result: set[tuple[str, str]] = set()
    try:
        first_level = [item for item in root.iterdir() if item.is_dir()]
    except OSError:
        return result
    # Current layout is biz/<account>/<package-code>/<version>.  Also accept
    # biz/<package-code>/<version> for private deployments and test fixtures.
    for first in first_level:
        try:
            second_level = [item for item in first.iterdir() if item.is_dir()]
        except OSError:
            second_level = []
        for second in second_level:
            # Direct layout: biz/<package-code>/<version>.
            result.add((first.name, second.name))
            try:
                third_level = [item for item in second.iterdir() if item.is_dir()]
            except OSError:
                third_level = []
            for third in third_level:
                # Current layout: biz/<account>/<package-code>/<version>.
                result.add((second.name, third.name))
    return result


def _business_cache_package_paths(cache_root: str | None) -> dict[tuple[str, str], Path]:
    """Index cached business package roots as ``(package_code, version)``."""
    if not cache_root:
        return {}
    root = Path(cache_root)
    if not root.is_dir():
        return {}
    result: dict[tuple[str, str], Path] = {}
    try:
        markers = root.rglob("*.task")
        for task_path in markers:
            version_dir = task_path.parent
            package_dir = version_dir.parent
            if not version_dir.is_dir() or not package_dir.is_dir():
                continue
            key = (package_dir.name, version_dir.name)
            result.setdefault(key, version_dir)
    except OSError:
        return result
    return result


@lru_cache(maxsize=256)
def _load_business_task_node(path: str, modified_ns: int, size: int, component_code: str) -> dict[str, Any]:
    """Load one cached business task node for contract enrichment."""
    del modified_ns, size
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    entities = payload.get("componentModelEntities") if isinstance(payload, dict) else None
    if not isinstance(entities, dict):
        return {}
    expected_type = f"TASK_COMPONENT_{component_code}" if component_code else ""
    for node in entities.values():
        if not isinstance(node, dict):
            continue
        if str(node.get("type") or "") == expected_type:
            return node
    # Business component packages commonly store a complete reusable flow in
    # <component-code>.task rather than a single TASK_COMPONENT node.  The
    # file's existence is still valuable materialization evidence, but it is
    # not a parameter contract, so keep that distinction explicit.
    return {"_task_path": path}


def _business_task_contract(
    package_paths: dict[tuple[str, str], Path],
    package_code: str,
    package_version: str,
    component_code: str,
) -> dict[str, Any]:
    """Find a cached task node and expose its actual native definition."""
    if not package_code or not component_code:
        return {}
    version_dir = package_paths.get((package_code, package_version))
    if not version_dir:
        return {}
    task_path = version_dir / f"{component_code}.task"
    if not task_path.is_file():
        return {}
    try:
        stat = task_path.stat()
    except OSError:
        return {}
    return _load_business_task_node(str(task_path), stat.st_mtime_ns, stat.st_size, component_code)


def _catalog_project_dependency_codes(
    paths: ShizaiPaths, project_ref: str | None
) -> tuple[set[str] | None, str | None]:
    """Read dependency-module names for one project without mutating it."""
    if not project_ref:
        return None, None
    candidate = Path(project_ref).expanduser()
    if not candidate.is_dir():
        candidate = paths.applications_root / project_ref
    if not candidate.is_dir():
        try:
            from .project import resolve_project

            candidate = resolve_project(paths, project_ref)
        except (FileNotFoundError, OSError, ValueError):
            return set(), None
    dependency_dir = candidate / "projects" / "dependency_modules"
    if not dependency_dir.is_dir():
        return set(), str(candidate)
    try:
        return {item.name for item in dependency_dir.iterdir() if item.is_dir()}, str(candidate)
    except OSError:
        return set(), str(candidate)


def _decode_optional_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_business_records(
    source: dict[str, Any],
    paths: ShizaiPaths,
    project_ref: str | None = None,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], dict[str, Any]]:
    """Flatten the business index while preserving package-level counts."""
    payload = _load_business_payload(source)
    cache_keys = _business_cache_keys(source.get("cache_root"))
    cache_paths = _business_cache_package_paths(source.get("cache_root"))
    dependency_codes, project_path = _catalog_project_dependency_codes(paths, project_ref)
    records: list[dict[str, Any]] = []
    packages: list[dict[str, Any]] = []
    category_counts: dict[str, dict[str, int]] = {}
    for category in payload.get("bizComponentInfo", []) if isinstance(payload.get("bizComponentInfo"), list) else []:
        if not isinstance(category, dict):
            continue
        category_name = str(category.get("typeName") or "未分类").strip() or "未分类"
        category_stat = category_counts.setdefault(category_name, {"packages": 0, "advertised": 0, "visible": 0})
        for package in category.get("childList", []) if isinstance(category.get("childList"), list) else []:
            if not isinstance(package, dict):
                continue
            package_name = str(package.get("typeName") or "未命名业务包").strip() or "未命名业务包"
            leaves = (
                [item for item in package.get("childList", []) if isinstance(item, dict)]
                if isinstance(package.get("childList"), list)
                else []
            )
            advertised = _int_value(package.get("releaseComponentTotal"), len(leaves))
            package_code = ""
            package_version = ""
            for leaf in leaves:
                package_code = package_code or str(leaf.get("componentPackageCode") or "").strip()
                package_version = package_version or str(leaf.get("componentPackageVersion") or "").strip()
            cache_present = bool(
                package_code
                and any(
                    code == package_code and (not package_version or version == package_version)
                    for code, version in cache_keys
                )
            )
            project_imported = bool(package_code and dependency_codes is not None and package_code in dependency_codes)
            package_status = (
                "project_imported" if project_imported else "installed" if cache_present else "catalog_only"
            )
            package_row = {
                "scope": "business",
                "category": category_name,
                "package": package_name,
                "package_code": package_code or None,
                "version": package_version or None,
                "advertised_component_count": advertised,
                "visible_component_count": len(leaves),
                "availability": package_status,
                "package_cache_available": cache_present,
                "project_imported": project_imported if dependency_codes is not None else None,
                "requires_import": not project_imported if dependency_codes is not None else not cache_present,
                "deployment": "saas_extension",
            }
            packages.append(package_row)
            category_stat["packages"] += 1
            category_stat["advertised"] += advertised
            category_stat["visible"] += len(leaves)
            for leaf in leaves:
                type_info = _decode_optional_object(leaf.get("typeInfo"))
                source_info = _decode_optional_object(leaf.get("componentSourceInfo"))
                component_code = str(leaf.get("componentCode") or "").strip()
                type_code = str(leaf.get("typeCode") or "").strip()
                native_type = f"TASK_COMPONENT_{component_code}" if component_code else None
                leaf_package_code = str(leaf.get("componentPackageCode") or package_code).strip()
                leaf_package_version = str(leaf.get("componentPackageVersion") or package_version).strip()
                cached_node = _business_task_contract(
                    cache_paths,
                    leaf_package_code,
                    leaf_package_version,
                    component_code,
                )
                cached_task_available = bool(cached_node)
                cached_args = cached_node.get("args0") if isinstance(cached_node.get("args0"), list) else []
                cached_options = cached_node.get("options0") if isinstance(cached_node.get("options0"), list) else []
                source_definition = (
                    source_info.get("definition") if isinstance(source_info.get("definition"), dict) else {}
                )
                definition = {
                    "args0": cached_args or source_definition.get("args0", []),
                    "options0": cached_options or source_definition.get("options0", []),
                }
                leaf_cache_present = bool(
                    leaf_package_code
                    and any(
                        code == leaf_package_code and (not leaf_package_version or version == leaf_package_version)
                        for code, version in cache_keys
                    )
                )
                leaf_imported = bool(
                    leaf_package_code and dependency_codes is not None and leaf_package_code in dependency_codes
                )
                records.append(
                    {
                        "scope": "business",
                        "type": native_type,
                        "native_type": native_type,
                        "component_code": component_code or None,
                        "type_code": type_code or None,
                        "label": str(leaf.get("typeName") or type_info.get("componentName") or package_name),
                        "category": [category_name],
                        "business_category": category_name,
                        "business_package": package_name,
                        "package": package_name,
                        "package_code": leaf_package_code or None,
                        "description": str(type_info.get("componentDesc") or ""),
                        "version": str(leaf.get("componentVersion") or leaf_package_version or ""),
                        "release_tag": leaf.get("typeStatus"),
                        "component_modes": source_info.get("componentMode") or [],
                        "tags": [category_name, package_name],
                        "definition": definition,
                        "codeDesc": [],
                        "documentation_url": type_info.get("componentIllustrateUrl"),
                        "availability": "project_imported"
                        if leaf_imported
                        else "installed"
                        if leaf_cache_present
                        else "catalog_only",
                        "package_cache_available": leaf_cache_present,
                        "project_imported": leaf_imported if dependency_codes is not None else None,
                        "requires_import": not leaf_imported
                        if dependency_codes is not None
                        else not leaf_cache_present,
                        "deployment": "saas_extension",
                        "materializable": bool(component_code and cached_task_available),
                        "contract_source": "cached_task" if cached_task_available else "business_index",
                    }
                )
    metadata = {
        "source": source,
        "project": project_path,
        "project_dependency_codes": sorted(dependency_codes) if dependency_codes is not None else None,
        "categories": [{"name": name, **values} for name, values in sorted(category_counts.items())],
        "advertised_component_total": sum(item["advertised"] for item in category_counts.values()),
        "visible_component_total": sum(item["visible"] for item in category_counts.values()),
        "package_total": len(packages),
    }
    return tuple(records), tuple(packages), metadata


def _compact_value(value: Any, depth: int = 0) -> Any:
    """Keep parameter metadata useful without returning UI-only megabytes."""
    if depth > 2:
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_compact_value(item, depth + 1) for item in value[:30]]
    if isinstance(value, dict):
        return {str(key): _compact_value(item, depth + 1) for key, item in list(value.items())[:40]}
    return str(value)


def _parameter_contract(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    keys = (
        "name",
        "label",
        "desc",
        "type",
        "defaultValue",
        "isExport",
        "isAdvanced",
        "role",
        "vType",
        "displayType",
        "displayParam",
        "valueType",
        "children",
        "wrapperType",
        "wrapperParam",
        "validatorRules",
        "tip",
    )
    result = {key: _compact_value(value[key]) for key in keys if key in value}
    if not result.get("name"):
        return None
    if "defaultValue" in value and isinstance(value["defaultValue"], str):
        try:
            result["semantic_default"] = json.loads(value["defaultValue"])
        except (ValueError, TypeError):
            pass
    if value.get("displayType") in {"dropDownSelect", "buttonGroup", "radioGroup"} and isinstance(value.get("displayParam"), list):
        choices = []
        for option in value["displayParam"]:
            if not isinstance(option, dict) or "value" not in option:
                break
            try:
                decoded = json.loads(option["value"]) if isinstance(option["value"], str) else option["value"]
            except (ValueError, TypeError):
                # An expression or an unknown native encoding is not an enum.
                break
            choices.append({"label": option.get("label"), "value": decoded})
        else:
            if choices:
                result["semantic_choices"] = choices
    return result


def _component_outputs(record: dict[str, Any], args: list[Any]) -> dict[str, Any]:
    assign_to: list[str] = []
    for item in record.get("codeDesc", []) if isinstance(record.get("codeDesc"), list) else []:
        if isinstance(item, dict) and item.get("assignTo"):
            raw_names = item["assignTo"] if isinstance(item["assignTo"], list) else str(item["assignTo"]).split(",")
            for raw_name in raw_names:
                name = str(raw_name).strip()
                if name and name not in assign_to:
                    assign_to.append(name)
    exported = [
        str(item["name"])
        for item in args
        if isinstance(item, dict) and item.get("name") and item.get("isExport") in (1, 3)
    ]
    default_values = {
        str(item["name"]): _compact_value(item.get("defaultValue"))
        for item in args
        if isinstance(item, dict)
        and item.get("name")
        and item.get("isExport") in (1, 3)
        and item.get("defaultValue") not in (None, "")
    }
    logical_defaults = [
        str(default_values[name]).strip()
        for name in assign_to
        if name in default_values and str(default_values[name]).strip()
    ]
    return {
        "assign_to": assign_to,
        "native_assignment_parameters": assign_to,
        "exported_parameters": exported,
        "primary": assign_to[0] if assign_to else (exported[0] if exported else None),
        "declared_defaults": default_values,
        "logical_defaults": logical_defaults,
        "planning_rule": (
            "omit plan.steps[].outputs and let MCP derive logical variables; "
            "assign_to/native_assignment_parameters are native config slots, not logical output names"
        ),
    }


def component_capabilities(record: dict[str, Any]) -> dict[str, Any]:
    """Infer planning semantics from one live component contract.

    The inference intentionally uses stable contract signals (component type,
    categories, parameter names, and outputs) instead of a version whitelist.
    New designer revisions therefore remain usable without an MCP release.
    """
    component_type = str(record.get("type") or record.get("native_type") or "").strip()
    lowered_type = component_type.casefold()
    categories = {item.casefold() for item in _category_values(record)}
    inputs = record.get("inputs") if isinstance(record.get("inputs"), list) else []
    input_names = {
        str(item.get("name") or "").strip()
        for item in inputs
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    outputs = record.get("outputs") if isinstance(record.get("outputs"), dict) else {}
    has_output = bool(outputs.get("primary") or outputs.get("assign_to"))
    capabilities: set[str] = set()

    # File/text/table components need discovery verbs too. Historically this
    # index focused on browser extraction, so File_Read/Write were invisible
    # to the very read/write queries small models naturally issue.
    words = set(re.sub(r"([a-z])([A-Z])", r"\1_\2", component_type).casefold().split("_"))
    family = lowered_type.split("_", 1)[0]
    if family == "file":
        capabilities.add("file")
    if family in {"strutil", "regex"}:
        capabilities.update(("text", "transform"))
    if family in {"excel", "bivartable"} or "csv" in words:
        capabilities.add("table")
    if family == "excel":
        capabilities.add("excel")
    if "csv" in words:
        capabilities.add("csv")
    if family in {"file", "excel", "bivartable"}:
        if "read" in words:
            capabilities.add("read")
        if words.intersection({"write", "save", "export"}):
            capabilities.update(("write", "output"))
        if "copy" in words:
            capabilities.add("copy")

    if any(
        token in lowered_type
        for token in ("webbrowser_create", "webbrowser_gettab", "webbrowser_gourl", "webbrowser_switch")
    ):
        capabilities.add("navigate")
    if "gettab" in lowered_type:
        capabilities.update(("navigate", "page_context"))
    if "uiselector" in {name.casefold() for name in input_names}:
        capabilities.add("page_element")
    if "setvalue" in lowered_type or (
        "uiSelector" in input_names and bool({"text", "words", "value"}.intersection(input_names))
    ):
        capabilities.add("input")
    if (
        any(token in lowered_type for token in ("element_click", "image_click", "mouseclick", "keyboard_press", "keyboard_send"))
        or "isEnter" in input_names
    ):
        capabilities.add("submit")
    collection_signals = {
        "config",
        "collect_items",
        "collect_pages",
        "selector_page",
        "selector_more",
        "toExcel",
    }
    if any(token in lowered_type for token in ("datacollection", "datacrawl", "collect", "extract")) or (
        bool(collection_signals.intersection(input_names))
        and bool(categories.intersection({"get_web_data", "data_parse", "get_soft_data"}))
    ):
        capabilities.add("extract")
    similar_element_list = "similarelementlist" in lowered_type and {"uiSelector", "action"}.issubset(input_names)
    if similar_element_list:
        # This native component can return repeated text, values, attributes,
        # or absolute URLs in one call. Surface that behavior to capability
        # searches so a small planner can find it as a collection fallback.
        capabilities.update(("extract", "list", "link"))
    if categories.intersection({"data_parse", "data_process"}) or "deduplication" in input_names:
        capabilities.add("transform")
    if {"collect_items", "max_rows", "limit"}.intersection(input_names):
        capabilities.add("limit")
    if {"collect_pages", "selector_page", "selector_more", "page_type"}.intersection(input_names):
        capabilities.add("paginate")
    if {"toExcel", "filePath", "filename_suffix", "save"}.intersection(input_names) or any(
        token in lowered_type for token in ("writerange", "writefile", "export", "save")
    ):
        capabilities.add("output")
    if any(token in lowered_type for token in ("delay", "wait")):
        capabilities.add("wait")
    if has_output:
        capabilities.add("produces_variable")

    plan_input_mode = (
        "none"
        if lowered_type.startswith("keyboard_")
        or lowered_type.startswith("basic_delay")
        or lowered_type.startswith("webbrowser_create")
        or lowered_type.startswith("webbrowser_gettab")
        else "variables"
    )

    operation_types: set[str] = set()
    if "navigate" in capabilities:
        operation_types.add("navigate")
    if capabilities.intersection({"input", "submit"}):
        operation_types.update(("search", "interact"))
    if "extract" in capabilities:
        operation_types.add("extract")
    if "transform" in capabilities:
        operation_types.add("transform")
    if "output" in capabilities:
        operation_types.add("output")

    deferred_bindings = [
        name
        for name in ("uiSelector", "config", "selector_page", "selector_more")
        if name in input_names
    ]
    conditional_requirements = []
    for item in inputs:
        if not isinstance(item, dict) or not item.get("name") or not isinstance(item.get("wrapperParam"), dict):
            continue
        conditional_requirements.append(
            {
                "parameter": str(item["name"]),
                "when": _compact_value(item["wrapperParam"]),
            }
        )

    hints: dict[str, Any] = {
        "semantic_values": (
            "Plan config uses semantic JSON values. Do not copy native encoded defaults; "
            "string parameters take plain JSON strings without embedded quotes. "
            'Runtime object parameters take {"variable":"output_name"} from a preceding producer, '
            "never a browser name, default variable label, or quoted placeholder. "
            "Run the object producer/navigation during design before inspecting its page."
        )
    }
    if "extract" in capabilities:
        hints["page_evidence"] = "Bind collection fields from the live result page after navigation."
    if similar_element_list:
        hints["similar_element_actions"] = {
            "selector": "return native element objects",
            "gettext": "return visible text values",
            "value": "return element values",
            "absoluteurl": "return href/src values; set attr_complate=true for absolute URLs",
            "attr": "return attr_name values",
        }
        hints["alignment_rule"] = (
            "The output is one flat list. When combining it with another table or other field lists, "
            "verify equal row counts and preserve row order before writing the final artifact."
        )
    if component_type == "WebBrowser_DataCollectionV5":
        hints["runtime_capability_limits"] = {
            "table_nested_attribute": (
                "When collectionConfig.tableSelector is populated, the installed RuntimeHost reads table-cell text. "
                "A link column configured as dataAttrName=href can therefore return the nested anchor text instead of href."
            ),
            "required_action": (
                "Validate a real output row. If the link column is not a URL, stop retrying column selectors and choose "
                "a live, materializable alternative such as Element_SimilarElementList_V2(action=absoluteurl), another "
                "native collection component, or a justified generic preprocessing step."
            ),
        }
    if "limit" in capabilities:
        hints["row_limit_parameters"] = [name for name in ("collect_items", "max_rows", "limit") if name in input_names]
    if "paginate" in capabilities:
        hints["pagination_parameters"] = [
            name for name in ("region", "collect_pages", "page_type", "selector_page", "selector_more") if name in input_names
        ]
        if "region" in input_names:
            hints["region_modes"] = {
                "0": {
                    "meaning": "all_pages",
                    "use_when": "the request asks for every available page without an exact row target",
                    "configure": ["page_type", "selector_page_or_selector_more"],
                    "ignore_for_acceptance": ["collect_pages", "collect_items"],
                },
                "1": {
                    "meaning": "current_page",
                    "use_when": "the request explicitly limits collection to the current page or first screen",
                    "configure": [],
                },
                "2": {
                    "meaning": "specified_page_count",
                    "use_when": "the request specifies a number of pages",
                    "configure": ["collect_pages", "page_type", "selector_page_or_selector_more"],
                },
                "3": {
                    "meaning": "specified_item_count",
                    "use_when": "the request specifies an exact or minimum row target",
                    "configure": ["collect_items", "page_type_num", "selector_page_or_selector_more"],
                },
            }
            hints["region_selection_rule"] = (
                "Choose region from the user's collection scope. Component defaults such as collect_items=20 "
                "are runtime parameters, not an output acceptance target. Do not invent output_contract.max_rows "
                "when the user asks for all pages without a row count."
            )
        # Pagination is an executable capability of the native collection
        # component, not merely a hint for the client model.  Expose the
        # ownership and stop condition explicitly so a planner does not add
        # a redundant click/loop step around the component.
        hints["pagination_execution"] = {
            "owner": "selected_component_or_model_composition",
            "mode": "advisory",
            "behavior": (
                "This component can traverse next-page or load-more controls internally when configured; "
                "the client model may instead compose explicit page actions, loops, JavaScript, or another strategy."
            ),
            "requires": "bind only the selectors required by this component, from live page evidence",
            "alternatives": ["explicit_next_page_step", "loop", "javascript", "download"],
        }
    if "output" in capabilities:
        hints["output_parameters"] = [
            name for name in ("toExcel", "filename_suffix", "filePath", "save") if name in input_names
        ]
    planning_patterns = []
    if {"extract", "limit", "paginate"}.issubset(capabilities):
        all_pages_config: dict[str, Any] = {}
        if "region" in input_names:
            all_pages_config["region"] = 0
        if "page_type" in input_names:
            all_pages_config["page_type"] = "$choose_from_live_page_evidence"
        if "selector_page" in input_names:
            all_pages_config["selector_page"] = "$defer_until_next_page_element_is_captured"
        if "selector_more" in input_names:
            all_pages_config["selector_more"] = "$defer_until_load_more_element_is_captured"
        planning_patterns.append(
            {
                "when": "request asks for all available pages and has no numeric row target",
                "goal": "collect every available page until the pagination control is unavailable",
                "semantic_config": all_pages_config,
                "output_contract": {"max_rows": None},
                "selection_rule": (
                    "Use the component's all-pages mode when its native pagination control fits the live page; "
                    "capture only the selected next-page or load-more control."
                ),
            }
        )
        pattern_config: dict[str, Any] = {}
        if "region" in input_names:
            pattern_config["region"] = 3
        if "collect_items" in input_names:
            pattern_config["collect_items"] = "$output_contract.max_rows"
        if "page_type" in input_names:
            pattern_config["page_type"] = "$choose_from_live_page_evidence"
        if "selector_page" in input_names:
            pattern_config["selector_page"] = "$defer_until_next_page_element_is_captured"
        if "selector_more" in input_names:
            pattern_config["selector_more"] = "$defer_until_load_more_element_is_captured"
        planning_patterns.append(
            {
                "when": "request has max_rows and may need multiple pages",
                "goal": "collect valid rows until max_rows or no next page remains",
                "semantic_config": pattern_config,
                "selection_rule": (
                    "Consider this component when its native pagination parameters fit the live page; "
                    "the model may choose another component or compose multiple steps."
                ),
            }
        )
    if "output" in capabilities and "toExcel" in input_names:
        planning_patterns.append(
            {
                "when": "the requested destination is a result file",
                "goal": "let the collection component write its table output directly",
                "semantic_config": {"toExcel": True},
            }
        )
    if {"input", "submit"}.issubset(capabilities) and "isEnter" in input_names:
        planning_patterns.append(
            {
                "when": "a search input should be submitted immediately after setting its value",
                "goal": "set the query and submit it with the same installed element component",
                "semantic_config": {"text": "$request_search_value", "isEnter": True},
            }
        )
    if similar_element_list:
        planning_patterns.append(
            {
                "when": "the page contains repeated product/detail anchors and a real URL list is required",
                "goal": "extract every repeated href/src value with one native component call",
                "semantic_config": {
                    "action": "absoluteurl",
                    "attr_complate": True,
                    "uiSelector": "$defer_until_two_repeated_links_are_verified",
                },
                "selection_rule": (
                    "Use current-page evidence for the repeated link selector. Verify list length and order before "
                    "combining it with other collected fields."
                ),
            }
        )
    return {
        "capabilities": sorted(capabilities),
        "operation_types": sorted(operation_types),
        "requires_page_evidence": bool(deferred_bindings),
        "plan_input_mode": plan_input_mode,
        "plan_input_rule": (
            "inputs must be empty; configure literal component arguments in config"
            if plan_input_mode == "none"
            else "inputs lists only runtime variables produced by earlier dependent steps"
        ),
        "deferred_bindings": deferred_bindings,
        "configuration_hints": hints,
        "conditional_requirements": conditional_requirements,
        "planning_patterns": planning_patterns,
    }


def _package_name(source: dict[str, Any], record: dict[str, Any]) -> str | None:
    base_type = str(record.get("baseType") or record.get("type") or "")
    prefix = base_type.split("_", 1)[0] if "_" in base_type else base_type
    if not prefix:
        return None
    package_dir = Path(str(source["engine_root"])) / "311" / "packages" / prefix
    return prefix if package_dir.is_dir() else None


def _top_level_values(record: dict[str, Any]) -> list[dict[str, str]]:
    """Return visible first-level menus matching a component's categories."""
    categories = {item.casefold() for item in _category_values(record)}
    result: list[dict[str, str]] = []
    for key, label, internal_categories in _TOP_LEVELS:
        if categories.intersection(item.casefold() for item in internal_categories):
            result.append({"id": key, "label": label})
    return result


def _designer_hidden(record: dict[str, Any]) -> bool:
    """Mirror the designer's EReleaseTag.hidden (128), not a version ranking.

    Confirmed in installed 7.3.6.1 vendor.js (old/new enum conversion) and
    app-package.js getComponentTemplateMap visibility filtering.
    """
    return bool(_int_value(record.get("releaseTag")) & 128)


def _builtin_records(source: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Annotate engine records and merge the designer-only static entries."""
    if not source.get("available"):
        return ()
    by_type: dict[str, dict[str, Any]] = {}
    for original in load_component_records(source):
        record = dict(original)
        record["scope"] = "builtin"
        record["deployment"] = "local_designer"
        record["availability"] = "installed"
        record["requires_import"] = False
        record["materializable"] = True
        record["designer_static"] = str(record.get("type") or "") in _STATIC_BASE_COMMAND_TYPES
        record["designer_static_index"] = record["designer_static"]
        if record["designer_static"]:
            record["materializable"] = False
        record["designer_static_extra"] = False
        record["native_type"] = record.get("type")
        by_type[str(record.get("type") or "")] = record
    for static in _STATIC_BASE_COMMANDS:
        component_type = str(static["type"])
        if component_type not in by_type:
            record = dict(static)
            record.update(
                {
                    "scope": "builtin",
                    "deployment": "local_designer",
                    "availability": "installed",
                    "requires_import": False,
                    "materializable": False,
                    "native_type": component_type,
                    "designer_static_index": True,
                    "designer_static_extra": True,
                }
            )
            by_type[component_type] = record
    return tuple(by_type.values())


def _summary(source: dict[str, Any], record: dict[str, Any], detail: str) -> dict[str, Any]:
    definition = record.get("definition") if isinstance(record.get("definition"), dict) else {}
    args = definition.get("args0") if isinstance(definition.get("args0"), list) else []
    options = definition.get("options0") if isinstance(definition.get("options0"), list) else []
    categories = _category_values(record)
    package = record.get("package") or _package_name(source, record)
    result: dict[str, Any] = {
        "scope": record.get("scope", "builtin"),
        "type": record.get("type"),
        "canonical_type": COMPONENT_TYPE_ALIASES.get(str(record.get("type") or ""), record.get("type")),
        "native_type": record.get("native_type") or record.get("type"),
        "label": record.get("label"),
        "category": categories[0] if len(categories) == 1 else categories,
        "categories": categories,
        "top_levels": _top_level_values(record),
        "description": record.get("description") or "",
        "base_type": record.get("baseType"),
        "version": record.get("version"),
        "release_tag": record.get("releaseTag"),
        "component_modes": record.get("componentMode") or [],
        "tags": [str(item.get("label")) for item in _tag_items(record) if item.get("label")],
        "package": package,
        "deployment": record.get("deployment", "local_designer"),
        "availability": record.get("availability", "installed"),
        "requires_import": bool(record.get("requires_import", False)),
        "materializable": bool(record.get("materializable", True)),
        "designer_static": bool(record.get("designer_static", False)),
        "designer_static_index": bool(record.get("designer_static_index", False)),
        "designer_hidden": _designer_hidden(record),
    }
    if record.get("type") == "File_Write" and record.get("version") == "3.3.5.0":
        # Verified against File.Write in this installed plugin version during
        # independent native runs. Advisory only; never substitute a type.
        result["runtime_preconditions"] = [
            "File_Write / File.Write 3.3.5.0 要求目标文件在执行本步骤前已存在；"
            "它不会创建缺失文件。新建输出请查询描述明确支持自动新建的 File_Write_V1 契约，"
            "或由前置步骤创建目标。文件不存在错误不能靠更换路径斜杠修复。"
        ]
    if detail in {"contract", "full"}:
        result["inputs"] = [item for item in (_parameter_contract(value) for value in args) if item]
        result["options"] = [item for item in (_parameter_contract(value) for value in options) if item]
        result["outputs"] = _component_outputs(record, args)
        result["code"] = _compact_value(record.get("codeDesc", []))
        result.update(component_capabilities(result))
    if result["designer_static"] or result["designer_static_index"]:
        result["materializable"] = False
        result["materialization_errors"] = ["designer-static entries are not executable component nodes"]
    if detail == "full":
        result["definition"] = _compact_value(definition)
        result["icon"] = record.get("icon")
        result["logo"] = record.get("logo")
    return result


def _business_summary(record: dict[str, Any], detail: str) -> dict[str, Any]:
    """Return a compact business-component row safe for MCP responses."""
    definition = record.get("definition") if isinstance(record.get("definition"), dict) else {}
    args = definition.get("args0") if isinstance(definition.get("args0"), list) else []
    options = definition.get("options0") if isinstance(definition.get("options0"), list) else []
    result: dict[str, Any] = {
        "scope": "business",
        "type": record.get("type"),
        "native_type": record.get("native_type"),
        "component_code": record.get("component_code"),
        "type_code": record.get("type_code"),
        "label": record.get("label"),
        "category": record.get("business_category"),
        "business_category": record.get("business_category"),
        "business_package": record.get("business_package"),
        "package": record.get("package"),
        "package_code": record.get("package_code"),
        "description": record.get("description") or "",
        "version": record.get("version"),
        "component_modes": record.get("component_modes") or [],
        "tags": record.get("tags") or [],
        "deployment": "saas_extension",
        "availability": record.get("availability"),
        "package_cache_available": bool(record.get("package_cache_available", False)),
        "project_imported": record.get("project_imported"),
        "requires_import": bool(record.get("requires_import", True)),
        "materializable": bool(record.get("materializable", False)),
        "documentation_url": record.get("documentation_url"),
        "contract_source": record.get("contract_source", "business_index"),
    }
    if detail in {"contract", "full"}:
        result["inputs"] = [item for item in (_parameter_contract(value) for value in args) if item]
        result["options"] = [item for item in (_parameter_contract(value) for value in options) if item]
        result["outputs"] = _component_outputs(record, args)
        result["code"] = []
        result.update(component_capabilities(result))
    if detail == "full":
        result["definition"] = _compact_value(definition)
    return result


def _matches(
    record: dict[str, Any],
    query: str,
    category: str,
    component_type: str,
    package: str,
    capability: str = "",
    capability_mode: str = "all",
    top_level: str = "",
    business_category: str = "",
    business_package: str = "",
) -> bool:
    if component_type and str(record.get("type") or "") != component_type:
        return False
    if category and category.casefold() not in {item.casefold() for item in _category_values(record)}:
        return False
    if (
        top_level
        and top_level.casefold() not in {key.casefold() for key, label, _categories in _TOP_LEVELS}
        and top_level.casefold() not in {label.casefold() for _key, label, _categories in _TOP_LEVELS}
    ):
        return False
    if top_level:
        matched_top_levels = _top_level_values(record)
        if top_level.casefold() not in {
            item["id"].casefold() for item in matched_top_levels
        } and top_level.casefold() not in {item["label"].casefold() for item in matched_top_levels}:
            return False
    if business_category and str(record.get("business_category") or "").casefold() != business_category.casefold():
        return False
    if business_package and str(record.get("business_package") or "").casefold() != business_package.casefold():
        return False
    if package:
        if record.get("scope") == "business":
            package_value = str(record.get("business_package") or "")
            if package_value.casefold() != package.casefold():
                return False
        else:
            base = str(record.get("baseType") or record.get("type") or "")
            prefix = base.split("_", 1)[0] if "_" in base else base
            if prefix.casefold() != package.casefold():
                return False
    if capability:
        normalized_record = dict(record)
        if "inputs" not in normalized_record:
            definition = record.get("definition") if isinstance(record.get("definition"), dict) else {}
            normalized_record["inputs"] = definition.get("args0") if isinstance(definition.get("args0"), list) else []
        requested = {item.strip().casefold() for item in re.split(r"[,，\s]+", capability) if item.strip()}
        observed = {item.casefold() for item in component_capabilities(normalized_record)["capabilities"]}
        capability_match = bool(requested.intersection(observed)) if capability_mode == "any" else requested.issubset(observed)
        if not capability_match:
            return False
    if not query:
        return True
    fields = [
        record.get("type"),
        record.get("label"),
        *(_category_values(record)),
        record.get("description"),
        record.get("business_category"),
        record.get("business_package"),
        record.get("component_code"),
        record.get("type_code"),
    ]
    fields.extend(item.get("label") for item in _tag_items(record))
    haystack = " ".join(str(item or "") for item in fields).casefold()
    component_name = str(record.get("type") or "").casefold()
    if "runjs" in component_name or "javascript" in haystack:
        # The designer calls this family "执行JS", while models commonly ask
        # for JavaScript, browser scripts, or page scripts. These aliases are
        # component vocabulary only; they do not encode any website behavior.
        haystack += " javascript javascript代码 js脚本 执行脚本 浏览器脚本 网页脚本 网页执行"
    return all(part in haystack for part in query.casefold().split() if part)


def _tag_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize optional designer tags from old and new component builds."""
    value = record.get("tag")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _category_values(record: dict[str, Any]) -> list[str]:
    """Normalize category fields that changed from string to array."""
    value = record.get("category")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None or not str(value).strip():
        return []
    return [str(value).strip()]


def component_catalog(
    paths: ShizaiPaths,
    *,
    scope: str = "all",
    query: str = "",
    category: str = "",
    component_type: str = "",
    package: str = "",
    capability: str = "",
    capability_mode: str = "all",
    top_level: str = "",
    business_category: str = "",
    business_package: str = "",
    project_ref: str | None = None,
    page: int = 1,
    limit: int = DEFAULT_LIMIT,
    detail: str = "contract",
    response_mode: str = "full",
    include_hidden: bool = True,
    engine_root: str | None = None,
    business_info_path: str | None = None,
    business_cache_root: str | None = None,
) -> dict[str, Any]:
    """Return a searchable catalog of built-in and SaaS business components.

    ``builtin`` is the local designer engine dictionary plus its two static
    designer-only entries. ``business`` is the account's SaaS extension index;
    its package and project-import status are returned separately. ``all``
    keeps built-ins first so existing agents can page through native tools
    before optional extensions.
    """
    if scope not in {"builtin", "business", "all"}:
        raise ValueError("scope must be builtin, business, or all")
    if page < 1:
        raise ValueError("page must be at least 1")
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    if detail not in {"summary", "contract", "full"}:
        raise ValueError("detail must be summary, contract, or full")
    if response_mode not in {"full", "compact"}:
        raise ValueError("response_mode must be full or compact")
    if capability_mode not in {"all", "any"}:
        raise ValueError("capability_mode must be all or any")

    builtin_source = component_source(paths, engine_root=engine_root)
    business_source = business_component_source(
        paths,
        info_path=business_info_path,
        cache_root=business_cache_root,
    )
    builtin_records = _builtin_records(builtin_source)
    business_records: tuple[dict[str, Any], ...] = ()
    business_packages: tuple[dict[str, Any], ...] = ()
    business_meta: dict[str, Any] = {
        "categories": [],
        "advertised_component_total": 0,
        "visible_component_total": 0,
        "package_total": 0,
        "source": business_source,
        "project": None,
    }
    if business_source["available"]:
        business_records, business_packages, business_meta = load_business_records(
            business_source,
            paths,
            project_ref=project_ref,
        )
    if scope == "builtin":
        records = builtin_records
    elif scope == "business":
        records = business_records
    else:
        records = (*builtin_records, *business_records)
    if not include_hidden:
        # Existing flows must remain inspectable by exact native type. Only
        # discovery follows the designer's visible new-component palette.
        records = tuple(record for record in records if record.get("scope") != "builtin"
                        or not _designer_hidden(record) or record.get("type") == component_type.strip())
    matches = [
        record
        for record in records
        if _matches(
            record,
            query.strip(),
            category.strip(),
            component_type.strip(),
            package.strip(),
            capability.strip(),
            capability_mode,
            top_level.strip(),
            business_category.strip(),
            business_package.strip(),
        )
    ]
    relaxed_filters: list[str] = []
    # Client models sometimes turn the semantic capability request into a
    # long free-text query (for example, joining every role name).  That query
    # is stricter than the live labels and can hide an otherwise valid
    # component.  If capability filtering produced no result, retry once
    # internally with only structured filters; this is still one catalog call
    # from the model and never guesses a component name.
    if not matches and capability.strip():
        relaxed_matches = [
            record
            for record in records
            if _matches(
                record,
                "",
                category.strip(),
                component_type.strip(),
                package.strip(),
                capability.strip(),
                capability_mode,
                "",
                business_category.strip(),
                business_package.strip(),
            )
        ]
        if relaxed_matches:
            matches = relaxed_matches
            if query.strip():
                relaxed_filters.append("query")
            if top_level.strip():
                relaxed_filters.append("top_level")
    if capability.strip() and capability_mode == "any":
        requested_capabilities = {
            item.strip().casefold() for item in re.split(r"[,，\s]+", capability) if item.strip()
        }

        def capability_rank(record: dict[str, Any]) -> tuple[int, int, str]:
            normalized_record = dict(record)
            if "inputs" not in normalized_record:
                definition = record.get("definition") if isinstance(record.get("definition"), dict) else {}
                normalized_record["inputs"] = (
                    definition.get("args0") if isinstance(definition.get("args0"), list) else []
                )
            inferred = component_capabilities(normalized_record)
            observed = {item.casefold() for item in inferred["capabilities"]}
            return (
                len(requested_capabilities.intersection(observed)),
                len(inferred["planning_patterns"]),
                str(record.get("type") or "").casefold(),
            )

        matches.sort(key=capability_rank, reverse=True)
    start = (page - 1) * limit
    selected = matches[start : start + limit]
    category_counts: dict[str, int] = {}
    for record in matches:
        categories = _category_values(record) or ["未分类"]
        for name in categories:
            category_counts[name] = category_counts.get(name, 0) + 1
    top_level_counts: dict[str, int] = {}
    for record in matches:
        for item in _top_level_values(record):
            top_level_counts[item["id"]] = top_level_counts.get(item["id"], 0) + 1
    package_matches = []
    for item in business_packages:
        if business_category and str(item.get("category") or "").casefold() != business_category.strip().casefold():
            continue
        if business_package and str(item.get("package") or "").casefold() != business_package.strip().casefold():
            continue
        if package and str(item.get("package") or "").casefold() != package.strip().casefold():
            continue
        if query.strip():
            haystack = " ".join(
                str(item.get(key) or "") for key in ("category", "package", "package_code", "version")
            ).casefold()
            if not all(part in haystack for part in query.casefold().split() if part):
                continue
        package_matches.append(item)
    matched_business_advertised = sum(_int_value(item.get("advertised_component_count")) for item in package_matches)
    selected_components = [
        _summary(builtin_source, record, detail)
        if record.get("scope") == "builtin"
        else _business_summary(record, detail)
        for record in selected
    ]
    # Give the client model an explicit role-level shortlist.  The catalog is
    # still live and request-driven; this is guidance derived from the
    # capability filter, not a site recipe or a fixed component sequence.
    requested_capabilities = {
        item.strip().casefold() for item in re.split(r"[,，\s]+", capability) if item.strip()
    }
    collection_guidance = None
    if {"extract", "limit", "paginate"}.intersection(requested_capabilities):
        required_collection = {"extract", "limit", "paginate", "output"}
        # Build the numeric-collection shortlist from the filtered result first.
        # A model may accidentally send a long free-text query or request
        # ``capability_mode=all`` with navigation/search capabilities that no
        # single collection component can expose.  Those filters must not hide
        # the live component that actually satisfies the collection contract.
        def _collection_candidates(records_to_scan: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
            candidates: list[dict[str, Any]] = []
            for record in records_to_scan:
                normalized_record = dict(record)
                if "inputs" not in normalized_record:
                    definition = record.get("definition") if isinstance(record.get("definition"), dict) else {}
                    normalized_record["inputs"] = (
                        definition.get("args0") if isinstance(definition.get("args0"), list) else []
                    )
                contract = component_capabilities(normalized_record)
                observed = set(contract.get("capabilities") or [])
                if not required_collection.issubset(observed):
                    continue
                output_contract = _component_outputs(record, normalized_record["inputs"])
                candidates.append(
                    {
                        "component": record.get("type"),
                        "canonical_component": COMPONENT_TYPE_ALIASES.get(
                            str(record.get("type") or ""), record.get("type")
                        ),
                        "label": record.get("label") or record.get("type"),
                        "capabilities": sorted(observed),
                        "operation_types": contract.get("operation_types") or [],
                        "plan_input_mode": contract.get("plan_input_mode", "variables"),
                        "plan_input_rule": contract.get(
                            "plan_input_rule",
                            "inputs lists only runtime variables produced by earlier dependent steps",
                        ),
                        "planning_patterns": contract.get("planning_patterns") or [],
                        "outputs": output_contract,
                        "required_outputs": (
                            list(output_contract.get("declared_defaults", {}).values())
                            if isinstance(output_contract.get("declared_defaults"), dict)
                            else []
                        ),
                        "output_binding_rule": (
                            "Step outputs must exactly match the component contract. For exported parameters, "
                            "use outputs.declared_defaults values when present (these are the runtime "
                            "logical names); do not use assign_to slot names or invent custom variables."
                        ),
                        "selection_reason": "live candidate exposes useful extraction/output capabilities; final composition is chosen by the model",
                    }
                )
            return candidates

        collection_candidates = _collection_candidates(matches)
        fallback_applied = False
        if not collection_candidates:
            # Contract fallback: discover the collection role from the live
            # dictionary even when the caller's menu/free-text filters are
            # incompatible with the component's display labels.  This keeps
            # pagination discovery deterministic without guessing a component
            # name or issuing a second MCP call.
            collection_candidates = _collection_candidates(records)
            fallback_applied = bool(collection_candidates)
        recommended_collection = collection_candidates[0] if collection_candidates else None
        collection_guidance = {
            "role": "structured_collection",
            "required_capabilities": sorted(required_collection),
            "must_select_for_numeric_target": False,
            "selection_policy": "advisory",
            "selection": {
                "status": "ready" if recommended_collection else "missing",
                "must_use": False,
                "fallback_applied": fallback_applied,
                "recommended_component": (
                    recommended_collection.get("component") if recommended_collection else None
                ),
                "candidate_count": len(collection_candidates),
                "if_missing": {
                    "action": "pause_and_report_missing_live_component",
                    "message": (
                        "No single live component covers all capabilities; compose installed components or report "
                        "that the output contract cannot currently be met."
                    ),
                },
            },
            "planner_directive": (
                "Numeric max_rows is the final valid-row count. Consider the recommended component when its "
                "parameters fit the page, or compose explicit page actions, loops, JavaScript, downloads, and "
                "other installed components. Defer only bindings required by the chosen components."
            ),
            "execution_contract": {
                "owner": "client_model",
                "mode": "model_decides",
                "start": "after the result list is captured",
                "loop": "use the selected component graph to append/transform valid rows and advance when needed",
                "stop": [
                    "output_contract.max_rows valid rows collected",
                    "no next-page/load-more control remains",
                ],
                "required_bindings": [],
                "advisory_bindings": ["page_type", "selector_page_or_selector_more"],
            },
            "decision_tree": [
                "If output_contract.max_rows is numeric, choose and explain a strategy capable of satisfying it.",
                "Use live page evidence for any selector required by the selected components.",
                "Submit the model's plan to shizai_rpa_validate_plan; strategy alternatives remain valid.",
            ],
            "must_configure": [
                "collect_items = output_contract.max_rows when the chosen component supports it",
                "page_type/selectors = configure only when required by the chosen component",
                "deduplication = consider when pages can overlap",
            ],
            "must_configure_policy": "advisory; these are component-specific suggestions, not MCP-wide requirements",
            "completion_check": (
                "The plan is incomplete only when its runtime result cannot satisfy the declared output contract; "
                "the mechanism for reaching max_rows is a model decision."
            ),
            "selection_rule": (
                "For a numeric row target, consider candidates and planning_patterns, then choose the strategy "
                "that best fits live evidence."
            ),
            "rejection_conditions": [
                "a strategy that cannot satisfy the declared output contract may produce a runtime warning",
                "an unsupported parameter or unbound required selector is a structural error",
            ],
            "candidates": collection_candidates[:8],
        }

    # Emit a compact role-level decision surface before the long component
    # list.  Client models otherwise tend to pick the first browser component
    # they see (usually navigation) for every operation, even though the live
    # dictionary contains dedicated search and collection contracts.
    role_selection: dict[str, Any] = {}

    def _role_candidates(
        required: set[str],
        *,
        operation: str,
        browser_only: bool = False,
    ) -> list[dict[str, Any]]:
        ranked: list[tuple[tuple[int, int, int, int, str], dict[str, Any]]] = []
        for record in records:
            normalized_record = dict(record)
            if "inputs" not in normalized_record:
                definition = record.get("definition") if isinstance(record.get("definition"), dict) else {}
                normalized_record["inputs"] = definition.get("args0") if isinstance(definition.get("args0"), list) else []
            contract = component_capabilities(normalized_record)
            observed = set(contract.get("capabilities") or [])
            if not required.issubset(observed) or operation not in set(contract.get("operation_types") or []):
                continue
            top_levels = {str(item.get("id") or "") for item in _top_level_values(record)}
            if browser_only and "webBrowser" not in top_levels:
                continue
            # Prefer the newest versioned contract while keeping the ranking
            # entirely data-driven (no component name is embedded here).
            component_name = str(record.get("type") or "")
            version_match = re.search(r"(?:v|_)(\d+)$", component_name, re.IGNORECASE)
            version = int(version_match.group(1)) if version_match else 0
            materializable = 1 if record.get("materializable", True) else 0
            # Password fields satisfy the same low-level input contract but
            # are not suitable for ordinary search terms. Keep them available
            # as fallbacks while preferring the plain text field contract.
            password_penalty = 0 if "password" in component_name.casefold() else 1
            candidate = {
                "component": component_name,
                "canonical_component": COMPONENT_TYPE_ALIASES.get(component_name, component_name),
                "label": record.get("label") or component_name,
                "capabilities": sorted(observed),
                "operation_types": contract.get("operation_types") or [],
                "plan_input_mode": contract.get("plan_input_mode", "variables"),
                "plan_input_rule": contract.get("plan_input_rule"),
                "requires_page_evidence": bool(contract.get("requires_page_evidence")),
                "deferred_bindings": contract.get("deferred_bindings") or [],
                "configuration_hints": contract.get("configuration_hints") or {},
                "planning_patterns": contract.get("planning_patterns") or [],
                "outputs": _component_outputs(record, normalized_record["inputs"]),
            }
            ranked.append(((materializable, password_penalty, version, len(observed), component_name.casefold()), candidate))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in ranked[:5]]

    navigation_candidates = _role_candidates({"navigate"}, operation="navigate", browser_only=True)
    search_candidates = _role_candidates({"input", "submit"}, operation="search", browser_only=True)
    if navigation_candidates:
        role_selection["browser_navigation"] = {
            "required_capabilities": ["navigate"],
            "recommended_component": navigation_candidates[0]["component"],
            "candidates": navigation_candidates,
            "rule": "This role is a navigation hint; the model may combine it with input, submit, or other components as needed.",
        }
    if search_candidates:
        role_selection["search_interaction"] = {
            "required_capabilities": ["input", "submit"],
            "recommended_component": search_candidates[0]["component"],
            "candidates": search_candidates,
            "rule": "These candidates are useful for search input/submit; choose or compose the operation from live contracts.",
        }
    if collection_guidance:
        role_selection["structured_collection"] = {
            "required_capabilities": collection_guidance["required_capabilities"],
            "recommended_component": collection_guidance["selection"]["recommended_component"],
            "candidates": collection_guidance["candidates"][:5],
            "rule": collection_guidance["planner_directive"],
        }
    role_selection_contract = {
        "order": [],
        "order_policy": "model_decides; dependencies and component contracts are validated",
        "select_one_per_required_role": False,
        "numeric_target": {
            "interpretation": "final_valid_row_count",
            "pagination_required": False,
            "pagination_capability": "discover from live components exposing paginate; selection is model-owned",
            "pagination_owner": "client_model",
            "pagination_mode": "model_decides",
            "collect_items": "$output_contract.max_rows_if_supported",
            "page_type": "$choose_if_supported_by_selected_component",
            "selector_page_or_more": "$bind_if_required_by_selected_component",
            "stop_when": [
                "output_contract.max_rows valid rows collected",
                "no further valid source data is available",
            ],
            "allowed_strategies": [
                "component_managed_pagination",
                "explicit_next_page_steps",
                "loops",
                "javascript_or_download",
            ],
        },
        "hard_rejections": [
            "component or parameter is not in the live contract",
            "required selector or graph reference is unresolved",
        ],
    }
    # Keep a compact, unconditional decision surface at the top of the
    # response. Models may call the catalog with an empty capability filter
    # or focus on the long component list; the numeric-target rule must still
    # be visible in that case. Candidate selection remains live and is only
    # populated when the request's capability filter exposes the collection
    # role above.
    numeric_collection_component = (
        collection_guidance["selection"]["recommended_component"]
        if collection_guidance and collection_guidance["selection"]["status"] == "ready"
        else None
    )
    if not numeric_collection_component:
        # Keep the decision surface useful for an unfiltered/summary call too.
        # This is still discovered from the live dictionary, never a site or
        # version-specific component name.
        for record in records:
            normalized_record = dict(record)
            if "inputs" not in normalized_record:
                definition = record.get("definition") if isinstance(record.get("definition"), dict) else {}
                normalized_record["inputs"] = definition.get("args0") if isinstance(definition.get("args0"), list) else []
            observed = set(component_capabilities(normalized_record).get("capabilities") or [])
            if {"extract", "limit", "paginate", "output"}.issubset(observed):
                numeric_collection_component = record.get("type")
                break

    numeric_target_contract = {
        "trigger": "analysis.evidence.collection.max_rows is a number",
        "decision": "advisory",
        "interpretation": "max_rows is the final valid-row count, not the first-page size",
        "required_capabilities": ["extract", "output"],
        "selection": (
            "Consider planning_guidance.collection candidates when their contract fits; otherwise compose "
            "multiple live components or choose another installed strategy."
        ),
        "required_config": {
            "collect_items": "$output_contract.max_rows_if_supported",
            "page_type": "$choose_if_supported_by_selected_component",
            "selector_page_or_selector_more": "$bind_if_required_by_selected_component",
        },
        "execution": {
            "owner": "client_model",
            "mode": "model_decides",
            "stop_when": ["max_rows valid rows collected", "no further valid source data is available"],
            "allowed": ["component-managed pagination", "standalone page action", "loop", "JavaScript", "download"],
        },
        "plan_fragment": {
            "role": "structured_collection",
            "component": numeric_collection_component,
            "config": {
                "collect_items": "$output_contract.max_rows_if_supported",
                "page_type": "$choose_if_supported_by_selected_component",
                "selector_page": "$bind_if_required_by_selected_component",
                "selector_more": "$bind_if_required_by_selected_component",
            },
            "after_navigation": "capture page evidence needed by the selected components, then replace deferred bindings",
            "completion": "emit output_contract.max_rows valid rows when the source provides them, or report the observed shortfall",
        },
        "missing_candidate_action": "let_model_compose_multiple_components_or_report_missing_capability",
    }
    sources = {"builtin": builtin_source, "business": business_source}
    if scope == "builtin":
        source = builtin_source
        ready = bool(builtin_source["available"])
    elif scope == "business":
        source = business_source
        ready = bool(business_source["available"])
    else:
        # Keep the original ``source`` shape for existing clients while the
        # richer ``sources`` object exposes both discovery backends.
        source = builtin_source if builtin_source["available"] or not business_source["available"] else business_source
        ready = bool(builtin_source["available"] or business_source["available"])
    if not ready:
        next_action = "安装或启动实在设计器后重新查询 builtin 组件；登录 SaaS 设计器后重新查询 business 扩展。"
    else:
        next_action = None
    returned_component_types = [
        str(item.get("type") or "").strip()
        for item in selected_components
        if str(item.get("type") or "").strip()
    ]
    if component_type and detail in {"contract", "full"} and returned_component_types:
        selection_guidance = {
            "phase": "contract_ready",
            "component_types": returned_component_types,
            "contract_complete": True,
            "repeat_lookup": False,
            "next": (
                "保留并使用本次返回的契约；同一请求不要再次查询这些 component_type。"
                "如果所有最终步骤的契约均已读取，直接构造并校验计划。"
            ),
        }
    elif detail == "summary":
        selection_guidance = {
            "phase": "shortlist_ready",
            "component_types": returned_component_types,
            "contract_complete": False,
            "repeat_lookup": False,
            "next": (
                "只从 component_types 中选择最终步骤，然后每个已选 type 精确读取一次 contract；"
                "不要猜测未返回的类型，也不要枚举未选替代组件。"
            ),
        }
    else:
        selection_guidance = {
            "phase": "catalog_page_ready",
            "component_types": returned_component_types,
            "contract_complete": bool(component_type and detail in {"contract", "full"}),
            "repeat_lookup": False,
        }
    result = {
        "kind": "native_rpa_component_catalog",
        "catalog_kind": "rpa_component_catalog",
        "scope": scope,
        "ready": ready,
        "source": source,
        "sources": sources,
        "filters": {
            "include_hidden": include_hidden,
            "query": query,
            "category": category,
            "component_type": component_type,
            "package": package,
            "capability": capability,
            "capability_mode": capability_mode,
            "top_level": top_level,
            "business_category": business_category,
            "business_package": business_package,
            "project_ref": project_ref,
        },
        "filter_relaxation": {
            "applied": bool(relaxed_filters),
            "ignored": relaxed_filters,
            "reason": (
                "structured capability matches were restored after free-text/menu filters removed all results"
                if relaxed_filters
                else None
            ),
        },
        "page": page,
        "limit": limit,
        "catalog_page_note": (
            "page/next_page paginate the component dictionary only; they are not workflow page-turning. "
            "Workflow pagination belongs to whichever model-selected components implement that strategy."
        ),
        "has_more": start + limit < len(matches),
        "next_page": page + 1 if start + limit < len(matches) else None,
        "total_components": len(records),
        "matched_components": len(matches),
        "advertised_component_total": (
            len(builtin_records) + business_meta["advertised_component_total"]
            if scope == "all"
            else len(builtin_records)
            if scope == "builtin"
            else business_meta["advertised_component_total"]
        ),
        "matched_business_advertised_total": matched_business_advertised,
        "counts": {
            "builtin": {
                "engine_records": len(load_component_records(builtin_source)) if builtin_source["available"] else 0,
                "designer_static_index_entries": sum(
                    1 for item in builtin_records if item.get("designer_static_index")
                ),
                "designer_static_extra": sum(1 for item in builtin_records if item.get("designer_static_extra")),
                "visible": len(builtin_records),
            },
            "business": {
                "advertised": business_meta["advertised_component_total"],
                "visible": len(business_records),
                "packages": business_meta["package_total"],
            },
        },
        "categories": [{"name": name, "count": count} for name, count in sorted(category_counts.items())],
        "top_levels": [
            {"id": key, "label": label, "count": top_level_counts.get(key, 0)}
            for key, label, _categories in _TOP_LEVELS
            if top_level_counts.get(key, 0)
        ],
        "business_categories": business_meta["categories"] if scope in {"business", "all"} else [],
        "business_packages": [{key: value for key, value in item.items() if key != "scope"} for item in package_matches]
        if scope in {"business", "all"}
        else [],
        "role_selection": role_selection,
        "role_selection_contract": role_selection_contract,
        "components": selected_components,
        "selection_guidance": selection_guidance,
        "planning_guidance": {
            "planning_policy": {
                "owner": "client_model",
                "mode": "model_decides",
                "mcp_role": "component_contract_and_safety_validation",
                "strategy_checks": "advisory",
                "allowed_strategies": [
                    "linear_components",
                    "loops",
                    "conditions",
                    "component_managed_pagination",
                    "explicit_next_page_steps",
                    "javascript_extraction_or_pagination",
                ],
                "hard_constraints": [
                    "component_exists_and_is_materializable",
                    "config_matches_live_contract",
                    "graph_references_are_resolvable",
                    "selectors_are_verified_before_run",
                    "runtime_and_write_permissions_are_ready",
                ],
                "notice": "Role selection and planning patterns below are advisory. They do not define a unique workflow or pagination protocol.",
            },
            "collection": collection_guidance,
            "roles": role_selection,
            "contract": role_selection_contract,
            "numeric_target": numeric_target_contract,
            "decision_gate": [
                "Read numeric_target as an acceptance criterion while reviewing the full live components list.",
                "Choose any composition whose live contracts can satisfy the target; the catalog does not select a unique paginator.",
                "Keep page bindings deferred until the current page provides evidence for whichever components need them.",
                "Submit the model-selected plan to validation; unsupported parameters and unresolved required bindings remain hard errors.",
            ],
            "general": (
                "Use role_selection and planning_patterns as matching hints before scanning the full components list. "
                "Map operations to live component capabilities, then let the model choose the composition, loops, "
                "page actions, JavaScript, or download strategy. A numeric target is checked after execution."
            ),
        },
        "usage": {
            "scope": "builtin 代表安装设计器即有的基础组件；business 代表 SaaS 扩展，私有化部署需导入包；all 先返回基础组件。",
            "detail": detail,
            "next_detail": "full 仅对选中的少量组件使用；写入 native .task 前仍需校验实际版本和参数。",
            "implementation_policy": {
                "default_strategy": "component_first",
                "rule": "优先从实时目录选择能表达目标的真实组件；结构化网页采集优先评估 WebBrowser_DataCollectionV5；短代码、循环、条件和多组件组合由客户端模型按任务需要决定。",
                "enforcement": "advisory_only",
                "fallback_rule": "如果原生组件不适用或验证失败，可以选择 JS、下载、循环或其它已安装组合，并说明原因、验证输出；不因未选原生组件而硬失败。",
                "short_code_exception": "使用短代码时保留清晰的输入输出和运行安全边界，并说明它与现有组件的关系。",
            },
            "request_plan": (
                "客户端模型必须根据 shizai_rpa_analyze_request 的原子操作选择本次请求需要的组件，"
                "显式提交顺序、依赖、变量和配置；MCP 不追加站点流程或固定输出步骤。"
            ),
            "planning_policy": {
                "owner": "client_model",
                "mode": "model_decides",
                "strategy_checks": "advisory",
                "allowed": [
                    "linear_components",
                    "loops",
                    "conditions",
                    "component_managed_pagination",
                    "explicit_next_page_steps",
                    "javascript_extraction_or_pagination",
                ],
                "mcp_hard_checks": [
                    "component_exists",
                    "parameter_contract",
                    "graph_references",
                    "verified_selectors",
                    "runtime_safety",
                ],
                "notice": "Recommendations and planning_patterns do not define a unique workflow. Choose the strategy that fits the user's target and live page evidence.",
            },
            "semantic_config_values": (
                "plan.config 传语义 JSON 值，不复制目录 defaultValue 的原生引号编码；"
                "例如 url=https://example.com，而不是 url=\"https://example.com\"。"
            ),
        },
        "next_action": next_action,
    }
    if response_mode == "compact":
        # Preserve selected component contracts exactly; omit the duplicated
        # global role/planning catalog that overwhelmed Luna even on zero hits.
        implementation_policy = result["usage"]["implementation_policy"]
        retained = (
            "kind", "scope", "ready", "source", "sources", "filters", "filter_relaxation",
            "page", "limit", "has_more", "next_page", "total_components", "matched_components",
            "components", "business_packages", "selection_guidance", "next_action",
        )
        result = {key: result[key] for key in retained}
        result["response_mode"] = "compact"
        result["usage"] = {
            "detail": detail,
            "search": "query 的多个词同时匹配；无结果时用单个能力关键词查询，不必反复查询全目录。",
            "next_detail": "summary 只用于选型；选定真实 component_type 后读取 contract，保留已有契约。",
            "pagination": "page/next_page 仅翻组件目录，不是业务网页翻页。",
            "semantic_config_values": "config 传语义 JSON 值，不能复制 defaultValue 的原生外层引号。",
            "implementation_policy": implementation_policy,
        }
    if component_type and detail in {"contract", "full"}:
        selected_types = {item["type"] for item in result["components"] if item.get("scope") == "builtin"}
        families = {item.get("base_type") for item in result["components"] if item.get("scope") == "builtin"}
        families.discard(None)
        families.discard("")
        variants = [record for record in builtin_records
                    if record.get("baseType") in families and record.get("type") not in selected_types
                    and (include_hidden or not _designer_hidden(record))]
        if variants:
            rows = [_summary(builtin_source, record, "summary") for record in variants[:8]]
            result["related_variants"] = {
                "notice": "同一 baseType 的其他实现；名称相似不代表行为相同，比较实时 description 和契约后再选择，不能自动替换。",
                "total": len(variants), "truncated": len(variants) > len(rows),
                "items": [{key: row.get(key) for key in ("type", "label", "description", "materializable")}
                          for row in rows],
                "lookup": {"tool": "shizai_rpa_component_catalog", "detail": "contract", "field": "component_type"},
            }
    return result


@lru_cache(maxsize=4)
def builtin_component_index(
    paths: ShizaiPaths,
    *,
    engine_root: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Return compact contract rows keyed by the exact built-in component type.

    The normal catalog API is paginated for MCP clients.  Regression tooling
    needs a complete in-process lookup instead, while still returning the same
    normalized fields as ``detail="contract"``.  No credentials or source
    paths are included in these rows.
    """
    source = component_source(paths, engine_root=engine_root)
    return {
        str(record.get("type")): _summary(source, record, "contract")
        for record in _builtin_records(source)
        if str(record.get("type") or "").strip()
    }


def builtin_component_contract(
    paths: ShizaiPaths,
    component: str,
    *,
    engine_root: str | None = None,
) -> dict[str, Any] | None:
    """Return one live built-in contract without summarizing the whole catalog."""
    requested = str(component or "").strip()
    if not requested:
        return None
    aliases = {
        requested,
        next(
            (legacy for legacy, canonical in COMPONENT_TYPE_ALIASES.items() if canonical == requested),
            "",
        ),
    }
    source = component_source(paths, engine_root=engine_root)
    for record in _builtin_records(source):
        if str(record.get("type") or "").strip() in aliases:
            return _summary(source, record, "contract")
    return None
