"""Cloud knowledge-base and desktop Agent clients for Shizai Intelligent.

The REST client handles document management.  The desktop WebSocket client uses
the already authenticated business channel exposed by the running application,
so chat and Agent inspection do not require copying a session token to disk.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunsplit

import requests

from .paths import ShizaiPaths

try:
    import websocket
except ImportError:  # pragma: no cover - exercised only in a missing dependency environment
    websocket = None  # type: ignore[assignment]


DEFAULT_API_BASE = "https://deop.ai-indeed.com/frameWorkPortal"
# Personal workspace identifiers must be supplied at runtime, never shipped in source.
DEFAULT_DATASET_ID = ""
DEFAULT_AGENT_ID = ""
DEFAULT_WS_URL = "ws://127.0.0.1:23408/?clientId=shizai-agent&browserWindowId=4"
SHIZAI_DOWNLOAD_URL = "https://www.ai-indeed.com/"


def _new_process_client_id() -> str:
    """Return one stable desktop-channel id for this CLI/MCP process."""
    return f"shizai-agent-mcp-{os.getpid()}-{uuid.uuid4().hex[:12]}"


_PROCESS_CLIENT_ID = _new_process_client_id()


def _replace_ws_client_id(url: str, client_id: str) -> str:
    """Set the local business-channel clientId without changing other params."""
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "clientId"]
    query.insert(0, ("clientId", client_id))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


INGESTION_FILE_TYPES = [
    {
        "name": "documents",
        "extensions": [
            "pdf",
            "doc",
            "docx",
            "xls",
            "xlsx",
            "ppt",
            "pptx",
            "txt",
            "md",
            "mdx",
            "markdown",
            "csv",
            "html",
            "htm",
            "xml",
            "json",
            "epub",
            "msg",
        ],
    },
    {"name": "images", "extensions": ["jpg", "jpeg", "png", "webp"]},
]
KNOWLEDGE_QA_PROMPT = """你是“AI知识库助手”，必须优先依据“知识检索”节点提供的召回内容回答用户。

请遵守以下规则：
1. 当召回内容非空时，只使用其中与问题相关的信息作答。不要说用户没有提供文章，不要说无法查看知识库，也不要要求用户粘贴已经召回的内容。
2. 回答要直接、准确、简洁。用户询问命令时，直接给出召回内容中的命令，并补充文章中的作用说明。
3. 用户要求总结、对比或步骤时，基于召回内容整理答案。
4. 召回内容不足以回答时，明确说明“知识库中没有找到足够依据”，不要编造信息。
5. 只有在召回内容为空时，才说明知识库没有命中相关内容。
6. 查询具体型号时，必须先在召回内容中找到完全一致的型号名称（包括大小写、连字符、后缀和 VL/Coder 等变体）。只可使用该型号对应的条目，不得用同系列或相近型号的参数补全；完全一致的型号未出现时，明确说明没有找到该型号的足够依据。

