from copy import deepcopy
from typing import Any, Dict, Optional
from contextvars import ContextVar


_default_llm_config: ContextVar[Optional[Dict[str, Any]]] = ContextVar('llm_config', default=None)


def set_default_llm_config(config: Dict[str, Any]) -> None:
    _default_llm_config.set(deepcopy(config))


def get_default_llm_config() -> Dict[str, Any]:
    config = _default_llm_config.get()
    if config is None:
        raise RuntimeError(
            "未配置默认 LLM 参数。请在业务启动器中调用 "
            "astra_core.runtime.settings.set_default_llm_config(...)。"
        )
    return deepcopy(config)
