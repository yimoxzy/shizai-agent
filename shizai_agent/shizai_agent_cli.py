"""Click command line interface for the Shizai Intelligent desktop app."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from . import __version__
from .core import agent as agent_core
from .core import component_catalog as component_catalog_core
from .core import debug_session as debug_core
from .core import doctor as doctor_core
from .core import project as project_core
from .core.cloud_api import ShizaiCloudApi, ShizaiCloudConfig, ShizaiCloudError, ShizaiDesktopAgent
from .core.extension_bridge import ShizaiExtensionBridge, open_current_chrome
from .core.native_ui import (
    MASK_TYPES,
    SMALL_CARD_STATES,
    NativeMask,
    NativeScanDemo,
    NativeSmallCard,
    NativeTakeoverFrame,
)
from .core.paths import ShizaiPaths
from .core.workflow_edit import WorkflowEditError, read_edit_context


def _configure_output_encoding() -> None:
    """Keep JSON and answers readable when invoked from Windows terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


_configure_output_encoding()


def _ctx(ctx: click.Context) -> dict[str, Any]:
    return ctx.ensure_object(dict)


def _emit(ctx: click.Context, value: Any) -> None:
    state = _ctx(ctx)
    if state.get("as_json"):
        click.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(value, (dict, list)):
        click.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    else:
        click.echo(value)


def _paths(ctx: click.Context) -> ShizaiPaths:
    return _ctx(ctx)["paths"]


def _fail(message: str) -> None:
    raise click.ClickException(message)


def _cloud() -> ShizaiCloudApi:
    """Use the explicit token or exchange the currently logged-in desktop session."""
    config = ShizaiCloudConfig.from_environment()
    return ShizaiCloudApi.from_logged_in_desktop(config)


def _desktop() -> ShizaiDesktopAgent:
    return ShizaiDesktopAgent()


def _cleanup_native_ui(client: ShizaiDesktopAgent) -> dict[str, Any]:
    """Best-effort cleanup for native UI left by an interrupted CLI command."""
    result: dict[str, Any] = {}
    actions = {
        "small_card": lambda: NativeSmallCard(client=client).close(),
        "takeover_frame": lambda: NativeTakeoverFrame(client=client).close(),
        "native_mask": lambda: NativeMask(client=client).close(force=True),
        "inspect": lambda: ShizaiExtensionBridge(desktop=client).stop_inspect(),
    }
    for key, action in actions.items():
        try:
            result[key] = action()
        except Exception as exc:  # cleanup must not hide the original command result
            result[f"{key}_warning"] = str(exc)
    return result


def _ensure_capture_session(bridge: ShizaiExtensionBridge, project_ref: str | None) -> dict[str, Any] | None:
    """Share the idempotent DeepCapture bootstrap across CLI capture commands."""
    normalized = str(project_ref or "").strip()
    if not normalized:
        return None
    ensure = getattr(bridge, "ensure_capture_session", None)
    if not callable(ensure):
        return None
    return ensure(package_path=normalized)


