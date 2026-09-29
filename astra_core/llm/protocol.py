from dataclasses import dataclass, field
from typing import Protocol


class ModelError(RuntimeError):
    """Sanitized transport or response error suitable for session reports."""

    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = details or {}


@dataclass(frozen=True)
class ModelReply:
    content: str
    usage: dict = field(default_factory=dict)


class DesignModel(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> ModelReply: ...
