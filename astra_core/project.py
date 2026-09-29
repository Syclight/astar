import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_PROJECT_CONFIG_PATH = "business/example_project/project.yaml"


@dataclass(frozen=True)
class BusinessProject:
    name: str
    package: str
    package_root: str
    project_file: str
    base_dir: str
    workflow_config: str
    tools_dir: str
    hooks_dir: Optional[str]
    paths: Dict[str, str]
    initial_task: str
    extensions: List[str]
    distribution: Dict[str, Any]
    llm_config: Optional[Dict[str, Any]] = None
    task_input: Optional[Dict[str, Any]] = None
    interaction: Optional[Dict[str, Any]] = None
    model_config: Optional[Dict[str, Any]] = None

    @property
    def output_dir(self) -> str:
        return self.paths.get("output", str(Path(self.base_dir) / "output"))

    @property
    def data_dir(self) -> str:
        return self.paths.get("data", str(Path(self.base_dir) / "data"))

    @property
    def prompts_dir(self) -> str:
        return self.paths.get("prompts", str(Path(self.base_dir) / "prompts"))


def load_business_project(path: str = DEFAULT_PROJECT_CONFIG_PATH) -> BusinessProject:
    project_file = resolve_project_file(path)
    config = _load_project_config(project_file)
    base_dir = project_file.parent

    name = _require_string(config, "name")
    package = _require_string(config, "package")
    package_root = _activate_project_package(project_file, package)
    workflow_config = _resolve_project_path(base_dir, _require_string(config, "workflow_config"))
    tools_dir = _resolve_project_path(base_dir, _require_string(config, "tools_dir"))
    hooks_dir = _load_optional_project_path(base_dir, config.get("hooks_dir"), "hooks_dir")
    initial_task = str(config.get("initial_task", ""))
    paths = _resolve_paths(base_dir, config.get("paths", {}))
    extensions = _load_extensions(config)
    distribution = _load_distribution(config)
    llm_config = _load_llm_config(config)
    from astra_core.task_contract import validate_contract
    validate_contract(config.get('task_input'), config.get('interaction'))
    from astra_core.llm.project_config import load_profile
    model_config = load_profile(base_dir, config['model_config']) if 'model_config' in config else None

    return BusinessProject(
        name=name,
        package=package,
        package_root=package_root,
        project_file=str(project_file),
        base_dir=str(base_dir),
        workflow_config=workflow_config,
        tools_dir=tools_dir,
        hooks_dir=hooks_dir,
        paths=paths,
        initial_task=initial_task,
        extensions=extensions,
        distribution=distribution,
        llm_config=llm_config,
        task_input=config.get('task_input'),
        interaction=config.get('interaction'),
        model_config=model_config,
    )


PROJECT_FILE_NAMES = ("project.yaml", "project.yml", "project.json")


def resolve_project_file(path: str | Path) -> Path:
    """Accept either a project config file or the project directory that contains it."""
    project_file = Path(path).resolve()
    if not project_file.is_dir():
        return project_file
    for name in PROJECT_FILE_NAMES:
        if (project_file / name).is_file():
            return project_file / name
    raise FileNotFoundError(f"目录中未找到业务项目配置（{' / '.join(PROJECT_FILE_NAMES)}）: {project_file}")


def _activate_project_package(project_file: Path, package: str) -> str:
    """Allow an external business package to be imported from its project file."""
    package_parts = package.split(".")
    for candidate in (project_file.parent, *project_file.parents):
        if candidate.joinpath(*package_parts).is_dir():
            package_root = str(candidate)
            if package_root not in sys.path:
                sys.path.insert(0, package_root)
            return package_root

    raise ValueError(
        f"业务项目 package 与目录不匹配: {package} (配置文件: {project_file})"
    )


def _load_project_config(project_file: Path) -> Dict[str, Any]:
    if not project_file.exists():
        raise FileNotFoundError(f"业务项目配置不存在: {project_file}")

    with project_file.open("r", encoding="utf-8") as file:
        if project_file.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml
            except ImportError as exc:
                raise RuntimeError("读取 YAML 业务项目配置需要安装 PyYAML。") from exc
            config = yaml.safe_load(file)
        else:
            config = json.load(file)

    if not isinstance(config, dict):
        raise ValueError(f"业务项目配置必须是 mapping/object: {project_file}")
    return config


def _require_string(config: Dict[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"业务项目配置缺少有效 {key}")
    return value


def _resolve_project_path(base_dir: Path, value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = base_dir / path
    return str(path)


def _resolve_paths(base_dir: Path, value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("业务项目配置字段 paths 必须是 object")

    return {
        str(name): _resolve_project_path(base_dir, str(path))
        for name, path in value.items()
        if isinstance(name, str) and isinstance(path, str) and path
    }


def _load_optional_project_path(base_dir: Path, value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"业务项目配置字段 {field_name} 必须是非空 string")
    return _resolve_project_path(base_dir, value)


def _load_llm_config(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    config_module = config.get("config_module")
    llm_config_name = config.get("llm_config")
    if config_module is None and llm_config_name is None:
        return None
    if not isinstance(config_module, str) or not config_module:
        raise ValueError("业务项目配置字段 config_module 必须是非空 string")
    if not isinstance(llm_config_name, str) or not llm_config_name:
        raise ValueError("业务项目配置字段 llm_config 必须是非空 string")

    module = importlib.import_module(config_module)
    llm_config = getattr(module, llm_config_name, None)
    if not isinstance(llm_config, dict):
        raise ValueError(f"未找到有效 LLM 配置: {config_module}.{llm_config_name}")
    return dict(llm_config)


def _load_extensions(config: Dict[str, Any]) -> List[str]:
    value = config.get("extensions", [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError("业务项目配置字段 extensions 必须是 string array")
    return list(value)


def _load_distribution(config: Dict[str, Any]) -> Dict[str, Any]:
    value = config.get("distribution", {})
    if not isinstance(value, dict):
        raise ValueError("业务项目配置字段 distribution 必须是 object")
    return dict(value)