def _capture_read_with_recovery(
    bridge: ShizaiExtensionBridge,
    project_ref: str | None,
    operation: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Retry a read once after reconnecting a stale cross-process session."""
    try:
        result = operation()
        if not isinstance(result, dict):
            raise ValueError("DeepCapture read returned a non-object response")
        return result, None
    except (ShizaiCloudError, OSError, TimeoutError) as exc:
        checker = getattr(ShizaiExtensionBridge, "_is_transient_capture_error", None)
        if not project_ref or not callable(checker) or not checker(exc):
            raise
        recovery = ShizaiExtensionBridge(desktop=bridge.desktop).start_capture_session(package_path=project_ref)
        result = operation()
        if not isinstance(result, dict):
            raise ValueError("DeepCapture read returned a non-object response after reconnect")
        result["capture_session_recovery"] = {
            "reconnected": True,
            "reason": str(exc),
            "session": recovery,
        }
        return result, recovery


def _stop_picker_after_capture(bridge: ShizaiExtensionBridge, *, keep_open: bool) -> dict[str, Any] | None:
    """Close the official picker after a capture unless another pick is requested."""
    if keep_open:
        return None
    return bridge.stop_inspect()


def _workflow_project_ref(ctx: click.Context, project_ref: str | None) -> str:
    """Resolve an explicit project argument or the global --project value."""
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    return str(ref)


def _ingest_source(
    api: ShizaiCloudApi,
    source: str,
    *,
    dataset_id: str | None,
    process_mode: str,
) -> dict[str, Any]:
    """Import a URL or local file through the matching knowledge API."""
    if source.startswith(("http://", "https://")):
        return api.add_url(source, dataset_id=dataset_id, process_mode=process_mode)
    path = Path(source).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Source is not an HTTP URL or a local file: {source}")
    return api.add_file(path, dataset_id=dataset_id, process_mode=process_mode)


@click.group(no_args_is_help=True)
@click.version_option(version=__version__)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--dry-run", is_flag=True, help="Do not persist session changes.")
@click.option("--project", "project_ref", default=None, help="Active project id or path.")
@click.pass_context
def cli(ctx: click.Context, as_json: bool, dry_run: bool, project_ref: str | None) -> None:
    """Inspect and automate the local Shizai Intelligent desktop application."""
    state = _ctx(ctx)
    state.update(
        {
            "as_json": as_json,
            "dry_run": dry_run,
            "project_ref": project_ref,
            "paths": ShizaiPaths.from_environment(),
        }
    )


@cli.command("doctor")
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Check local data, installation, Agent Brain bundle, and business port."""
    _emit(ctx, doctor_core.diagnose(_paths(ctx), _ctx(ctx).get("project_ref")))


@cli.group("ui")
def ui() -> None:
    """触发实在 Agent 桌面端原生 UI 反馈。"""


@ui.command("small-card")
@click.option(
    "--state",
    type=click.Choice([*SMALL_CARD_STATES, "hidden"]),
    default="designing",
    show_default=True,
    help="原生小卡状态；scanning 会保持 TARS 转圈动画。",
)
@click.option("--message", default=None, help="可选的自定义小卡文案。")
@click.option(
    "--duration",
    type=click.FloatRange(min=0, max=3600),
    default=0.0,
    show_default=True,
    help="小卡保持显示的秒数；0 表示保持打开。",
)
@click.option("--close", "close_after", is_flag=True, help="达到持续时间后关闭小卡。")
@click.option("--demo", is_flag=True, help="播放接管、设计、扫描、捕获、校验和完成状态序列。")
@click.option(
    "--interval",
    type=click.FloatRange(min=0.1, max=60),
    default=1.5,
    show_default=True,
    help="使用 --demo 时各状态之间的间隔秒数。",
)
@click.option("--allow-write", is_flag=True, help="允许修改桌面端原生 UI。")
@click.pass_context
def ui_small_card(
    ctx: click.Context,
    state: str,
    message: str | None,
    duration: float,
    close_after: bool,
    demo: bool,
    interval: float,
    allow_write: bool,
) -> None:
    """在当前桌面会话打开并更新原生 TARS 小卡。"""
    if not allow_write:
        _fail("ui small-card requires --allow-write")
    try:
        card = NativeSmallCard(client=_desktop())
        result = (
            card.demo(interval=interval)
            if demo
            else card.show(
                state=state,
                message=message,
                duration=duration,
                close_after=close_after,
            )
        )
        _emit(ctx, result)
    except (OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@ui.command("mask")
@click.option(
    "--mask-type",
    type=click.Choice(list(MASK_TYPES)),
    default="white",
    show_default=True,
    help="原生运行遮罩样式提示；完整运行遮罩由 MaskAppService 统一分发。",
)
@click.option("--tips", default=None, help="可选的遮罩提示文案。")
@click.option("--request-id", default=None, help="关闭指定遮罩时复用 request id。")
@click.option(
    "--duration", type=click.FloatRange(min=0, max=3600), default=0, show_default=True, help="遮罩保持显示的秒数。"
)
@click.option("--close", "close_after", is_flag=True, help="达到持续时间后关闭遮罩。")
@click.option("--close-now", is_flag=True, help="立即关闭当前遮罩。")
@click.option("--force", is_flag=True, help="request id 不匹配时也强制关闭。")
@click.option("--activate/--no-activate", default=True, help="显示前激活实在 Agent 主窗口。")
@click.option("--allow-write", is_flag=True, help="允许修改桌面端原生 UI。")
@click.pass_context
def ui_mask(
    ctx: click.Context,
    mask_type: str,
    tips: str | None,
    request_id: str | None,
    duration: float,
    close_after: bool,
    close_now: bool,
    force: bool,
    activate: bool,
    allow_write: bool,
) -> None:
    """显示或关闭设计器原生完整运行遮罩。"""
    if not allow_write:
        _fail("ui mask requires --allow-write")
    if close_now and close_after:
        _fail("--close and --close-now cannot be used together")
    try:
        mask = NativeMask(client=_desktop())
        result = (
            mask.close(request_id=request_id, force=force or not request_id)
            if close_now
            else mask.show(
                mask_type=mask_type,
                request_id=request_id,
                tips=tips,
                duration=duration,
                close_after=close_after,
                activate=activate,
            )
        )
        _emit(ctx, result)
    except (OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@ui.command("scan-demo")
@click.option(
    "--mask-type",
    type=click.Choice(list(MASK_TYPES)),
    default="white",
    show_default=True,
    help="演示期间使用的原生运行遮罩样式提示。",
)
@click.option(
    "--interval",
    type=click.FloatRange(min=0.1, max=60),
    default=1.5,
    show_default=True,
    help="原生截图拾取反馈阶段之间的间隔秒数。",
)
@click.option("--keep-open", is_flag=True, help="演示结束后保留遮罩和小卡。")
@click.option("--activate/--no-activate", default=True, help="演示前激活实在 Agent 主窗口。")
@click.option("--allow-write", is_flag=True, help="允许修改桌面端原生 UI。")
@click.pass_context
def ui_scan_demo(
    ctx: click.Context,
    mask_type: str,
    interval: float,
    keep_open: bool,
    activate: bool,
    allow_write: bool,
) -> None:
    """按设计器原版顺序播放四周遮罩、截图拾取和 TARS 小卡反馈。"""
    if not allow_write:
        _fail("ui scan-demo requires --allow-write")
    try:
        result = NativeScanDemo(client=_desktop()).run(
            interval=interval,
            mask_type=mask_type,
            close_after=not keep_open,
            activate=activate,
        )
        _emit(ctx, result)
    except (OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@ui.command("status")
@click.pass_context
def ui_status(ctx: click.Context) -> None:
    """读取最近一次 MCP 捕获或流程 UI 反馈状态。"""
    try:
        _emit(ctx, ShizaiExtensionBridge(desktop=_desktop()).ui_status())
    except (OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@cli.group("project")
def project() -> None:
    """Inspect, validate, and export local Shizai projects."""


@project.command("list")
@click.pass_context
def project_list(ctx: click.Context) -> None:
    """List local application projects."""
    _emit(ctx, project_core.list_projects(_paths(ctx)))


@project.command("inspect")
@click.argument("project_ref", required=False)
@click.pass_context
def project_inspect(ctx: click.Context, project_ref: str | None) -> None:
    """Inspect PROJECT_REF, an application id or project directory."""
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    try:
        _emit(ctx, project_core.inspect_project(_paths(ctx), ref))
    except (FileNotFoundError, OSError) as exc:
        _fail(str(exc))


@project.command("info")
@click.argument("project_ref", required=False)
@click.pass_context
def project_info(ctx: click.Context, project_ref: str | None) -> None:
    """Show concise information for PROJECT_REF."""
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    try:
        _emit(ctx, project_core.info(_paths(ctx), ref))
    except (FileNotFoundError, OSError) as exc:
        _fail(str(exc))


@project.command("profiles")
@click.pass_context
def project_profiles(ctx: click.Context) -> None:
    """Summarize project modes observed in local application data."""
    _emit(ctx, project_core.list_profiles(_paths(ctx)))


@project.command("validate")
@click.argument("project_ref", required=False)
@click.pass_context
def project_validate(ctx: click.Context, project_ref: str | None) -> None:
    """Validate PROJECT_REF without changing source files."""
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    try:
        result = project_core.validate_project(_paths(ctx), ref)
    except (FileNotFoundError, OSError) as exc:
        _fail(str(exc))
    _emit(ctx, result)
    if not result["valid"]:
        raise click.exceptions.Exit(2)


@project.command("export")
@click.argument("project_ref", required=False)
@click.option("--output", "output_path", "-o", required=True, help="ZIP output path.")
@click.pass_context
def project_export(ctx: click.Context, project_ref: str | None, output_path: str) -> None:
    """Export PROJECT_REF as a ZIP copy for review or transfer."""
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    try:
        _emit(ctx, project_core.export_project(_paths(ctx), ref, output_path))
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@project.command("register")
@click.argument("project_ref", required=False)
@click.option("--import-mode", type=click.Choice(["create-new", "overwrite"]), default="create-new", show_default=True)
@click.option("--application-type", type=click.Choice(["normal", "agent"]), default="normal", show_default=True)
@click.option("--no-verify", is_flag=True, help="Skip the post-import application-list check.")
@click.option("--allow-write", is_flag=True, help="Import the project into the running designer.")
@click.pass_context
def project_register(
    ctx: click.Context,
    project_ref: str | None,
    import_mode: str,
    application_type: str,
    no_verify: bool,
    allow_write: bool,
) -> None:
    """Register PROJECT_REF in the running Shizai designer application list."""
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    if not allow_write:
        _fail("project register requires --allow-write")
    try:
        _emit(
            ctx,
            agent_core.import_native_project(
                ref,
                import_mode=import_mode,
                application_type=application_type,
                verify=not no_verify,
                client=_desktop(),
            ),
        )
    except (FileNotFoundError, ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@project.command("elements")
@click.argument("project_ref", required=False)
@click.pass_context
def project_elements(ctx: click.Context, project_ref: str | None) -> None:
    """List native browser elements and selector summaries."""
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    try:
        _emit(ctx, project_core.read_elements(_paths(ctx), ref))
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@project.command("import-elements")
@click.argument("project_ref", required=False)
@click.option("--input", "input_path", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--replace", is_flag=True, help="Replace the existing element map.")
@click.option(
    "--force-replace", is_flag=True, help="Allow replacement even when existing flow steps reference elements."
)
@click.option("--allow-write", is_flag=True, help="Allow writing elements.json.")
@click.pass_context
def project_import_elements(
    ctx: click.Context, project_ref: str | None, input_path: Path, replace: bool, force_replace: bool, allow_write: bool
) -> None:
    """Import browser-captured element JSON into a project."""
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    if not allow_write:
        _fail("--allow-write is required")
    try:
        elements = json.loads(input_path.read_text(encoding="utf-8"))
        if isinstance(elements, dict):
            elements = elements.get("elements", [])
        _emit(
            ctx,
            project_core.import_elements(
                _paths(ctx),
                ref,
                elements,
                replace=replace,
                force_replace=force_replace,
                capture_source="model_candidate",
            ),
        )
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(str(exc))


@project.command("validate-elements")
@click.argument("project_ref", required=False)
@click.option(
    "--input",
    "input_path",
    required=False,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Candidate browser-capture JSON to validate without writing.",
)
@click.option("--strict", is_flag=True, help="Treat selector warnings as errors.")
@click.pass_context
def project_validate_elements(
    ctx: click.Context, project_ref: str | None, input_path: Path | None, strict: bool
) -> None:
    """Validate candidate or native browser elements before RPA generation."""
    try:
        if input_path:
            elements = json.loads(input_path.read_text(encoding="utf-8"))
            if isinstance(elements, dict):
                if isinstance(elements.get("elements"), list):
                    elements = elements["elements"]
                elif isinstance(elements.get("elementMap"), dict):
                    element_map = elements["elementMap"]
                    elements = []
                    for element_id, element in element_map.items():
                        if isinstance(element, dict):
                            record = dict(element)
                            record.setdefault("id", element_id)
                            elements.append(record)
                else:
                    elements = []
            result = project_core.validate_captured_elements(elements, strict=strict)
        else:
            ref = project_ref or _ctx(ctx).get("project_ref")
            if not ref:
                _fail("PROJECT_REF is required, or pass --project PROJECT_REF, unless --input is used")
            result = project_core.validate_project_elements(_paths(ctx), ref, strict=strict)
        _emit(ctx, result)
        if not result["valid"]:
            raise click.exceptions.Exit(2)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(str(exc))


@cli.group("extension")
def extension() -> None:
    """Use the installed Shizai Agent Chrome extension for live capture."""


@extension.command("status")
@click.pass_context
def extension_status(ctx: click.Context) -> None:
    """Show extension installation and DeepCapture readiness without secrets."""
    try:
        _emit(ctx, ShizaiExtensionBridge(desktop=_desktop()).status(probe_deep_capture=True))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@extension.command("start-capture")
@click.option(
    "--mode", type=click.Choice(["1", "2"]), default="2", show_default=True, help="1=hover, 2=mouse-left-down."
)
@click.option(
    "--project",
    "--project-ref",
    "project_ref",
    required=True,
    help="Client flow directory used as DeepCapture packagePath.",
)
@click.option("--step-id", required=True, help="Existing unbound element component that will receive the selector.")
@click.option("--allow-write", is_flag=True, help="Start the DeepCapture inspection session.")
@click.pass_context
def extension_start_capture(
    ctx: click.Context,
    mode: str,
    project_ref: str,
    step_id: str,
    allow_write: bool,
) -> None:
    """Start DeepCapture through the installed Shizai extension."""
    if not allow_write:
        _fail("extension start-capture requires --allow-write")
    desktop = _desktop()
    bridge = ShizaiExtensionBridge(desktop=desktop)
    try:
        target_step = project_core.unbound_rpa_element_step(_paths(ctx), project_ref, step_id)
        result = bridge.start_capture(mode=int(mode), package_path=project_ref)
        result["target_step"] = target_step
        _emit(ctx, result)
    except (ShizaiCloudError, OSError, ValueError) as exc:
        try:
            bridge.stop_inspect()
        except Exception:
            pass
        try:
            NativeMask(client=desktop).close(force=True)
        except Exception:
            pass
        _fail(str(exc))


@extension.command("stop-capture")
@click.option(
    "--project",
    "--project-ref",
    "project_ref",
    default=None,
    help="Client flow directory used as DeepCapture packagePath.",
)
@click.option("--allow-write", is_flag=True, help="Stop the DeepCapture inspection session.")
@click.pass_context
def extension_stop_capture(ctx: click.Context, project_ref: str | None, allow_write: bool) -> None:
    """Stop DeepCapture through the installed Shizai extension."""
    if not allow_write:
        _fail("extension stop-capture requires --allow-write")
    try:
        desktop = _desktop()
        bridge = ShizaiExtensionBridge(desktop=desktop)
        try:
            result = bridge.stop_capture(package_path=project_ref)
        except (ShizaiCloudError, OSError, ValueError):
            try:
                NativeMask(client=desktop).close(force=True)
            except Exception:
                pass
            raise
        try:
            result["native_mask"] = NativeMask(client=desktop).close(force=True)
        except Exception as exc:
            result["native_mask_warning"] = str(exc)
        _emit(ctx, result)
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@extension.command("call")
@click.argument("method")
@click.option("--args-json", default="[]", help="JSON array of DeepCapture method arguments.")
@click.option("--allow-write", is_flag=True)
@click.pass_context
def extension_call(ctx: click.Context, method: str, args_json: str, allow_write: bool) -> None:
    """Call one supported DeepCapture METHOD."""
    try:
        call_args = json.loads(args_json)
        if not isinstance(call_args, list):
            raise ValueError("--args-json must decode to an array")
        _emit(ctx, ShizaiExtensionBridge(desktop=_desktop()).call(method, call_args, allow_write=allow_write))
    except (ShizaiCloudError, OSError, ValueError, json.JSONDecodeError, PermissionError) as exc:
        _fail(str(exc))


@cli.group("agent")
def agent() -> None:
    """Discover and operate local or cloud Agent Brain capabilities."""


@agent.command("list")
@click.pass_context
def agent_list(ctx: click.Context) -> None:
    """List locally discovered agents."""
    _emit(ctx, agent_core.list_agents(_paths(ctx)))


@agent.command("inspect")
@click.argument("agent_id", default="agent-brain")
@click.pass_context
def agent_inspect(ctx: click.Context, agent_id: str) -> None:
    """Inspect one local agent and its capability map."""
    try:
        _emit(ctx, agent_core.inspect_agent(_paths(ctx), agent_id))
    except (FileNotFoundError, KeyError) as exc:
        _fail(str(exc))


@agent.command("capabilities")
@click.pass_context
def agent_capabilities(ctx: click.Context) -> None:
    """Show discovered Agent Brain RPC services, methods, and routes."""
    _emit(ctx, agent_core.capabilities(_paths(ctx)))


@agent.command("auth-status")
@click.pass_context
def agent_auth_status(ctx: click.Context) -> None:
    """Check desktop login and cloud credential readiness without exposing secrets."""
    try:
        _emit(ctx, agent_core.authentication_status(client=_desktop()))
    except (ShizaiCloudError, OSError, TypeError, ValueError) as exc:
        _fail(str(exc))


def _resolved_agent_id(agent_id: str | None) -> str:
    """Resolve an optional Agent id without making a network request."""
    return agent_id or _desktop().config.agent_id


@agent.command("inventory")
@click.pass_context
def agent_inventory(ctx: click.Context) -> None:
    """Collect capabilities plus a bounded live read-only workspace inventory."""
    try:
        _emit(ctx, agent_core.inventory(_paths(ctx), client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("rpc")
@click.argument("service")
@click.argument("method")
@click.option("--args-json", default="[]", show_default=True, help="RPC argument list as a JSON array.")
@click.option("--allow-write", is_flag=True, help="Allow methods outside the read-only allowlist.")
@click.pass_context
def agent_rpc(ctx: click.Context, service: str, method: str, args_json: str, allow_write: bool) -> None:
    """Call SERVICE.METHOD on the local Agent Brain business channel."""
    try:
        args = json.loads(args_json)
        if not isinstance(args, list):
            raise ValueError("--args-json must decode to a JSON array")
        _emit(ctx, agent_core.rpc(service, method, args, allow_write=allow_write, client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError, PermissionError) as exc:
        _fail(str(exc))


@agent.command("apps")
@click.option("--page", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--limit", type=click.IntRange(min=1, max=100), default=20, show_default=True)
@click.pass_context
def agent_apps(ctx: click.Context, page: int, limit: int) -> None:
    """List published Agent apps from the current workspace."""
    try:
        _emit(ctx, agent_core.query_apps(page=page, limit=limit, client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("app-info")
@click.argument("agent_id", required=False)
@click.pass_context
def agent_app_info(ctx: click.Context, agent_id: str | None) -> None:
    """Read one Agent app's detail payload."""
    try:
        _emit(
            ctx,
            agent_core.query_agent_resource(
                "getAppDetail", [{"appId": _resolved_agent_id(agent_id)}], client=_desktop()
            ),
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("models")
@click.option("--type", "model_type", default="llm", show_default=True)
@click.option("--support-function-call", is_flag=True)
@click.option("--support-vision", is_flag=True)
@click.pass_context
def agent_models(ctx: click.Context, model_type: str, support_function_call: bool, support_vision: bool) -> None:
    """List models supported by the current workspace."""
    try:
        _emit(
            ctx,
            agent_core.query_models(
                model_type=model_type,
                support_func_call=support_function_call,
                support_vision=support_vision,
                client=_desktop(),
            ),
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("model-providers")
@click.pass_context
def agent_model_providers(ctx: click.Context) -> None:
    """List model providers and their supported model types."""
    try:
        _emit(ctx, agent_core.query_agent_resource("getModelProviders", [], client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("tools")
@click.pass_context
def agent_tools(ctx: click.Context) -> None:
    """List built-in, custom, workflow, and extension tools."""
    try:
        _emit(ctx, agent_core.query_tools(client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("components")
@click.option("--agent-id", default=None, help="Agent id; defaults to SHIZAI_AGENT_ID.")
@click.pass_context
def agent_components(ctx: click.Context, agent_id: str | None) -> None:
    """Inspect node types, default configs, variables, and tool parameters."""
    try:
        _emit(ctx, agent_core.query_components(agent_id=agent_id, client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("generation-context")
@click.option("--agent-id", default=None, help="Agent id; defaults to SHIZAI_AGENT_ID.")
@click.option("--query", default=None, help="Optional one-sentence flow request to carry into the planning context.")
@click.pass_context
def agent_generation_context(ctx: click.Context, agent_id: str | None, query: str | None) -> None:
    """Return the live designer contract and one-sentence planning contract."""
    try:
        _emit(ctx, agent_core.generation_context(query=query, agent_id=agent_id, client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("draft")
@click.option("--agent-id", default=None, help="Agent id; defaults to SHIZAI_AGENT_ID.")
@click.pass_context
def agent_draft(ctx: click.Context, agent_id: str | None) -> None:
    """Read the advanced-chat workflow draft."""
    try:
        _emit(ctx, agent_core.query_draft(agent_id=agent_id, client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("validate-draft")
@click.option("--agent-id", default=None, help="Agent id; defaults to SHIZAI_AGENT_ID.")
@click.option("--draft-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--draft-json", default=None, help="Workflow draft as a JSON object.")
@click.option("--strict", is_flag=True, help="Treat live-catalog warnings as validation failures.")
@click.pass_context
def agent_validate_draft(
    ctx: click.Context,
    agent_id: str | None,
    draft_file: Path | None,
    draft_json: str | None,
    strict: bool,
) -> None:
    """Validate a draft graph before saving it to the designer."""
    try:
        if bool(draft_file) == bool(draft_json):
            raise ValueError("provide exactly one of --draft-file or --draft-json")
        source = draft_file.read_text(encoding="utf-8") if draft_file else draft_json
        draft = json.loads(source or "")
        if not isinstance(draft, dict):
            raise ValueError("draft must be a JSON object")
        _emit(ctx, agent_core.validate_draft(draft, agent_id=agent_id, strict=strict, client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(str(exc))


@agent.command("save-draft")
@click.option("--agent-id", default=None, help="Agent id; defaults to SHIZAI_AGENT_ID.")
@click.option("--draft-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--draft-json", default=None, help="Workflow draft as a JSON object.")
@click.pass_context
def agent_save_draft(ctx: click.Context, agent_id: str | None, draft_file: Path | None, draft_json: str | None) -> None:
    """Save a generated advanced-chat workflow draft."""
    try:
        if bool(draft_file) == bool(draft_json):
            raise ValueError("provide exactly one of --draft-file or --draft-json")
        source = draft_file.read_text(encoding="utf-8") if draft_file else draft_json
        draft = json.loads(source or "")
        if not isinstance(draft, dict):
            raise ValueError("draft must be a JSON object")
        _emit(ctx, agent_core.save_draft(draft, agent_id=agent_id, client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(str(exc))


@agent.command("run-draft")
@click.argument("query")
@click.option("--agent-id", default=None, help="Agent id; defaults to SHIZAI_AGENT_ID.")
@click.option("--conversation-id", default=None)
@click.option("--timeout", type=float, default=None)
@click.pass_context
def agent_run_draft(
    ctx: click.Context,
    query: str,
    agent_id: str | None,
    conversation_id: str | None,
    timeout: float | None,
) -> None:
    """Run the current advanced-chat workflow draft with QUERY."""
    try:
        _emit(
            ctx,
            agent_core.run_draft(
                query,
                agent_id=agent_id,
                conversation_id=conversation_id,
                timeout=timeout,
                client=_desktop(),
            ),
        )
    except (ShizaiCloudError, OSError, ValueError, TypeError) as exc:
        _fail(str(exc))


@agent.command("runs")
@click.option("--agent-id", default=None, help="Agent id; defaults to SHIZAI_AGENT_ID.")
@click.pass_context
def agent_runs(ctx: click.Context, agent_id: str | None) -> None:
    """List advanced-chat workflow runs for one Agent."""
    try:
        _emit(
            ctx,
            agent_core.query_agent_resource(
                "getAdvancedChatWorkflowRuns", [{"appId": _resolved_agent_id(agent_id)}], client=_desktop()
            ),
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("logs")
@click.option("--agent-id", default=None, help="Agent id; defaults to SHIZAI_AGENT_ID.")
@click.option("--conversation-id", required=True)
@click.option("--message-id", required=True)
@click.pass_context
def agent_logs(ctx: click.Context, agent_id: str | None, conversation_id: str, message_id: str) -> None:
    """Read detailed execution logs for one conversation message."""
    try:
        _emit(
            ctx,
            agent_core.query_agent_resource(
                "getAgentLogDetail",
                [
                    {
                        "appId": _resolved_agent_id(agent_id),
                        "params": {"conversation_id": conversation_id, "message_id": message_id},
                    }
                ],
                client=_desktop(),
            ),
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("tracing")
@click.option("--agent-id", default=None, help="Agent id; defaults to SHIZAI_AGENT_ID.")
@click.option("--run-id", required=True)
@click.pass_context
def agent_tracing(ctx: click.Context, agent_id: str | None, run_id: str) -> None:
    """Read node-level tracing for one workflow run."""
    try:
        _emit(
            ctx,
            agent_core.query_agent_resource(
                "getTracingList", [{"appId": _resolved_agent_id(agent_id), "runID": run_id}], client=_desktop()
            ),
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("messages")
@click.option("--agent-id", default=None, help="Agent id; defaults to SHIZAI_AGENT_ID.")
@click.option("--conversation-id", required=True)
@click.pass_context
def agent_messages(ctx: click.Context, agent_id: str | None, conversation_id: str) -> None:
    """Read messages in one Agent conversation."""
    try:
        _emit(
            ctx,
            agent_core.query_agent_resource(
                "getConversationMessages",
                [{"appId": _resolved_agent_id(agent_id), "conversationId": conversation_id}],
                client=_desktop(),
            ),
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("suggested")
@click.option("--agent-id", default=None, help="Agent id; defaults to SHIZAI_AGENT_ID.")
@click.option("--message-id", required=True)
@click.pass_context
def agent_suggested(ctx: click.Context, agent_id: str | None, message_id: str) -> None:
    """Read suggested questions for one Agent message."""
    try:
        _emit(
            ctx,
            agent_core.query_agent_resource(
                "getSuggestedQuestions",
                [{"appId": _resolved_agent_id(agent_id), "messageId": message_id}],
                client=_desktop(),
            ),
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("mcp-servers")
@click.option("--page", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--limit", type=click.IntRange(min=1, max=100), default=100, show_default=True)
@click.option("--keyword", default="")
@click.pass_context
def agent_mcp_servers(ctx: click.Context, page: int, limit: int, keyword: str) -> None:
    """List configured MCP servers with credentials redacted."""
    try:
        _emit(ctx, agent_core.query_mcp_servers(page=page, limit=limit, keyword=keyword, client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("datasets")
@click.option("--page", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--limit", type=click.IntRange(min=1, max=100), default=20, show_default=True)
@click.option("--keyword", default="")
@click.pass_context
def agent_datasets(ctx: click.Context, page: int, limit: int, keyword: str) -> None:
    """List knowledge bases visible to the current Agent workspace."""
    try:
        _emit(ctx, agent_core.query_datasets(page=page, limit=limit, keyword=keyword, client=_desktop()))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("cloud-list")
@click.option("--page", type=int, default=1, show_default=True)
@click.option("--limit", type=int, default=20, show_default=True)
@click.pass_context
def agent_cloud_list(ctx: click.Context, page: int, limit: int) -> None:
    """List cloud Agents visible to the current account."""
    try:
        _emit(ctx, _cloud().list_apps(page=page, limit=limit, mode="advanced-chat"))
    except (ShizaiCloudError, OSError) as exc:
        _fail(str(exc))


@agent.command("cloud-info")
@click.argument("agent_id", required=False)
@click.pass_context
def agent_cloud_info(ctx: click.Context, agent_id: str | None) -> None:
    """Show one cloud Agent's metadata and publish state."""
    try:
        _emit(ctx, _cloud().app_info(agent_id))
    except (ShizaiCloudError, OSError) as exc:
        _fail(str(exc))


@agent.command("configure-knowledge")
@click.option("--dataset-id", default=None, help="Knowledge-base id; defaults to SHIZAI_DATASET_ID.")
@click.option("--agent-id", default=None, help="Cloud Agent id; defaults to SHIZAI_AGENT_ID.")
@click.option("--top-k", type=click.IntRange(1, 20), default=None)
@click.pass_context
def agent_configure_knowledge(
    ctx: click.Context, dataset_id: str | None, agent_id: str | None, top_k: int | None
) -> None:
    """Bind the cloud Agent's knowledge-retrieval node to a knowledge base."""
    try:
        _emit(
            ctx,
            _desktop().configure_knowledge(dataset_id or _cloud().config.dataset_id, agent_id=agent_id, top_k=top_k),
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("publish")
@click.option("--agent-id", default=None, help="Cloud Agent id; defaults to SHIZAI_AGENT_ID.")
@click.option(
    "--publish-range", type=click.Choice(["only_me", "all_team_members"]), default="only_me", show_default=True
)
@click.pass_context
def agent_publish(ctx: click.Context, agent_id: str | None, publish_range: str) -> None:
    """Publish the current advanced-chat workflow."""
    try:
        _emit(
            ctx,
            agent_core.publish(
                agent_id=agent_id,
                publish_range=publish_range,
                client=_desktop(),
            ),
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@cli.group("feishu")
def feishu() -> None:
    """Inspect or configure the desktop application's native Feishu route."""


@feishu.command("native-status")
@click.pass_context
def feishu_native_status(ctx: click.Context) -> None:
    """Inspect the native Feishu bot configuration in the running desktop app."""
    try:
        _emit(ctx, _desktop().native_feishu_status())
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@feishu.command("native-disable")
@click.option("--user-id", default=None, help="Desktop user id; defaults to the logged-in account.")
@click.pass_context
def feishu_native_disable(ctx: click.Context, user_id: str | None) -> None:
    """Disable the built-in desktop Feishu/Tars route without opening the UI."""
    try:
        _emit(
            ctx,
            _desktop().disable_native_feishu(
                user_id=user_id,
                persist=not _ctx(ctx).get("dry_run"),
            ),
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@feishu.command("native-configure")
@click.option("--client-id", "client_id", envvar="FEISHU_APP_ID", default=None, help="Feishu app id.")
@click.option(
    "--client-secret",
    "client_secret",
    envvar="FEISHU_APP_SECRET",
    default=None,
    help="Feishu app secret; prefer FEISHU_APP_SECRET or --secret-stdin.",
)
@click.option("--secret-stdin", is_flag=True, help="Read the Feishu app secret from stdin.")
@click.option("--user-id", default=None, help="Desktop user id; defaults to the logged-in account.")
@click.option("--inactive", is_flag=True, help="Save the configuration without activating the native bot.")
@click.pass_context
def feishu_native_configure(
    ctx: click.Context,
    client_id: str | None,
    client_secret: str | None,
    secret_stdin: bool,
    user_id: str | None,
    inactive: bool,
) -> None:
    """Configure the native Feishu bot through desktop RPC, without opening the UI."""
    if secret_stdin:
        if client_secret:
            _fail("Use either --secret-stdin or --client-secret/FEISHU_APP_SECRET, not both")
        client_secret = sys.stdin.read().strip()
    if not client_id:
        _fail("FEISHU_APP_ID or --client-id is required")
    if not client_secret:
        _fail("FEISHU_APP_SECRET, --client-secret, or --secret-stdin is required")
    try:
        _emit(
            ctx,
            _desktop().configure_native_feishu(
                client_id,
                client_secret,
                user_id=user_id,
                active_mode=not inactive,
                persist=not _ctx(ctx).get("dry_run"),
            ),
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@agent.command("configure-ingestion")
@click.option("--agent-id", default=None, help="Cloud Agent id; defaults to SHIZAI_AGENT_ID.")
@click.pass_context
def agent_configure_ingestion(ctx: click.Context, agent_id: str | None) -> None:
    """Add file-reading and webpage-analysis branches to the Agent workflow."""
    try:
        _emit(ctx, _desktop().configure_ingestion_tools(agent_id=agent_id))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@cli.group("knowledge")
def knowledge() -> None:
    """Manage the cloud AI knowledge base."""


@knowledge.command("info")
@click.option("--dataset-id", default=None)
@click.pass_context
def knowledge_info(ctx: click.Context, dataset_id: str | None) -> None:
    """Show knowledge-base metadata."""
    try:
        _emit(ctx, _cloud().dataset_info(dataset_id))
    except (ShizaiCloudError, OSError) as exc:
        _fail(str(exc))


@knowledge.command("list")
@click.option("--dataset-id", default=None)
@click.option("--page", type=int, default=1, show_default=True)
@click.option("--limit", type=click.IntRange(1, 100), default=20, show_default=True)
@click.pass_context
def knowledge_list(ctx: click.Context, dataset_id: str | None, page: int, limit: int) -> None:
    """List documents currently in the knowledge base."""
    try:
        _emit(ctx, _cloud().list_documents(dataset_id, page=page, limit=limit))
    except (ShizaiCloudError, OSError) as exc:
        _fail(str(exc))


@knowledge.command("wait")
@click.argument("document_ids", nargs=-1, required=True)
@click.option("--dataset-id", default=None)
@click.option("--timeout", type=click.FloatRange(min=0), default=300.0, show_default=True)
@click.option("--interval", type=click.FloatRange(min=0), default=2.0, show_default=True)
@click.pass_context
def knowledge_wait(
    ctx: click.Context, document_ids: tuple[str, ...], dataset_id: str | None, timeout: float, interval: float
) -> None:
    """Wait for DOCUMENT_IDS to finish indexing."""
    try:
        result = _cloud().wait_for_documents(
            list(document_ids),
            dataset_id=dataset_id,
            timeout=timeout,
            interval=interval,
        )
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))
    _emit(ctx, result)
    if not result["ready"]:
        raise click.exceptions.Exit(2)


@knowledge.command("upload")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dataset-id", default=None)
@click.option("--process-mode", type=click.Choice(["automatic", "custom"]), default="automatic", show_default=True)
@click.option("--wait", "wait_for_index", is_flag=True, help="Wait until the document is indexed and available.")
@click.option("--timeout", type=click.FloatRange(min=0), default=300.0, show_default=True)
@click.option("--interval", type=click.FloatRange(min=0), default=2.0, show_default=True)
@click.pass_context
def knowledge_upload(
    ctx: click.Context,
    file_path: Path,
    dataset_id: str | None,
    process_mode: str,
    wait_for_index: bool,
    timeout: float,
    interval: float,
) -> None:
    """Upload FILE_PATH and enqueue it for indexing."""
    try:
        api = _cloud()
        result = api.add_file(file_path, dataset_id=dataset_id, process_mode=process_mode)
        if wait_for_index and result["document_ids"]:
            result["indexing"] = api.wait_for_documents(
                result["document_ids"], dataset_id=dataset_id, timeout=timeout, interval=interval
            )
        _emit(ctx, result)
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@knowledge.command("add-url")
@click.argument("url")
@click.option("--dataset-id", default=None)
@click.option("--process-mode", type=click.Choice(["automatic", "custom"]), default="automatic", show_default=True)
@click.option("--wait", "wait_for_index", is_flag=True, help="Wait until the document is indexed and available.")
@click.option("--timeout", type=click.FloatRange(min=0), default=300.0, show_default=True)
@click.option("--interval", type=click.FloatRange(min=0), default=2.0, show_default=True)
@click.pass_context
def knowledge_add_url(
    ctx: click.Context,
    url: str,
    dataset_id: str | None,
    process_mode: str,
    wait_for_index: bool,
    timeout: float,
    interval: float,
) -> None:
    """Add URL and enqueue its webpage content for indexing."""
    try:
        api = _cloud()
        result = api.add_url(url, dataset_id=dataset_id, process_mode=process_mode)
        if wait_for_index and result["document_ids"]:
            result["indexing"] = api.wait_for_documents(
                result["document_ids"], dataset_id=dataset_id, timeout=timeout, interval=interval
            )
        _emit(ctx, result)
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@knowledge.command("ingest")
@click.argument("source")
@click.option("--dataset-id", default=None)
@click.option("--process-mode", type=click.Choice(["automatic", "custom"]), default="automatic", show_default=True)
@click.option("--wait", "wait_for_index", is_flag=True, help="Wait until the document is indexed and available.")
@click.option("--timeout", type=click.FloatRange(min=0), default=300.0, show_default=True)
@click.option("--interval", type=click.FloatRange(min=0), default=2.0, show_default=True)
@click.pass_context
def knowledge_ingest(
    ctx: click.Context,
    source: str,
    dataset_id: str | None,
    process_mode: str,
    wait_for_index: bool,
    timeout: float,
    interval: float,
) -> None:
    """Import SOURCE, accepting either a webpage URL or a local file."""
    try:
        api = _cloud()
        result = _ingest_source(api, source, dataset_id=dataset_id, process_mode=process_mode)
        if wait_for_index and result["document_ids"]:
            result["indexing"] = api.wait_for_documents(
                result["document_ids"], dataset_id=dataset_id, timeout=timeout, interval=interval
            )
        _emit(ctx, result)
    except (FileNotFoundError, ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@knowledge.command("delete")
@click.argument("document_id")
@click.option("--dataset-id", default=None)
@click.pass_context
def knowledge_delete(ctx: click.Context, document_id: str, dataset_id: str | None) -> None:
    """Delete a document by id."""
    try:
        _emit(ctx, _cloud().delete_document(document_id, dataset_id))
    except (ShizaiCloudError, OSError) as exc:
        _fail(str(exc))


@cli.group("chat")
def chat() -> None:
    """Chat with the configured cloud Agent through the logged-in desktop app."""


@chat.command("ask")
@click.argument("question")
@click.option("--agent-id", default=None)
@click.option("--conversation-id", default=None)
@click.pass_context
def chat_ask(ctx: click.Context, question: str, agent_id: str | None, conversation_id: str | None) -> None:
    """Ask QUESTION and return the answer with workflow metadata."""
    try:
        _emit(ctx, _desktop().chat(question, agent_id=agent_id, conversation_id=conversation_id))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@cli.group("workflow")
def workflow() -> None:
    """Workflow-oriented aliases for project inspection."""


@workflow.command("validate")
@click.argument("project_ref", required=False)
@click.pass_context
def workflow_validate(ctx: click.Context, project_ref: str | None) -> None:
    """Validate a workflow project using the native project checks."""
    ctx.invoke(project_validate, project_ref=project_ref)


@workflow.command("rpa-context")
@click.option("--query", default=None, help="Natural-language RPA request used for context only.")
@click.pass_context
def workflow_rpa_context(ctx: click.Context, query: str | None) -> None:
    """Print the browser RPA capture and generation contract for an AI model."""
    _emit(ctx, project_core.rpa_generation_context(query=query))


@workflow.command("rpa-analyze-request")
@click.argument("query")
@click.pass_context
def workflow_rpa_analyze_request(ctx: click.Context, query: str) -> None:
    """Analyze an original request without creating a project or controlling UI."""
    _emit(ctx, project_core.analyze_rpa_request(query))


@workflow.command("rpa-validate-plan")
@click.option("--plan-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--analysis-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.pass_context
def workflow_rpa_validate_plan(ctx: click.Context, plan_file: Path, analysis_file: Path | None) -> None:
    """Read-only plan preflight using the same live contracts as MCP."""
    try:
        plan = json.loads(plan_file.read_text(encoding="utf-8-sig"))
        analysis = json.loads(analysis_file.read_text(encoding="utf-8-sig")) if analysis_file else {}
        result = project_core.preflight_rpa_execution_plan(
            analysis, plan, available_components=component_catalog_core.builtin_component_index(_paths(ctx)),
            planning_policy="mcp",
        )
        _emit(ctx, result)
        if not result.get("valid"):
            ctx.exit(1)
    except (OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-open-browser")
@click.option("--url", required=True)
@click.option("--wait-seconds", type=click.FloatRange(min=0, max=10), default=1.0, show_default=True)
@click.option("--allow-write", is_flag=True, help="Open the URL in the current Chrome profile.")
@click.pass_context
def workflow_rpa_open_browser(ctx: click.Context, url: str, wait_seconds: float, allow_write: bool) -> None:
    """Open URL in the current Chrome profile for design-time element capture."""
    if not allow_write:
        _fail("workflow rpa-open-browser requires --allow-write")
    try:
        _emit(ctx, open_current_chrome(url, wait_seconds=wait_seconds))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-browser-session-start")
@click.argument("project_ref", required=False)
@click.option("--allow-write", is_flag=True, help="Initialize the project-bound DeepCapture read session.")
@click.pass_context
def workflow_rpa_browser_session_start(ctx: click.Context, project_ref: str | None, allow_write: bool) -> None:
    """Initialize DeepCapture for page reads without opening the picker overlay."""
    if not allow_write:
        _fail("workflow rpa-browser-session-start requires --allow-write")
    try:
        ref = _workflow_project_ref(ctx, project_ref)
        result = ShizaiExtensionBridge(desktop=_desktop()).ensure_capture_session(package_path=ref)
        result["next_action"] = "workflow rpa-capture-windows"
        _emit(ctx, result)
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-browser-session-status")
@click.argument("project_ref", required=False)
@click.pass_context
def workflow_rpa_browser_session_status(ctx: click.Context, project_ref: str | None) -> None:
    """Read the last known DeepCapture session state without starting it."""
    try:
        ref = _workflow_project_ref(ctx, project_ref)
        _emit(ctx, ShizaiExtensionBridge(desktop=_desktop()).capture_session_status(package_path=ref))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-browser-session-stop")
@click.argument("project_ref", required=False)
@click.option("--allow-write", is_flag=True, help="Stop the project-bound DeepCapture session and picker if active.")
@click.pass_context
def workflow_rpa_browser_session_stop(ctx: click.Context, project_ref: str | None, allow_write: bool) -> None:
    """Stop DeepCapture and its official picker overlay for a project."""
    if not allow_write:
        _fail("workflow rpa-browser-session-stop requires --allow-write")
    try:
        ref = _workflow_project_ref(ctx, project_ref)
        _emit(ctx, ShizaiExtensionBridge(desktop=_desktop()).stop_capture(package_path=ref))
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-start-design")
@click.option("--name", default="网页 RPA 流程", show_default=True)
@click.option("--url", "target_url", required=True, help="Target URL derived from the current request plan.")
@click.option("--keyword", default="", help="Optional value from the current request; never inferred by the CLI.")
@click.option("--output-file", default="result.xlsx", show_default=True)
@click.option(
    "--required-column", "required_columns", multiple=True, help="Output column header; repeat for multiple columns."
)
@click.option(
    "--use-excel-components/--direct-excel-export",
    default=True,
    show_default=True,
    help="Use Excel_Cs_NewV4 and Excel_Cs_WriteRange after data collection.",
)
@click.option(
    "--open-new-tab/--reuse-current-tab",
    default=False,
    show_default=True,
    help="Open a separate runtime Chrome tab; default reuses the current tab.",
)
@click.option(
    "--use-js-link/--native-link-capture",
    default=False,
    show_default=True,
    help="Legacy compatibility only; new plans must provide explicit JavaScript through rpa-add-step.",
)
@click.option(
    "--use-js-table/--native-data-collection",
    default=False,
    show_default=True,
    help="Return the requested result columns directly from page JavaScript and write them with Excel_Cs_WriteRange.",
)
@click.option("--max-rows", type=click.IntRange(min=1, max=1000), default=None)
@click.option(
    "--wait-after-submit/--no-wait-after-submit",
    default=True,
    show_default=True,
    help="Add Basic_Delay_V1 after Enter so the result page can render before recapture.",
)
@click.option(
    "--refresh-browser-tab/--reuse-browser-handle",
    default=True,
    show_default=True,
    help="Add WebBrowser_GetTAB_V1 after the result wait and reuse the current Chrome session.",
)
@click.option("--wait-seconds", type=click.FloatRange(min=0, max=10), default=1.0, show_default=True)
@click.option("--open-browser/--no-open-browser", default=True, show_default=True)
@click.option("--allow-write", is_flag=True, help="Create the client-owned flow and write its first planned component.")
@click.pass_context
def workflow_rpa_start_design(
    ctx: click.Context,
    name: str,
    target_url: str,
    keyword: str,
    output_file: str,
    required_columns: tuple[str, ...],
    use_excel_components: bool,
    open_new_tab: bool,
    use_js_link: bool,
    use_js_table: bool,
    max_rows: int,
    wait_after_submit: bool,
    refresh_browser_tab: bool,
    wait_seconds: float,
    open_browser: bool,
    allow_write: bool,
) -> None:
    """Create a native designer flow, then take over only before real browser I/O."""
    if not allow_write:
        _fail("workflow rpa-start-design requires --allow-write")
    desktop: ShizaiDesktopAgent | None = None
    native_ui_active = False
    result: dict[str, Any] | None = None
    try:
        desktop = _desktop()
        result = project_core.start_rpa_design(
            _paths(ctx),
            name=name,
            target_url=target_url,
            keyword=keyword,
            output_file=output_file,
            required_columns=list(required_columns) or None,
            use_excel_components=use_excel_components,
            open_new_tab=open_new_tab,
            use_js_link=use_js_link,
            use_js_table=use_js_table,
            max_rows=max_rows,
            wait_seconds=wait_seconds,
            wait_after_submit=wait_after_submit,
            refresh_browser_tab=refresh_browser_tab,
            client=desktop,
        )
        if open_browser:
            native_ui_active = True
            native_ui: dict[str, Any] = {}
            card = NativeSmallCard(client=desktop)
            for key, action in (
                (
                    "plan_ready",
                    lambda: card.show(
                        state="ready",
                        message=f"塔斯已完成流程设计，准备开始实际操作：{name}",
                    ),
                ),
                (
                    "countdown",
                    lambda: card.countdown(
                        seconds=5,
                        message_template="塔斯将在 {seconds} 秒后接管你的电脑。",
                    ),
                ),
                ("takeover_frame", lambda: NativeTakeoverFrame(client=desktop).show()),
                (
                    "small_card",
                    lambda: card.show(
                        state="takeover",
                        message="塔斯正在接管你的电脑并打开计划中的目标页面。",
                    ),
                ),
            ):
                try:
                    native_ui[key] = action()
                except (ShizaiCloudError, OSError, TimeoutError, ValueError) as exc:
                    native_ui[f"{key}_warning"] = str(exc)
            result["native_ui"] = native_ui
            try:
                result["design_time_browser"] = open_current_chrome(target_url, wait_seconds=wait_seconds)
                if isinstance(result["design_time_browser"], dict) and result["design_time_browser"].get("ui_feedback"):
                    result["ui_feedback"] = result["design_time_browser"]["ui_feedback"]
            except (ShizaiCloudError, OSError, ValueError) as exc:
                result["design_time_browser"] = {
                    "opened": False,
                    "browser": "chrome",
                    "url": target_url,
                    "error": str(exc),
                    "message": "流程已创建；请在当前 Chrome 手动打开目标网址后继续元素采集。",
                    "next_action": "shizai_rpa_capture_windows",
                }
                result["browser_open_warning"] = str(exc)
            try:
                bridge = ShizaiExtensionBridge(desktop=desktop)
                session = _ensure_capture_session(bridge, result.get("flow_ref") or result.get("project_ref"))
                if session is not None:
                    result["capture"] = session
                    result["capture_started"] = True
            except Exception as exc:
                result["capture_warning"] = str(exc)
            native_ui["final_cleanup"] = _cleanup_native_ui(desktop)
            native_ui_active = False
        else:
            result["design_time_browser"] = None
        _emit(ctx, result)
    except (FileExistsError, FileNotFoundError, ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))
    finally:
        if native_ui_active and desktop is not None:
            cleanup = _cleanup_native_ui(desktop)
            if isinstance(result, dict):
                native_ui = result.setdefault("native_ui", {})
                if isinstance(native_ui, dict):
                    native_ui["final_cleanup"] = cleanup


@workflow.command("rpa-capture-windows")
@click.option("--include-non-chrome", is_flag=True)
@click.option("--project", "project_ref", default=None, help="Active flow used to initialize DeepCapture.")
@click.pass_context
def workflow_rpa_capture_windows(ctx: click.Context, include_non_chrome: bool, project_ref: str | None) -> None:
    """List visible Chrome windows available to DeepCapture."""
    try:
        bridge = ShizaiExtensionBridge(desktop=_desktop())
        session = _ensure_capture_session(bridge, project_ref or _ctx(ctx).get("project_ref"))
        result, recovery = _capture_read_with_recovery(
            bridge,
            project_ref or _ctx(ctx).get("project_ref"),
            lambda: bridge.capture_windows(include_non_chrome=include_non_chrome),
        )
        session = recovery or session
        if session is not None:
            result["capture_session"] = session
        _emit(ctx, result)
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-capture-tree")
@click.option("--element-id", required=True)
@click.option("--level", type=click.IntRange(min=0, max=20), default=6, show_default=True)
@click.option("--max-nodes", type=click.IntRange(min=1, max=1000), default=120, show_default=True)
@click.option("--project", "project_ref", default=None, help="Active flow used to initialize DeepCapture.")
@click.pass_context
def workflow_rpa_capture_tree(
    ctx: click.Context, element_id: str, level: int, max_nodes: int, project_ref: str | None
) -> None:
    """Read a DeepCapture tree below ELEMENT_ID."""
    try:
        bridge = ShizaiExtensionBridge(desktop=_desktop())
        session = _ensure_capture_session(bridge, project_ref or _ctx(ctx).get("project_ref"))
        result, recovery = _capture_read_with_recovery(
            bridge,
            project_ref or _ctx(ctx).get("project_ref"),
            lambda: bridge.capture_tree(element_id, level=level, max_nodes=max_nodes),
        )
        session = recovery or session
        if session is not None:
            result["capture_session"] = session
        _emit(ctx, result)
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-scan-page")
@click.option(
    "--root-element-id",
    default=None,
    help="Chrome window or Document element id; omit to inspect current Chrome windows.",
)
@click.option("--query", default="", help="Semantic filter such as 商品名称、价格、销量 or 搜索框.")
@click.option("--level", type=click.IntRange(min=1, max=20), default=6, show_default=True)
@click.option("--max-nodes", type=click.IntRange(min=1, max=1000), default=250, show_default=True)
@click.option("--page-title", default=None)
@click.option("--page-url", default=None)
@click.option("--scan-token", default=None, help="Reuse a token returned by rpa-scan-page without rescanning.")
@click.option(
    "--strategy",
    type=click.Choice(["auto", "targeted", "full"]),
    default="auto",
    show_default=True,
    help="Use bounded control lookup for search/submit queries, or force full-page scanning.",
)
@click.option("--project", "project_ref", default=None, help="Active flow used to initialize DeepCapture.")
@click.pass_context
def workflow_rpa_scan_page(
    ctx: click.Context,
    root_element_id: str | None,
    query: str,
    level: int,
    max_nodes: int,
    page_title: str | None,
    page_url: str | None,
    scan_token: str | None,
    strategy: str,
    project_ref: str | None,
) -> None:
    """Scan the current Document subtree for semantic webpage element candidates."""
    try:
        bridge = ShizaiExtensionBridge(desktop=_desktop())
        session = _ensure_capture_session(bridge, project_ref or _ctx(ctx).get("project_ref"))
        result, recovery = _capture_read_with_recovery(
            bridge,
            project_ref or _ctx(ctx).get("project_ref"),
            lambda: bridge.scan_page(
                root_element_id=root_element_id,
                query=query,
                level=level,
                max_nodes=max_nodes,
                page_title=page_title,
                page_url=page_url,
                scan_token=scan_token,
                strategy=strategy,
            ),
        )
        session = recovery or session
        if session is not None:
            result["capture_session"] = session
        _emit(ctx, result)
    except (ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-capture-page-fields")
@click.option(
    "--field",
    "fields",
    multiple=True,
    help=(
        "Semantic field to capture; repeat for product_id, product_name, merchant, price, sales, "
        "sales_metric, link."
    ),
)
@click.option(
    "--field-element-id",
    "field_element_ids",
    multiple=True,
    help="Optional field=id override; repeat to select exact candidates from one scan.",
)
@click.option("--candidate-limit", type=click.IntRange(min=1, max=20), default=4, show_default=True)
@click.option("--level", type=click.IntRange(min=1, max=20), default=6, show_default=True)
@click.option("--max-nodes", type=click.IntRange(min=1, max=1000), default=250, show_default=True)
@click.option("--project", "project_ref", default=None)
@click.option(
    "--step-id",
    default=None,
    help="Bind the selected field elements to this existing DataCollectionV5 step after import.",
)
@click.option("--page-title", default=None)
@click.option("--page-url", default=None)
@click.option("--scan-token", default=None, help="Reuse the exact candidate snapshot returned by rpa-scan-page.")
@click.option("--card-grouping", type=click.Choice(["auto", "instance", "family"]), default="auto", show_default=True)
@click.option(
    "--use-js-link/--native-link-capture",
    default=False,
    show_default=True,
    help="Capture four product fields and let the JS link component supply 商品链接.",
)
@click.option("--prune-unused", is_flag=True)
@click.option(
    "--allow-write", is_flag=True, help="Import captured native elements into PROJECT and optionally bind them."
)
@click.pass_context
def workflow_rpa_capture_page_fields(
    ctx: click.Context,
    fields: tuple[str, ...],
    field_element_ids: tuple[str, ...],
    candidate_limit: int,
    level: int,
    max_nodes: int,
    project_ref: str | None,
    step_id: str | None,
    page_title: str | None,
    page_url: str | None,
    scan_token: str | None,
    card_grouping: str,
    use_js_link: bool,
    prune_unused: bool,
    allow_write: bool,
) -> None:
    """Scan and capture a coherent page-field set in one DeepCapture session."""
    if project_ref and not allow_write:
        _fail("workflow rpa-capture-page-fields with --project requires --allow-write")
    selected_fields = list(
        fields
        or (
            ("product_id", "product_name", "price", "sales", "sales_metric")
            if use_js_link
            else ("product_id", "product_name", "merchant", "price", "sales", "sales_metric", "link")
        )
    )
    overrides: dict[str, str] = {}
    for value in field_element_ids:
        if "=" not in value:
            _fail(f"--field-element-id must use field=id: {value}")
        field, element_id = value.split("=", 1)
        if not field.strip() or not element_id.strip():
            _fail(f"--field-element-id must use field=id: {value}")
        overrides[field.strip()] = element_id.strip()
    try:
        if project_ref:
            flow = project_core.read_rpa_flow(_paths(ctx), project_ref).get("flow") or {}
            required = project_core._result_capture_requirements(
                flow.get("required_columns") or project_core.DEFAULT_RPA_COLUMNS,
                use_js_link=use_js_link,
                use_js_table=bool(flow.get("use_js_table", False)),
            )
            selected_fields.extend(
                field
                for field in project_core._RPA_RESULT_FIELD_ORDER
                if field in required and field not in selected_fields
            )
        if use_js_link:
            selected_fields = [field for field in selected_fields if field not in {"merchant", "link"}]
        bridge = ShizaiExtensionBridge(desktop=_desktop())
        session = _ensure_capture_session(bridge, project_ref or _ctx(ctx).get("project_ref"))
        result, recovery = _capture_read_with_recovery(
            bridge,
            project_ref or _ctx(ctx).get("project_ref"),
            lambda: bridge.capture_page_fields(
                fields=selected_fields,
                field_element_ids=overrides,
                level=level,
                max_nodes=max_nodes,
                candidate_limit=candidate_limit,
                page_title=page_title,
                page_url=page_url,
                scan_token=scan_token,
                card_grouping=card_grouping,
            ),
        )
        session = recovery or session
        if project_ref:
            if not result.get("complete"):
                _fail("page-field capture is incomplete; review selected_group/missing_fields before importing")
            result["import"] = project_core.import_elements(
                _paths(ctx),
                project_ref,
                result["elements"],
                replace=False,
                force_replace=False,
                capture_source="deep_capture",
            )
            if step_id:
                selected_field_ids = result.get("selected_field_ids") or {}
                flow = project_core.read_rpa_flow(_paths(ctx), project_ref).get("flow") or {}
                binding_ids = [
                    str(selected_field_ids.get(project_core._canonical_result_field(column)) or "").strip()
                    for column in flow.get("required_columns") or project_core.DEFAULT_RPA_COLUMNS
                ]
                binding_ids = [element_id for element_id in binding_ids if element_id]
                result["binding"] = project_core.set_rpa_step_elements(
                    _paths(ctx),
                    project_ref,
                    step_id,
                    binding_ids or result["import"]["imported"],
                    client=_desktop(),
                )
            if prune_unused:
                result["prune"] = project_core.prune_unused_rpa_elements(
                    _paths(ctx), project_ref, keep_ids=result["import"]["imported"]
                )
            result["result_state"] = project_core.mark_rpa_result_capture_ready(
                _paths(ctx),
                project_ref,
                captured_fields=result.get("selected_field_ids") or {},
                field_element_ids=result.get("selected_field_ids") or {},
                use_js_link=use_js_link,
                selector_scope=result.get("selector_scope"),
            )
        if session is not None:
            result["capture_session"] = session
        _emit(ctx, result)
    except (FileNotFoundError, ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-runtime-preflight")
@click.option("--project", "project_ref", default=None, help="Client application directory or id.")
@click.option("--step-id", default=None, help="Optional element-dependent native step id.")
@click.pass_context
def workflow_rpa_runtime_preflight(ctx: click.Context, project_ref: str | None, step_id: str | None) -> None:
    """Check the current Chrome page before executing a native element step."""
    try:
        _emit(
            ctx,
            debug_core.runtime_preflight(
                _paths(ctx),
                _workflow_project_ref(ctx, project_ref),
                step_id=step_id,
                client=_desktop(),
            ),
        )
    except (FileNotFoundError, ShizaiCloudError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-capture-element")
@click.option("--element-id", required=True)
@click.option("--project", "project_ref", default=None, help="Active flow used to initialize DeepCapture.")
@click.option("--page-title", default=None, help="Current browser page title when the UIA selector has no tab entry.")
@click.option("--page-url", default=None, help="Current browser page URL when the UIA selector has no tab entry.")
@click.option(
    "--keep-picker-open",
    is_flag=True,
    help="Keep the official red picker open for another capture; otherwise close it after this read.",
)
@click.pass_context
def workflow_rpa_capture_element(
    ctx: click.Context,
    element_id: str,
    project_ref: str | None,
    page_title: str | None,
    page_url: str | None,
    keep_picker_open: bool,
) -> None:
    """Read one native DeepCapture element and selector."""
    bridge: ShizaiExtensionBridge | None = None
    try:
        bridge = ShizaiExtensionBridge(desktop=_desktop())
        session = _ensure_capture_session(bridge, project_ref or _ctx(ctx).get("project_ref"))
        result, recovery = _capture_read_with_recovery(
            bridge,
            project_ref or _ctx(ctx).get("project_ref"),
            lambda: bridge.capture_element(element_id, page_title=page_title, page_url=page_url),
        )
        session = recovery or session
        if session is not None:
            result["capture_session"] = session
        result["picker_cleanup"] = _stop_picker_after_capture(bridge, keep_open=keep_picker_open)
        _emit(ctx, result)
    except click.ClickException:
        if bridge is not None:
            try:
                _stop_picker_after_capture(bridge, keep_open=keep_picker_open)
            except Exception:
                pass
        raise
    except (ShizaiCloudError, OSError, ValueError) as exc:
        if bridge is not None:
            try:
                _stop_picker_after_capture(bridge, keep_open=keep_picker_open)
            except Exception:
                pass
        _fail(str(exc))


@workflow.command("rpa-capture-elements")
@click.option("--element-id", "element_ids", multiple=True, required=True)
@click.option("--project", "--project-ref", "project_ref", default=None)
@click.option("--step-id", default=None, help="Bind all captured ids to this existing flow step after import.")
@click.option(
    "--as-page-selector",
    is_flag=True,
    help="Bind the first captured id as DataCollectionV5's pagination control instead of a data column.",
)
@click.option(
    "--pagination-mode",
    type=click.Choice(["next-page", "load-more"]),
    default="next-page",
    show_default=True,
    help="Pagination control type used with --as-page-selector.",
)
@click.option("--page-title", default=None, help="Current browser page title when the UIA selector has no tab entry.")
@click.option("--page-url", default=None, help="Current browser page URL when the UIA selector has no tab entry.")
@click.option("--scan-token", default=None, help="Token returned by rpa-scan-page for these exact element ids.")
@click.option("--replace", is_flag=True)
@click.option("--force-replace", is_flag=True, help="Allow replacement when the flow already references elements.")
@click.option("--prune-unused", is_flag=True, help="Remove element records that the flow no longer references.")
@click.option(
    "--keep-picker-open",
    is_flag=True,
    help="Keep the official red picker open for another capture; otherwise close it after this import.",
)
@click.option("--allow-write", is_flag=True, help="Import captured native elements into PROJECT.")
@click.pass_context
def workflow_rpa_capture_elements(
    ctx: click.Context,
    element_ids: tuple[str, ...],
    project_ref: str | None,
    step_id: str | None,
    as_page_selector: bool,
    pagination_mode: str,
    page_title: str | None,
    page_url: str | None,
    scan_token: str | None,
    replace: bool,
    force_replace: bool,
    prune_unused: bool,
    keep_picker_open: bool,
    allow_write: bool,
) -> None:
    """Capture several elements and optionally import them into a project."""
    if project_ref and not allow_write:
        _fail("workflow rpa-capture-elements with --project requires --allow-write")
    if as_page_selector and not step_id:
        _fail("--as-page-selector requires --step-id for the DataCollectionV5 step")
    if as_page_selector and not project_ref:
        _fail("--as-page-selector requires --project so the pagination binding can be persisted")
    if as_page_selector and len(element_ids) != 1:
        _fail("--as-page-selector requires exactly one captured element id")
    bridge: ShizaiExtensionBridge | None = None
    try:
        bridge = ShizaiExtensionBridge(desktop=_desktop())
        session = _ensure_capture_session(bridge, project_ref or _ctx(ctx).get("project_ref"))
        result, recovery = _capture_read_with_recovery(
            bridge,
            project_ref or _ctx(ctx).get("project_ref"),
            lambda: bridge.capture_elements(
                element_ids, page_title=page_title, page_url=page_url, scan_token=scan_token
            ),
        )
        session = recovery or session
        if project_ref:
            result["import"] = project_core.import_elements(
                _paths(ctx),
                project_ref,
                result["elements"],
                replace=replace,
                force_replace=force_replace,
                capture_source="deep_capture",
            )
            if step_id:
                if as_page_selector:
                    result["binding"] = project_core.bind_rpa_pagination(
                        _paths(ctx),
                        project_ref,
                        step_id,
                        result["import"]["imported"][0],
                        mode=pagination_mode,
                        client=_desktop(),
                    )
                else:
                    result["binding"] = project_core.set_rpa_step_elements(
                        _paths(ctx), project_ref, step_id, result["import"]["imported"], client=_desktop()
                    )
            if prune_unused:
                result["prune"] = project_core.prune_unused_rpa_elements(
                    _paths(ctx), project_ref, keep_ids=result["import"]["imported"]
                )
        if session is not None:
            result["capture_session"] = session
        result["picker_cleanup"] = _stop_picker_after_capture(bridge, keep_open=keep_picker_open)
        _emit(ctx, result)
    except click.ClickException:
        if bridge is not None:
            try:
                _stop_picker_after_capture(bridge, keep_open=keep_picker_open)
            except Exception:
                pass
        raise
    except (FileNotFoundError, ShizaiCloudError, OSError, ValueError) as exc:
        if bridge is not None:
            try:
                _stop_picker_after_capture(bridge, keep_open=keep_picker_open)
            except Exception:
                pass
        _fail(str(exc))


@workflow.command("rpa-components")
@click.option(
    "--flow",
    "flow_ref",
    default=None,
    help="Client application directory returned by rpa-start-design.",
)
@click.option("--template", "source_project_ref", default=None, help="Captured native RPA template directory or id.")
@click.pass_context
def workflow_rpa_components(ctx: click.Context, flow_ref: str | None, source_project_ref: str | None) -> None:
    """List native RPA components and binding requirements."""
    try:
        _emit(ctx, project_core.rpa_components(_paths(ctx), flow_ref=flow_ref, source_project_ref=source_project_ref))
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-component-catalog")
@click.option(
    "--scope",
    type=click.Choice(["builtin", "business", "all"]),
    default="all",
    show_default=True,
    help="基础组件、业务扩展或全部能力目录。",
)
@click.option("--query", default="", help="Search type, label, description, category, or tag.")
@click.option("--category", default="", help="Exact designer component category.")
@click.option("--component-type", default="", help="Exact native component type.")
@click.option("--package", "package_name", default="", help="Package prefix such as WebBrowser, Excel, or OCR.")
@click.option(
    "--top-level", default="", help="Visible first-level menu id or label, for example webBrowser or 网页浏览器."
)
@click.option("--business-category", default="", help="Exact SaaS business category, for example 京东 or 飞书.")
@click.option("--business-package", default="", help="Exact SaaS business package name.")
@click.option(
    "--project", "project_ref", default=None, help="Optional client project used to report imported business packages."
)
@click.option("--page", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--limit", type=click.IntRange(min=1, max=200), default=20, show_default=True)
@click.option("--detail", type=click.Choice(["summary", "contract", "full"]), default="contract", show_default=True)
@click.option("--response-mode", type=click.Choice(["full", "compact"]), default="full", show_default=True)
@click.option("--include-hidden/--visible-only", default=True, show_default=True,
              help="Match the designer palette with --visible-only; exact component types remain inspectable.")
@click.option(
    "--engine-root",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Optional local rpa-engine directory.",
)
@click.option(
    "--business-info-path",
    default=None,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Optional bizComponentInfo.json path.",
)
@click.option(
    "--business-cache-root",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Optional child-module-packages/biz path.",
)
@click.pass_context
def workflow_rpa_component_catalog(
    ctx: click.Context,
    scope: str,
    query: str,
    category: str,
    component_type: str,
    package_name: str,
    top_level: str,
    business_category: str,
    business_package: str,
    project_ref: str | None,
    page: int,
    limit: int,
    detail: str,
    response_mode: str,
    include_hidden: bool,
    engine_root: Path | None,
    business_info_path: Path | None,
    business_cache_root: Path | None,
) -> None:
    """Search built-in designer components and SaaS business extensions."""
    try:
        _emit(
            ctx,
            component_catalog_core.component_catalog(
                _paths(ctx),
                scope=scope,
                query=query,
                category=category,
                component_type=component_type,
                package=package_name,
                top_level=top_level,
                business_category=business_category,
                business_package=business_package,
                project_ref=project_ref,
                page=page,
                limit=limit,
                detail=detail,
                response_mode=response_mode,
                include_hidden=include_hidden,
                engine_root=str(engine_root) if engine_root else None,
                business_info_path=str(business_info_path) if business_info_path else None,
                business_cache_root=str(business_cache_root) if business_cache_root else None,
            ),
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-add-step")
@click.option("--flow", "flow_ref", required=True, help="Client application directory or legacy staged flow reference.")
@click.option("--component", required=True)
@click.option("--step-id", default=None)
@click.option("--label", default=None)
@click.option("--element-id", "element_ids", multiple=True)
@click.option("--config-json", default=None, help="Component-specific config object as JSON.")
@click.option(
    "--allow-unbound-predecessors",
    is_flag=True,
    help="Recovery-only: append while an earlier element step is temporarily unbound; reorder and bind before runtime.",
)
@click.option("--allow-write", is_flag=True, help="Allow the client task or legacy staged flow to be updated.")
@click.pass_context
def workflow_rpa_add_step(
    ctx: click.Context,
    flow_ref: str,
    component: str,
    step_id: str | None,
    label: str | None,
    element_ids: tuple[str, ...],
    config_json: str | None,
    allow_unbound_predecessors: bool,
    allow_write: bool,
) -> None:
    """Append one native RPA component and return its incremental verification gate."""
    if not allow_write:
        _fail("workflow rpa-add-step requires --allow-write")
    try:
        config = json.loads(config_json) if config_json else None
        if config is not None and not isinstance(config, dict):
            raise ValueError("--config-json must decode to an object")
        _emit(
            ctx,
            project_core.add_rpa_step(
                _paths(ctx),
                flow_ref,
                component,
                step_id=step_id,
                label=label,
                element_ids=list(element_ids),
                config=config,
                client=_desktop(),
                allow_unbound_predecessors=allow_unbound_predecessors,
            ),
        )
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError, json.JSONDecodeError) as exc:
        _fail(str(exc))


@workflow.command("rpa-reorder-steps")
@click.option("--flow", "flow_ref", required=True)
@click.option(
    "--step-id",
    "step_ids",
    multiple=True,
    required=True,
    help="Complete desired execution order; repeat once for every existing step.",
)
@click.option("--allow-write", is_flag=True, help="Allow the native task and runtime artifacts to be updated.")
@click.pass_context
def workflow_rpa_reorder_steps(
    ctx: click.Context, flow_ref: str, step_ids: tuple[str, ...], allow_write: bool
) -> None:
    """Atomically reorder existing native steps and rebuild runtime artifacts."""
    if not allow_write:
        _fail("workflow rpa-reorder-steps requires --allow-write")
    try:
        _emit(
            ctx,
            project_core.reorder_native_rpa_steps(
                _paths(ctx), flow_ref, list(step_ids), client=_desktop()
            ),
        )
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-bind-elements")
@click.option("--flow", "flow_ref", required=True)
@click.option("--step-id", required=True)
@click.option("--element-id", "element_ids", multiple=True, required=True)
@click.option(
    "--as-page-selector",
    is_flag=True,
    help="Bind the first imported id as DataCollectionV5's pagination control instead of replacing column bindings.",
)
@click.option(
    "--pagination-mode",
    type=click.Choice(["next-page", "load-more"]),
    default="next-page",
    show_default=True,
    help="Pagination control type used with --as-page-selector.",
)
@click.option("--allow-write", is_flag=True, help="Allow the client task or legacy staged flow to be updated.")
@click.pass_context
def workflow_rpa_bind_elements(
    ctx: click.Context,
    flow_ref: str,
    step_id: str,
    element_ids: tuple[str, ...],
    as_page_selector: bool,
    pagination_mode: str,
    allow_write: bool,
) -> None:
    """Bind imported element ids to one staged RPA step."""
    if not allow_write:
        _fail("workflow rpa-bind-elements requires --allow-write")
    try:
        if as_page_selector:
            if len(element_ids) != 1:
                _fail("--as-page-selector requires exactly one imported element id")
            result = project_core.bind_rpa_pagination(
                _paths(ctx),
                flow_ref,
                step_id,
                element_ids[0],
                mode=pagination_mode,
                client=_desktop(),
            )
        else:
            result = project_core.set_rpa_step_elements(
                _paths(ctx), flow_ref, step_id, list(element_ids), client=_desktop()
            )
        _emit(ctx, result)
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-bind-pagination")
@click.option("--flow", "flow_ref", required=True)
@click.option("--step-id", required=True, help="Existing WebBrowser_DataCollectionV5 step.")
@click.option("--element-id", required=True, help="Imported next-page or load-more element id.")
@click.option(
    "--mode",
    type=click.Choice(["next-page", "load-more"]),
    default="next-page",
    show_default=True,
    help="Native pagination control type.",
)
@click.option(
    "--region",
    type=click.Choice(["0", "2", "3"]),
    default=None,
    help="DataCollectionV5 pagination region; omit to preserve the current mode.",
)
@click.option("--allow-write", is_flag=True, help="Allow the native task and metadata to be updated.")
@click.pass_context
def workflow_rpa_bind_pagination(
    ctx: click.Context,
    flow_ref: str,
    step_id: str,
    element_id: str,
    mode: str,
    region: str | None,
    allow_write: bool,
) -> None:
    """Bind one native pagination control without replacing collection columns."""
    if not allow_write:
        _fail("workflow rpa-bind-pagination requires --allow-write")
    try:
        _emit(
            ctx,
            project_core.bind_rpa_pagination(
                _paths(ctx),
                flow_ref,
                step_id,
                element_id,
                mode=mode,
                region=region,
                client=_desktop(),
            ),
        )
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-repair-flow")
@click.argument("flow_ref")
@click.option("--allow-write", is_flag=True, help="Repair duplicate native collection nodes and rebuild artifacts.")
@click.pass_context
def workflow_rpa_repair_flow(ctx: click.Context, flow_ref: str, allow_write: bool) -> None:
    """Repair a native flow with duplicate final collection nodes."""
    if not allow_write:
        _fail("workflow rpa-repair-flow requires --allow-write")
    try:
        _emit(
            ctx,
            project_core.repair_native_rpa_flow(
                _paths(ctx), flow_ref, client=_desktop()
            ),
        )
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-update-step")
@click.option("--flow", "flow_ref", required=True)
@click.option("--step-id", required=True)
@click.option("--config-json", required=True, help="Supported component parameter updates as a JSON object.")
@click.option("--dry-run", is_flag=True, help="Preview native parameter changes without writes or desktop control.")
@click.option("--expected-revision", default=None, help="Revision returned by rpa-read --detail edit.")
@click.option("--allow-write", is_flag=True, help="Allow the native task and metadata to be updated.")
@click.pass_context
def workflow_rpa_update_step(
    ctx: click.Context, flow_ref: str, step_id: str, config_json: str, allow_write: bool,
    dry_run: bool, expected_revision: str | None,
) -> None:
    """Update supported parameters on an existing native RPA component."""
    if not allow_write and not dry_run:
        _fail("workflow rpa-update-step requires --allow-write")
    try:
        config = json.loads(config_json)
        _emit(ctx, project_core.update_rpa_step_config(
            _paths(ctx), flow_ref, step_id, config, client=None if dry_run else _desktop(),
            dry_run=dry_run, expected_revision=expected_revision,
        ))
    except WorkflowEditError as exc:
        _emit(ctx, exc.result)
        ctx.exit(1)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(str(exc))


@workflow.command("rpa-reload-editor")
@click.argument("project_ref")
@click.option("--expected-revision", required=True, help="Revision returned by rpa-read --detail edit.")
@click.option("--ready-timeout", type=click.FloatRange(min=1, max=30), default=10, show_default=True)
@click.option("--allow-write", is_flag=True, help="Allow the active designer editor to reload.")
@click.pass_context
def workflow_rpa_reload_editor(
    ctx: click.Context, project_ref: str, expected_revision: str, ready_timeout: float, allow_write: bool
) -> None:
    """Reload the active designer editor after an MCP write."""
    if not allow_write:
        _fail("workflow rpa-reload-editor requires --allow-write")
    try:
        _emit(ctx, project_core.reload_rpa_editor(
            _paths(ctx), project_ref, expected_revision=expected_revision,
            client=_desktop(), ready_timeout=ready_timeout,
        ))
    except WorkflowEditError as exc:
        _emit(ctx, exc.result)
        ctx.exit(1)
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-run-component")
@click.argument("project_ref")
@click.argument("step_id")
@click.option("--expected-revision", required=True, help="Revision returned by rpa-read --detail edit.")
@click.option("--allow-write", is_flag=True, help="Allow one native component to execute in the designer.")
@click.pass_context
def workflow_rpa_run_component(
    ctx: click.Context, project_ref: str, step_id: str, expected_revision: str, allow_write: bool
) -> None:
    """Run exactly one component in the active designer."""
    if not allow_write:
        _fail("workflow rpa-run-component requires --allow-write")
    try:
        _emit(ctx, project_core.run_rpa_component(
            _paths(ctx), project_ref, step_id, expected_revision=expected_revision, client=_desktop(),
        ))
    except WorkflowEditError as exc:
        _emit(ctx, exc.result)
        ctx.exit(1)
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-save-editor")
@click.argument("project_ref")
@click.option("--expected-revision", required=True, help="Revision returned by rpa-read --detail edit.")
@click.option("--dry-run", is_flag=True, help="Read the designer's unsaved state without saving.")
@click.option("--allow-write", is_flag=True, help="Allow the active designer editor to save.")
@click.pass_context
def workflow_rpa_save_editor(
    ctx: click.Context, project_ref: str, expected_revision: str, dry_run: bool, allow_write: bool
) -> None:
    """Save the active designer editor and verify the persisted revision."""
    if not allow_write and not dry_run:
        _fail("workflow rpa-save-editor requires --allow-write")
    try:
        _emit(ctx, project_core.save_rpa_editor(
            _paths(ctx), project_ref, expected_revision=expected_revision,
            dry_run=dry_run, client=None if dry_run else _desktop(),
        ))
    except WorkflowEditError as exc:
        _emit(ctx, exc.result)
        ctx.exit(1)
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-remove-step")
@click.argument("project_ref")
@click.argument("step_id")
@click.option("--expected-revision", required=True, help="Revision returned by rpa-read --detail edit.")
@click.option("--allow-dependents", is_flag=True, help="Force removal even when a later step reads this step's output.")
@click.option("--dry-run", is_flag=True, help="Preview the removal without writes or desktop control.")
@click.option("--allow-write", is_flag=True, help="Allow the native task and runtime artifacts to be updated.")
@click.pass_context
def workflow_rpa_remove_step(
    ctx: click.Context,
    project_ref: str,
    step_id: str,
    expected_revision: str,
    allow_dependents: bool,
    dry_run: bool,
    allow_write: bool,
) -> None:
    """Delete one main-flow step and rebuild the native runtime artifacts."""
    if not allow_write and not dry_run:
        _fail("workflow rpa-remove-step requires --allow-write")
    try:
        _emit(ctx, project_core.remove_rpa_step(
            _paths(ctx), project_ref, step_id, expected_revision=expected_revision,
            client=None if dry_run else _desktop(), allow_dependents=allow_dependents, dry_run=dry_run,
        ))
    except WorkflowEditError as exc:
        _emit(ctx, exc.result)
        ctx.exit(1)
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-prune-elements")
@click.option("--flow", "flow_ref", required=True, help="Client application directory or legacy staged flow reference.")
@click.option("--keep-id", "keep_ids", multiple=True, help="Explicitly preserve an otherwise unreferenced element id.")
@click.option("--allow-write", is_flag=True, help="Allow the native element catalog to be updated.")
@click.pass_context
def workflow_rpa_prune_elements(
    ctx: click.Context, flow_ref: str, keep_ids: tuple[str, ...], allow_write: bool
) -> None:
    """Remove native element records that are not referenced by the flow."""
    if not allow_write:
        _fail("workflow rpa-prune-elements requires --allow-write")
    try:
        _emit(ctx, project_core.prune_unused_rpa_elements(_paths(ctx), flow_ref, keep_ids=list(keep_ids)))
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-read")
@click.argument("flow_ref")
@click.option("--detail", type=click.Choice(["full", "edit"]), default="full", show_default=True)
@click.option("--step-id", default=None, help="Expand one native node's parameters and contract with --detail edit.")
@click.pass_context
def workflow_rpa_read(ctx: click.Context, flow_ref: str, detail: str, step_id: str | None) -> None:
    """Read the ordered staged RPA component plan."""
    try:
        if detail == "edit":
            _emit(ctx, read_edit_context(_paths(ctx), flow_ref, step_id=step_id))
        elif step_id is not None:
            _fail("--step-id requires --detail edit")
        else:
            _emit(ctx, project_core.read_rpa_flow(_paths(ctx), flow_ref))
    except WorkflowEditError as exc:
        _emit(ctx, exc.result)
        ctx.exit(1)
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-validate-step")
@click.argument("flow_ref")
@click.argument("step_id")
@click.option("--strict", is_flag=True, help="Treat warnings as errors.")
@click.pass_context
def workflow_rpa_validate_step(ctx: click.Context, flow_ref: str, step_id: str, strict: bool) -> None:
    """Static-check one component; use the returned gate for design-time execution evidence."""
    try:
        result = project_core.validate_rpa_step(_paths(ctx), flow_ref, step_id, strict=strict)
        _emit(ctx, result)
        if not result["valid"]:
            raise click.exceptions.Exit(2)
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-assistant-run-step")
@click.argument("step_id")
@click.option(
    "--exec-status/--no-exec-status",
    default=True,
    show_default=True,
    help="Pass the step execution status expected by the designer assistant.",
)
@click.option("--allow-write", is_flag=True, help="Allow the designer RPA assistant session to execute one step.")
@click.pass_context
def workflow_rpa_assistant_run_step(ctx: click.Context, step_id: str, exec_status: bool, allow_write: bool) -> None:
    """Execute one step in the designer's stateful RPA assistant session."""
    if not allow_write:
        _fail("workflow rpa-assistant-run-step requires --allow-write")
    try:
        _emit(
            ctx,
            agent_core.run_rpa_assistant_step(
                step_id,
                exec_status=exec_status,
                client=_desktop(),
            ),
        )
    except (OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-debug-capabilities")
@click.argument("project_ref", required=False)
@click.pass_context
def workflow_rpa_debug_capabilities(ctx: click.Context, project_ref: str | None) -> None:
    """Probe native component-level single-step and Chrome-session reuse support."""
    try:
        _emit(ctx, debug_core.debug_capabilities(_paths(ctx), _workflow_project_ref(ctx, project_ref)))
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-debug-start")
@click.argument("project_ref", required=False)
@click.option("--start-from", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--start-timeout", type=click.FloatRange(min=0.1, max=180), default=30, show_default=True)
@click.option("--allow-write", is_flag=True, help="Start or take over the real native debug runtime.")
@click.pass_context
def workflow_rpa_debug_start(
    ctx: click.Context, project_ref: str | None, start_from: int, start_timeout: float, allow_write: bool
) -> None:
    """Start or reuse a native DAP session and stop at one component."""
    if not allow_write:
        _fail("workflow rpa-debug-start requires --allow-write")
    try:
        _emit(
            ctx,
            debug_core.debug_start(
                _paths(ctx),
                _workflow_project_ref(ctx, project_ref),
                start_from=start_from,
                start_timeout=start_timeout,
                client=_desktop(),
            ),
        )
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-debug-status")
@click.argument("project_ref", required=False)
@click.pass_context
def workflow_rpa_debug_status(ctx: click.Context, project_ref: str | None) -> None:
    """Read the current native debug session and paused component."""
    try:
        _emit(ctx, debug_core.debug_status(_paths(ctx), _workflow_project_ref(ctx, project_ref), client=_desktop()))
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


def _run_rpa_debug_command(
    ctx: click.Context, project_ref: str | None, operation: str, wait_seconds: float, allow_write: bool
) -> None:
    """Run one real native debug command through the persistent MCP session."""
    if not allow_write:
        _fail(f"workflow rpa-debug-{operation.lower()} requires --allow-write")
    try:
        resolved_ref = _workflow_project_ref(ctx, project_ref)
        result = debug_core.debug_command(
            _paths(ctx),
            resolved_ref,
            operation,
            wait_seconds=wait_seconds,
            client=_desktop(),
        )
        _emit(
            ctx,
            project_core.attach_rpa_debug_verification(_paths(ctx), resolved_ref, result),
        )
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-debug-next")
@click.argument("project_ref", required=False)
@click.option("--wait-seconds", type=click.FloatRange(min=0.1, max=120), default=15, show_default=True)
@click.option(
    "--allow-write", is_flag=True, help="Execute the current component and continue to the next component breakpoint."
)
@click.pass_context
def workflow_rpa_debug_next(
    ctx: click.Context, project_ref: str | None, wait_seconds: float, allow_write: bool
) -> None:
    """Execute the current component and stop at the next component breakpoint."""
    _run_rpa_debug_command(ctx, project_ref, "next", wait_seconds, allow_write)


@workflow.command("rpa-debug-continue")
@click.argument("project_ref", required=False)
@click.option("--wait-seconds", type=click.FloatRange(min=0.1, max=120), default=15, show_default=True)
@click.option("--allow-write", is_flag=True, help="Continue the real native debug runtime.")
@click.pass_context
def workflow_rpa_debug_continue(
    ctx: click.Context, project_ref: str | None, wait_seconds: float, allow_write: bool
) -> None:
    """Continue to the next stop or confirmed runtime completion."""
    _run_rpa_debug_command(ctx, project_ref, "continue", wait_seconds, allow_write)


@workflow.command("rpa-debug-step-in")
@click.argument("project_ref", required=False)
@click.option("--wait-seconds", type=click.FloatRange(min=0.1, max=120), default=15, show_default=True)
@click.option("--allow-write", is_flag=True, help="Send a DAP stepIn command.")
@click.pass_context
def workflow_rpa_debug_step_in(
    ctx: click.Context, project_ref: str | None, wait_seconds: float, allow_write: bool
) -> None:
    """Step into the current component implementation at DAP line level."""
    _run_rpa_debug_command(ctx, project_ref, "stepIn", wait_seconds, allow_write)


@workflow.command("rpa-debug-step-out")
@click.argument("project_ref", required=False)
@click.option("--wait-seconds", type=click.FloatRange(min=0.1, max=120), default=15, show_default=True)
@click.option("--allow-write", is_flag=True, help="Send a DAP stepOut command.")
@click.pass_context
def workflow_rpa_debug_step_out(
    ctx: click.Context, project_ref: str | None, wait_seconds: float, allow_write: bool
) -> None:
    """Step out of the current component implementation at DAP line level."""
    _run_rpa_debug_command(ctx, project_ref, "stepOut", wait_seconds, allow_write)


@workflow.command("rpa-debug-stop")
@click.argument("project_ref", required=False)
@click.option("--allow-write", is_flag=True, help="Disconnect the native debug session and stop an MCP-owned run.")
@click.pass_context
def workflow_rpa_debug_stop(ctx: click.Context, project_ref: str | None, allow_write: bool) -> None:
    """Disconnect and stop the current native debug session."""
    if not allow_write:
        _fail("workflow rpa-debug-stop requires --allow-write")
    try:
        _emit(ctx, debug_core.debug_stop(_paths(ctx), _workflow_project_ref(ctx, project_ref), client=_desktop()))
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


@workflow.command("rpa-validate-flow")
@click.argument("flow_ref")
@click.option("--strict", is_flag=True, help="Treat binding warnings as errors.")
@click.pass_context
def workflow_rpa_validate_flow(ctx: click.Context, flow_ref: str, strict: bool) -> None:
    """Validate staged component order before native generation."""
    try:
        result = project_core.validate_rpa_flow(_paths(ctx), flow_ref, strict=strict)
        _emit(ctx, result)
        if not result["valid"]:
            raise click.exceptions.Exit(2)
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-validate")
@click.argument("project_ref", required=False)
@click.option("--strict", is_flag=True, help="Treat selector warnings as errors.")
@click.pass_context
def workflow_rpa_validate(ctx: click.Context, project_ref: str | None, strict: bool) -> None:
    """Validate native elements and browser-open-first ordering in an RPA project."""
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    try:
        result = project_core.validate_rpa_project(_paths(ctx), ref, strict=strict)
        _emit(ctx, result)
        if not result["valid"]:
            raise click.exceptions.Exit(2)
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-sync-diagnose")
@click.argument("project_ref", required=False)
@click.pass_context
def workflow_rpa_sync_diagnose(ctx: click.Context, project_ref: str | None) -> None:
    """Diagnose native package IDs, index freshness, and runtime prerequisites."""
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    try:
        _emit(ctx, project_core.native_rpa_sync_diagnose(_paths(ctx), ref))
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-materialize")
@click.argument("project_ref", required=False)
@click.option("--strict/--no-strict", default=True, show_default=True, help="Treat selector warnings as errors.")
@click.option("--allow-write", is_flag=True, help="Generate or refresh the native runtime support tree.")
@click.pass_context
def workflow_rpa_materialize(ctx: click.Context, project_ref: str | None, strict: bool, allow_write: bool) -> None:
    """Generate the native projects tree and file index before runtime execution."""
    if not allow_write:
        _fail("workflow rpa-materialize requires --allow-write")
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    try:
        _emit(ctx, project_core.materialize_native_rpa(_paths(ctx), ref, strict=strict, client=_desktop()))
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-run")
@click.argument("project_ref", required=False)
@click.option("--input-param-json", default=None, help="Native workflow inputParam object encoded as JSON.")
@click.option("--run-uuid", default=None, help="Optional stable debug UUID for a retry or log lookup.")
@click.option("--workflow-run-id", default=None, help="Optional workflow run id for a retry or log lookup.")
@click.option("--start-take-work/--no-start-take-work", default=False, show_default=True)
@click.option(
    "--wait/--no-wait",
    default=True,
    show_default=True,
    help="Keep the designer channel open until the native run reaches a terminal state.",
)
@click.option(
    "--wait-seconds",
    type=click.FloatRange(min=1.0, max=3600.0),
    default=900.0,
    show_default=True,
    help="Maximum time to service the native run channel.",
)
@click.option("--allow-write", is_flag=True, help="Allow the designer to start the native RPA debug run.")
@click.pass_context
def workflow_rpa_run(
    ctx: click.Context,
    project_ref: str | None,
    input_param_json: str | None,
    run_uuid: str | None,
    workflow_run_id: str | None,
    start_take_work: bool,
    wait: bool,
    wait_seconds: float,
    allow_write: bool,
) -> None:
    """Run a validated client-owned native RPA flow through the designer."""
    if not allow_write:
        _fail("workflow rpa-run requires --allow-write")
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    input_param = None
    if input_param_json:
        try:
            input_param = json.loads(input_param_json)
        except json.JSONDecodeError as exc:
            _fail(f"--input-param-json must be valid JSON: {exc}")
        if not isinstance(input_param, dict):
            _fail("--input-param-json must decode to an object")
    desktop = _desktop()
    stale_ui_cleanup = _cleanup_native_ui(desktop)
    result: Any = None
    run_error: Exception | None = None
    try:
        result = project_core.native_rpa_run(
            _paths(ctx),
            ref,
            input_param=input_param,
            run_uuid=run_uuid,
            workflow_run_id=workflow_run_id,
            start_take_work=start_take_work,
            wait=wait,
            wait_seconds=wait_seconds,
            client=desktop,
        )
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        run_error = exc
    finally:
        # A waiting CLI run owns the terminal transition.  Always close the
        # picker and native UI after that transition, including run errors.
        waiting_for_user = isinstance(result, dict) and (
            bool(result.get("interaction_required"))
            or result.get("status") == "waiting_for_user"
            or (
                isinstance(result.get("status"), dict)
                and result["status"].get("status") == "waiting_for_user"
            )
        )
        final_ui_cleanup = _cleanup_native_ui(desktop) if wait and not waiting_for_user else {
            "status": "deferred",
            "reason": (
                "waiting_for_user; preserve browser for user interaction"
                if waiting_for_user
                else "wait=false; native run may still be active"
            ),
        }
    if run_error is not None:
        _fail(str(run_error))
    if isinstance(result, dict):
        native_ui = result.setdefault("native_ui", {})
        if isinstance(native_ui, dict):
            native_ui["stale_ui_cleanup"] = stale_ui_cleanup
            native_ui["final_cleanup"] = final_ui_cleanup
    _emit(ctx, result)


@workflow.command("rpa-status")
@click.argument("project_ref", required=False)
@click.pass_context
def workflow_rpa_status(ctx: click.Context, project_ref: str | None) -> None:
    """Read local native RPA run status, logs, and output evidence."""
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    try:
        _emit(ctx, project_core.native_rpa_status(_paths(ctx), ref))
    except (FileNotFoundError, OSError, ValueError) as exc:
        _fail(str(exc))


@workflow.command("rpa-stop")
@click.argument("project_ref", required=False)
@click.option("--allow-write", is_flag=True, help="Allow the designer to stop the native RPA debug run.")
@click.pass_context
def workflow_rpa_stop(ctx: click.Context, project_ref: str | None, allow_write: bool) -> None:
    """Stop the current native RPA run and report cleanup confirmation."""
    if not allow_write:
        _fail("workflow rpa-stop requires --allow-write")
    ref = project_ref or _ctx(ctx).get("project_ref")
    if not ref:
        _fail("PROJECT_REF is required, or pass --project PROJECT_REF")
    try:
        _emit(ctx, project_core.native_rpa_stop(_paths(ctx), ref, client=_desktop()))
    except (FileNotFoundError, OSError, ValueError, ShizaiCloudError) as exc:
        _fail(str(exc))


main = cli


if __name__ == "__main__":
    main()
