from astra_core.extensions.api import ExtensionContext
from astra_core.tools.loader import load_all_tools


def register_extension(context: ExtensionContext) -> None:
    """Load a business project's framework-neutral tool specifications."""
    loaded_modules = load_all_tools(context.project.tools_dir)
    context.provide("tool_modules", loaded_modules)
