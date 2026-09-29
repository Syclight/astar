"""Small proposal responses assembled without weakening the final contract."""
from copy import deepcopy
import json

from jsonschema import Draft202012Validator

from astra_designer.contracts.requirements import validate_record
from astra_designer.prompts import discovery_prompt

STEPS = ('requirements', 'team', 'contract')


def object_schema(properties):
    return {'type': 'object', 'additionalProperties': False,
            'required': list(properties), 'properties': properties}


def request_section(request, draft):
    request = deepcopy(request)
    context = json.loads(request[-1]['content'])
    properties = context['response_schema']['properties']
    step = draft['step']
    if step == 'requirements':
        schema = object_schema({k: properties[k] for k in ('summary', 'items', 'issues', 'questions')})
    elif step == 'team':
        schema = object_schema({'team_proposal': deepcopy(properties['team_proposal'])})
    else:
        schema = object_schema({k: properties[k] for k in ('task_input', 'interaction', 'deliverables')})
    if step != 'requirements':
        clarification = object_schema({k: deepcopy(properties[k]) for k in ('summary', 'issues', 'questions')})
        clarification['properties']['issues']['minItems'] = 1
        clarification['properties']['questions']['minItems'] = 1
        schema = {'anyOf': [schema, object_schema({'clarification': clarification})]}
    context.update(section=step, accepted_sections=draft['record'], response_schema=schema)
    request[-1]['content'] = json.dumps(context, ensure_ascii=False)
    request[0]['content'] = discovery_prompt('proposal', section=step) + (
        f'\n本轮分步生成。当前步骤为 {step}，只输出本次 response_schema 的字段；'
        'accepted_sections 是已通过校验的前序结果，无需重写，由程序组装。'
        '内部字段、角色分类和格式错误自行修复；已回答或已授权团队决定的事项不再询问；只在运行时才需要的素材写进 task_input，不提前索要。')
    if step == 'requirements':
        request[0]['content'] += '\n结合最新用户消息更新需求，需要时用本步骤的 issues/questions 提问。'
    else:
        request[0]['content'] += ('\n发现必须由用户决定的冲突或缺失时，返回 clarification 分支：简短 summary、issues 和一至两个具体问题，'
                                 '说明缺什么、为何影响设计，尽量给出选项，不猜测用户的决定。')
        if step == 'contract':
            request[0]['content'] += '\ndeliverables 与已接受团队方案的交付成果一致，写清内容和形式。'
    if context.get('repair_candidate') is not None:
        request[0]['content'] += ('\nrepair_candidate 是本步骤上次未通过校验的回复；按 validation_feedback 修正，'
                                 '保留其余有效内容，只返回本步骤的完整 JSON，不重写 accepted_sections。')
    return request


def assemble(draft, section, schema, transcript):
    if 'anyOf' in schema:
        schema = schema['anyOf'][1 if isinstance(section, dict) and 'clarification' in section else 0]
    errors = list(Draft202012Validator(schema).iter_errors(section))
    if errors:
        error = errors[0]
        path = '/' + '/'.join(map(str, error.absolute_path))
        raise ValueError(f'当前步骤 {draft["step"]} 结构无效 {path}: {error.message}')
    record = deepcopy(draft['record'])
    if 'clarification' in section:
        # Publish only requirements and questions; the partial plan is kept
        # separately and must never pass as a confirmed, executable proposal.
        record = {k: record[k] for k in ('summary', 'items', 'issues', 'questions')}
        clarification = section['clarification']
        issues = {issue['id']: issue for issue in record['issues']}
        issues.update({issue['id']: issue for issue in clarification['issues']})
        if len({issue['id'] for issue in clarification['issues']}) != len(clarification['issues']):
            raise ValueError('澄清事项 ID 重复')
        record.update(summary=clarification['summary'], issues=list(issues.values()), questions=clarification['questions'])
        validate_record(record, transcript)
        return record
    record.update(section)
    validate_record(record, transcript)
    return record
