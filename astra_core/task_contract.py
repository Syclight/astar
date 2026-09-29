"""Reusable task input and pre-execution interaction contracts."""
import json
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from astra_core.llm.contracts import check_schema


def validate_contract(schema=None, interaction=None):
    if schema is None:
        if interaction is not None:
            raise ValueError('interaction 需要同时声明 task_input')
        return
    try:
        check_schema(schema)
    except (SchemaError, TypeError) as exc:
        raise ValueError('无效的任务输入 JSON Schema') from exc
    if not isinstance(schema, dict) or schema.get('type') != 'object' or not isinstance(schema.get('properties'), dict):
        raise ValueError('task_input 必须是声明 properties 的 object JSON Schema')
    if schema.get('additionalProperties') is not False:
        raise ValueError('task_input 必须设置 additionalProperties: false')
    if not set(schema.get('required', [])) <= schema['properties'].keys():
        raise ValueError('task_input.required 引用了未定义字段')
    for key, spec in schema['properties'].items():
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key):
            raise ValueError('任务输入字段必须使用小写英文标识')
        if not isinstance(spec, dict) or spec.get('type') not in ('string', 'number', 'integer', 'boolean', 'array', 'object'):
            raise ValueError('任务字段必须声明明确的 JSON type')
    for problem in condition_problems(schema):
        raise ValueError(problem)
    if interaction is None:
        return
    if not isinstance(interaction, dict) or set(interaction) - {'on_missing', 'confirm_before_run', 'questions'}:
        raise ValueError('interaction 仅支持 on_missing、confirm_before_run、questions')
    if interaction.get('on_missing', 'ask') not in {'ask', 'error'}:
        raise ValueError('on_missing 必须是 ask 或 error')
    if type(interaction.get('confirm_before_run', False)) is not bool:
        raise ValueError('confirm_before_run 必须是布尔值')
    questions = interaction.get('questions', {})
    if not isinstance(questions, dict) or not set(questions) <= schema['properties'].keys() or not all(
            isinstance(q, str) and q.strip() for q in questions.values()):
        raise ValueError('questions 必须为已定义字段到非空问题的映射')


REQUIRED_WHEN = 'x-astra-required-when'


def condition_problems(schema):
    """x-astra-required-when: [{"field": "mode", "values": ["new"], "required": ["genre"]}, ...] — fields that are
    required only when another field has one of the given values (e.g. a topic for a new book only)."""
    rules = schema.get(REQUIRED_WHEN)
    if rules is None:
        return []
    fields = schema['properties']
    if not isinstance(rules, list) or not rules:
        return [f'{REQUIRED_WHEN} 必须是非空数组']
    problems = []
    for number, rule in enumerate(rules, 1):
        where = f'{REQUIRED_WHEN} 第 {number} 条'
        if not isinstance(rule, dict) or set(rule) != {'field', 'values', 'required'}:
            problems.append(f'{where} 必须恰好包含 field、values、required')
            continue
        if rule['field'] not in fields:
            problems.append(f"{where} 的 field {rule['field']} 不是 task_input 的字段")
            continue
        allowed = fields[rule['field']].get('enum')
        if not isinstance(rule['values'], list) or not rule['values'] or \
                any(isinstance(value, (dict, list)) for value in rule['values']):
            problems.append(f'{where} 的 values 必须是非空的取值数组')
        elif allowed is not None and any(value not in allowed for value in rule['values']):
            problems.append(f"{where} 的 values 必须是字段 {rule['field']} 的可选值：{allowed}")
        required = rule['required']
        if not isinstance(required, list) or not required or not all(isinstance(key, str) for key in required):
            problems.append(f'{where} 的 required 必须是非空的字段名数组')
        elif set(required) - fields.keys():
            problems.append(f"{where} 的 required 引用了未定义字段：{'、'.join(sorted(set(required) - fields.keys()))}")
        elif rule['field'] in required:
            problems.append(f'{where} 的 required 不能包含条件字段本身')
    return problems


def conditionally_missing(schema, values):
    """{field: reason} for fields that the chosen option makes necessary but were not given."""
    missing = {}
    for rule in schema.get(REQUIRED_WHEN) or []:
        if rule['field'] in values and values[rule['field']] in rule['values']:
            for key in rule['required']:
                if values.get(key) in (None, '', []) and key not in missing:
                    missing[key] = f"{rule['field']} 为 {values[rule['field']]} 时需要提供 {key}"
    return missing


def input_issues(schema, values, interaction):
    if not isinstance(values, dict):
        raise ValueError('本次任务输入必须是 JSON object')
    json.dumps(values, allow_nan=False)
    errors = list(Draft202012Validator(schema).iter_errors(values))
    fields = set(schema.get('required', [])) - values.keys()
    fields.update(str(e.path[0]) for e in errors if e.path)
    messages = [e.message for e in errors]
    for field, reason in conditionally_missing(schema, values).items():
        fields.add(field)
        messages.append(reason)
    for field, spec in schema['properties'].items():
        kind = spec.get('x-astra-path')
        if not kind or field in fields or values.get(field) in (None, '', []):
            continue
        # A mistyped path is asked again now, instead of failing halfway through the run.
        for item in values[field] if isinstance(values[field], list) else [values[field]]:
            if not isinstance(item, str):
                continue
            from pathlib import Path
            path = Path(item.strip().strip('"')).expanduser()
            if not (path.is_file() if kind == 'file' else path.is_dir()):
                messages.append(f"{field}：找不到{'文件' if kind == 'file' else '文件夹'} {item.strip()}")
                fields.add(field)
    questions = []
    for field, spec in schema['properties'].items():
        if field in fields:
            questions.append({'field': field, 'question': interaction.get('questions', {}).get(
                field, spec.get('description') or f'请提供 {field}'), 'schema': spec})
    return messages, questions
