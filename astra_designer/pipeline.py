"""Goal -> clarification or validated blueprint. Never executes business code."""
import copy
import json
import re
import time
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from astra_designer.catalog.registry import schemas, catalog_lock
from astra_designer.contracts.session import save_json, session_lock
from astra_designer.llm.protocol import ModelError
from astra_designer.planning.repair import PATCH_SCHEMA, apply_patch, parse_candidate, parse_patch, patch_messages
from astra_designer.planning.workflow import messages, model_catalog, parse_reply, requirements_brief
from astra_designer.validation.static import WINDOWS_RESERVED, normalize_references, validate

MAX_PLAN_ATTEMPTS = 6


def normalize_inputs(inputs):
    if not isinstance(inputs, dict):
        raise ValueError('inputs 必须是输入标识到 path/schema 的映射')
    normalized = {}
    for key, spec in inputs.items():
        if not isinstance(key, str) or not re.fullmatch('[a-z][a-z0-9_]{0,63}', key) or key in WINDOWS_RESERVED:
            raise ValueError('输入标识无效')
        if not isinstance(spec, dict) or set(spec) != {'path', 'schema'} or not isinstance(spec['schema'], str) or spec['schema'] not in schemas() or not isinstance(spec['path'], (str, Path)):
            raise ValueError('输入必须声明 path 和已知 schema')
        normalized[key] = {'path': str(Path(spec['path']).resolve()), 'schema': spec['schema']}
    return normalized


def snapshot_inputs(inputs, revision):
    resources = {}
    for key, spec in inputs.items():
        path = Path(spec['path'])
        if path.stat().st_size > 5_000_000:
            raise ValueError('设计样例输入不能超过 5 MB')
        raw = path.read_text(encoding='utf-8')
        data = json.loads(raw)
        json.dumps(data, allow_nan=False)
        Draft202012Validator(schemas()[spec['schema']]).validate(data)
        target = revision / 'inputs' / f'{key}.json'
        target.parent.mkdir(exist_ok=True)
        save_json(target, data)
        resources[key] = {'path': f'inputs/{key}.json', 'schema': spec['schema']}
    return resources


def summary_issues(result):
    """The summary must not promise pending integrations the blueprint does not declare."""
    from astra_designer.catalog.pending import dependencies
    summary = result.get('summary', '')
    claims = re.search(r'待接入', summary) and not re.search(
        r'(无|没有|不需要|不涉及|不存在)[^，。；]{0,8}待接入|待接入[^，。；]{0,6}[：:]\s*(无|没有|暂无|不需要)', summary)
    if claims and not dependencies(result['blueprint']):
        return ['/summary: 摘要提到待接入能力，但蓝图没有 integration.pending@1 节点。需要这些能力就在工作流中声明待接入节点；'
                '不需要就从摘要删除这一说法，不能只在摘要里声称']
    return []


def evaluate(result, *, execution, resources, requirements, goal, revision, record):
    """Fill in what the program knows, normalize mechanical wiring, and list what is still wrong."""
    issues = []
    if result['status'] == 'ready':
        blueprint = result['blueprint']
        if isinstance(blueprint, dict):
            blueprint['execution'] = execution or {'default_framework': 'auto'}
        if isinstance(blueprint, dict):
            # Fields the program already knows are filled in, not left for a repair round.
            blueprint.setdefault('version', '1')
            if blueprint.get('inputs') not in (None, resources):
                record.setdefault('normalized', []).append('inputs 恢复为用户提供的资源')
            blueprint['inputs'] = copy.deepcopy(resources)  # never invented by the model
        if requirements is not None and isinstance(blueprint, dict):
            blueprint['requirements'] = requirements
            if isinstance(blueprint.get('project'), dict):
                blueprint['project']['goal'] = goal
            for key in ('task_input', 'interaction'):
                if key in requirements['document']:
                    blueprint.setdefault(key, copy.deepcopy(requirements['document'][key]))
        if isinstance(blueprint, dict):
            try:
                normalized = normalize_references(blueprint)
            except (AttributeError, TypeError, KeyError, ValueError, IndexError):
                normalized = []  # malformed parts are reported precisely by validate() below
            if normalized:
                record.setdefault('normalized', []).extend(normalized)
        validation = validate(blueprint, revision)
        issues = [f'{i.path}: {i.message}' for i in validation.issues]
        # Structural errors come first: the checks below assume a well-formed blueprint, and
        # a crash here would replace the precise schema feedback with a generic message.
        if not any(i.code == 'schema' for i in validation.issues):
            issues += program_checks(blueprint, resources, goal)
    if result['status'] == 'ready' and not issues:
        issues += summary_issues(result)
    return issues


