import importlib
from typing import List

from astra_core.extensions.api import ExtensionContext
from astra_core.core.orchestrator import OrchestratorAgent
from astra_core.project import BusinessProject


def load_project_extensions(
    project: BusinessProject,
    orchestrator: OrchestratorAgent,
    output_dir: str,
) -> ExtensionContext:
    context = ExtensionContext(project=project, orchestrator=orchestrator, output_dir=output_dir)
    for module_name in project.extensions:
        register_extension(module_name, context)
    return context


def validate_project_extensions(project: BusinessProject) -> List[str]:
    validated: List[str] = []
    for module_name in project.extensions:
        module = importlib.import_module(module_name)
        if not callable(getattr(module, "register_extension", None)):
            raise ValueError(f"扩展 {module_name} 必须暴露 register_extension(context) 函数")
        validated.append(module_name)
    return validated


def register_extension(module_name: str, context: ExtensionContext) -> None:
    module = importlib.import_module(module_name)
    register = getattr(module, "register_extension", None)
    if not callable(register):
        raise ValueError(f"扩展 {module_name} 必须暴露 register_extension(context) 函数")
    register(context)
    context.loaded_extensions.append(module_name)
