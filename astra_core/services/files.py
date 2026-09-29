import json
import re
from typing import Any
from astra_core.runtime.context import resolve_resource


def read_md_file(file_path: str) -> str:
    """读 md 文档，返回去除 Markdown 注释后的文本内容。"""
    with open(resolve_resource(file_path), "r", encoding="utf-8") as file:
        content = file.read()
    return re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)


def read_md_files(*file_paths: str, ending: str = "\n\n") -> str:
    combined_content = ""
    for path in file_paths:
        combined_content += read_md_file(path) + ending
    return combined_content


def write_json_file(json_file: str, data: Any, write_mode: str = "w") -> None:
    with open(resolve_resource(json_file), write_mode, encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=4)


def write_text_file(file_path: str, content: str, write_mode: str = "w") -> None:
    with open(resolve_resource(file_path), write_mode, encoding="utf-8", newline="\n") as file:
        file.write(content)
