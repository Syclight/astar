"""Load blueprint data without importing any user code."""
import json
from pathlib import Path

import yaml

from astra_designer.contracts.validation import BlueprintError, Issue, ValidationResult


class BlueprintLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValueError("蓝图字段名必须是字符串")
        if key in mapping:
            raise ValueError(f"蓝图字段重复: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


BlueprintLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def load_blueprint(path: str | Path) -> dict:
    try:
        value = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=BlueprintLoader)
        # Reject YAML-only objects (dates, sets, recursive aliases, NaN, etc.).
        json.dumps(value, allow_nan=False)
        return value
    except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
        raise BlueprintError(ValidationResult([Issue("/", "load", str(exc))])) from exc
