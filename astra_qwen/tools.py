from typing import Set
from threading import RLock
from copy import deepcopy

from qwen_agent.tools.base import BaseTool, register_tool as qwen_register_tool

from astra_core.tools.base import ToolSpec, tool_registry
from astra_core.runtime.hooks import HookEvent, get_active_execution_context, get_active_hook_manager
from astra_core.extensions.api import ExtensionContext


_registered_qwen_tools: Set[str] = set()
_registration_lock = RLock()


def register_qwen_tool(spec: ToolSpec) -> None:
    if spec.name in _registered_qwen_tools:
        return

    def initialize(self, *args, **kwargs):
        # The SDK registry stores a class globally, but every instance binds to
        # the current project's definition, including its parameter schema.
        self._astra_spec = tool_registry.get(spec.name)
        self.description = self._astra_spec.description
        self.parameters = deepcopy(self._astra_spec.parameters)
        BaseTool.__init__(self, *args, **kwargs)

    def call(self, params, **kwargs):
        hook_manager = get_active_hook_manager()
        execution_context = get_active_execution_context()
        tool_context = {
            **execution_context,
            "event_type": HookEvent.PRE_TOOL_USE,
            "tool_name": spec.name,
            "tool_params": params,
            "tool_kwargs": kwargs,
        }
        pre_result = hook_manager.run(HookEvent.PRE_TOOL_USE, tool_context)
        if pre_result.block:
            raise RuntimeError(pre_result.reason or pre_result.message or f"Tool {spec.name} blocked by hook")

        try:
            result = self._astra_spec.handler(params, **kwargs)
        except Exception as exc:
            error_context = {
                **execution_context,
                "event_type": HookEvent.ON_ERROR,
                "tool_name": spec.name,
                "tool_params": params,
                "tool_kwargs": kwargs,
                "error": str(exc),
            }
            hook_manager.run(HookEvent.ON_ERROR, error_context)
            raise

        post_context = {
            **execution_context,
            "event_type": HookEvent.POST_TOOL_USE,
            "tool_name": spec.name,
            "tool_params": params,
            "tool_kwargs": kwargs,
            "result": result,
            "status": "success",
        }
        post_result = hook_manager.run(HookEvent.POST_TOOL_USE, post_context)
        if post_result.patch and isinstance(result, dict):
            merged_result = dict(result)
            merged_result.update(post_result.patch)
            return merged_result
        return result

    tool_class_name = "".join(part.title() for part in spec.name.split("_")) + "QwenTool"
    tool_class = type(
        tool_class_name,
        (BaseTool,),
        {
            "__init__": initialize,
            "description": spec.description,
            "parameters": spec.parameters,
            "call": call,
        },
    )
    qwen_register_tool(spec.name)(tool_class)
    _registered_qwen_tools.add(spec.name)


def register_all_qwen_tools() -> None:
    with _registration_lock:
        for spec in tool_registry.all():
            register_qwen_tool(spec)


def register_extension(context: ExtensionContext) -> None:
    """Expose project tool specifications to Qwen Assistant agents."""
    register_all_qwen_tools()
