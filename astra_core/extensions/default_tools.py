from astra_core.extensions.api import ExtensionContext
from astra_core.tools.builtin import register_builtin_tools


def register_extension(context: ExtensionContext) -> None:
    """Register standard Tools scoped to the current business project."""
    register_builtin_tools(context.project.data_dir)
    context.provide("builtin_tool_names", ["http_request", "json_file_reader"])
