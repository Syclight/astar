import datetime
import json
import os
import sys
import traceback
from typing import Any


COLORS = {
    "DEBUG": "\033[94m",
    "INFO": "\033[92m",
    "WARN": "\033[93m",
    "ERROR": "\033[91m",
    "RESET": "\033[0m",
}


def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"<JSON序列化失败: {exc}>: {repr(obj)}"


def _get_file_col(stack_index: int = -3) -> tuple[str, int]:
    stack = traceback.extract_stack()[stack_index]
    return stack.filename, stack.lineno


# Concise by default; ASTRA_VERBOSE=1 or `astra ... --verbose` shows timestamps, source lines and detail messages.
_VERBOSE = os.environ.get("ASTRA_VERBOSE", "").strip().lower() in {"1", "true", "yes", "on"}


def set_verbose(enabled: bool) -> None:
    global _VERBOSE
    _VERBOSE = bool(enabled)


def is_verbose() -> bool:
    return _VERBOSE


def print_debug(
    msg: Any,
    tip: str = "",
    stack_index: int = -3,
    use_json_format: bool = False,
    level: str = "DEBUG",
    end_char: str = "\n",
) -> None:
    level = level.upper()
    if not _VERBOSE:
        if level == "DEBUG":
            return
        body = _safe_json(msg) if use_json_format else msg
        prefix = {"ERROR": "错误：", "WARN": "注意："}.get(level, "")
        sys.stderr.write(f"{prefix}{tip}{body}" + end_char)
        return
    filename, lineno = _get_file_col(stack_index)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = COLORS.get(level, "\033[0m")
    reset = "\033[0m"
    header = f"{color}[{level}] {timestamp} - {filename}:{lineno}{reset}"
    body = _safe_json(msg) if use_json_format else msg
    output = f">>>> 调试打印：{header}\n{tip}{body}\n"
    # Diagnostics go to stderr so stdout stays machine-readable (e.g. `astra run --json`).
    sys.stderr.write(output + end_char)


def print_detail(msg: Any, tip: str = "", use_json_format: bool = False, end_char: str = "\n") -> None:
    """Shown only in verbose mode: internal steps a user does not need to follow."""
    print_debug(msg, tip=tip, stack_index=-4, use_json_format=use_json_format, level="DEBUG", end_char=end_char)


def print_info(msg: Any, tip: str = "", use_json_format: bool = False, end_char: str = "\n") -> None:
    print_debug(msg, tip=tip, stack_index=-4, use_json_format=use_json_format, level="INFO", end_char=end_char)


def print_error(msg: Any, use_json_type: bool = False, is_raise: bool = False) -> None:
    print_debug(msg, use_json_format=use_json_type, level="ERROR")
    if is_raise:
        raise Exception("出现了致命错误！")


def get_debug_string(
    msg: Any,
    tip: str = "",
    stack_index: int = -3,
    use_json_format: bool = False,
    level: str = "DEBUG",
    show_file_info: bool = True,
) -> str:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = _safe_json(msg) if use_json_format else msg
    if show_file_info:
        filename, lineno = _get_file_col(stack_index)
        header = f"[{level}] {timestamp} - {filename}:{lineno}"
        return f"{header}\n{tip}{body}\n"

    header = f"[{level}] {timestamp}"
    return f"{header} {tip}{body}"


def get_info_string(msg: Any, tip: str = "", use_json_format: bool = False, show_file_info: bool = True) -> str:
    return get_debug_string(
        msg,
        tip=tip,
        stack_index=-4,
        use_json_format=use_json_format,
        level="INFO",
        show_file_info=show_file_info,
    )


def get_error_string(msg: Any, tip: str = "", use_json_format: bool = False, show_file_info: bool = True) -> str:
    return get_debug_string(
        msg,
        tip=tip,
        stack_index=-4,
        use_json_format=use_json_format,
        level="ERROR",
        show_file_info=show_file_info,
    )
