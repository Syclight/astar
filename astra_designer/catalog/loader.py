"""Load reusable Agent recipes as data. Never import catalog code."""
import hashlib
import json
import os
from pathlib import Path

from jsonschema import Draft202012Validator

MANIFEST_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['id', 'title', 'description', 'tags', 'base', 'parameters'],
    'properties': {
        'id': {'type': 'string', 'pattern': r'^[a-z][a-z0-9_.-]*@[1-9][0-9]*$'},
        'title': {'type': 'string', 'minLength': 1},
        'description': {'type': 'string', 'minLength': 1},
        'tags': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'uniqueItems': True},
        'base': {'type': 'string', 'minLength': 1},
        'parameters': {'type': 'object'},
    },
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def load_recipes(builtin):
    paths = os.environ.get('ASTRA_CAPABILITY_DIRS')
    roots = [Path(p).resolve() for p in paths.split(os.pathsep) if p] if paths else [Path('workspace/capabilities').resolve()]
    recipes = {}
    for root in sorted(set(roots)):
        if not root.exists() and not paths:
            continue
        if not root.is_dir():
            raise ValueError(f'能力目录不存在: {root}')
        for path in sorted(root.rglob('*.capability.json')):
            if not path.resolve().is_relative_to(root):
                raise ValueError('能力文件不能通过符号链接越出目录')
            if path.stat().st_size > 200000:
                raise ValueError(f'能力描述过大: {path.name}')
            from astra_core.llm.contracts import parse_json
            value = parse_json(path.read_text(encoding='utf-8'))
            errors = list(Draft202012Validator(MANIFEST_SCHEMA).iter_errors(value))
            if errors:
                raise ValueError(f'无效能力描述 {path.name}: {errors[0].message}')
            if value['id'] in builtin or value['id'] in recipes:
                raise ValueError(f'能力 ID 重复，不能覆盖: {value["id"]}')
            if value['base'] not in builtin:
                raise ValueError(f'能力 {value["id"]} 必须引用内置 base，不能加载任意类或链式模板')
            errors = list(Draft202012Validator(builtin[value['base']]['parameters']).iter_errors(value['parameters']))
            if errors:
                raise ValueError(f'能力默认参数不完整或无效 {value["id"]}: {errors[0].message}')
            recipes[value['id']] = value
    return recipes
