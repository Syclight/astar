import ast
import importlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from astra_core.core.base_agent import BaseAgent
from astra_core.core.orchestrator import OrchestratorAgent
from astra_core.runtime.hooks import HookManager


DEFAULT_WORKFLOW_CONFIG_PATH = "workflow.yaml"
DEFAULT_AGENT_PACKAGES: Tuple[str, ...] = ()


def load_workflow_config(path: str = DEFAULT_WORKFLOW_CONFIG_PATH) -> Dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as file:
        if config_path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml
            except ImportError as exc:
                raise RuntimeError("读取 YAML 工作流配置需要安装 PyYAML。") from exc
            config = yaml.safe_load(file)
        else:
            config = json.load(file)
    if not isinstance(config, dict):
        raise ValueError(f"工作流配置必须是 mapping/object: {config_path}")
    return config


def _require_mapping(config: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = config.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"工作流配置字段 {key} 必须是 object")
    return value


def _require_list(config: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    value = config.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"工作流配置字段 {key} 必须是 array")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"工作流配置字段 {key} 的每一项都必须是 object")
    return value


def _optional_mapping(value: Any, field_name: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"工作流配置字段 {field_name} 必须是 object")
    return dict(value)


def _load_class_from_module(module_name: str, class_name: str) -> Optional[Type[Any]]:
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name, None)
    return cls if isinstance(cls, type) else None


def _optional_string_list(value: Any, field_name: str, default: List[str]) -> List[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"工作流配置字段 {field_name} 必须是非空 string array")
    return list(value)


def _package_dir(package_name: str) -> Optional[Path]:
    try:
        package = importlib.import_module(package_name)
    except ImportError:
        return None

    package_paths = getattr(package, "__path__", None)
    if package_paths:
        return Path(next(iter(package_paths))).resolve()

    package_file = getattr(package, "__file__", None)
    if package_file:
        return Path(package_file).resolve().parent
    return None


def _module_name_from_package_file(package_name: str, package_dir: Path, path: Path) -> str:
    relative_path = path.relative_to(package_dir).with_suffix("")
    return ".".join((package_name, *relative_path.parts))


def _discover_agent_class_modules(class_name: str, agent_packages: List[str]) -> List[Tuple[str, str]]:
    matches: List[Tuple[str, str]] = []
    for package_name in agent_packages:
        package_dir = _package_dir(package_name)
        if not package_dir or not package_dir.exists():
            continue

        for path in package_dir.rglob("*.py"):
            if path.name == "__init__.py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError):
                continue

            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == class_name:
                    matches.append((_module_name_from_package_file(package_name, package_dir, path), class_name))
                    break
    return matches


def _load_short_agent_class(class_name: str, agent_packages: List[str]) -> Type[Any]:
    matches = _discover_agent_class_modules(class_name, agent_packages)

    if not matches:
        packages = ", ".join(agent_packages)
        raise ValueError(f"未找到 Agent class: {class_name}，已搜索包: {packages}")
    if len(matches) > 1:
        module_names = ", ".join(module_name for module_name, _ in matches)
        raise ValueError(f"Agent class 名称存在歧义: {class_name}，匹配模块: {module_names}")

    module_name, discovered_class_name = matches[0]
    cls = _load_class_from_module(module_name, discovered_class_name)
    if cls is None:
        raise ValueError(f"未找到 Agent class: {class_name}")
    return cls


def _load_class(class_path: str, agent_packages: List[str]) -> Type[Any]:
    if "." not in class_path:
        return _load_short_agent_class(class_path, agent_packages)

    module_name, _, class_name = class_path.rpartition(".")
    if not module_name or not class_name:
        raise ValueError(f"Agent class 路径无效: {class_path}")
    cls = _load_class_from_module(module_name, class_name)
    if cls is None:
        raise ValueError(f"未找到 Agent class: {class_path}")
    return cls


def _build_agent(agent_config: Dict[str, Any], agent_packages: List[str]) -> BaseAgent:
    role = agent_config.get("role")
    class_path = agent_config.get("class")
    if not isinstance(role, str) or not role:
        raise ValueError("Agent 配置缺少有效 role")
    if not isinstance(class_path, str) or not class_path:
        raise ValueError(f"Agent {role} 缺少有效 class")

    args = agent_config.get("args", [])
    kwargs = agent_config.get("kwargs", {})
    if not isinstance(args, list):
        raise ValueError(f"Agent {role} 的 args 必须是 array")
    if not isinstance(kwargs, dict):
        raise ValueError(f"Agent {role} 的 kwargs 必须是 object")

    cls = _load_class(class_path, agent_packages)
    agent = cls(*args, **kwargs)
    if not isinstance(agent, BaseAgent):
        raise ValueError(f"Agent {role} 必须继承 BaseAgent: {class_path}")
    return agent


def _optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"{field_name} 必须是 int")
    return value


