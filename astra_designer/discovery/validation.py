"""Structural traceability: semantic fidelity still needs user review."""
import re
from astra_designer.contracts.requirements import digest, ready, response_schema
from jsonschema import Draft202012Validator


def validate_trace(blueprint):
    snapshot = blueprint.get('requirements')
    trace = blueprint.get('traceability')
    if snapshot is None and trace is None:
        return
    if not isinstance(snapshot, dict) or not isinstance(trace, dict):
        raise ValueError('需求快照与追溯映射必须同时存在')
    doc = snapshot.get('document')
    if list(Draft202012Validator(response_schema()).iter_errors(doc)) or not ready(doc):
        raise ValueError('需求快照未完成')
    for key in ('task_input', 'interaction'):
        if key in doc and blueprint.get(key) != doc[key]:
            raise ValueError('蓝图必须原样保留已确认的任务契约: ' + key)
    approval = snapshot.get('approval', {})
    if not isinstance(approval, dict):
        raise ValueError('无效的需求确认记录')
    if (snapshot.get('sha256') != digest(doc) or approval.get('sha256') != digest(doc)
            or approval.get('revision') != snapshot.get('revision') or approval.get('action') != 'confirm'):
        raise ValueError('需求确认摘要不一致')
    errors = []  # collected, so one repair round can fix every problem
    from astra_designer.contracts.team import validate_team_mapping
    try:
        validate_team_mapping(blueprint)
    except ValueError as exc:
        errors.append(str(exc))
    deliverables = {item['id']: item for item in doc.get('deliverables', [])}
    known = [item['id'] for item in doc['items']] + list(deliverables)
    missing = [key for key in known if key not in trace]
    unknown = [key for key in trace if key not in known]
    if missing:
        errors.append(f"traceability 缺少 {'、'.join(missing)}；每项已确认需求和交付物都必须映射到蓝图路径")
    if unknown:
        errors.append(f"traceability 含未知需求或交付物 {'、'.join(map(str, unknown))}；只能使用 {'、'.join(known)}")
    # Any existing path under these roots is accepted; a deeper path (e.g. an
    # Agent's prompt) is more precise, not less valid.
    allowed = r'/project/goal|/(?:inputs/[a-z][a-z0-9_]*|task_input|interaction|stages/\d+|acceptance/\d+)(?:/[^/]+)*'
    forms = '/project/goal、/inputs/<资源>、/task_input[/…]、/interaction、/stages/<序号>[/…]、/acceptance/<序号>'
    invalid = []
    for requirement, targets in trace.items():
        if requirement not in known:
            continue
        if not isinstance(targets, list) or not targets:
            errors.append(f'{requirement} 的追溯目标不能为空')
            continue
        for target in targets:
            if not isinstance(target, str) or not re.fullmatch(allowed, target):
                invalid.append(f'{requirement} {target!r}')
                continue
            value = blueprint
            try:
                for token in target.split('/')[1:]:
                    value = value[int(token)] if isinstance(value, list) else value[token]
            except (KeyError, IndexError, TypeError, ValueError):
                errors.append(f'{requirement} 的追溯目标 {target} 在蓝图中不存在')
    for item in doc['items']:
        targets = trace.get(item['id'])
        targets = [t for t in targets if isinstance(t, str)] if isinstance(targets, list) else []
        prefixes = {'acceptance': ('/acceptance/',), 'rule': ('/stages/',),
                    'input': ('/inputs/', '/task_input')}.get(item['category'])
        if prefixes and targets and not any(t.startswith(prefixes) for t in targets):
            errors.append(f"{item['id']}（{item['category']}）的追溯至少要有一个以 {' 或 '.join(prefixes)} 开头的路径，"
                          f'当前为 {targets}')
    errors += delivery_errors(blueprint, deliverables, trace)
    if invalid:
        errors.insert(0, f"追溯目标无效：{'、'.join(invalid)}。只能使用：{forms}")
    if errors:
        raise ValueError('；'.join(errors))


ACTION_CAPABILITIES = {'fs.cleanup@1', 'http.request@1', 'email.send@1', 'integration.pending@1'}


