import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from astra_designer.catalog.registry import resource_text


def resolve_pointer(value, pointer):
    for token in pointer.split('/')[1:]:
        key = token.replace('~1', '/').replace('~0', '~')
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def verify(project_file: Path, state: dict) -> dict:
    checks = yaml.safe_load((project_file.parent / 'tests/acceptance.yaml').read_text(encoding='utf-8'))
    schema = json.loads(resource_text('blueprint.schema.json'))
    Draft202012Validator({**schema, 'required': [], 'properties': {'acceptance': schema['properties']['acceptance']}}).validate({'acceptance': checks})
    results = []
    for check in checks:
        try:
            actual = resolve_pointer(state, check['path'])
            if check['op'] == 'equals':
                passed = json.dumps(actual, sort_keys=True, ensure_ascii=False) == json.dumps(check['value'], sort_keys=True, ensure_ascii=False)
            elif check['op'] == 'matches_schema':
                from astra_core.llm.contracts import check_schema
                check_schema(check['value'])
                passed = Draft202012Validator(check['value']).is_valid(actual)
            else:
                output_root = Path(state['data']['run']['output_dir']).resolve()
                paths = actual if isinstance(actual, list) else [actual]  # one file per chapter gives a list
                passed = bool(paths) and all(Path(item).resolve().is_relative_to(output_root) and Path(item).resolve().is_file()
                                             for item in paths)
            results.append({**check, 'passed': passed, 'actual': actual})
        except (KeyError, IndexError, ValueError, TypeError, OSError) as exc:
            results.append({**check, 'passed': False, 'error': str(exc)})
    return {'passed': state.get('status') == 'completed' and all(item['passed'] for item in results),
            'runtime_status': state.get('status'), 'checks': results}
