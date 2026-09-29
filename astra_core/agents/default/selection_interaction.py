from typing import Protocol


class SelectionInteractor(Protocol):
    """Selection Agent 的输入端口，可由终端、Web 或测试替换。"""

    def read(self, prompt: str) -> str:
        """Return one user response for the supplied prompt."""


class ConsoleSelectionInteractor:
    """SelectionInteractor 的默认终端实现。"""

    def read(self, prompt: str) -> str:
        return input(prompt)


class ScriptedSelectionInteractor:
    """按顺序提供预设输入，适用于自动化测试和非交互式运行。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)

    def read(self, prompt: str) -> str:
        del prompt
        try:
            return next(self._responses)
        except StopIteration as exc:
            raise RuntimeError("ScriptedSelectionInteractor 缺少可用输入。") from exc
