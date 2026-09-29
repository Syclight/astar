import importlib
from pathlib import Path
from typing import List

from astra_core.extensions.api import ExtensionContext


def load_all_hooks(context: ExtensionContext) -> List[str]:
    """Import and register every public Hook module in a business project's hooks directory."""
    hooks_dir = context.project.hooks_dir
    if not hooks_dir:
        raise ValueError("启用 project_hooks 时，业务项目必须配置 hooks_dir")

    hooks_path = Path(hooks_dir).resolve()
    if not hooks_path.is_dir():
        raise FileNotFoundError(f"业务项目 Hook 目录不存在: {hooks_path}")

    package_dir = _package_dir(context.project.package)
    try:
        relative_hooks_dir = hooks_path.relative_to(package_dir)
    except ValueError as exc:
        raise ValueError(f"hooks_dir 必须位于业务包目录内: {hooks_path}") from exc

    loaded_modules: List[str] = []
    for file_path in sorted(hooks_path.rglob("*.py")):
        if file_path.name == "__init__.py" or file_path.name.startswith("_"):
            continue
        module_name = _module_name(context.project.package, relative_hooks_dir, hooks_path, file_path)
        module = importlib.import_module(module_name)
        register_hooks = getattr(module, "register_hooks", None)
        if not callable(register_hooks):
            raise ValueError(f"Hook 模块必须暴露 register_hooks(context): {module_name}")
        register_hooks(context)
        loaded_modules.append(module_name)

    return loaded_modules


def _package_dir(package_name: str) -> Path:
    package = importlib.import_module(package_name)
    package_paths = getattr(package, "__path__", None)
    if package_paths:
        return Path(next(iter(package_paths))).resolve()
    package_file = getattr(package, "__file__", None)
    if package_file:
        return Path(package_file).resolve().parent
    raise ValueError(f"无法定位业务包目录: {package_name}")


def _module_name(package: str, relative_hooks_dir: Path, hooks_dir: Path, file_path: Path) -> str:
    relative_file = file_path.relative_to(hooks_dir).with_suffix("")
    return ".".join((package, *relative_hooks_dir.parts, *relative_file.parts))
