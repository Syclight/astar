from typing import Any, Dict, Mapping, MutableMapping, TypedDict, cast


JsonObject = Dict[str, Any]


class OrchestratorData(TypedDict, total=False):
    """Dynamic shared data carried in ``state["data"]`` across agents and hooks.

    The runtime only requires string keys and does not know business-specific
    fields. Domain schemas should live with the application package that owns
    those fields.
    """


KNOWN_ORCHESTRATOR_DATA_KEYS = frozenset(OrchestratorData.__annotations__)


def new_orchestrator_data() -> OrchestratorData:
    return cast(OrchestratorData, {})


def coerce_orchestrator_data(value: Mapping[str, Any] | None) -> OrchestratorData:
    if value is None:
        return new_orchestrator_data()
    return cast(OrchestratorData, dict(value))


def merge_orchestrator_data(
    target: MutableMapping[str, Any],
    patch: Mapping[str, Any],
) -> OrchestratorData:
    for key, value in patch.items():
        if not isinstance(key, str):
            raise TypeError(f'state["data"] keys must be str, got {type(key).__name__}')
        target[key] = value
    return cast(OrchestratorData, target)
