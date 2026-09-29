import json
import time
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


def _merge_query(url: str, query: Optional[Dict[str, Any]]) -> str:
    if not query:
        return url

    parsed = urlparse(url)
    current_query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    current_query.update({key: value for key, value in query.items() if value is not None})
    return urlunparse(parsed._replace(query=urlencode(current_query, doseq=True)))


def _normalize_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    return {str(key): str(value) for key, value in headers.items() if value is not None}


def _build_request_body(params: Dict[str, Any], headers: Dict[str, str]) -> Optional[bytes]:
    if "json" in params and params["json"] is not None:
        headers.setdefault("Content-Type", "application/json")
        return json.dumps(params["json"], ensure_ascii=False).encode("utf-8")

    if "form" in params and params["form"] is not None:
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        return urlencode(params["form"], doseq=True).encode("utf-8")

    body = params.get("body")
    if body is None:
        return None
    if isinstance(body, bytes):
        return body
    if isinstance(body, (dict, list)):
        headers.setdefault("Content-Type", "application/json")
        return json.dumps(body, ensure_ascii=False).encode("utf-8")
    return str(body).encode("utf-8")


def _decode_response(raw_body: bytes, content_type: str, max_response_chars: int) -> Dict[str, Any]:
    text = raw_body.decode("utf-8", errors="replace")
    truncated = len(text) > max_response_chars
    response_text = text[:max_response_chars] if truncated else text

    response: Dict[str, Any] = {
        "text": response_text,
        "truncated": truncated,
    }
    if "application/json" in content_type.lower():
        try:
            response["json"] = json.loads(text)
        except json.JSONDecodeError:
            response["json_parse_error"] = "响应 Content-Type 为 JSON，但正文不是有效 JSON。"
    return response


def send_http_request(params: Dict[str, Any]) -> Dict[str, Any]:
    """发送 HTTP 请求并返回结构化结果，供 Tool 和普通 Agent 共同复用。"""
    request_params = params if isinstance(params, dict) else {}
    url = str(request_params.get("url", "")).strip()
    method = str(request_params.get("method", "GET")).strip().upper()
    timeout = float(request_params.get("timeout", 15))
    max_response_chars = int(request_params.get("max_response_chars", 20000))

    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return {"ok": False, "error": "url 必须是合法的 http 或 https 地址。"}

    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        return {"ok": False, "error": f"不支持的 HTTP 方法: {method}"}

    headers = _normalize_headers(request_params.get("headers"))
    request_url = _merge_query(url, request_params.get("query"))
    request_body = _build_request_body(request_params, headers)
    request = Request(request_url, data=request_body, headers=headers, method=method)

    started_at = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            raw_body = response.read()
            elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
            response_headers = dict(response.headers.items())
            content_type = response_headers.get("Content-Type", "")
            return {
                "ok": 200 <= response.status < 400,
                "status_code": response.status,
                "reason": response.reason,
                "url": response.geturl(),
                "elapsed_ms": elapsed_ms,
                "headers": response_headers,
                "body": _decode_response(raw_body, content_type, max_response_chars),
            }
    except HTTPError as exc:
        raw_body = exc.read()
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        content_type = response_headers.get("Content-Type", "")
        return {
            "ok": False,
            "status_code": exc.code,
            "reason": exc.reason,
            "url": request_url,
            "elapsed_ms": elapsed_ms,
            "headers": response_headers,
            "body": _decode_response(raw_body, content_type, max_response_chars),
        }
    except (TimeoutError, URLError, OSError) as exc:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        return {
            "ok": False,
            "status_code": None,
            "reason": None,
            "url": request_url,
            "elapsed_ms": elapsed_ms,
            "error": str(exc),
        }
