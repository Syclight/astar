"""Targeted blueprint repair: the model returns edits, the program applies them.

A full rewrite lets a model "fix" one issue by redesigning everything, and weak models do exactly
that. Here each issue arrives with the flagged snippet, the capability contract and a concrete
direction; the model answers with a few edits; the program resolves their paths, refuses changes to
existing content unrelated to the issues, and the caller rolls the patch back if it makes things worse.
"""
import copy
import json
import re

from astra_designer.catalog.registry import capability_for, resource_text, schemas
from astra_designer.prompts import with_product_context, capability_guides

PROGRAM_OWNED = {'inputs', 'task_input', 'interaction', 'requirements', 'execution', 'version'}
BOOKKEEPING = {'acceptance', 'traceability', 'team_assignment', 'schemas'}
MAX_EDITS = 60

PATCH_SCHEMA = {'oneOf': [
    {'type': 'object', 'additionalProperties': False, 'required': ['status', 'summary', 'edits'],
     'properties': {'status': {'const': 'patch'}, 'summary': {'type': 'string', 'minLength': 1},
                    'edits': {'type': 'array', 'minItems': 1, 'maxItems': MAX_EDITS, 'items': {
                        'type': 'object', 'additionalProperties': False, 'required': ['op', 'path'],
                        'properties': {'op': {'enum': ['set', 'remove', 'insert']},
                                       'path': {'type': 'string', 'pattern': '^/'}, 'value': {}}}}}},
    {'type': 'object', 'additionalProperties': False, 'required': ['status', 'summary', 'questions'],
     'properties': {'status': {'const': 'needs_clarification'}, 'summary': {'type': 'string', 'minLength': 1},
                    'questions': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {
                        'type': 'object', 'additionalProperties': False, 'required': ['id', 'question'],
                        'properties': {'id': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]*$'},
                                       'question': {'type': 'string', 'pattern': r'\S'}}}}}},
]}

INSTRUCTIONS = '''
本轮是定点修补：blueprint 是上次未通过校验的蓝图，issues 是校验器发现的问题，每项附出错位置的原文（context）和修改方向（hint）。
- 只返回 {"status":"patch","summary":"一句话说明改了什么","edits":[...]}，不要返回完整蓝图，不要重新设计。
- 每个 edit 是 {"op":"set|remove|insert","path":"/...","value":...}：set 替换或新增字段；remove 删除；insert 向数组插入（序号或 - 表示末尾）。
- path 从蓝图根开始，例如 /stages/1/agents/0/parameters/input_schemas/state；阶段和 Agent 可以写 id 代替序号，例如 /stages/write/agents/writer/inputs/brief。edits 依次执行，后面的 path 以前面修改后的蓝图为准，所以优先用 id。
- 只改 issues 涉及的 Agent、与它们直接相连的 Agent，以及 acceptance、traceability、team_assignment、schemas；可以新增阶段、Agent 或验收项。inputs、task_input、interaction 由程序维护，不能修改。改了数据键时，同步修改读取它的端口、验收和追溯。
- /summary 是方案摘要，只有 issues 指出摘要问题时才修改。
- 问题需要用户决定时返回 needs_clarification。
'''


class PatchError(ValueError):
    pass


def parse_candidate(candidate):
    """The previous reply as a dict, when it is a ready blueprint that edits can apply to."""
    try:
        result = json.loads(candidate) if isinstance(candidate, str) else candidate
    except ValueError:
        return None
    if isinstance(result, dict) and result.get('status') == 'ready' and isinstance(result.get('blueprint'), dict) \
            and isinstance(result['blueprint'].get('stages'), list):
        return result
    return None


def parse_patch(content):
    from jsonschema import Draft202012Validator
    reply = json.loads(content)
    errors = list(Draft202012Validator(PATCH_SCHEMA).iter_errors(reply))
    if errors:
        if isinstance(reply, dict) and reply.get('status') == 'ready':
            raise PatchError('本轮只接受 edits 修补，不接受完整蓝图；请返回 {"status":"patch","summary":...,"edits":[...]}')
        error = min(errors, key=lambda e: len(list(e.absolute_path)))
        raise PatchError('修补回复不符合格式：/' + '/'.join(map(str, error.absolute_path)) + ': ' + error.message[:300])
    return reply


def _tokens(path):
    return [token.replace('~1', '/').replace('~0', '~') for token in path.split('/')[1:]]


