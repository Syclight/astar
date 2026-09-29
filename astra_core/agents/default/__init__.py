"""Default agents bundled with the Astra astra_core."""

from astra_core.agents.default.selection import (
    GenericSelectionAgent,
    MultiSelectionAgent,
    SelectionAgentConfig,
)
from astra_core.agents.default.selection_interaction import (
    ConsoleSelectionInteractor,
    ScriptedSelectionInteractor,
    SelectionInteractor,
)

__all__ = [
    "GenericSelectionAgent",
    "MultiSelectionAgent",
    "ConsoleSelectionInteractor",
    "ScriptedSelectionInteractor",
    "SelectionInteractor",
    "SelectionAgentConfig",
]
