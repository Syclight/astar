"""Persistent exploration with explicit revision approval; no business execution."""
import json
import time
from pathlib import Path
from uuid import uuid4

from astra_core.llm.contracts import parse_json
from astra_designer.contracts.requirements import (
    CORE, confirmed_document, digest, ready, validate_record,
)
from astra_designer.contracts.session import failure_details, model_settings, now, save_json, session_lock
from astra_designer.discovery.context import messages
from astra_designer.llm.protocol import ModelError


def load_session(directory):
    state = json.loads((Path(directory) / 'discovery.json').read_text(encoding='utf-8'))
    if state.get('version') != 1:
        raise ValueError('不支持的需求会话版本')
    # Legacy cumulative budgets no longer gate user-initiated exploration.
    state.pop('budget', None)
    if state.get('status') == 'budget_exhausted':
        state['status'] = 'failed'
    for key in ('proposal_draft', 'clarification_draft'):
        # The retired estimates step sat between team and contract.
        if (state.get(key) or {}).get('step') == 'estimates':
            state[key]['step'] = 'contract'
    if 'design_version' not in state:
        from astra_designer.discovery.versions import update_design_version
        update_design_version(state, migrating=True)
    return state


def persist(directory, state):
    from astra_designer.discovery.versions import update_design_version
    update_design_version(state)
    save_json(directory / 'discovery.json', state)  # authoritative atomic record
    if state.get('document'):
        # Human-readable projection of the current requirements, including the user's original words.
        import yaml
        target = directory / 'requirements.yaml'
        temporary = target.with_name('.requirements.yaml.tmp')
        temporary.write_text(yaml.safe_dump({
            'revision': state['revision'], 'design_version': state['design_version'], 'status': state['status'],
            'approval': state.get('approval'), 'team_choice': state.get('team_choice'), **state['document']},
            allow_unicode=True, sort_keys=False, width=120), encoding='utf-8')
        temporary.replace(target)