召回内容：
{{#context#}}

用户问题：
{{#sys.query#}}"""


def _normalize_model_tables(markdown: str) -> str:
    """Turn model tables into one self-contained Markdown entry per model.

    Retrieval systems can split a wide table between rows and lose the model
    name from the chunk containing its specifications.  Keeping each row with
    its headers makes a model lookup stable without changing the source facts.
    """
    output: list[str] = []
    headers: list[str] | None = None
    in_model_table = False

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("|") and line.endswith("|"):
            cells = [item.strip() for item in line.strip("|").split("|")]
            if cells and cells[0].lower() in {"模型", "model"} and len(cells) >= 2:
                headers = cells
                in_model_table = True
                output.append("### 模型支持条目")
                continue
            if in_model_table and cells and all(re.fullmatch(r":?-+:?", item or "-") for item in cells):
                continue
            if in_model_table and cells and cells[0] and cells[0].lower() not in {"模型", "model"}:
                output.append(f"### {cells[0]}")
                for header, value in zip(headers[1:] if headers else [], cells[1:]):
                    if value and not re.fullmatch(r":?-+:?", value):
                        output.append(f"- {header}: {value}")
                output.append("")
                continue
        output.append(raw_line)

    return "\n".join(output).strip() + "\n"


def _extract_http_urls(text: str) -> list[str]:
    """Return de-duplicated HTTP(S) URLs from a user message."""
    candidates = re.findall(r"https?://[^\s<>\"'\u3001\u3002\uff0c\uff1b]+", text or "")
    return list(dict.fromkeys(item.rstrip(".,);]}") for item in candidates))


def _looks_like_expired_token(detail: str) -> bool:
    """Identify the console responses that mean the desktop session token is stale."""
    normalized = detail.lower()
    markers = (
        "token",
        "令牌",
        "用户信息",
        "用户信息",
        "expired",
        "过期",
        "无效",
    )
    return any(marker in normalized for marker in markers)


def _rewind_uploads(files: Any) -> None:
    """Rewind multipart file handles before retrying an upload request."""
    if not isinstance(files, dict):
        return
    for value in files.values():
        handle = value[1] if isinstance(value, tuple) and len(value) > 1 else value
        seek = getattr(handle, "seek", None)
        if seek is not None:
            seek(0)


@contextmanager
def _desktop_channel_lock(timeout: float, operation: str):
    """Serialize all desktop-channel operations across CLI processes."""
    lock_path = Path(tempfile.gettempdir()) / "shizai-agent.channel.lock"
    handle = lock_path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + max(0.0, timeout)
    locked = False
    try:
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - exercised on Unix CI only
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise ShizaiCloudError(
                        f"Another Shizai desktop {operation} is still running; try again after it finishes"
                    )
                time.sleep(0.1)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on Unix CI only
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _builtin_tool_node(
    node_id: str,
    *,
    title: str,
    provider_id: str,
    tool_name: str,
    tool_label: str,
    tool_parameters: dict[str, Any],
    output_parameters: list[dict[str, Any]],
    position: tuple[int, int],
) -> dict[str, Any]:
    """Build the workflow node shape used by the installed Agent editor."""
    return {
        "id": node_id,
        "type": "custom",
        "data": {
            "type": "tool",
            "title": title,
            "desc": "",
            "tool_parameters": tool_parameters,
            "tool_configurations": {},
            "provider_id": provider_id,
            "provider_type": "builtin",
            "provider_name": provider_id,
            "tool_name": tool_name,
            "tool_label": tool_label,
            "output_parameters": output_parameters,
            "selected": False,
        },
        "position": {"x": position[0], "y": position[1]},
        "targetPosition": "left",
        "sourcePosition": "right",
        "selected": False,
    }


class ShizaiCloudError(RuntimeError):
    """Raised when a Shizai cloud or desktop request fails."""


def _auto_start_enabled() -> bool:
    """Allow unattended clients to opt out of opening the desktop app."""
    return os.environ.get("SHIZAI_AGENT_AUTO_START", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _desktop_start_error(detail: str, *, executable: Path | None = None) -> ShizaiCloudError:
    """Build an actionable connection error without exposing session data."""
    if executable is None:
        return ShizaiCloudError(
            "未检测到正在运行的实在智能设计器，也没有找到本机安装程序。"
            f"请先下载安装并登录：{SHIZAI_DOWNLOAD_URL} "
            "安装完成后重新执行当前操作。"
        )
    return ShizaiCloudError(f"实在智能设计器已尝试启动但业务通道仍不可用：{detail}。请等待设计器打开并完成登录后重试。")


class _WebpageMarkdownParser(HTMLParser):
    """Extract a readable Markdown section from a normal HTML document."""

    _HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    _BLOCKS = {"p", "li", "blockquote", "pre"}
    _SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header"}

    def __init__(self, fragment: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.fragment = unquote(fragment or "").strip().lower() or None
        self.active = self.fragment is None
        self.section_level: int | None = None
        self.heading_attrs: dict[str, str] = {}
        self.skip_depth = 0
        self.text_buffer: list[str] = []
        self.heading_buffer: list[str] = []
        self.row_cells: list[str] = []
        self.cell_buffer: list[str] = []
        self.in_cell = False
        self.row_is_header = False
        self.lines: list[str] = []

    @staticmethod
    def _clean(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    def _append_line(self, value: str) -> None:
        value = self._clean(value)
        if value and (not self.lines or self.lines[-1] != value):
            self.lines.append(value)

    def _heading_matches(self, attrs: dict[str, str], text: str) -> bool:
        if not self.fragment:
            return False
        anchor = attrs.get("id", "").strip().lower()
        return anchor == self.fragment or self.fragment in text.lower()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag in self._SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self._HEADINGS:
            self.heading_buffer = []
            self.heading_attrs = attrs_dict
            return
        if not self.active:
            return
        if tag == "tr":
            self.row_cells = []
            self.row_is_header = False
        elif tag in {"th", "td"}:
            self.in_cell = True
            self.cell_buffer = []
            self.row_is_header = self.row_is_header or tag == "th"
        elif tag in self._BLOCKS:
            self.text_buffer = []
        elif tag == "br":
            self._append_line(" ".join(self.text_buffer))
            self.text_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in self._HEADINGS:
            heading = self._clean("".join(self.heading_buffer))
            level = int(tag[1])
            if not self.active and self._heading_matches(self.heading_attrs, heading):
                self.active = True
                self.section_level = level
                self._append_line(f"## {heading}")
            elif self.active and self.section_level is not None and level <= self.section_level:
                self.active = False
            elif self.active:
                self._append_line(f"{'#' * min(6, level)} {heading}")
            self.heading_buffer = []
            self.heading_attrs = {}
            return
        if not self.active:
            return
        if tag in {"th", "td"} and self.in_cell:
            self.row_cells.append(self._clean("".join(self.cell_buffer)))
            self.cell_buffer = []
            self.in_cell = False
        elif tag == "tr" and self.row_cells:
            self._append_line("| " + " | ".join(self.row_cells) + " |")
            if self.row_is_header:
                self._append_line("| " + " | ".join("---" for _ in self.row_cells) + " |")
            self.row_cells = []
        elif tag in self._BLOCKS:
            self._append_line(" ".join(self.text_buffer))
            self.text_buffer = []

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if not self.active:
            self.heading_buffer.append(data)
            return
        if self.in_cell:
            self.cell_buffer.append(data)
        else:
            self.text_buffer.append(data)
            self.heading_buffer.append(data)

    def markdown(self) -> str:
        raw = "\n\n".join(line for line in self.lines if line.strip())
        return _normalize_model_tables(raw)


def _download_webpage_html(url: str) -> str:
    """Fetch HTML with a requests-first, curl-backed WAF fallback."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    errors: list[str] = []
    parsed = urlparse(url)
    candidates = [url]
    if parsed.query:
        canonical = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))
        if canonical not in candidates:
            candidates.append(canonical)

    for candidate in candidates:
        try:
            response = requests.get(candidate, headers=headers, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            errors.append(f"requests {candidate}: {exc}")

    curl = shutil.which("curl") or shutil.which("curl.exe")
    if curl:
        for candidate in candidates:
            try:
                completed = subprocess.run(
                    [
                        curl,
                        "--location",
                        "--compressed",
                        "--silent",
                        "--show-error",
                        "--max-time",
                        "45",
                        "--user-agent",
                        headers["User-Agent"],
                        "--header",
                        f"Accept: {headers['Accept']}",
                        "--write-out",
                        "\n__SHIZAI_HTTP_STATUS__:%{http_code}",
                        candidate,
                    ],
                    capture_output=True,
                    timeout=55,
                    check=False,
                )
                output = completed.stdout
                marker = b"\n__SHIZAI_HTTP_STATUS__:"
                body, _, status_bytes = output.rpartition(marker)
                status = int(status_bytes.strip() or b"0")
                if completed.returncode == 0 and 200 <= status < 400 and body:
                    return body.decode("utf-8", errors="replace")
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                errors.append(f"curl {candidate}: HTTP {status}" + (f" ({detail})" if detail else ""))
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                errors.append(f"curl {candidate}: {exc}")

    detail = "; ".join(errors[-4:])
    raise ShizaiCloudError(f"Webpage download failed for {url}: {detail}")


def _download_webpage_markdown(url: str) -> str:
    """Download a URL and keep its fragment section when one is supplied."""
    try:
        html = _download_webpage_html(url)
    except ShizaiCloudError:
        raise
    parser = _WebpageMarkdownParser(urlparse(url).fragment)
    parser.feed(html)
    content = parser.markdown()
    if not content:
        raise ShizaiCloudError(f"Webpage download returned no readable content: {url}")
    return f"# Imported webpage\n\nSource: {url}\n\n{content}\n"


@dataclass(frozen=True)
class ShizaiCloudConfig:
    """Environment-backed configuration for cloud and desktop operations."""

    api_base: str = DEFAULT_API_BASE
    token: str | None = None
    dataset_id: str = DEFAULT_DATASET_ID
    agent_id: str = DEFAULT_AGENT_ID
    ws_url: str = DEFAULT_WS_URL
    client_id: str = field(default_factory=lambda: _PROCESS_CLIENT_ID)
    timeout: float = 30.0

    @classmethod
    def from_environment(cls) -> "ShizaiCloudConfig":
        """Resolve configuration without writing or reading authentication state."""
        client_id = os.environ.get("SHIZAI_AGENT_CLIENT_ID") or _PROCESS_CLIENT_ID
        configured_ws_url = os.environ.get("SHIZAI_AGENT_WS_URL")
        return cls(
            api_base=os.environ.get("SHIZAI_AGENT_API_BASE", DEFAULT_API_BASE).rstrip("/"),
            token=os.environ.get("SHIZAI_AGENT_TOKEN") or None,
            dataset_id=os.environ.get("SHIZAI_DATASET_ID", DEFAULT_DATASET_ID),
            agent_id=os.environ.get("SHIZAI_AGENT_ID", DEFAULT_AGENT_ID),
            ws_url=(configured_ws_url or _replace_ws_client_id(DEFAULT_WS_URL, client_id)),
            client_id=client_id,
            timeout=float(os.environ.get("SHIZAI_AGENT_TIMEOUT", "30")),
        )


class ShizaiCloudApi:
    """Small REST wrapper for the Shizai Agent console API."""

    def __init__(
        self,
        config: ShizaiCloudConfig | None = None,
        session: Any = None,
        token_refresher: Callable[[], str] | None = None,
    ) -> None:
        self.config = config or ShizaiCloudConfig.from_environment()
        self.session = session or requests.Session()
        self.token_refresher = token_refresher

    def _require_token(self) -> str:
        if not self.config.token:
            raise ShizaiCloudError(
                "SHIZAI_AGENT_TOKEN is required for cloud knowledge-base operations. "
                "The token is read from the environment and never stored by this CLI."
            )
        return self.config.token

    @classmethod
    def from_logged_in_desktop(
        cls,
        config: ShizaiCloudConfig | None = None,
        *,
        desktop: "ShizaiDesktopAgent" | None = None,
        session: Any = None,
    ) -> "ShizaiCloudApi":
        """Create a REST client using a short-lived token from the logged-in app."""
        current = config or ShizaiCloudConfig.from_environment()
        if current.token:
            return cls(current, session=session)
        bridge = desktop or ShizaiDesktopAgent(current)
        token = bridge.exchange_token()
        return cls(
            replace(current, token=token),
            session=session,
            token_refresher=bridge.exchange_token,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
        data: Any = None,
    ) -> Any:
        """Perform an authenticated API request and normalize API errors."""
        url = f"{self.config.api_base}{path}"
        for attempt in range(2):
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._require_token()}",
            }
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            response = self.session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                files=files,
                data=data,
                timeout=self.config.timeout,
            )
            if response.status_code >= 400:
                detail = response.text[:2000]
                if attempt == 0 and self.token_refresher and _looks_like_expired_token(detail):
                    self.config = replace(self.config, token=self.token_refresher())
                    _rewind_uploads(files)
                    continue
                raise ShizaiCloudError(f"{method} {path} failed ({response.status_code}): {detail}")
            if response.status_code == 204 or not response.text:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise ShizaiCloudError(f"{method} {path} returned non-JSON data") from exc
        raise ShizaiCloudError(f"{method} {path} failed after refreshing the desktop session")

    def dataset_info(self, dataset_id: str | None = None) -> dict[str, Any]:
        """Return knowledge-base metadata."""
        return self._request("GET", f"/agent/console/api/datasets/{dataset_id or self.config.dataset_id}")

    def list_documents(self, dataset_id: str | None = None, page: int = 1, limit: int = 20) -> dict[str, Any]:
        """List documents in a knowledge base."""
        return self._request(
            "GET",
            f"/agent/console/api/datasets/{dataset_id or self.config.dataset_id}/documents",
            params={"page": page, "limit": limit},
        )

    def upload_file(self, file_path: str | Path) -> dict[str, Any]:
        """Upload a local file and return its cloud file metadata."""
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        with path.open("rb") as handle:
            return self._request(
                "POST",
                "/agent/console/api/files/upload",
                params={"source": "datasets"},
                files={"file": (path.name, handle, "application/octet-stream")},
            )

    def add_file(
        self,
        file_path: str | Path,
        *,
        dataset_id: str | None = None,
        process_mode: str = "automatic",
    ) -> dict[str, Any]:
        """Upload and enqueue a local file for knowledge-base indexing."""
        uploaded = self.upload_file(file_path)
        file_id = _extract_file_id(uploaded)
        body = {
            "indexing_technique": "high_quality",
            "data_source": {
                "type": "upload_file",
                "info_list": {
                    "data_source_type": "upload_file",
                    "file_info_list": {"file_ids": [file_id]},
                },
            },
            "process_rule": {"mode": process_mode},
        }
        result = self._request(
            "POST",
            f"/agent/console/api/datasets/{dataset_id or self.config.dataset_id}/documents",
            json_body=body,
        )
        return {"upload": uploaded, "document": result, "document_ids": _extract_document_ids(result)}

    def add_url(
        self,
        url: str,
        *,
        dataset_id: str | None = None,
        process_mode: str = "automatic",
    ) -> dict[str, Any]:
        """Enqueue a webpage for crawling and knowledge-base indexing."""
        if not url.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")

        # Current Shizai/Dify builds expect website_crawl_info_list to be a
        # list of source records. Older builds accepted a single object with
        # a ``urls`` array, so retain that shape as a compatibility fallback.
        body = {
            "indexing_technique": "high_quality",
            "data_source": {
                "type": "website_crawl",
                "info_list": {
                    "data_source_type": "website_crawl",
                    "website_crawl_info_list": [
                        {
                            "url": url,
                            "job_id": "",
                            "only_main_content": True,
                        }
                    ],
                },
            },
            "process_rule": {"mode": process_mode},
        }
        try:
            result = self._request(
                "POST",
                f"/agent/console/api/datasets/{dataset_id or self.config.dataset_id}/documents",
                json_body=body,
            )
        except ShizaiCloudError as exc:
            if "Website source info is required" not in str(exc):
                raise
            # Older console builds use one object with a ``urls`` array.
            body["data_source"]["info_list"]["website_crawl_info_list"] = {
                "urls": [url],
                "job_id": "",
                "only_main_content": True,
            }
            try:
                result = self._request(
                    "POST",
                    f"/agent/console/api/datasets/{dataset_id or self.config.dataset_id}/documents",
                    json_body=body,
                )
            except ShizaiCloudError as legacy_exc:
                # Some current console versions expose webpage import in the
                # UI but reject the legacy REST source schema. Download the
                # requested section and send it through the stable file path.
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        suffix=".md",
                        prefix="shizai-web-",
                        encoding="utf-8",
                        delete=False,
                    ) as handle:
                        temporary_path = Path(handle.name)
                        handle.write(_download_webpage_markdown(url))
                    uploaded = self.add_file(
                        temporary_path,
                        dataset_id=dataset_id,
                        process_mode=process_mode,
                    )
                except (OSError, ShizaiCloudError) as fallback_exc:
                    raise ShizaiCloudError(
                        f"URL import rejected by the current console ({legacy_exc}); "
                        f"download/upload fallback failed ({fallback_exc})"
                    ) from fallback_exc
                finally:
                    if "temporary_path" in locals():
                        temporary_path.unlink(missing_ok=True)
                return {
                    "url": url,
                    "fallback": "download_upload",
                    "document": uploaded["document"],
                    "document_ids": uploaded["document_ids"],
                }
        return {"url": url, "document": result, "document_ids": _extract_document_ids(result)}

    def wait_for_documents(
        self,
        document_ids: list[str] | tuple[str, ...],
        *,
        dataset_id: str | None = None,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> dict[str, Any]:
        """Wait until document ids are indexed and available in the dataset."""
        ids = [str(item) for item in document_ids if str(item).strip()]
        if not ids:
            raise ValueError("At least one DOCUMENT_ID is required")
        wait_timeout = self.config.timeout if timeout is None else max(0.0, timeout)
        poll_interval = 2.0 if interval is None else max(0.0, interval)
        deadline = time.monotonic() + wait_timeout
        latest: list[dict[str, Any]] = []
        terminal_failed = {"error", "failed", "failure"}

        while True:
            payload = self.list_documents(dataset_id, page=1, limit=100)
            records = _document_records(payload)
            by_id = {str(record.get("id")): record for record in records if record.get("id")}
            latest = [_document_summary(by_id.get(item, {"id": item})) for item in ids]
            failed = [item for item in latest if item.get("error") or item.get("indexing_status") in terminal_failed]
            ready = [
                item
                for item in latest
                if item.get("indexing_status") in {"completed", "available"}
                and item.get("display_status") in {"available", "completed", None}
            ]
            if failed:
                return {
                    "ready": False,
                    "status": "failed",
                    "document_ids": ids,
                    "documents": latest,
                    "failed": failed,
                }
            if len(ready) == len(ids):
                return {
                    "ready": True,
                    "status": "available",
                    "document_ids": ids,
                    "documents": latest,
                }
            if time.monotonic() >= deadline:
                return {
                    "ready": False,
                    "status": "timed_out",
                    "document_ids": ids,
                    "documents": latest,
                }
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    def delete_document(self, document_id: str, dataset_id: str | None = None) -> dict[str, Any]:
        """Delete a document from a knowledge base."""
        self._request(
            "DELETE",
            f"/agent/console/api/datasets/{dataset_id or self.config.dataset_id}/documents/{document_id}",
        )
        return {"deleted": True, "document_id": document_id}

    def app_info(self, agent_id: str | None = None) -> dict[str, Any]:
        """Return cloud Agent metadata."""
        return self._request("GET", f"/agent/console/api/apps/{agent_id or self.config.agent_id}")

    def list_apps(self, *, page: int = 1, limit: int = 20, mode: str | None = None) -> dict[str, Any]:
        """List cloud Agents visible to the current account."""
        params: dict[str, Any] = {"page": page, "limit": limit}
        if mode:
            params["mode"] = mode
        return self._request("GET", "/agent/console/api/apps", params=params)


def _extract_file_id(payload: Any) -> str:
    """Extract the file id returned by different console versions."""
    candidates = [payload]
    if isinstance(payload, dict):
        candidates.extend(payload.get(key) for key in ("data", "file", "file_info"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("id", "file_id", "upload_file_id", "fileId", "hash"):
                value = candidate.get(key)
                if value:
                    return str(value)
    raise ShizaiCloudError("File upload response did not contain a file id")


def _extract_document_ids(payload: Any) -> list[str]:
    """Extract document ids from the console's ingestion response shapes."""
    if not isinstance(payload, dict):
        return []
    records = _document_records(payload)
    return [str(record["id"]) for record in records if record.get("id")]


def _document_records(payload: Any) -> list[dict[str, Any]]:
    """Normalize document list/create responses to a list of records."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("documents", "data", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _document_records(value)
            if nested:
                return nested
    if payload.get("id") and any(
        key in payload for key in ("name", "indexing_status", "display_status", "data_source_type")
    ):
        return [payload]
    return []


def _document_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Return stable status fields for machine-readable wait results."""
    return {
        "id": str(record.get("id", "")),
        "name": record.get("name"),
        "indexing_status": record.get("indexing_status"),
        "display_status": record.get("display_status"),
        "enabled": record.get("enabled"),
        "error": record.get("error"),
    }


def _decode_json_value(value: Any) -> Any:
    """Decode a JSON string while preserving already-decoded protocol values."""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _extract_push_event(args: Any) -> dict[str, Any] | None:
    """Normalize push-service arguments from different desktop app versions."""
    if not isinstance(args, list) or not args:
        return None
    envelope = _decode_json_value(args[0])
    if not isinstance(envelope, dict):
        return None
    message = _decode_json_value(envelope.get("message", envelope))
    if not isinstance(message, dict):
        return None
    event = _decode_json_value(message.get("data", message))
    return event if isinstance(event, dict) and event.get("event") else None


def _extract_retriever_sources(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return compact, de-duplicated source records from retrieval events."""
    sources: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        candidates: Any = None
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            candidates = metadata.get("retriever_resources")
        if not isinstance(candidates, list):
            data = event.get("data")
            outputs = data.get("outputs") if isinstance(data, dict) else None
            if isinstance(outputs, dict) and isinstance(outputs.get("result"), list):
                candidates = outputs["result"]
        if not isinstance(candidates, list):
            continue
        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else item
            document_id = str(item_metadata.get("document_id") or "")
            segment_id = str(item_metadata.get("segment_id") or "")
            key = (document_id, segment_id or str(item_metadata.get("position") or len(sources)))
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                {
                    "position": item_metadata.get("position"),
                    "document_id": document_id or None,
                    "document_name": item_metadata.get("document_name") or item.get("title"),
                    "segment_id": segment_id or None,
                    "score": item_metadata.get("score"),
                    "content": item.get("content") or item_metadata.get("content") or "",
                }
            )
    return sources


def _desktop_result_data(result: Any) -> Any:
    """Unwrap the common desktop RPC result envelope."""
    if not isinstance(result, dict):
        return result
    data = result.get("data")
    return data if isinstance(data, (dict, list)) else result


def _native_user_id(account: Any) -> str:
    """Extract the logged-in account id used by the native IM settings map."""
    data = _desktop_result_data(account)
    if not isinstance(data, dict):
        raise ShizaiCloudError("Logged-in desktop account information was not returned")
    user_id = data.get("userId") or data.get("user_id") or data.get("id")
    if not user_id:
        raise ShizaiCloudError("Logged-in desktop account did not contain a user id")
    return str(user_id)


def merge_native_feishu_config(
    origin_config: dict[str, Any],
    *,
    user_id: str,
    client_id: str,
    client_secret: str,
    checked: bool = True,
    active_mode: bool = True,
) -> dict[str, Any]:
    """Merge one user's native Feishu settings without dropping other IM data."""
    if not client_id.strip() or not client_secret.strip():
        raise ValueError("client_id and client_secret are required")
    updated = deepcopy(origin_config)
    tars_im = updated.get("tarsIM")
    if not isinstance(tars_im, dict):
        tars_im = {}
    user_config = tars_im.get(str(user_id))
    if not isinstance(user_config, dict):
        user_config = {}
    feishu_config = user_config.get("feishu")
    if not isinstance(feishu_config, dict):
        feishu_config = {}
    feishu_config.update({"clientId": client_id.strip(), "clientSecret": client_secret.strip()})
    user_config.update(
        {
            "checked": bool(checked),
            "activeMode": bool(active_mode),
            "feishu": feishu_config,
        }
    )
    tars_im[str(user_id)] = user_config
    updated["tarsIM"] = tars_im
    return updated


def remove_native_feishu_config(origin_config: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    """Remove only one user's native Feishu entry and preserve other IM providers."""
    updated = deepcopy(origin_config)
    tars_im = updated.get("tarsIM")
    if isinstance(tars_im, dict):
        user_config = tars_im.get(str(user_id))
        if isinstance(user_config, dict):
            user_config.pop("feishu", None)
            provider_keys = set(user_config) - {"checked", "activeMode"}
            if not provider_keys:
                tars_im.pop(str(user_id), None)
        if tars_im:
            updated["tarsIM"] = tars_im
        else:
            updated.pop("tarsIM", None)

    session_map = updated.get("tarsIMSessionMap")
    if isinstance(session_map, dict):
        session_config = session_map.get(str(user_id))
        if isinstance(session_config, dict):
            for key in list(session_config):
                if str(key).startswith("feishu_"):
                    session_config.pop(key, None)
            if not session_config:
                session_map.pop(str(user_id), None)
        if session_map:
            updated["tarsIMSessionMap"] = session_map
        else:
            updated.pop("tarsIMSessionMap", None)
    return updated


def native_feishu_summary(origin_config: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Return a secret-free summary of one user's native Feishu configuration."""
    tars_im = origin_config.get("tarsIM") if isinstance(origin_config, dict) else None
    user_config = tars_im.get(str(user_id)) if isinstance(tars_im, dict) else None
    user_config = user_config if isinstance(user_config, dict) else {}
    feishu_config = user_config.get("feishu")
    feishu_config = feishu_config if isinstance(feishu_config, dict) else {}
    return {
        "user_id": str(user_id),
        "configured": bool(feishu_config.get("clientId") and feishu_config.get("clientSecret")),
        "client_id_present": bool(feishu_config.get("clientId")),
        "client_secret_present": bool(feishu_config.get("clientSecret")),
        "checked": bool(user_config.get("checked")),
        "active_mode": user_config.get("activeMode", True) is not False,
    }


class ShizaiDesktopAgent:
    """Call the running desktop Agent Brain over its local business WebSocket."""

    def __init__(
        self, config: ShizaiCloudConfig | None = None, connection_factory: Callable[..., Any] | None = None
    ) -> None:
        self.config = config or ShizaiCloudConfig.from_environment()
        self.connection_factory = connection_factory

    def call(self, service: str, method: str, args: list[Any] | None = None) -> Any:
        """Make a synchronous local RPC call."""
        with _desktop_channel_lock(self.config.timeout, "RPC"):
            return self.call_unlocked(service, method, args)

    def call_unlocked(self, service: str, method: str, args: list[Any] | None = None) -> Any:
        """Make a local RPC call without taking the process-wide channel lock.

        Native debug runs can keep ``AppRunService.run`` open until the runtime
        exits.  The debug adapter must remain callable while that request is in
        flight, so the debug session worker uses this deliberately explicit
        escape hatch.  All ordinary calls continue to use :meth:`call`.
        """
        sock = self._connect()
        call_id = f"cli-{uuid.uuid4()}"
        packet = {
            "packetType": "methodCall",
            "body": {"callId": call_id, "serviceName": service, "methodName": method, "args": args or []},
        }
        try:
            sock.send(json.dumps(packet, ensure_ascii=False))
            deadline = time.monotonic() + self.config.timeout
            while time.monotonic() < deadline:
                raw = self._recv(sock, f"{service}.{method}")
                message = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                body = message.get("body", {})
                if message.get("packetType") == "callResult" and body.get("callId") == call_id:
                    result = body.get("result")
                    if isinstance(result, dict) and result.get("isSuccess") is False:
                        detail = str(result.get("message") or "Desktop RPC failed")
                        code = result.get("code")
                        if code not in (None, ""):
                            detail = f"{detail} (code={code})"
                        raise ShizaiCloudError(detail)
                    return result
            raise ShizaiCloudError(f"Desktop RPC timed out: {service}.{method}")
        finally:
            sock.close()

    def run_native_rpa(
        self,
        request: dict[str, Any],
        *,
        timeout: float | None = None,
        abort_check: Callable[[], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Submit a native RPA run and service its render calls until it ends.

        ``workflowDebugRunRPA`` returns an acceptance result before the
        RuntimeHost has finished.  The designer then sends render requests on
        this same socket (mask and tray-state updates included).  Keeping the
        socket alive here is part of the RPC protocol, not an optional UI
        convenience.
        """
        wait_timeout = self.config.timeout if timeout is None else max(0.0, timeout)
        with _desktop_channel_lock(wait_timeout, "native RPA"):
            return self._run_native_rpa_unlocked(request, timeout=wait_timeout, abort_check=abort_check)

    def _run_native_rpa_unlocked(
        self,
        request: dict[str, Any],
        *,
        timeout: float,
        abort_check: Callable[[], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        # Import lazily because agent.py uses this module for its desktop
        # client; keeping the import here avoids a module-level cycle.
        from .agent import redact_payload

        sock = self._connect(max(timeout, 1.0))
        call_id = f"cli-{uuid.uuid4()}"
        sock.send(
            json.dumps(
                {
                    "packetType": "methodCall",
                    "body": {
                        "callId": call_id,
                        "serviceName": "CorpBrainService",
                        "methodName": "workflowDebugRunRPA",
                        "args": [request],
                    },
                },
                ensure_ascii=False,
            )
        )
        deadline = time.monotonic() + timeout
        accepted = False
        accepted_at: float | None = None
        terminal_status: str | None = None
        render_events: list[dict[str, Any]] = []
        runtime_mask_suppressions: list[dict[str, Any]] = []
        runtime_mask_force_close: dict[str, Any] | None = None
        runtime_blocker: dict[str, Any] | None = None
        set_timeout = getattr(sock, "settimeout", None)
        if callable(set_timeout):
            # Runtime failures can be logged without a terminal tray event.
            # Poll briefly so callers can inspect logs during a long run.
            set_timeout(min(1.0, max(timeout, 0.1)))
        try:
            while time.monotonic() < deadline:
                if (
                    accepted_at is not None
                    and runtime_mask_force_close is None
                    and time.monotonic() - accepted_at >= 0.75
                ):
                    runtime_mask_force_close = self._force_close_native_runtime_mask(sock)
                runtime_blocker = abort_check() if abort_check else None
                if runtime_blocker:
                    terminal_status = str(runtime_blocker.get("terminal_status") or "error").casefold()
                    break
                try:
                    raw = sock.recv()
                except Exception as exc:
                    if self._is_socket_timeout(exc):
                        continue
                    raise ShizaiCloudError(f"Native RPA failed on the Shizai desktop business channel: {exc}") from exc
                if raw in (None, "", b""):
                    break
                packet = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                body = packet.get("body", {})
                packet_type = packet.get("packetType")
                if packet_type == "callResult" and body.get("callId") == call_id:
                    result = body.get("result")
                    if isinstance(result, dict) and result.get("isSuccess") is False:
                        detail = str(result.get("message") or "Native RPA run failed")
                        code = result.get("code")
                        if code not in (None, ""):
                            detail = f"{detail} (code={code})"
                        raise ShizaiCloudError(detail)
                    accepted = True
                    accepted_at = time.monotonic()
                    continue
                if packet_type != "node-call-render-req":
                    continue
                self._ack_render_call(sock, body)
                event = {
                    "service": body.get("serviceName"),
                    "method": body.get("methodName"),
                    "args": body.get("args") or [],
                    "call_id": body.get("callId"),
                }
                render_events.append(redact_payload(event))
                if self._is_native_runtime_mask_show(body):
                    runtime_mask_suppressions.append(self._suppress_native_runtime_mask(sock, body))
                terminal_status = self._native_render_terminal_status(body) or terminal_status
                if terminal_status in {"success", "error", "stopped"}:
                    break
        finally:
            sock.close()
        timed_out = terminal_status is None and time.monotonic() >= deadline
        return {
            "accepted": accepted,
            "completed": terminal_status in {"success", "error", "stopped"},
            "timed_out": timed_out,
            "terminal_status": terminal_status,
            "render_events": render_events,
            "runtime_mask_suppressions": runtime_mask_suppressions,
            "runtime_mask_force_close": runtime_mask_force_close,
            "runtime_blocker": runtime_blocker,
        }

    @staticmethod
    def _is_native_runtime_mask_show(body: dict[str, Any]) -> bool:
        """Match only the RuntimeHost's production run-mask render request."""
        service = str(body.get("serviceName") or "").strip().casefold()
        method = str(body.get("methodName") or "").strip().casefold()
        return service == "maskservice" and method == "showmaskv2"

    @staticmethod
    def _native_runtime_mask_request_id(body: dict[str, Any]) -> str:
        """Extract the vendor mask request id without assuming one payload shape."""
        pending: list[Any] = [body.get("args") or []]
        while pending:
            candidate = pending.pop(0)
            if isinstance(candidate, dict):
                for key in ("requestId", "request_id", "id"):
                    value = candidate.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                pending.extend(candidate.values())
            elif isinstance(candidate, (list, tuple)):
                pending.extend(candidate)
        return ""

    @classmethod
    def _suppress_native_runtime_mask(cls, sock: Any, body: dict[str, Any]) -> dict[str, Any]:
        """Close the red production mask while keeping the Runtime RPC unblocked.

        Interactive element picking uses DeepCaptureInspect on a separate API
        and is intentionally unaffected by this production-only handler.
        """
        request_id = cls._native_runtime_mask_request_id(body)
        close_call_id = f"cli-{uuid.uuid4()}"
        try:
            sock.send(
                json.dumps(
                    {
                        "packetType": "methodCall",
                        "body": {
                            "callId": close_call_id,
                            "serviceName": "MaskAppService",
                            "methodName": "closeRunMask",
                            "args": [{"requestId": request_id}],
                        },
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as exc:
            return {
                "suppressed": False,
                "request_id": request_id or None,
                "close_call_id": close_call_id,
                "warning": str(exc),
            }
        return {
            "suppressed": True,
            "request_id": request_id or None,
            "close_call_id": close_call_id,
            "service": "MaskAppService",
            "method": "closeRunMask",
        }

    @staticmethod
    def _force_close_native_runtime_mask(sock: Any) -> dict[str, Any]:
        """Close the production mask when Runtime renders it on another provider.

        Some desktop versions route the mask directly to the application
        renderer, so no ``showMaskV2`` request appears on the CLI socket. The
        renderer's force flag is the supported way to close that active mask
        without knowing its generated request id.
        """
        close_call_id = f"cli-{uuid.uuid4()}"
        try:
            sock.send(
                json.dumps(
                    {
                        "packetType": "methodCall",
                        "body": {
                            "callId": close_call_id,
                            "serviceName": "MaskAppService",
                            "methodName": "closeRunMask",
                            "args": [{"requestId": "", "force": True}],
                        },
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as exc:
            return {"suppressed": False, "close_call_id": close_call_id, "warning": str(exc)}
        return {
            "suppressed": True,
            "close_call_id": close_call_id,
            "service": "MaskAppService",
            "method": "closeRunMask",
            "force": True,
        }

    @staticmethod
    def _is_socket_timeout(exc: Exception) -> bool:
        """Recognize a receive timeout without treating it as a run failure."""
        if isinstance(exc, TimeoutError):
            return True
        return bool(websocket is not None and isinstance(exc, getattr(websocket, "WebSocketTimeoutException", ())))

    @staticmethod
    def _native_render_terminal_status(body: dict[str, Any]) -> str | None:
        """Extract the RuntimeHost terminal state from the tray render call."""
        service = str(body.get("serviceName") or "").casefold()
        method = str(body.get("methodName") or "").casefold()
        if "traymanager" not in service and "runstatechange" not in method:
            return None
        args = body.get("args") or []
        candidates: list[Any] = list(args) if isinstance(args, list) else [args]
        for candidate in candidates:
            if isinstance(candidate, dict):
                candidates.extend(candidate.values())
            elif isinstance(candidate, (list, tuple)):
                candidates.extend(candidate)
        for candidate in candidates:
            value = str(candidate).strip().casefold()
            if value in {"running", "success", "error", "stopped"}:
                return value
        return None

    def exchange_token(self) -> str:
        """Exchange the logged-in desktop session for a process-local REST token."""
        result = self.call("LoginService", "changeToken", [10])
        payload = result.get("data") if isinstance(result, dict) else None
        token = payload.get("data") if isinstance(payload, dict) else payload
        if not token:
            raise ShizaiCloudError("Logged-in desktop session did not return a cloud token")
        return str(token)

    def authentication_status(self) -> dict[str, Any]:
        """Return secret-free readiness for the desktop session and cloud REST API."""
        token_configured = bool(self.config.token)
        status: dict[str, Any] = {
            "desktop": {
                "configured": bool(self.config.ws_url),
                "checked": False,
                "reachable": False,
                "authenticated": False,
            },
            "cloud_rest": {
                "configured": token_configured,
                "ready": token_configured,
                "source": "SHIZAI_AGENT_TOKEN" if token_configured else None,
            },
        }

        # A headless token is sufficient for REST operations, so avoid making
        # desktop availability a requirement in that mode.
        if token_configured:
            status["desktop"]["skipped"] = True
            status["ready"] = True
            return status

        status["desktop"]["checked"] = True
        try:
            account = self.call("LoginService", "getCurrentUserAccount")
            _native_user_id(account)
        except ShizaiCloudError as exc:
            detail = str(exc)
            status["desktop"]["error"] = detail
            status["desktop"]["reachable"] = not any(
                marker in detail.lower()
                for marker in ("could not connect", "business channel", "timed out", "failed on")
            )
        else:
            status["desktop"].update({"reachable": True, "authenticated": True})
            status["cloud_rest"].update(
                {
                    "ready": True,
                    "source": "logged_in_desktop_session",
                    "configured": False,
                }
            )

        status["ready"] = bool(status["cloud_rest"]["ready"] or status["desktop"]["authenticated"])
        return status

    def native_feishu_status(self) -> dict[str, Any]:
        """Inspect native Feishu configuration and runtime flags in the desktop app."""
        account = self.call("LoginService", "getCurrentUserAccount")
        user_id = _native_user_id(account)
        origin_result = self.call("NewStorageService", "getOriginConfig")
        origin_config = _desktop_result_data(origin_result)
        if not isinstance(origin_config, dict):
            raise ShizaiCloudError("Desktop storage configuration was not returned")
        runtime_result = self.call("IMTarsAppService", "checkTarsIM")
        runtime = _desktop_result_data(runtime_result)
        return {
            "backend": "native-desktop",
            "user_id": user_id,
            "config": native_feishu_summary(origin_config, user_id),
            "runtime": runtime if isinstance(runtime, dict) else {},
        }

    def configure_native_feishu(
        self,
        client_id: str,
        client_secret: str,
        *,
        user_id: str | None = None,
        checked: bool = True,
        active_mode: bool = True,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Configure native Feishu IM for the current desktop account."""
        if not client_id.strip() or not client_secret.strip():
            raise ValueError("client_id and client_secret are required")
        account = self.call("LoginService", "getCurrentUserAccount")
        resolved_user_id = str(user_id or _native_user_id(account))
        origin_result = self.call("NewStorageService", "getOriginConfig")
        origin_config = _desktop_result_data(origin_result)
        if not isinstance(origin_config, dict):
            raise ShizaiCloudError("Desktop storage configuration was not returned")
        updated = merge_native_feishu_config(
            origin_config,
            user_id=resolved_user_id,
            client_id=client_id,
            client_secret=client_secret,
            checked=checked,
            active_mode=active_mode,
        )
        result: Any = None
        if persist:
            result = self.call("NewStorageService", "updateAppConfig", [{"tarsIM": updated["tarsIM"]}])
        summary = native_feishu_summary(updated, resolved_user_id)
        summary.update(
            {
                "backend": "native-desktop",
                "saved": bool(persist),
                "rpc_result_received": result is not None,
            }
        )
        return summary

    def disable_native_feishu(self, *, user_id: str | None = None, persist: bool = True) -> dict[str, Any]:
        """Stop the native Feishu IM route without touching other providers."""
        account = self.call("LoginService", "getCurrentUserAccount")
        resolved_user_id = str(user_id or _native_user_id(account))
        origin_result = self.call("NewStorageService", "getOriginConfig")
        origin_config = _desktop_result_data(origin_result)
        if not isinstance(origin_config, dict):
            raise ShizaiCloudError("Desktop storage configuration was not returned")
        updated = remove_native_feishu_config(origin_config, user_id=resolved_user_id)
        result: Any = None
        removed = updated != origin_config
        if persist and removed:
            result = self.call("NewStorageService", "updateAppConfig", [{"tarsIM": updated.get("tarsIM", {})}])
        summary = native_feishu_summary(updated, resolved_user_id)
        summary.update(
            {
                "backend": "native-desktop",
                "saved": bool(persist and removed),
                "removed": removed,
                "rpc_result_received": result is not None,
            }
        )
        return summary

    def chat(
        self,
        query: str,
        *,
        agent_id: str | None = None,
        conversation_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run one serialized desktop chat and collect its streamed answer."""
        chat_timeout = timeout or self.config.timeout
        with _desktop_channel_lock(chat_timeout, "chat"):
            return self._chat_unlocked(
                query,
                agent_id=agent_id,
                conversation_id=conversation_id,
                timeout=timeout,
            )

    def _chat_unlocked(
        self,
        query: str,
        *,
        agent_id: str | None = None,
        conversation_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run the configured advanced-chat Agent and collect its streamed answer."""
        if not query.strip():
            raise ValueError("QUESTION is required")
        sock = self._connect(timeout or self.config.timeout)
        call_id = f"cli-{uuid.uuid4()}"
        listener_id = f"listener-{uuid.uuid4()}"
        data: dict[str, Any] = {"inputs": {"query": query}, "query": query}
        if conversation_id:
            data["conversation_id"] = conversation_id
        request = {
            "appId": agent_id or self.config.agent_id,
            "data": data,
            "listenerId": listener_id,
            "clientId": self.config.client_id,
        }
        sock.send(
            json.dumps(
                {
                    "packetType": "methodCall",
                    "body": {
                        "callId": call_id,
                        "serviceName": "AgentBrainAppService",
                        "methodName": "runAdvancedChatWorkflowDraft",
                        "args": [request],
                    },
                },
                ensure_ascii=False,
            )
        )
        answer_parts: list[str] = []
        events: list[dict[str, Any]] = []
        conversation: str | None = conversation_id
        task_id: str | None = None
        final_data: dict[str, Any] | None = None
        deadline = time.monotonic() + (timeout or self.config.timeout)
        try:
            while time.monotonic() < deadline:
                raw = self._recv(sock, "Agent chat")
                packet = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                body = packet.get("body", {})
                if packet.get("packetType") == "callResult" and body.get("callId") == call_id:
                    result = body.get("result") or {}
                    if isinstance(result, dict) and result.get("isSuccess") is False:
                        raise ShizaiCloudError(result.get("message", "Agent run failed"))
                    continue
                if packet.get("packetType") != "node-call-render-req":
                    continue
                self._ack_render_call(sock, body)
                if body.get("serviceName") != "SinglePointPushClientService":
                    continue
                event = _extract_push_event(body.get("args") or [])
                if not event:
                    continue
                event_task_id = event.get("task_id")
                if task_id and event_task_id and event_task_id != task_id:
                    continue
                events.append(event)
                event_name = event.get("event")
                if event_name == "workflow_started":
                    conversation = event.get("conversation_id") or conversation
                    task_id = event.get("task_id")
                elif event_name in {"message", "agent_message", "text_chunk"}:
                    answer = event.get("answer") or event.get("text") or ""
                    if answer:
                        answer_parts.append(str(answer))
                elif event_name == "workflow_finished":
                    conversation = event.get("conversation_id") or conversation
                    task_id = event.get("task_id") or task_id
                    final_data = event.get("data") or {}
                    outputs = final_data.get("outputs") or {}
                    output = outputs.get("answer") if isinstance(outputs, dict) else None
                    if output is not None:
                        answer_parts = [str(output)]
                    break
                elif event_name == "error":
                    raise ShizaiCloudError(event.get("message", "Agent workflow failed"))
            else:
                raise ShizaiCloudError("Agent workflow timed out")
        finally:
            sock.close()
        return {
            "answer": "".join(answer_parts),
            "conversation_id": conversation,
            "task_id": task_id,
            "workflow": final_data,
            "events": events,
            "sources": _extract_retriever_sources(events),
        }

    @staticmethod
    def _recv(sock: Any, operation: str) -> Any:
        """Receive one desktop packet and normalize transport failures."""
        try:
            return sock.recv()
        except Exception as exc:
            websocket_error = websocket is not None and isinstance(exc, websocket.WebSocketException)
            if websocket_error or isinstance(exc, (TimeoutError, OSError)):
                raise ShizaiCloudError(f"{operation} failed on the Shizai desktop business channel: {exc}") from exc
            raise

    def get_workflow_draft(self, agent_id: str | None = None) -> dict[str, Any]:
        """Return the advanced-chat workflow draft for an Agent."""
        result = self.call(
            "AgentBrainAppService",
            "getWorkflowDraft",
            [{"appId": agent_id or self.config.agent_id}],
        )
        # Desktop RPC wraps the service payload in ``data``.  Normalize that
        # envelope here so callers can work with both desktop versions.
        if not isinstance(result, dict):
            return {}
        data = result.get("data")
        return data if isinstance(data, dict) else result

    def configure_knowledge(
        self,
        dataset_id: str,
        *,
        agent_id: str | None = None,
        top_k: int | None = None,
    ) -> dict[str, Any]:
        """Replace dataset ids on every knowledge-retrieval node and save the draft."""
        app_id = agent_id or self.config.agent_id
        draft_response = self.get_workflow_draft(app_id)
        draft = draft_response.get("draft") if isinstance(draft_response, dict) else None
        if not isinstance(draft, dict):
            raise ShizaiCloudError("Agent workflow draft was not returned")
        graph = draft.get("graph") or {}
        nodes = graph.get("nodes") or []
        changed = 0
        prompt_changed = 0
        for node in nodes:
            data = node.get("data") or {}
            node_type = data.get("type")
            if node_type == "knowledge-retrieval":
                data["dataset_ids"] = [dataset_id]
                if top_k is not None:
                    config = data.get("multiple_retrieval_config") or {}
                    config["top_k"] = top_k
                    data["multiple_retrieval_config"] = config
                node["data"] = data
                changed += 1
            elif node_type == "llm" and data.get("context", {}).get("enabled"):
                templates = data.get("prompt_template") or []
                system_template = next(
                    (item for item in templates if item.get("role") == "system"),
                    None,
                )
                if system_template is None:
                    system_template = {
                        "role": "system",
                        "edition_type": "basic",
                        "id": str(uuid.uuid4()),
                    }
                    templates.insert(0, system_template)
                system_template["text"] = KNOWLEDGE_QA_PROMPT
                data["prompt_template"] = templates
                node["data"] = data
                prompt_changed += 1
        if changed == 0:
            raise ShizaiCloudError("Agent workflow has no knowledge-retrieval node")
        saved = self.call(
            "AgentBrainAppService",
            "saveWorkflowDraft",
            [{"appId": app_id, "draft": draft}],
        )
        return {
            "saved": True,
            "agent_id": app_id,
            "dataset_id": dataset_id,
            "nodes_changed": changed,
            "prompt_nodes_changed": prompt_changed,
            "result": saved,
        }

    def configure_ingestion_tools(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """Add file-reading and webpage-analysis branches to the Agent draft.

        These built-in tools analyze incoming content. Knowledge-base writes stay
        in the authenticated console client and are never embedded in the draft.
        """
        app_id = agent_id or self.config.agent_id
        draft_response = self.get_workflow_draft(app_id)
        draft = draft_response.get("draft") if isinstance(draft_response, dict) else None
        if not isinstance(draft, dict):
            raise ShizaiCloudError("Agent workflow draft was not returned")
        graph = draft.get("graph") or {}
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        by_id = {node.get("id"): node for node in nodes if node.get("id")}
        start = next((node for node in nodes if node.get("data", {}).get("type") == "start"), None)
        retrieval = next((node for node in nodes if node.get("data", {}).get("type") == "knowledge-retrieval"), None)
        if start is None or retrieval is None:
            raise ShizaiCloudError("Agent workflow must contain start and knowledge-retrieval nodes")

        features = draft.setdefault("features", {})
        features["file_upload"] = {
            "image": {"enabled": True, "number_limits": 10, "transfer_methods": ["local_file", "remote_url"]}
        }
        features["support_file_types"] = INGESTION_FILE_TYPES
        features["opening_statement"] = (
            "你好，我是 AI 知识库助手。你可以上传 PDF、Word、Excel、TXT、Markdown、CSV，"
            "也可以发送网页链接。我会先分析内容；需要沉淀到知识库时，请在知识库管理中导入，"
            "或使用配套的知识库导入命令，完成索引后再回来提问。"
        )

        def add_node(node: dict[str, Any]) -> None:
            if node["id"] not in by_id:
                nodes.append(node)
                by_id[node["id"]] = node
                return
            existing = by_id[node["id"]]
            existing["data"] = node.get("data", existing.get("data", {}))
            for key in ("position", "targetPosition", "sourcePosition", "selected"):
                if key in node:
                    existing[key] = node[key]

        def add_edge(source: str, target: str, handle: str = "source") -> None:
            edge_id = f"{source}-{handle}-{target}-target"
            if any(edge.get("id") == edge_id for edge in edges):
                return
            edges.append(
                {
                    "id": edge_id,
                    "type": "custom",
                    "source": source,
                    "target": target,
                    "sourceHandle": handle,
                    "targetHandle": "target",
                    "data": {"sourceType": by_id[source]["data"]["type"], "targetType": by_id[target]["data"]["type"]},
                }
            )

        router_id = "source-router-kb"
        file_id = "file-reader-kb"
        url_extract_id = "url-extractor-kb"
        web_id = "webscraper-kb"
        file_llm_id = "llm-file-kb"
        web_llm_id = "llm-web-kb"
        file_answer_id = "answer-file-kb"
        web_answer_id = "answer-web-kb"
        add_node(
            {
                "id": router_id,
                "type": "custom",
                "data": {
                    "type": "if-else",
                    "title": "识别文件或网页",
                    "desc": "文件和网页先分析，否则走知识库问答。",
                    "cases": [
                        {
                            "id": "file",
                            "case_id": "file",
                            "logical_operator": "and",
                            "conditions": [
                                {
                                    "id": str(uuid.uuid4()),
                                    "variable_selector": ["sys", "files"],
                                    "comparison_operator": "not empty",
                                    "value": "",
                                    "varType": "array[file]",
                                }
                            ],
                        },
                        {
                            "id": "url",
                            "case_id": "url",
                            "logical_operator": "and",
                            "conditions": [
                                {
                                    "id": str(uuid.uuid4()),
                                    "variable_selector": ["sys", "query"],
                                    "comparison_operator": "contains",
                                    "value": "http",
                                    "varType": "string",
                                }
                            ],
                        },
                    ],
                    "selected": False,
                },
                "position": {"x": 80, "y": 282},
                "targetPosition": "left",
                "sourcePosition": "right",
                "selected": False,
            }
        )
        add_node(
            _builtin_tool_node(
                file_id,
                title="读取上传文件",
                provider_id="indeed_stable",
                tool_name="file_reader",
                tool_label="文件读取",
                tool_parameters={"files": {"type": "variable", "value": ["sys", "files"]}},
                output_parameters=[
                    {
                        "name": "text",
                        "type": "string",
                        "required": True,
                        "label": {"zh_Hans": "文件内容", "en_US": "file content"},
                        "default": "",
                    }
                ],
                position=(350, 100),
            )
        )
        add_node(
            _builtin_tool_node(
                web_id,
                title="读取网页内容",
                provider_id="webscraper",
                tool_name="webscraper",
                tool_label="网页爬虫",
                tool_parameters={"url": {"type": "variable", "value": [url_extract_id, "result"]}},
                output_parameters=[
                    {
                        "name": "text",
                        "type": "string",
                        "required": True,
                        "label": {"zh_Hans": "网页内容", "en_US": "web content"},
                        "default": "",
                    }
                ],
                position=(350, 470),
            )
        )
        add_node(
            {
                "id": url_extract_id,
                "type": "custom",
                "data": {
                    "id": url_extract_id,
                    "type": "code",
                    "title": "提取网页链接",
                    "desc": "从用户问题中提取第一个 HTTP(S) 链接，避免把整句问题传给网页抓取工具。",
                    "variables": [{"variable": "query", "value_selector": ["sys", "query"]}],
                    "code_language": "python3",
                    "code": (
                        "import re\n\n"
                        "def main(query: str) -> dict:\n"
                        "    urls = re.findall(r'https?://[^\\s<>\\\"\\'\\u3001\\u3002\\uff0c\\uff1b]+', query or '')\n"
                        "    url = urls[0].rstrip('.,);]}') if urls else ''\n"
                        "    return {'result': url}\n"
                    ),
                    "outputs": {"result": {"type": "string", "children": None}},
                    "selected": False,
                },
                "position": {"x": 350, "y": 650},
                "targetPosition": "left",
                "sourcePosition": "right",
                "selected": False,
            }
        )

        llm = next((node for node in nodes if node.get("data", {}).get("type") == "llm"), None)
        answer = next((node for node in nodes if node.get("data", {}).get("type") == "answer"), None)
        if llm is None or answer is None:
            raise ShizaiCloudError("Agent workflow must contain LLM and answer nodes")

        def add_analysis_branch(
            node_id: str, title: str, source_id: str, source_label: str, answer_id: str, y: int
        ) -> None:
            data = json.loads(json.dumps(llm["data"], ensure_ascii=False))
            data.update(
                {"id": node_id, "type": "llm", "title": title, "context": {"enabled": False, "variable_selector": []}}
            )
            data["prompt_template"] = [
                {
                    "role": "system",
                    "text": f"你是 AI 知识库助手。请基于以下{source_label}回答用户问题。只使用已提供内容，不要编造；先给出分析结论，再列出关键依据。\n\n{source_label}：{{{{#{source_id}.text#}}}}\n\n用户问题：{{{{#sys.query#}}}}",
                    "edition_type": "basic",
                    "id": str(uuid.uuid4()),
                }
            ]
            add_node(
                {
                    "id": node_id,
                    "type": "custom",
                    "data": data,
                    "position": {"x": 650, "y": y},
                    "targetPosition": "left",
                    "sourcePosition": "right",
                    "selected": False,
                }
            )
            add_node(
                {
                    "id": answer_id,
                    "type": "custom",
                    "data": {
                        "type": "answer",
                        "title": f"{title}回复",
                        "desc": "",
                        "variables": [],
                        "answer": f"{{{{#{node_id}.text#}}}}",
                        "selected": False,
                    },
                    "position": {"x": 950, "y": y},
                    "targetPosition": "left",
                    "sourcePosition": "right",
                    "selected": False,
                }
            )

        add_analysis_branch(file_llm_id, "文件分析", file_id, "文件内容", file_answer_id, 100)
        add_analysis_branch(web_llm_id, "网页分析", web_id, "网页内容", web_answer_id, 470)

        graph["nodes"] = nodes
        graph["edges"] = edges
        old_edge_ids = {
            edge.get("id")
            for edge in edges
            if edge.get("source") == start["id"] and edge.get("target") == retrieval["id"]
        }
        edges[:] = [
            edge
            for edge in edges
            if edge.get("id") not in old_edge_ids
            and not (edge.get("source") == router_id and edge.get("target") == web_id)
        ]
        add_edge(start["id"], router_id)
        add_edge(router_id, file_id, "file")
        add_edge(router_id, retrieval["id"], "false")
        add_edge(file_id, file_llm_id)
        add_edge(file_llm_id, file_answer_id)
        add_edge(router_id, url_extract_id, "url")
        add_edge(url_extract_id, web_id)
        add_edge(web_id, web_llm_id)
        add_edge(web_llm_id, web_answer_id)

        saved = self.call("AgentBrainAppService", "saveWorkflowDraft", [{"appId": app_id, "draft": draft}])
        return {
            "saved": True,
            "agent_id": app_id,
            "nodes_added": [
                router_id,
                file_id,
                url_extract_id,
                web_id,
                file_llm_id,
                web_llm_id,
                file_answer_id,
                web_answer_id,
            ],
            "result": saved,
        }

    def publish(self, *, agent_id: str | None = None, publish_info: dict[str, Any] | None = None) -> Any:
        """Publish the current workflow draft with the requested publish settings."""
        app_id = agent_id or self.config.agent_id
        info = publish_info or {"publish_range": "only_me"}
        return self.call(
            "AgentBrainAppService",
            "publishWorkflow",
            [{"appId": app_id, "appPublishInfo": info}],
        )

    def _connect(self, timeout: float | None = None) -> Any:
        ws_url = self.config.ws_url
        if ws_url == DEFAULT_WS_URL:
            ws_url = _replace_ws_client_id(ws_url, self.config.client_id)
        if self.connection_factory:
            return self.connection_factory(ws_url, timeout=timeout or self.config.timeout)
        if websocket is None:
            raise ShizaiCloudError("websocket-client is required for desktop Agent operations")
        connect_timeout = timeout or self.config.timeout
        try:
            return websocket.create_connection(ws_url, timeout=connect_timeout)
        except Exception as exc:  # websocket-client exposes several exception types
            if not _auto_start_enabled() or not self._is_local_desktop_url():
                raise ShizaiCloudError(f"Could not connect to the Shizai desktop business channel: {exc}") from exc
            started, executable = self._start_desktop()
            if not started:
                raise _desktop_start_error(str(exc), executable=executable) from exc
            deadline = time.monotonic() + min(max(connect_timeout, 0.0), 12.0)
            last_error = exc
            while time.monotonic() < deadline:
                try:
                    return websocket.create_connection(ws_url, timeout=min(1.0, max(deadline - time.monotonic(), 0.1)))
                except Exception as retry_exc:
                    last_error = retry_exc
                    time.sleep(0.25)
            raise _desktop_start_error(str(last_error), executable=executable) from last_error

    def _is_local_desktop_url(self) -> bool:
        parsed = urlparse(self.config.ws_url)
        return parsed.scheme in {"ws", "wss"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}

    @staticmethod
    def _start_desktop() -> tuple[bool, Path | None]:
        """Start the installed designer once when its local channel is absent."""
        paths = ShizaiPaths.from_environment()
        executable = paths.executable
        if executable is None or not executable.is_file():
            return False, None
        try:
            creation_flags = 0
            if os.name == "nt":
                creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
                )
            subprocess.Popen(
                [str(executable)],
                cwd=str(paths.install_root) if paths.install_root else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
                close_fds=os.name != "nt",
            )
        except (OSError, ValueError):
            return False, executable
        return True, executable

    @staticmethod
    def _ack_render_call(sock: Any, body: dict[str, Any]) -> None:
        call_id = body.get("callId")
        if not call_id:
            return
        sock.send(
            json.dumps(
                {
                    "packetType": "node-call-render-res",
                    "body": {
                        "callId": call_id,
                        "result": {"isSuccess": True, "code": 0, "message": "ok", "data": None},
                    },
                }
            )
        )
