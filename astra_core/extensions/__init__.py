"""Extension API for project-local tools, runtime adapters, and lifecycle hooks."""

from astra_core.extensions.api import ExtensionContext
from astra_core.extensions.loader import load_project_extensions, validate_project_extensions

__all__ = ["ExtensionContext", "load_project_extensions", "validate_project_extensions"]