def explore(message, session_dir, *, client=None, action='message', revision=None, samples=None, choice=None, command=None):
    if action not in {'message', 'retry', 'continue', 'confirm', 'select'}:
        raise ValueError('未知需求探索操作')
    directory = Path(session_dir).resolve()
    with session_lock(directory):
        state = load_session(directory) if (directory / 'discovery.json').exists() else {
            'version': 1, 'status': 'exploring', 'revision': 0, 'calls': 0,
            'transcript': [], 'samples': {}, 'document': None, 'approval': None,
            'plans': [], 'plan_calls': 0, 'feedback': [], 'team_planning': True,
        }
        if action == 'select':
            proposal = (state.get('document') or {}).get('team_proposal')
            if (type(revision) is not int or revision != state['revision'] or not proposal
                    or state['status'] not in {'awaiting_confirmation', 'confirmed'}
                    or choice not in {o['id'] for o in proposal['options']}):
                raise ValueError('方案不存在或需求已更新；用法：/select 方案ID，方案 ID 见 /team')
            state.update(team_choice=choice, approval=None, plan=None, status='awaiting_confirmation')
            state['transcript'].append({'turn': len(state['transcript']) + 1, 'role': 'user', 'at': now(),
                                        'command': '/select', 'text': f'我选择团队方案 {choice}。'})
            state['revision'] += 1
            revisions = directory / 'requirements-history'
            revisions.mkdir(exist_ok=True)
            save_json(revisions / f'{state["revision"]:04d}.json', state['document'])
            persist(directory, state)
            return state
        if action == 'confirm':
            if (state['status'] != 'awaiting_confirmation' or type(revision) is not int
                    or revision != state['revision'] or not ready(state['document'])):
                raise ValueError('当前没有待确认的方案，或需求已更新；请查看最新方案后输入“确认”')
            state['approval'] = {'action': 'confirm', 'revision': revision, 'sha256': digest(state['document']), 'at': now()}
            from astra_designer.contracts.team import chosen_team
            team = chosen_team(state)
            if team is not None:
                state['approval']['team_sha256'] = digest(team)
            state['status'] = 'confirmed'
            persist(directory, state)
            # The confirmed document itself is requirements-history/<revision>.json; record only the approval.
            snapshot = confirmed_document(state)
            save_json(directory / f'confirmed-{revision:04d}.json', {
                'revision': revision, 'sha256': snapshot['sha256'], 'approval': state['approval'],
                'document': f'requirements-history/{revision:04d}.json',
                'selected_team': (snapshot.get('selected_team') or {}).get('id')})
            return state
        if action == 'message':
            if not isinstance(message, str) or not message.strip() or len(message) > 20000:
                raise ValueError('请提供 1–20000 字符的业务描述或回答')
            entry = {'turn': len(state['transcript']) + 1, 'role': 'user', 'at': now(), 'text': message}
            if command:
                entry['command'] = command  # a command expansion, never evidence for requirements
            state['transcript'].append(entry)
            state['team_planning'] = True
            state['approval'] = None
            state['team_choice'] = None
            state['plan'] = None
            state['feedback'] = []
            state.pop('repair_candidate', None)
            state.pop('failure_kind', None)
            state.pop('failure_details', None)
            state.pop('proposal_draft', None)
        elif not state['transcript']:
            raise ValueError('请先描述业务目标')
        elif state['status'] not in {'failed', 'budget_exhausted'}:
            raise ValueError('仅失败的探索可以重试')
        if samples is not None:
            from astra_designer.pipeline import normalize_inputs
            state['samples'] = normalize_inputs(samples)
        state['status'] = 'exploring'
        persist(directory, state)
        if client is None:
            from astra_designer.llm.config import configured_model
            try:
                client = configured_model()
            except (ValueError, OSError) as exc:
                state.update(status='failed', feedback=[str(exc)])
                persist(directory, state)
                return state
        feedback = state.get('feedback', [])
        repair_candidate = state.get('repair_candidate')
        from astra_designer.discovery.sections import STEPS, request_section, assemble
        staged = bool(state.get('clarification_draft') or (state.get('document') and ready(state['document'])))
        if staged and 'proposal_draft' not in state:
            state['proposal_draft'] = {'step': STEPS[0], 'record': {}}
            repair_candidate = None  # Legacy whole-document candidates are not section responses.
            state.pop('repair_candidate', None)
        attempts = 2 * len(STEPS) if staged else 2
        section_failures = 0
        for attempt in range(attempts):
            state['calls'] += 1
            state['status'] = 'failed'  # interruption leaves a retryable state
            persist(directory, state)
            diagnostic_path = directory / 'diagnostics' / f"call-{state['calls']:04d}.json"
            diagnostic_path.parent.mkdir(exist_ok=True)
            diagnostic = {'call': state['calls'], 'status': 'requesting', 'started_at': now(), 'model': model_settings(client)}
            state['diagnostics_path'] = str(diagnostic_path.parent)
            save_json(diagnostic_path, diagnostic)
            started = time.monotonic()
            last_progress_save = started
            def report_progress(kind, text):
                nonlocal last_progress_save
                if kind not in {'content', 'reasoning'}:
                    return
                progress = diagnostic.setdefault('progress', {'content_chars': 0, 'reasoning_chars': 0})
                progress[kind + '_chars'] += len(text)
                now = time.monotonic()
                progress['last_delta_seconds'] = round(now - started, 3)
                if now - last_progress_save >= 1:
                    diagnostic['elapsed_seconds'] = round(now - started, 3)
                    save_json(diagnostic_path, diagnostic)
                    last_progress_save = now
            reply = None
            try:
                request_messages = messages(state, feedback, repair_candidate, phase_override='proposal' if staged else None)
                if staged:
                    request_messages = request_section(request_messages, state['proposal_draft'])
                request_context = json.loads(request_messages[-1]['content'])
                request_context['attempt_kind'] = 'repair' if repair_candidate is not None or section_failures else 'generate'
                request_messages[-1]['content'] = json.dumps(request_context, ensure_ascii=False)
                diagnostic['phase'] = request_context['phase']
                diagnostic['attempt_kind'] = request_context['attempt_kind']
                diagnostic['response_format'] = getattr(client, 'response_format', 'custom')
                if staged:
                    diagnostic['section'] = state['proposal_draft']['step']
                diagnostic['request_chars'] = sum(len(m['content']) for m in request_messages)
                save_json(diagnostic_path, diagnostic)
                from astra_designer.llm.recording import recorded_complete_json
                diagnostic['model_trace'] = f'model-calls/call-{state["calls"]:04d}.json'
                reply = recorded_complete_json(client, request_messages, request_context['response_schema'],
                                              on_progress=report_progress,
                                              trace_path=diagnostic_path.parent / diagnostic['model_trace'])
                diagnostic.update(status='validating', response=reply.content[:200000],
                                  response_chars=len(reply.content), response_truncated=len(reply.content) > 200000,
                                  usage=reply.usage)
                save_json(diagnostic_path, diagnostic)
                record = parse_json(reply.content)
                if staged:
                    draft = state['proposal_draft']
                    record = assemble(draft, record, request_context['response_schema'], state['transcript'])
                    if draft['step'] != STEPS[-1] and not record.get('questions'):
                        if draft['step'] == 'requirements' and not ready(record):
                            raise ValueError('需求尚不完整，请保留已有需求并提出必要的业务问题')
                        state['proposal_draft'] = {'step': STEPS[STEPS.index(draft['step']) + 1], 'record': record}
                        state.pop('repair_candidate', None)
                        state.pop('failure_kind', None)
                        state['feedback'] = []
                        feedback, repair_candidate, section_failures = [], None, 0
                        diagnostic['status'] = 'section_accepted'
                        persist(directory, state)
                        continue
                if (isinstance(record, dict) and record.get('summary') == ''
                        and all(record.get(key) == [] for key in ('items', 'issues', 'questions'))):
                    diagnostic['failure_kind'] = 'empty_record'
                    raise ValueError('模型只返回空模板（/summary 为空，/items、/issues、/questions 均为空），没有处理本轮用户回答；已有需求保持不变。请依据最新回答保留已有需求并新增输入与约束，再提出必要问题。')
                if (isinstance(record, dict) and record.get('issues') == []
                        and isinstance(record.get('questions'), list) and record['questions']
                        and all(isinstance(q, dict) and isinstance(q.get('issue_id'), str)
                                and isinstance(q.get('question'), str) for q in record['questions'])):
                    # Questions already express the missing information. Derive
                    # their redundant issue entries without inventing business facts.
                    by_id = {}
                    for question in record['questions']:
                        by_id.setdefault(question['issue_id'], []).append(question['question'])
                    record['issues'] = [{'id': identifier, 'kind': 'missing',
                                         'description': '\n'.join(questions), 'blocking': True}
                                        for identifier, questions in by_id.items()]
                    diagnostic['normalization'] = 'derived_empty_issues_from_questions'
                if isinstance(record, dict) and record.get('questions'):
                    # Clarification takes priority over a speculative team plan.
                    record.pop('team_proposal', None)
                validate_record(record, state['transcript'])
                if request_context['phase'] == 'proposal' and ready(record):
                    # Fixed samples replace the per-run input contract, never the deliverables.
                    required = ('team_proposal', 'deliverables') + (() if state['samples'] else ('task_input', 'interaction'))
                    if any(not record.get(key) for key in required):
                        raise ValueError('方案必须包含团队方案、任务输入规范、交互规则和交付物清单；根据已有需求补齐，不要求用户提供实际素材。')
                if len(record['questions']) > 2:
                    raise ValueError('每轮最多两个问题，请只保留当前最关键的业务问题')
                if not record['questions'] and not ready(record):
                    missing = sorted(CORE - {item['category'] for item in record['items']})
                    raise ValueError('尚未记录基本设计信息: ' + ', '.join(missing)
                                     + '。先从已有用户回答整理；只有原文确实不足时才提问，不要为补齐分类重复访谈。')
                # Repeated questions are allowed after an invalid response, not after an answered turn.
                asked = {q['question'] for t in state['transcript'] if t['role'] == 'assistant'
                         for q in t.get('questions', [])}
                if any(q['question'] in asked for q in record['questions']):
                    raise ValueError('不要重复已提问的问题；结合用户回答重新澄清')
                state['revision'] += 1
                state['document'] = record
                state['approval'] = None
                state['status'] = 'awaiting_confirmation' if ready(record) else 'exploring'
                if ready(record) and state.get('team_planning') and 'team_proposal' not in record:
                    state['status'] = 'requirements_ready'
                state['feedback'] = []
                state.pop('repair_candidate', None)
                state.pop('failure_kind', None)
                if staged and record['questions']:
                    state['clarification_draft'] = state['proposal_draft']
                else:
                    state.pop('clarification_draft', None)
                state.pop('proposal_draft', None)
                state['transcript'].append({'turn': len(state['transcript']) + 1, 'role': 'assistant', 'at': now(),
                                            'text': record['summary'], 'questions': record['questions']})
                state.pop('failure_details', None)
                revisions = directory / 'requirements-history'
                revisions.mkdir(exist_ok=True)
                save_json(revisions / f'{state["revision"]:04d}.json', record)
                persist(directory, state)
                diagnostic['status'] = 'awaiting_user' if record['questions'] else 'accepted'
                return state
            except ModelError as exc:
                state['failure_kind'] = 'transport_error'
                state['failure_details'] = failure_details(exc, client)
                state['failure_details'].update({k: v for k, v in diagnostic.get('progress', {}).items() if k.endswith('_chars')})
                feedback = [str(exc)]
                diagnostic.update(status='transport_error', error={'type': type(exc).__name__, 'message': str(exc)})
                if exc.details:
                    diagnostic['error']['details'] = exc.details
                break
            except (ValueError, TypeError, KeyError, RecursionError) as exc:
                section_failures += 1
                state['failure_kind'] = 'validation_error'
                # Keep the complete candidate for targeted repair, never a truncated JSON fragment.
                repair_candidate = reply.content if reply is not None and len(reply.content) <= 200000 else None
                state['repair_candidate'] = repair_candidate
                error = {'type': type(exc).__name__, 'message': str(exc)[:2000]}
                if isinstance(exc, json.JSONDecodeError):
                    error.update(line=exc.lineno, column=exc.colno, position=exc.pos)
                    feedback = [f'模型回答不是合法 JSON：第 {exc.lineno} 行、第 {exc.colno} 列（字符位置 {exc.pos}），{exc.msg}']
                else:
                    feedback = [str(exc)[:2000]]
                diagnostic.update(status='validation_error', error=error)
                report = getattr(client, 'validation_failed', None)
                if callable(report):
                    report(retrying=section_failures < 2 and attempt + 1 < attempts)
                if section_failures >= 2:
                    break
            finally:
                diagnostic['elapsed_seconds'] = round(time.monotonic() - started, 3)
                save_json(diagnostic_path, diagnostic)
        state['feedback'] = feedback
        state['status'] = 'failed'
        persist(directory, state)
        return state