def _index(container, token, name, for_insert=False):
    if token == '-' and for_insert:
        return len(container)
    if token.isdigit():
        index = int(token)
        if index < len(container) + (1 if for_insert else 0):
            return index
        raise PatchError(f'{name} 没有第 {index} 项（共 {len(container)} 项）')
    ids = [item.get('id') if isinstance(item, dict) else None for item in container]
    if token in ids:
        return ids.index(token)
    raise PatchError(f"{name} 中没有 id 为 {token} 的项；可用：{'、'.join(str(i) for i in ids if i)}")


def resolve(document, path, *, for_insert=False):
    """(parent, key, containers, canonical path); stage and Agent ids may stand for their positions."""
    tokens = _tokens(path)
    if not tokens or tokens == ['']:
        raise PatchError('path 不能是蓝图根')
    node, containers, canonical = document, [document], []
    for position, token in enumerate(tokens):
        last = position == len(tokens) - 1
        name = '/' + '/'.join(canonical) if canonical else '蓝图'
        if isinstance(node, list):
            key = _index(node, token, name, for_insert and last)
        elif isinstance(node, dict):
            key = token
            if not last and key not in node:
                raise PatchError(f'{name} 中没有字段 {token}')
        else:
            raise PatchError(f'{name} 不是对象或数组，不能继续向下访问 {token}')
        canonical.append(str(key))
        if last:
            return node, key, containers, '/' + '/'.join(canonical)
        node = node[key]
        containers.append(node)


def wiring(blueprint):
    """[(path, agent)] and data key -> producers/consumers, for scope and hints."""
    agents, producers, consumers = [], {}, {}
    for s, stage in enumerate(blueprint.get('stages') or []):
        for a, agent in enumerate(stage.get('agents') or [] if isinstance(stage, dict) else []):
            if not isinstance(agent, dict):
                continue
            agents.append((f'/stages/{s}/agents/{a}', s, agent))
            for key in (agent.get('outputs') or {}).values() if isinstance(agent.get('outputs'), dict) else []:
                producers.setdefault(key, []).append(agent)
            for key in (agent.get('inputs') or {}).values() if isinstance(agent.get('inputs'), dict) else []:
                consumers.setdefault(key, []).append(agent)
    return agents, producers, consumers


def issue_path(issue):
    match = re.match(r'(/[^:\s]*):\s', issue)
    return match.group(1) if match else ''


def editable(blueprint, issues):
    """Objects the edits may change: flagged Agents and stages plus those wired to them."""
    agents, producers, consumers = wiring(blueprint)
    by_path = {path: (stage, agent) for path, stage, agent in agents}
    allowed = set()
    stages = blueprint.get('stages') or []
    for issue in issues:
        path = issue_path(issue)
        match = re.match(r'/stages/(\d+)(?:/agents/(\d+))?', path)
        if not match:
            continue
        s = int(match.group(1))
        if match.group(2) is None:
            if s < len(stages):
                allowed.add(id(stages[s]))
            continue
        found = by_path.get(f'/stages/{s}/agents/{match.group(2)}')
        if not found:
            continue
        stage, agent = found
        allowed.add(id(agent))
        for key in (agent.get('inputs') if isinstance(agent.get('inputs'), dict) else {}).values():
            allowed.update(id(other) for other in producers.get(key, []))
        for key in (agent.get('outputs') if isinstance(agent.get('outputs'), dict) else {}).values():
            allowed.update(id(other) for other in consumers.get(key, []))
        if '尚未由上游产生' in issue or 'Schema 是' in issue:
            # The fix may be upstream: an earlier Agent producing the missing or mismatched data.
            allowed.update(id(other) for _, at, other in agents if at <= stage)
    return allowed