def acts(base, parameters):
    """Whether the Agent can really change something: a dry-run cleanup or a GET request cannot."""
    if base == 'fs.cleanup@1':
        return parameters.get('mode', 'dry_run') != 'dry_run'
    if base == 'http.request@1':
        return parameters.get('method') in {'POST', 'PUT', 'PATCH', 'DELETE'}
    return True


def delivery_errors(blueprint, deliverables, trace):
    """Each confirmed deliverable must be produced and checked by an acceptance item of the right kind."""
    from astra_designer.catalog.registry import capability_for
    writers, actors = {}, {}  # data key -> effective parameters of the Agent that writes files / acts
    for stage in blueprint.get('stages', []):
        for agent in stage.get('agents', []):
            spec = capability_for(agent) if isinstance(agent.get('parameters'), dict) else None
            if spec is None:
                continue  # unknown capabilities are reported by the structural checks
            base, parameters = spec['base'], spec['effective_parameters']  # local recipes count by their base
            for key in agent.get('outputs', {}).values():
                if base in {'document.write@1', 'table.write@1'}:
                    writers[key] = parameters
                elif base in {'image.generate@1', 'image.edit@1'}:
                    writers[key] = {'formats': ['image']}  # pictures are checked with artifact_exists /data/<key>/image
                if base in ACTION_CAPABILITIES and acts(base, parameters):
                    actors[key] = base
    checks = blueprint.get('acceptance', [])
    errors = []
    for identifier, item in deliverables.items():
        targets = trace.get(identifier) if isinstance(trace.get(identifier), list) else []
        mapped = [checks[int(t.split('/')[2])] for t in targets if isinstance(t, str)
                  and re.fullmatch(r'/acceptance/\d+', t) and int(t.split('/')[2]) < len(checks)]
        if not mapped:
            errors.append(f"交付物 {identifier}（{item['name']}）必须映射到检查它的验收项 /acceptance/<序号>")
            continue
        paths = [str(check.get('path', '')).split('/') for check in mapped]
        if item['form'] == 'action':
            if not any(len(parts) > 2 and parts[1] == 'data' and parts[2] in actors for parts in paths):
                errors.append(f"交付物 {identifier}（{item['name']}）是动作型，必须由真正执行该动作的 Agent"
                              f"（{'、'.join(sorted(ACTION_CAPABILITIES))}）产出执行记录，并用 /data/<记录键> 验收；"
                              'fs.cleanup@1 的 mode 不能是 dry_run，http.request@1 须用 POST、PUT、PATCH 或 DELETE')
            continue
        if item['form'] == 'data':
            if not any(len(parts) > 2 and parts[1] == 'data' for parts in paths):
                errors.append(f"交付物 {identifier}（{item['name']}）的验收必须检查 /data/<数据键>")
            continue
        keys = {parts[2] for parts in paths if len(parts) > 2 and parts[1] == 'data' and parts[2] in writers}
        if not keys:
            errors.append(f"交付物 {identifier}（{item['name']}）是文件，须由 document.write@1（txt/md/docx/pdf）"
                          f"、table.write@1（csv/xlsx）或 image.generate@1/image.edit@1（image）写出，"
                          f"并用 artifact_exists 检查 /data/<输出键>/<格式>；要求的格式：{'、'.join(item['formats'])}")
            continue
        written = {fmt for key in keys for fmt in writers[key].get('formats', [])}
        if any(writers[key].get('format_field') for key in keys):
            # The run chooses among the formats, but every promised format must still be writable.
            lacking = set(item['formats']) - written
            if lacking:
                errors.append(f"交付物 {identifier}（{item['name']}）要求的格式 {'、'.join(sorted(lacking))} 没有被写出")
            continue
        checked = {parts[3] for parts, check in zip(paths, mapped)
                   if check.get('op') == 'artifact_exists' and len(parts) == 4 and parts[2] in keys}
        for label, lacking in (('写出', set(item['formats']) - written), ('验收', set(item['formats']) - checked)):
            if lacking:
                errors.append(f"交付物 {identifier}（{item['name']}）要求的格式 {'、'.join(sorted(lacking))} 没有被{label}")
    return errors
