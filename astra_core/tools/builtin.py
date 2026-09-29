import json
from pathlib import Path
from typing import Any, Dict

from astra_core.services.http_client import send_http_request
from astra_core.tools.base import ToolSpec, load_params, register_tool


def _json_text(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=4)


def _resolve_project_data_path(data_dir: str, file_path: Any) -> Path:
    root = Path(data_dir).resolve()
    raw_path = str(file_path or "").strip()
    if not raw_path:
        raise ValueError("file_path 不能为空。")
    requested_path = Path(raw_path)

    candidate = requested_path.resolve() if requested_path.is_absolute() else (root / requested_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("file_path 必须位于当前项目的 data/ 目录内。") from exc
    return candidate


def register_builtin_tools(data_dir: str) -> None:
    """Register standard project-scoped tools for the active business project."""

    def http_request_handler(params: Any, **kwargs: Any) -> str:
        del kwargs
        return _json_text(send_http_request(load_params(params)))

    def json_file_reader_handler(params: Any, **kwargs: Any) -> str:
        del kwargs
        try:
            file_path = _resolve_project_data_path(data_dir, load_params(params).get("file_path"))
            with file_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
            return _json_text(payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return _json_text({"error": f"读取 JSON 文件失败: {exc}"})

    register_tool(
        ToolSpec(
            name="http_request",
            description="发送通用 HTTP 请求并返回结构化 JSON 结果。",
            parameters=[
                {"name": "url", "type": "string", "description": "http 或 https 请求地址。", "required": True},
                {"name": "method", "type": "string", "description": "HTTP 方法，默认 GET。", "required": False},
                {"name": "headers", "type": "object", "description": "请求头。", "required": False},
                {"name": "query", "type": "object", "description": "查询参数。", "required": False},
                {"name": "json", "type": "object", "description": "JSON 请求体。", "required": False},
                {"name": "form", "type": "object", "description": "表单请求体。", "required": False},
                {"name": "body", "type": "string", "description": "原始请求体。", "required": False},
                {"name": "timeout", "type": "number", "description": "超时时间，单位秒。", "required": False},
                {"name": "max_response_chars", "type": "integer", "description": "响应正文最大字符数。", "required": False},
            ],
            handler=http_request_handler,
        )
    )
    register_tool(
        ToolSpec(
            name="json_file_reader",
            description="读取当前项目 data/ 目录内的 JSON 文件并返回 JSON 字符串。",
            parameters=[
                {"name": "file_path", "type": "string", "description": "相对 data/ 的文件路径，或该目录内的绝对路径。", "required": True},
            ],
            handler=json_file_reader_handler,
        )
    )