def apply_patch(result, edits, issues):
    """A new result with the edits applied; out-of-scope edits are skipped and reported, bad paths raise."""
    patched = copy.deepcopy(result)
    blueprint = patched['blueprint']
    allowed = editable(blueprint, issues)
    applied, ignored = [], []
    for number, edit in enumerate(edits, 1):
        op, path = edit['op'], edit['path']
        if path == '/summary':
            if op != 'set' or not isinstance(edit.get('value'), str) or not edit['value'].strip():
                raise PatchError('第 {} 项：/summary 只能 set 为非空文字'.format(number))
            patched['summary'] = edit['value']
            applied.append('set /summary')
            continue
        if op in {'set', 'insert'} and 'value' not in edit:
            raise PatchError(f'第 {number} 项（{op} {path}）缺少 value')
        try:
            parent, key, containers, canonical = resolve(blueprint, path, for_insert=op == 'insert')
        except PatchError as exc:
            raise PatchError(f'第 {number} 项（{op} {path}）：{exc}') from None
        top = canonical.split('/')[1]
        if top in PROGRAM_OWNED or canonical.startswith('/project/goal'):
            ignored.append(f'{canonical}（由程序维护）')
            continue
        target = parent[key] if (op != 'insert' and (isinstance(parent, list) or key in parent)) else None
        emptied = isinstance(target, dict) and top == 'stages' and target.get('agents') == []
        adding = op == 'insert' and re.fullmatch(r'/stages/\d+(/agents/\d+)?', canonical)  # a new stage or Agent
        removing_related_stage = (op == 'remove' and re.fullmatch(r'/stages/\d+', canonical)
                                  and isinstance(target, dict) and isinstance(target.get('agents'), list)
                                  and bool(target['agents'])
                                  and all(id(agent) in allowed for agent in target['agents']))
        in_scope = (top in BOOKKEEPING or adding or emptied or removing_related_stage
                    or any(id(item) in allowed for item in containers[1:] + ([target] if target is not None else [])))
        if not in_scope:
            ignored.append(f'{canonical}（与本轮问题无关）')
            continue
        if op == 'remove':
            if isinstance(parent, list):
                parent.pop(key)
            elif key in parent:
                del parent[key]
            else:
                raise PatchError(f'第 {number} 项：{canonical} 不存在，无法删除')
        elif op == 'insert':
            if not isinstance(parent, list):
                raise PatchError(f'第 {number} 项：insert 只能用于数组，{canonical} 的上级不是数组')
            parent.insert(key, edit['value'])
            _allow_new(edit['value'], allowed)
        else:
            parent[key] = edit['value']
            _allow_new(edit['value'], allowed)
        applied.append(f'{op} {canonical}')
    if not applied:
        raise PatchError('没有可应用的修改' + (f"：{'；'.join(ignored)}" if ignored else ''))
    return patched, applied, ignored


def _allow_new(value, allowed):
    """Content the patch itself created may be edited by later edits of the same patch."""
    if isinstance(value, dict):
        allowed.add(id(value))
        for item in value.values():
            _allow_new(item, allowed)
    elif isinstance(value, list):
        for item in value:
            _allow_new(item, allowed)


# ---------- issues with context and direction ----------

def _schema_label(agent, direction, port):
    spec = capability_for(agent) or {}
    return (spec.get(direction) or {}).get(port)


def data_flow(blueprint):
    """A compact map of who produces and reads which data key, with Schemas."""
    lines = []
    for path, _, agent in wiring(blueprint)[0]:
        try:
            spec = capability_for(agent) or {}
        except (AttributeError, TypeError, KeyError):
            spec = {}
        def side(direction):
            ports = agent.get(direction) if isinstance(agent.get(direction), dict) else {}
            return {port: f"{key}（{(spec.get(direction) or {}).get(port, '?')}）" for port, key in ports.items()}
        lines.append({'path': path, 'id': agent.get('id'), 'capability': agent.get('capability'),
                      'inputs': side('inputs'), 'outputs': side('outputs')})
    return lines


def _produced_before(blueprint, stage_index):
    keys = {'task_input': 'task_input'} if 'task_input' in blueprint else {}
    for _, at, agent in wiring(blueprint)[0]:
        if at >= stage_index:
            continue
        spec = capability_for(agent) or {}
        for port, key in (agent.get('outputs') or {}).items():
            keys.setdefault(key, (spec.get('outputs') or {}).get(port))
    return keys


def _listing(keys):
    return '、'.join(f'{key}（{schema}）' for key, schema in keys.items()) or '无'