def repair_candidate(result, raw):
    """The reply with the program's own fixes applied, minus fields the program injects anyway."""
    if not isinstance(result, dict) or not isinstance(result.get('blueprint'), dict):
        return raw
    shown = dict(result, blueprint={k: v for k, v in result['blueprint'].items() if k not in {'requirements', 'execution'}})
    return json.dumps(shown, ensure_ascii=False)


def program_checks(blueprint, resources, goal):
    issues = []
    if blueprint.get('inputs') != resources:
        issues.append('蓝图必须原样引用用户提供的资源，不得添加或修改输入')
    if blueprint['project'].get('goal') != goal:
        issues.append('project.goal 必须保留用户原始目标')
    produced = {key for stage in blueprint['stages'] for agent in stage['agents'] for key in agent['outputs'].values()}
    if not any(len(str(check['path']).split('/')) > 2 and str(check['path']).split('/')[1] == 'data'
               and str(check['path']).split('/')[2] in produced for check in blueprint['acceptance']):
        issues.append('acceptance 至少要有一项以 /data/<数据键> 检查业务输出；本蓝图产出的数据键：'
                      + ('、'.join(sorted(produced)) or '无'))
    return issues


def design(goal, session_dir, client, *, inputs=None, answers=None, max_attempts=2, requirements=None, execution=None,
           resume=None):
    """resume={'candidate', 'feedback'} continues repairing a previous failed plan instead of starting over."""
    if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_PLAN_ATTEMPTS:
        raise ValueError(f'max_attempts 必须在 1–{MAX_PLAN_ATTEMPTS} 之间')
    directory = Path(session_dir).resolve()
    with session_lock(directory):
        path = directory / 'session.json'
        if path.exists():
            session = json.loads(path.read_text(encoding='utf-8'))
            if execution is not None and session.get('execution') != execution:
                raise ValueError('框架配置已变化，请新建规划会话')
            if requirements is not None and session.get('requirements') != requirements:
                raise ValueError('需求版本已变化，请创建新的规划会话')
            if goal is not None and goal != session['goal']:
                raise ValueError('已有会话的目标不可修改，请创建新会话')
            if session['status'] in {'ready', 'unsupported'}:
                if inputs is not None or answers:
                    raise ValueError('已完成设计不能继续修改，请创建新会话')
                return session
        else:
            if not isinstance(goal, str) or not goal.strip() or len(goal) > 20000:
                raise ValueError('新会话必须提供非空业务目标，最长 20000 字符')
            from astra_designer.contracts.settings import load_settings
            session = {'version': 1, 'goal': goal, 'status': 'new', 'calls': 0,
                       'max_calls': load_settings()['max_plan_calls'],
                       'revision': 0, 'history': [], 'inputs': {}, 'questions': []}
        if execution is not None:
            session['execution'] = execution
        execution = session.get('execution')
        if requirements is not None:
            if session.get('requirements') not in (None, requirements):
                raise ValueError('需求版本已变化，请创建新的规划会话')
            session['requirements'] = requirements
        requirements = session.get('requirements')
        if inputs is not None:
            session['inputs'] = normalize_inputs(inputs)
        pending = {q['id'] for q in session.get('questions', [])}
        if pending:
            if not isinstance(answers, dict) or set(answers) != pending or not all(isinstance(v, str) and v.strip() and len(v) <= 10000 for v in answers.values()):
                raise ValueError('请通过 answers 提供全部待澄清问题的非空答案')
            session['history'].append({'questions': session['questions'], 'answers': answers})
            session['questions'] = []
        elif answers:
            raise ValueError('当前会话没有待回答的问题')
        limit = session.get('max_calls')
        if limit is not None and session['calls'] >= limit:
            session.update(status='failed', summary=f'设计会话已达到 {limit} 次模型请求上限')
            save_json(path, session)
            return session

        session['revision'] += 1
        revision = directory / 'revisions' / f"{session['revision']:04d}"
        revision.mkdir(parents=True, exist_ok=False)
        session.update(status='designing', blueprint_path=None, summary='正在设计')
        save_json(path, session)
        try:
            resources = snapshot_inputs(session['inputs'], revision)
        except (OSError, ValueError, TypeError) as exc:
            session.update(status='failed', summary=f'输入样例无效: {exc}')
            save_json(path, session)
            return session
        except Exception as exc:
            # jsonschema.ValidationError includes user data; retain only its message.
            from jsonschema.exceptions import ValidationError
            if not isinstance(exc, ValidationError):
                raise
            session.update(status='failed', summary='输入样例不符合声明的 Schema')
            save_json(path, session)
            return session

        attempts, feedback, candidate, notes, stalled = [], [], None, [], 0
        if resume and resume.get('candidate') and resume.get('feedback'):
            feedback, candidate = list(resume['feedback']), resume['candidate']
            notes, stalled = list(resume.get('notes') or []), int(resume.get('stalled') or 0)
            session['resumed_from'] = resume.get('source')
        from astra_designer.contracts.session import CallProgress, failure_details, model_settings, now
        for _ in range(max_attempts if limit is None else min(max_attempts, limit - session['calls'])):
            session['calls'] += 1
            # 'calls' counts toward the limit; 'requests' numbers every request, counted or not.
            session['requests'] = session.get('requests', session['calls'] - 1) + 1
            number = session['requests']
            save_json(path, session)  # A crash or timeout still consumes the reserved call.
            record = {'call': number, 'started_at': now(), 'model': model_settings(client)}
            progress = CallProgress()
            started = time.monotonic()
            reply = None
            try:
                from astra_designer.llm.recording import recorded_complete_json
                from astra_designer.planning.workflow import generation_schema
                base = parse_candidate(candidate) if candidate and feedback else None
                check = lambda result: evaluate(result, execution=execution, resources=resources, requirements=requirements,
                                                goal=session['goal'], revision=revision, record=record)
                if base is not None and stalled < 2:
                    # Targeted repair: the model returns edits; the program applies and checks them.
                    record['mode'] = 'patch'
                    request = patch_messages(base, feedback, requirements_brief(requirements), model_catalog(), notes)
                    record['request_chars'] = sum(len(m['content']) for m in request)
                    record['model_trace'] = f'model-call-{number:04d}.json'
                    reply = recorded_complete_json(client, request, PATCH_SCHEMA, on_progress=progress,
                                                   trace_path=revision / record['model_trace'])
                    record['usage'] = reply.usage
                    save_json(revision / f"response-{number:04d}.json", {
                        'content': reply.content[:200000], 'truncated': len(reply.content) > 200000, 'usage': reply.usage})
                    try:
                        patch = parse_patch(reply.content)
                        record['patch_summary'] = patch['summary']
                        if patch['status'] == 'patch':
                            result, record['edits'], ignored = apply_patch(base, patch['edits'], feedback)
                            record['ignored'] = ignored
                        else:
                            result, ignored = patch, []
                    except ValueError as exc:  # unparseable reply or a path that does not exist
                        notes, stalled = [f'上次修补无法应用：{str(exc)[:600]}'], stalled + 1
                        record.update(issues=feedback, patch_error=str(exc)[:1000])
                        continue
                    issues = check(result)
                    if result['status'] == 'ready' and len(issues) > len(feedback):
                        added = [issue for issue in issues if issue not in feedback]
                        notes = [f'上次修补后问题从 {len(feedback)} 个变成 {len(issues)} 个，已撤销，下面仍是原蓝图的问题。'
                                 '那次修补引入的问题：' + '；'.join(added[:5])]
                        stalled += 1
                        record.update(issues=issues, rolled_back=True)
                        continue
                    stalled = 0 if len(issues) < len(feedback) else stalled + 1
                    notes = [f"以下修改超出本轮问题范围，已忽略：{'；'.join(ignored)}"] if ignored else []
                else:
                    if base is not None:
                        record['mode'] = 'rewrite'  # patches stopped making progress; let the model rethink
                    request = messages(session['goal'], resources, session['history'], feedback, requirements=requirements,
                                       previous=candidate)
                    record['request_chars'] = sum(len(m['content']) for m in request)
                    record['model_trace'] = f'model-call-{number:04d}.json'
                    reply = recorded_complete_json(client, request, generation_schema(requirements), on_progress=progress,
                                                   trace_path=revision / record['model_trace'])
                    record['usage'] = reply.usage
                    save_json(revision / f"response-{number:04d}.json", {
                        'content': reply.content[:200000], 'truncated': len(reply.content) > 200000, 'usage': reply.usage})
                    result = parse_reply(reply.content)
                    if requirements and result['status'] == 'unsupported':
                        raise ValueError('已确认团队应返回蓝图或具体澄清问题；缺少实现须声明待接入节点。')
                    issues = check(result)
                    if record.get('mode') == 'rewrite':
                        stalled, notes = 0, []  # Reset only after a usable rewrite, never on a failed request.
                record['issues'] = issues
                if issues:
                    feedback = issues
                    candidate = repair_candidate(result, reply.content)  # repaired in place next time, not regenerated
                else:
                    session.update(status=result['status'], summary=result['summary'], questions=result.get('questions', []))
                    if result['status'] == 'ready':
                        blueprint_path = revision / 'blueprint.yaml'
                        with blueprint_path.open('x', encoding='utf-8') as file:
                            yaml.safe_dump(result['blueprint'], file, allow_unicode=True, sort_keys=False)
                        from astra_designer.frameworks.registry import resolve
                        save_json(revision / 'frameworks.lock.json', resolve(result['blueprint']))
                        save_json(revision / 'capabilities.lock.json', catalog_lock(result['blueprint']))
                        session['blueprint_path'] = str(blueprint_path)
                        from astra_designer.catalog.pending import dependencies
                        session['pending_integrations'] = dependencies(result['blueprint'])
                        session['runnable'] = not session['pending_integrations']
                    save_json(revision / 'result.json', result)
                    break
            except ModelError as exc:
                record.update(error=str(exc), counted=False)
                session['calls'] -= 1  # truncation or connection failures do not use up the limit
                session.update(status='failed', summary=str(exc), failure=failure_details(exc, client, progress))
                if progress.content:
                    # The partial answer shows how far the model got before the limit.
                    save_json(revision / f"response-{number:04d}.json",
                              {'content': progress.content, 'complete': False})
                break  # No silent network retries or extra billing.
            except (ValueError, TypeError, RecursionError, AttributeError) as exc:
                if record.get('mode') == 'patch':
                    # The blueprint being repaired stays; only this patch is discarded.
                    notes, stalled = [f'上次修补无法应用：{str(exc)[:600]}'], stalled + 1
                    record.update(issues=feedback, patch_error=str(exc)[:1000])
                    continue
                # Keep the concrete reason: a generic message gives the repair round nothing to fix.
                feedback = [f'模型输出无效：{str(exc)[:1000]}']
                if not isinstance(exc, json.JSONDecodeError) and reply is not None and len(reply.content) <= 200000:
                    candidate = reply.content  # parseable JSON with a contract error is still repairable
                    notes, stalled = [], 2  # an envelope error is fixed by rewriting the reply, not by edits
                else:
                    candidate = None
                record['issues'] = feedback
            finally:
                record.update(progress.summary(), elapsed_seconds=round(time.monotonic() - started, 3))
                attempts.append(record)
                save_json(revision / 'attempts.json', attempts)
        if session['status'] == 'designing':
            session.update(status='failed', summary=f'本轮已完成 {len(attempts)} 次模型调用，仍有校验问题；已保存候选蓝图，可用 /plan 继续修补', validation_feedback=feedback)
        elif session['status'] == 'failed' and candidate and feedback:
            # A model-side failure mid-repair must not lose the blueprint being repaired.
            session['validation_feedback'] = feedback
        if session['status'] == 'failed' and candidate:
            session['candidate'] = candidate  # the next /plan continues from here
            session.update(repair_stalled=stalled, repair_notes=notes)
        save_json(path, session)
        return session
