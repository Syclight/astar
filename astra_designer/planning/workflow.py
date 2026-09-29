import json
from jsonschema import Draft202012Validator

from astra_designer.catalog.registry import capabilities, resource_text, schemas
from astra_designer.prompts import with_product_context, planning_examples, capability_guides


def generation_schema(requirements=None):
    """Constrain the blueprint itself, not just its response envelope."""
    source = json.loads(resource_text('blueprint.schema.json'))
    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if '$ref' in value:
            target = source
            for token in value['$ref'].removeprefix('#/').split('/'):
                target = target[token]
            return expand(target)
        return {key: expand(item) for key, item in value.items() if key not in {'$defs', '$schema'}}
    blueprint = expand(source)
    for key in ('requirements', 'execution'):
        blueprint['properties'].pop(key, None)
    if requirements:
        blueprint['required'] += ['traceability']
        if requirements.get('selected_team'):
            blueprint['required'] += ['team_assignment']
        for key in ('task_input', 'interaction'):
            blueprint['properties'].pop(key, None)
        if requirements['document'].get('task_input'):
            blueprint.pop('allOf', None)
    result = response_schema(design_with_dependencies=bool(requirements))
    result['oneOf'][0]['properties']['blueprint'] = blueprint
    return result


def response_schema(*, design_with_dependencies=False):
    schema = {'oneOf': [
        {'type': 'object', 'additionalProperties': False, 'required': ['status', 'summary', 'blueprint'],
         'properties': {'status': {'const': 'ready'}, 'summary': {'type': 'string', 'minLength': 1},
                        'blueprint': {'type': 'object'}}},
        {'type': 'object', 'additionalProperties': False, 'required': ['status', 'summary', 'questions'],
         'properties': {'status': {'const': 'needs_clarification'}, 'summary': {'type': 'string', 'minLength': 1},
                        'questions': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {
                            'type': 'object', 'additionalProperties': False, 'required': ['id', 'question'],
                            'properties': {'id': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]*$'},
                                           'question': {'type': 'string', 'pattern': r'\S'}}}}}},
        {'type': 'object', 'additionalProperties': False, 'required': ['status', 'summary'],
         'properties': {'status': {'const': 'unsupported'}, 'summary': {'type': 'string', 'minLength': 1}}},
    ]}
    if design_with_dependencies:
        schema['oneOf'] = schema['oneOf'][:2]
    return schema


def parse_reply(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('模型 JSON 包含重复字段')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('模型 JSON 包含非有限数值')
    result = json.loads(content, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if list(Draft202012Validator(response_schema()).iter_errors(result)):
        if not isinstance(result, dict) or result.get('status') not in {'ready', 'needs_clarification', 'unsupported'}:
            raise ValueError('status 必须是 ready、needs_clarification 或 unsupported')
        # Name the offending field, e.g. team_assignment placed beside blueprint instead of inside it.
        branch = next(b for b in response_schema()['oneOf'] if b['properties']['status']['const'] == result['status'])
        extra = sorted(set(result) - set(branch['properties']))
        missing = [key for key in branch['required'] if key not in result]
        detail = '；'.join(filter(None, [
            extra and f"顶层不能有 {'、'.join(extra)}（蓝图字段须放在 blueprint 内）",
            missing and f"缺少 {'、'.join(missing)}"]))
        if not detail:
            error = next(Draft202012Validator(branch).iter_errors(result))
            detail = '/' + '/'.join(map(str, error.absolute_path)) + ': ' + error.message
        raise ValueError(f"{result['status']} 回复不符合响应契约：{detail}")
    questions = result.get('questions', [])
    if len({q['id'] for q in questions}) != len(questions):
        raise ValueError('澄清问题标识重复')
    return result


def requirements_brief(requirements):
    """What the planner needs from a confirmed snapshot.

    Evidence quotes, unchosen team options and retired budget fields do not
    change the blueprint; sending them only lengthens the request. The full
    snapshot is still injected into the blueprint by the program.
    """
    if not requirements:
        return requirements
    document = requirements['document']
    brief = {'revision': requirements['revision'], 'document': {
        'items': [{key: item[key] for key in ('id', 'category', 'statement')} for item in document['items']],
        **{key: document[key] for key in ('task_input', 'interaction', 'deliverables') if key in document}}}
    team = requirements.get('selected_team')
    if team:
        brief['selected_team'] = {'id': team['id'], 'label': team['label'], 'deliverables': team.get('deliverables', []),
                                  'roles': [{key: role[key] for key in ('id', 'priority', 'responsibility', 'requirement_ids')}
                                            for role in team['roles']]}
    return brief


def model_catalog():
    """What the planner needs to choose and wire a capability; class names and digests are for the generator."""
    view = {}
    for identifier, spec in capabilities().items():
        entry = {key: spec[key] for key in ('title', 'description', 'inputs', 'outputs', 'parameters')}
        if spec.get('defaults'):
            entry.update(base=spec['base'], defaults=spec['defaults'])
        if spec.get('source') == 'pack':
            entry.update(availability=spec['availability'], guide=spec.get('guide', ''),
                         package={k: spec['pack'][k] for k in ('name', 'version', 'permissions')})
        view[identifier] = entry
    return view


def messages(goal, resources, history, feedback, requirements=None, previous=None):
    from astra_designer.catalog.search import search_capabilities
    recommended = [{'id': item['id'], 'title': item['title']} for item in search_capabilities(goal, limit=5) if item['score']]
    context = {'confirmed_requirements': requirements_brief(requirements), 'recommended_capabilities': recommended,
               'goal': goal, 'resources': resources, 'history': history, 'validation_feedback': feedback,
               'blueprint_schema': generation_schema(requirements)['oneOf'][0]['properties']['blueprint'],
               'capabilities': model_catalog(), 'data_schemas': schemas(),
               'reference_examples': planning_examples()}
    context['capability_guides'] = capability_guides(
        {'goal': goal, 'requirements': requirements_brief(requirements), 'history': history,
         'feedback': feedback, 'previous': previous}, context['capabilities'])
    prompt = resource_text('designer_prompt.md')
    if previous is not None:
        context['previous_response'] = previous
        prompt += ('\n本轮是定向修复：previous_response 是上次未通过校验的完整回复。'
                   '只修改 validation_feedback 指出的问题，其余内容保持原样，返回修复后的完整 JSON，不要从头重新设计。')
    return [{'role': 'system', 'content': with_product_context(prompt)},
            {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]
