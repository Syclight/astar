import importlib.util
from pathlib import Path
from typing import List

from astra_core.services.console import print_detail, print_error


def load_all_tools(tools_dir: str = "tools") -> List[str]:
    """
    递归导入工具模块，以便它们注册与框架无关的ToolSpec对象。
    """
    tools_path = Path(tools_dir)
    if not tools_path.exists():
        print_error(f"错误：找不到工具目录 {tools_dir}")
        return []

    loaded_modules = []
    for filepath in tools_path.rglob("*.py"):
        if filepath.name == "__init__.py":
            continue

        module_name = _module_name_for_tool(filepath)
        spec = importlib.util.spec_from_file_location(module_name, filepath)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            loaded_modules.append(module_name)

    print_detail(f"成功批量加载了 {len(loaded_modules)} 个工具文件。")
    return loaded_modules


def _module_name_for_tool(filepath: Path) -> str:
    try:
        relative_path = filepath.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        relative_path = filepath.name

    if isinstance(relative_path, Path):
        module_parts = relative_path.with_suffix("").parts
    else:
        module_parts = (Path(relative_path).stem,)
    return ".".join(part.replace("-", "_") for part in module_parts)
