"""Execution-local services; never change process working directory or environment."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from copy import deepcopy

from astra_core.tools.base import ToolRegistry, _active_registry
from astra_core.runtime.settings import _default_llm_config
from astra_core.runtime.hooks import active_hook_manager, HookManager

project_directory = ContextVar('project_directory', default=None)
project_model_config = ContextVar('project_model_config', default=None)


def resolve_resource(path):
    path = Path(path)
    root = project_directory.get()
    return root / path if root is not None and not path.is_absolute() else path


@contextmanager
def project_context(project):
    values = [
        (_active_registry, ToolRegistry()),
        (_default_llm_config, deepcopy(project.llm_config)),
        (active_hook_manager, HookManager(profile='minimal')),
        (project_directory, Path(project.base_dir)),
        (project_model_config, deepcopy(project.model_config)),
    ]
    tokens = [(variable, variable.set(value)) for variable, value in values]
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)