def hint(issue, blueprint):
    message = issue.split(': ', 1)[1] if issue_path(issue) else issue
    path = issue_path(issue)
    stage = re.match(r'/stages/(\d+)', path)
    before = _produced_before(blueprint, int(stage.group(1))) if stage else {}
    if match := re.search(r'数据 (\S+) 的 Schema 是 (\S+)，但端口 (\S+) 需要 (\S+?)；', message):
        key, have, port, need = match.groups()
        same = {k: s for k, s in before.items() if s == need}
        return (f'二选一：让端口 {port} 接受 {have}（llm.transform/llm.map 改 parameters.input_schemas.{port}；'
                f'其他能力改决定该端口 Schema 的参数），或让 {port} 改读 Schema 为 {need} 的数据键（前面已有：{_listing(same)}）')
    if match := re.search(r'数据 (\S+) 尚未由上游产生', message):
        return (f'{match.group(1)} 在此之前没有 Agent 产出。改读前面已有的数据键（{_listing(before)}），'
                '或在前面的阶段新增产出它的 Agent，或把产出它的 Agent 移到前面；数据键是 outputs 右侧的名字，不是端口名')
    if match := re.search(r'数据 (\S+) 没有任何 Agent 产出', message):
        return f'optional_inputs 读取的 {match.group(1)} 没有 Agent 产出：改为后面检查步骤真正产出的数据键，或删除这个端口'
    if match := re.search(r'optional_inputs 中的 (\S+) 不是 input_schemas 的端口', message):
        later = {key: schema for key, schema in _produced_before(blueprint, 10 ** 6).items()
                 if key not in before and key != 'task_input'}
        return (f'optional_inputs 写端口名。要读取后面产出的意见：inputs 加 "{match.group(1)}": "<意见的数据键>"，'
                f'input_schemas 加 "{match.group(1)}": "<意见的 Schema>"（后面产出的数据：{_listing(later)}）；'
                '不需要就从 optional_inputs 删除')
    if '既未被后续 Agent 使用，也未被验收检查' in message:
        return ('二选一：删除该 Agent（remove 它，并删除 team_assignment、traceability 中对它的引用）；'
                '或者它的成果确实需要，就在 acceptance 增加检查 /data/<它的输出键> 的验收项，或让后面的 Agent 读取它')
    if match := re.search(r'inputs 端口必须是 (\[.*?\])，当前为', message):
        return f'把 inputs 的端口名改成 {match.group(1)}，每个端口绑定一个数据键；brief 端口绑定 task_input'
    if match := re.search(r'outputs 端口必须是 (\[.*?\])，当前为', message):
        return f'把 outputs 的端口名改成 {match.group(1)}，数据键（右侧）保持不变'
    if '引用了未知 Schema' in message:
        return '在 /schemas 中声明这个 Schema（自包含 JSON Schema），或改用 data_schemas 中已有的标识'
    if path.startswith('/traceability') or '追溯' in message or 'traceability' in message:
        checks = [f"/acceptance/{i}: {c.get('op')} {c.get('path')}" for i, c in enumerate(blueprint.get('acceptance') or [])
                  if isinstance(c, dict)]
        return '在 traceability 中为每个 REQ/DEL 写路径列表；DEL 指向检查它的验收项。当前验收：' + ('；'.join(checks) or '无')
    if path.startswith('/acceptance') or 'artifact_exists' in message:
        return '验收路径写 /data/<数据键>，文件写 /data/<文件输出键>/<该 Agent 写出的格式>；数据键见 data_flow'
    return None


def context_for(issue, blueprint):
    path = issue_path(issue)
    match = re.match(r'/stages/(\d+)(?:/agents/(\d+))?', path)
    try:
        if match and match.group(2) is not None:
            agent = blueprint['stages'][int(match.group(1))]['agents'][int(match.group(2))]
            spec = capability_for(agent) or {}
            contract = {'inputs': spec.get('inputs'), 'outputs': spec.get('outputs'),
                        'parameters': spec.get('parameters')}
            return {'agent': agent, 'stage': blueprint['stages'][int(match.group(1))].get('id'),
                    'capability_contract': contract}
        if match:
            stage = blueprint['stages'][int(match.group(1))]
            return {'stage': {'id': stage.get('id'), 'agents': [a.get('id') for a in stage.get('agents', [])]}}
        acceptance = re.match(r'/acceptance/(\d+)', path)
        if acceptance:
            return {'acceptance': blueprint['acceptance'][int(acceptance.group(1))]}
    except (IndexError, KeyError, TypeError, AttributeError):
        return None
    return None


def structured(issues, blueprint):
    result = []
    for issue in issues:
        item = {'path': issue_path(issue) or None, 'problem': issue.split(': ', 1)[1] if issue_path(issue) else issue}
        try:
            direction = hint(issue, blueprint)
        except (AttributeError, TypeError, KeyError, ValueError):
            direction = None
        if direction:
            item['hint'] = direction
        found = context_for(issue, blueprint)
        if found:
            item['context'] = found
        result.append(item)
    return result


def patch_messages(base, issues, requirements_brief, catalog, notes=()):
    blueprint = {key: value for key, value in base['blueprint'].items() if key not in {'requirements', 'execution'}}
    context = {'confirmed_requirements': requirements_brief, 'summary': base.get('summary'),
               'issues': structured(issues, base['blueprint']), 'data_flow': data_flow(base['blueprint']),
               'blueprint': blueprint, 'capabilities': catalog, 'data_schemas': schemas()}
    if notes:
        context['previous_attempt'] = list(notes)
    context['capability_guides'] = capability_guides(
        {'requirements': requirements_brief, 'issues': issues}, catalog, base['blueprint'])
    prompt = resource_text('repair_prompt.md') + INSTRUCTIONS
    return [{'role': 'system', 'content': with_product_context(prompt)},
            {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]
