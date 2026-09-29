"""Self-contained JSON contracts; no network schema resolution."""
import json
from jsonschema import Draft202012Validator


def check_schema(schema):
    def walk(value):
        if isinstance(value, dict):
            if any(key in value for key in ('$ref', '$dynamicRef')):
                raise ValueError('当前版本只接受自包含 Schema，不支持 $ref/$dynamicRef')
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(schema)
    Draft202012Validator.check_schema(schema)


def parse_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('模型 JSON 存在重复字段')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('模型 JSON 包含非有限数值')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def complete_json(client, messages, schema, *, on_progress=None):
    """Preserve compatibility with user-supplied complete-only clients."""
    if on_progress is not None and callable(getattr(type(client), 'complete_json_with_progress', None)):
        return client.complete_json_with_progress(messages, schema, on_progress)
    method = getattr(type(client), 'complete_json', None)
    return client.complete_json(messages, schema) if callable(method) else client.complete(messages)
