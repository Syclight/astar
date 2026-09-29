from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Issue:
    path: str
    code: str
    message: str


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {"valid": self.valid, "issues": [asdict(issue) for issue in self.issues]}

    def require_valid(self) -> None:
        if not self.valid:
            raise BlueprintError(self)


class BlueprintError(ValueError):
    def __init__(self, result: ValidationResult):
        self.result = result
        super().__init__("; ".join(f"{i.path}: {i.message}" for i in result.issues))
