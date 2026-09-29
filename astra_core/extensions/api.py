from dataclasses import dataclass, field
from typing import Any, Dict, List

from astra_core.core.orchestrator import OrchestratorAgent
from astra_core.project import BusinessProject


@dataclass
class ExtensionContext:
    """Runtime services available to one project extension during registration."""

    project: BusinessProject
    orchestrator: OrchestratorAgent
    output_dir: str
    services: Dict[str, Any] = field(default_factory=dict)
    loaded_extensions: List[str] = field(default_factory=list)

    def provide(self, name: str, value: Any) -> None:
        self.services[name] = value

    def require(self, name: str) -> Any:
        if name not in self.services:
            raise RuntimeError(f"扩展依赖未提供: {name}")
        return self.services[name]