def plan(session_dir, *, client=None, samples=None):
    from astra_designer.api import design_project
    directory = Path(session_dir).resolve()
    with session_lock(directory):
        state = load_session(directory)
        snapshot = confirmed_document(state)
        if samples is not None:
            from astra_designer.pipeline import normalize_inputs
            state['samples'] = normalize_inputs(samples)
        contract = ('deliverables',) + (() if state['samples'] else ('task_input', 'interaction'))
        if any(not snapshot['document'].get(key) for key in contract):
            from astra_designer.discovery.input_contract import complete_contract
            return complete_contract(state, directory, client, fields=contract)
        from astra_designer.contracts.settings import limit_message, load_settings
        limit = load_settings()['max_plan_calls']
        if limit is not None and state['plan_calls'] >= limit:
            raise ValueError(limit_message('蓝图规划', limit))
        from astra_designer.pipeline import MAX_PLAN_ATTEMPTS
        budget = MAX_PLAN_ATTEMPTS if limit is None else min(MAX_PLAN_ATTEMPTS, limit - state['plan_calls'])
        state['plan_calls'] += budget  # reserve even on process interruption
        persist(directory, state)
        destination = directory / 'plans' / f'r{state["revision"]}-{uuid4().hex[:8]}'
        goal = state['transcript'][0]['text']
        result = design_project(goal, destination, inputs=state['samples'], client=client,
                                max_attempts=budget, requirements=snapshot, execution=state.get('execution', {'default_framework': 'auto'}),
                                resume=previous_candidate(state))
        state['plan_calls'] -= budget - result['calls']
        state['plan'] = plan_summary(result, destination)
        state['plans'].append({'requirements_revision': state['revision'], 'path': str(destination),
                               'status': result['status'], 'at': now(),
                               **({'summary': result['summary']} if result['status'] == 'failed' else {})})
        if result['status'] == 'needs_clarification':
            state['transcript'].append({'turn': len(state['transcript']) + 1, 'role': 'assistant', 'at': now(),
                                        'text': result['summary'], 'questions': result['questions']})
            state['status'] = 'exploring'
            state['approval'] = None
        persist(directory, state)
        return result


def previous_candidate(state):
    """The last failed blueprint for the same confirmed requirements, to keep repairing it."""
    last = state.get('plan') or {}
    history = state.get('plans') or []
    if (last.get('status') != 'failed' or not last.get('validation_feedback') or not history
            or history[-1].get('requirements_revision') != state['revision']):
        return None
    try:
        session = json.loads((Path(last['path']) / 'session.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, KeyError):
        return None
    if not session.get('candidate'):
        return None
    return {'candidate': session['candidate'], 'feedback': last['validation_feedback'], 'source': last['path'],
            'notes': session.get('repair_notes', []), 'stalled': session.get('repair_stalled', 0)}


PLAN_FIELDS = ('status', 'summary', 'questions', 'blueprint_path', 'pending_integrations', 'runnable',
               'validation_feedback', 'failure', 'calls')


def plan_summary(result, destination):
    """What the chat needs about the latest plan; the full record stays in plans/<id>/session.json."""
    return {'path': str(destination), **{key: result[key] for key in PLAN_FIELDS if key in result}}
