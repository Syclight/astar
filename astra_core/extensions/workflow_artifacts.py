from astra_core.extensions.api import ExtensionContext
from astra_core.runtime.artifacts import register_workflow_artifact_hook


def register_extension(context: ExtensionContext) -> None:
    """Attach standard artifact persistence to the active project run."""
    register_workflow_artifact_hook(context.orchestrator.hook_manager, output_dir=context.output_dir)
