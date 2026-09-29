from astra_core.extensions.api import ExtensionContext
from astra_core.hooks import load_all_hooks


def register_extension(context: ExtensionContext) -> None:
    """Load Hook modules declared by the current business project's hooks directory."""
    loaded_modules = load_all_hooks(context)
    context.provide("hook_modules", loaded_modules)