def build_orchestrator_from_config(
    path: str = DEFAULT_WORKFLOW_CONFIG_PATH,
    output_dir_override: Optional[str] = None,
    hook_manager: Optional[HookManager] = None,
) -> OrchestratorAgent:
    config = load_workflow_config(path)
    if output_dir_override:
        _apply_output_dir_override(config, output_dir_override)
    runtime_config = _require_mapping(config, "runtime")
    agent_packages = _optional_string_list(
        runtime_config.get("agent_packages"),
        "runtime.agent_packages",
        list(DEFAULT_AGENT_PACKAGES),
    )
    orchestrator_config = _require_mapping(config, "orchestrator")
    outputs_config = _require_mapping(config, "outputs")

    stage_routing = orchestrator_config.get("stage_routing", {})
    if not isinstance(stage_routing, dict):
        raise ValueError("orchestrator.stage_routing 必须是 object")

    orchestrator = OrchestratorAgent(
        stage_routing=dict(stage_routing),
        max_retries=int(orchestrator_config.get("max_retries", 3)),
        hook_profile=str(orchestrator_config.get("hook_profile", "minimal")),
        workflow_flow=_optional_mapping(orchestrator_config.get("flow"), "orchestrator.flow"),
        output_dir=str(outputs_config.get("output_dir", "output")),
        hook_manager=hook_manager,
    )

    try:
        registered_roles = set()
        for agent_config in _require_list(config, "agents"):
            role = str(agent_config["role"])
            orchestrator.register_agent(
                role,
                _build_agent(agent_config, agent_packages),
                next_role=agent_config.get("next_role"),
                max_retries=_optional_int(agent_config.get("max_retries"), f"workflow.agents.{role}.max_retries"),
            )
            registered_roles.add(role)

        for stage_config in _require_list(config, "stages"):
            stage_id = stage_config.get("stage_id")
            agent_roles = stage_config.get("agents")
            if not isinstance(stage_id, str) or not stage_id:
                raise ValueError("Stage 配置缺少有效 stage_id")
            if not isinstance(agent_roles, list) or not all(isinstance(role, str) for role in agent_roles):
                raise ValueError(f"Stage {stage_id} 的 agents 必须是 string array")
            missing_roles = sorted(set(agent_roles) - registered_roles)
            if missing_roles:
                raise ValueError(f"Stage {stage_id} 引用了未注册 Agent: {', '.join(missing_roles)}")

            orchestrator.register_stage(
                stage_id=stage_id,
                agent_roles=agent_roles,
                next_stage=stage_config.get("next_stage"),
                mode=str(stage_config.get("mode", "serial")),
                flow=_optional_mapping(stage_config.get("flow"), f"stages.{stage_id}.flow"),
                message=stage_config.get("message"),
            )
            outcome_routes = stage_config.get("outcome_routes", {})
            if not isinstance(outcome_routes, dict):
                raise ValueError(f"stages.{stage_id}.outcome_routes 必须是 object")
            orchestrator.stages[stage_id]["outcome_routes"] = outcome_routes

    except BaseException:
        orchestrator.close()
        raise

    return orchestrator


def _apply_output_dir_override(config: Dict[str, Any], output_dir: str) -> None:
    """Route all standard artifacts for one workflow execution into its run directory."""
    outputs_config = _require_mapping(config, "outputs")
    outputs_config["output_dir"] = output_dir
    outputs_config["structure_figure_path"] = os.path.join(output_dir, "orchestrator_structure.png")
    config["outputs"] = outputs_config



def get_structure_figure_path(
    path: str = DEFAULT_WORKFLOW_CONFIG_PATH,
    output_dir_override: Optional[str] = None,
) -> str:
    config = load_workflow_config(path)
    if output_dir_override:
        _apply_output_dir_override(config, output_dir_override)
    outputs_config = _require_mapping(config, "outputs")
    output_dir = str(outputs_config.get("output_dir", "output"))
    return str(outputs_config.get("structure_figure_path", os.path.join(output_dir, "orchestrator_structure.png")))


def describe_workflow_config(path: str = DEFAULT_WORKFLOW_CONFIG_PATH) -> Dict[str, Any]:
    """Describe a workflow without importing its business Agent classes."""
    config = load_workflow_config(path)
    orchestrator_config = _require_mapping(config, "orchestrator")
    agents_by_role = {
        str(agent.get("role")): agent
        for agent in _require_list(config, "agents")
        if isinstance(agent.get("role"), str)
    }
    stages = []
    for stage_config in _require_list(config, "stages"):
        roles = stage_config.get("agents", [])
        stages.append(
            {
                "stage_id": stage_config.get("stage_id"),
                "message": stage_config.get("message"),
                "mode": (stage_config.get("flow") or {}).get("type", stage_config.get("mode", "serial")),
                "next_stage": stage_config.get("next_stage"),
                "agents": [
                    {
                        "role": role,
                        "class_name": agents_by_role.get(role, {}).get("class"),
                        "max_retries": agents_by_role.get(role, {}).get("max_retries"),
                    }
                    for role in roles
                ],
            }
        )
    return {
        "orchestrator": {
            "class_name": "OrchestratorAgent",
            "max_retries": orchestrator_config.get("max_retries", 3),
            "start_stage": orchestrator_config.get("stage_routing", {}).get("init"),
        },
        "stages": stages,
        "stage_routing": dict(orchestrator_config.get("stage_routing", {})),
    }
