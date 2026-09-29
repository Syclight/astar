from dataclasses import dataclass
from typing import Any, Callable, Dict, List
from contextvars import ContextVar

import json5


ToolHandler = Callable[..., Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: List[Dict[str, Any]]
    handler: ToolHandler


class ToolRegistry:
    """在将工具适配到任何代理SDK之前，使用与框架无关的工具注册表。"""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        # Allow reloads during local debugging; the latest imported definition wins.
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        return self._tools[name]

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def all(self) -> List[ToolSpec]:
        return list(self._tools.values())


_active_registry: ContextVar[ToolRegistry | None] = ContextVar('tool_registry', default=None)


class ContextToolRegistry:
    def __getattr__(self, name):
        registry = _active_registry.get()
        if registry is None:
            registry = ToolRegistry()
            _active_registry.set(registry)
        return getattr(registry, name)


tool_registry = ContextToolRegistry()


def register_tool(spec: ToolSpec) -> ToolSpec:
    return tool_registry.register(spec)


def load_params(params: Any) -> Dict[str, Any]:
    if isinstance(params, dict):
        return params
    if isinstance(params, str) and params.strip():
        loaded = json5.loads(params)
        return loaded if isinstance(loaded, dict) else {}
    return {}
