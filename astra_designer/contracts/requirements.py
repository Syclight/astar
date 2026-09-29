"""Requirements evidence and explicit approval contract."""
import hashlib
import json

from jsonschema import Draft202012Validator
from astra_designer.catalog.registry import resource_text


# Categories organize evidence; they are not a nine-question interview checklist.
# Other material gaps are represented by blocking issues, independently of labels.
CORE = {'goal', 'input', 'output', 'scope'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def response_schema():
    return json.loads(resource_text('requirements.schema.json'))


def validate_record(record, transcript):
    errors = list(Draft202012Validator(response_schema()).iter_errors(record))
    if errors:
        path = '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1') for part in errors[0].absolute_path)
        raise ValueError('需求响应结构无效 ' + path + ': ' + errors[0].message)
    from astra_core.task_contract import validate_contract
    validate_contract(record.get('task_input'), record.get('interaction'))
    for collection in ('items', 'issues'):
        ids = [item['id'] for item in record[collection]]
        if len(ids) != len(set(ids)):
            raise ValueError('需求或问题 ID 重复')
    from astra_designer.contracts.team import validate_team
    validate_team(record)
    validate_io(record)
    users = {item['turn']: item['text'] for item in transcript if item['role'] == 'user' and not item.get('command')}
    for item in record['items']:
        for source in item['sources']:
            if source['turn'] not in users or source['quote'] not in users[source['turn']]:
                raise ValueError('需求来源必须引用真实用户原文')
    issues = {item['id'] for item in record['issues']}
    if any(q['issue_id'] not in issues for q in record['questions']):
        raise ValueError('问题必须引用当前未决事项')
    if any(i['blocking'] for i in record['issues']) and not record['questions']:
        raise ValueError('存在阻塞事项时必须提问')


# Words that postpone what the team delivers instead of stating it.
VAGUE = ('待定', '运行时决定', '运行时确定', '视情况', '另行确定')


def validate_io(record):
    """A business team needs a definite input and definite deliverables, not placeholders."""
    task = record.get('task_input')
    if task is not None and not task.get('required'):
        raise ValueError('task_input 至少要有一个必填字段（required），否则团队每次运行没有明确的输入')
    if task is not None:
        import re
        from astra_core.task_contract import REQUIRED_WHEN
        conditional = {key for rule in task.get(REQUIRED_WHEN) or [] if isinstance(rule, dict)
                       for key in rule.get('required') or []}
        for key, spec in (task.get('properties') or {}).items():
            text = str((spec or {}).get('description', ''))
            # A field used only in one mode is still needed in that mode; left optional, it is never asked.
            if key not in task.get('required', []) and key not in conditional and '模式' in text \
                    and not re.search(r'可选|选填|可不填|可以不填|可留空', text):
                raise ValueError(
                    f'task_input.{key}（{text}）只在某种模式下使用，但既不在 required 中，也没有写进 {REQUIRED_WHEN}，'
                    f'运行时不会向用户询问。该模式下必须提供时，在 task_input 中加入 {REQUIRED_WHEN}，如 '
                    f'[{{"field": "mode", "values": ["new"], "required": ["{key}"]}}]；确实可以不填时在 description 中写明“可选”')
    deliverables = record.get('deliverables')
    if deliverables is None:
        return
    ids = [item['id'] for item in deliverables]
    if len(ids) != len(set(ids)):
        raise ValueError('交付物 ID 重复')
    items = {item['id']: item['category'] for item in record['items']}
    for item in deliverables:
        unknown = [key for key in item['requirement_ids'] if key not in items]
        if unknown:
            raise ValueError(f"交付物 {item['id']} 引用了不存在的需求 {'、'.join(unknown)}")
        vague = next((word for word in VAGUE if word in item['name'] + item['description'] + item.get('evidence', '')), None)
        if vague:
            raise ValueError(f"交付物 {item['id']} 含“{vague}”，没有写明交付什么。name 和 description 要写清成果内容与形式；"
                             '未指定的低影响细节（格式、命名等）自行选择合理默认值；仅用户要求每次选择或业务必需的内容放进 task_input，description 写“按 task_input.<字段> 生成”')
    for item in deliverables:
        if item['form'] == 'action' and len(item.get('evidence', '')) < 4:
            raise ValueError(f"交付物 {item['id']} 是动作型，evidence 要写清如何验证它确实做了，"
                             '例如“已处理文件清单与释放空间”“接口返回的受理编号”')
    covered = {key for item in deliverables for key in item['requirement_ids']}
    missing = [key for key, category in items.items() if category == 'output' and key not in covered]
    if missing:
        raise ValueError(f"输出需求 {'、'.join(missing)} 没有对应的交付物；每项 output 需求都要落到 deliverables 中")


def ready(record):
    return (CORE <= {item['category'] for item in record['items']}
            and not any(item['blocking'] for item in record['issues'])
            and not record['questions'])


def confirmed_document(state):
    document = state.get('document')
    approval = state.get('approval')
    if (state.get('status') != 'confirmed' or not document or not approval
            or approval['sha256'] != digest(document) or approval['revision'] != state['revision']):
        hint = {'awaiting_confirmation': '需求与团队方案尚未确认，输入“确认”后再 /plan。',
                'requirements_ready': '尚未生成团队方案，输入“继续”获取方案，确认后再 /plan。',
                'exploring': '需求仍在澄清中，请先回答待解决的问题，确认方案后再 /plan。',
                'failed': '上次处理未完成，输入 /retry 重试，确认方案后再 /plan。'}
        raise ValueError(hint.get(state.get('status'), '请先确认当前版本的需求与团队方案。'))
    from astra_designer.contracts.team import chosen_team
    team = chosen_team(state)
    if team is not None and approval.get('team_sha256') != digest(team):
        raise ValueError('团队方案已变化，请重新确认')
    result = {'revision': state['revision'], 'sha256': digest(document),
              'document': document, 'approval': approval}
    if team is not None:
        result['selected_team'] = team
    return result
