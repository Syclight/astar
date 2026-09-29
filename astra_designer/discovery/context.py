from astra_designer.contracts.requirements import response_schema, ready
from astra_designer.prompts import discovery_prompt
import json


def messages(state, feedback, repair_candidate=None, *, phase_override=None):
    previous = state.get('document')
    phase = phase_override or ('proposal' if previous and ready(previous) else 'clarify')
    schema = response_schema()
    schema['properties']['summary']['maxLength'] = 400
    schema['properties']['questions']['maxItems'] = 2
    schema['properties']['items']['minItems'] = 1
    from astra_designer.contracts.team import model_team_schema
    team = model_team_schema(schema['properties']['team_proposal'])
    # A review role is conditional on the requirements, so only delivery is structural.
    team['properties']['options']['items']['properties']['roles']['contains'] = {
        'type': 'object', 'properties': {'priority': {'const': 'delivery'}}, 'required': ['priority']}
    # Bind each quotation to one real user turn, rather than asking the model
    # to reproduce arbitrary text and turn numbers independently.
    sources = []
    for entry in state['transcript']:
        if entry['role'] != 'user' or entry.get('command'):
            continue  # command expansions are not the user's own words
        quotes = [entry['text'][start:start + 6000]
                  for start in range(0, len(entry['text']), 6000)]
        # Preserve already validated quotations rather than expanding them to
        # entire turns whenever the proposal is regenerated.
        for item in (previous or {}).get('items', []):
            for source in item.get('sources', []):
                if source.get('turn') == entry['turn'] and source.get('quote') in entry['text']:
                    quotes.append(source['quote'])
        quotes = list(dict.fromkeys(quotes))
        quotes = [quote for quote in quotes if quote.strip()]
        if quotes:
            sources.append({'type': 'object', 'additionalProperties': False,
                            'required': ['turn', 'quote'], 'properties': {
                                'turn': {'const': entry['turn']},
                                'quote': {'enum': quotes}}})
    if sources:
        schema['properties']['items']['items']['properties']['sources']['items'] = {'anyOf': sources}
    prompt = discovery_prompt(phase, include_contract=bool(previous and previous.get('task_input')))
    if phase == 'proposal':
        task = schema['properties']['task_input']
        task['required'] = ['type', 'properties', 'required', 'additionalProperties']
        task['properties']['properties']['propertyNames'] = {'pattern': '^[a-z][a-z0-9_]{0,63}$'}
        schema['anyOf'] = [
            {'properties': {'questions': {'minItems': 1}}},
            {'required': ['team_proposal', 'task_input', 'interaction', 'deliverables']}]
        schema['properties']['interaction']['required'] = ['on_missing', 'confirm_before_run', 'questions']
    if phase == 'clarify':
        schema['properties'].pop('team_proposal', None)
        if not previous or not previous.get('task_input'):
            schema['properties'].pop('task_input', None)
            schema['properties'].pop('interaction', None)
            schema['properties'].pop('deliverables', None)
            prompt += '\n本轮省略 task_input、interaction 和 deliverables，它们在方案阶段定义，不要输出空对象占位。\n'
        # Old sessions can contain premature team plans. Do not seed another one.
        if previous:
            previous = {key: val for key, val in previous.items() if key != 'team_proposal'}
    value = {'phase': phase, 'transcript': state['transcript'], 'previous': previous,
             'samples': {key: {'schema': item['schema']} for key, item in state.get('samples', {}).items()},
             'validation_feedback': feedback,
             'response_schema': schema}
    if phase == 'proposal':
        from astra_designer.catalog.registry import capabilities
        value['installed_capability_packs'] = [
            {'id': identifier, 'description': spec['description'], 'availability': spec['availability']}
            for identifier, spec in capabilities().items() if spec.get('source') == 'pack']
    if state.get('clarification_draft'):
        value['previous_proposal_draft'] = state['clarification_draft']
        prompt += '\n用户正在回答方案设计中的问题：结合最新回答更新需求、来源和对应 issue。previous_proposal_draft 仅供参考，受新回答影响的部分须重新核对。\n'
    if repair_candidate is not None:
        value['repair_candidate'] = repair_candidate
        prompt += ('\n本轮是定向修复：repair_candidate 是上次未通过校验的回答，不是已确认需求。'
                   '只修正 validation_feedback 指出的问题，保留其余有效内容，返回完整 JSON；内部字段或分类问题自行修复，不问用户。\n')
    return [{'role': 'system', 'content': prompt},
            {'role': 'user', 'content': json.dumps(value, ensure_ascii=False)}]
