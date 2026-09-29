"""Minimal project Hook: annotate successful Agent output with its project."""
from astra_core.runtime.hooks import HookEvent, HookResult


def register_hooks(context):
    project_name = context.project.name

    def annotate(context):
        return HookResult(patch={"example_project_name": project_name})

    context.orchestrator.register_hook(
        HookEvent.POST_AGENT, annotate, name="example_project_annotation"
    )
