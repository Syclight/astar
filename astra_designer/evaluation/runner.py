"""Run the evaluation cases with real models and score every step.

For each case: confirmed requirements -> plan (the designer model, with the usual repair rounds)
-> generate -> run with sample input (the project model) -> blueprint acceptance -> case checks.
Confirmations and reviews are answered automatically, as a cooperative user would.
"""
import contextlib
import datetime
import io
import json
import os
import time
import traceback
from pathlib import Path

WEIGHTS = {'design': 40, 'generate': 10, 'run': 20, 'acceptance': 15, 'checks': 15}


class CannedModel:
    """Returns the confirmed requirements once, standing in for the requirements conversation."""

    def __init__(self, reply):
        self.reply = reply

    def complete(self, messages):
        from astra_core.llm.protocol import ModelReply
        return ModelReply(json.dumps(self.reply, ensure_ascii=False), {})


def missing_needs(case):
    return [name for name in case['needs'] if not os.environ.get(name)]


def run_project(project, task_input, run_limit=8):
    """Run once, answering confirmations and reviews the way a cooperative user would."""
    from astra_core import engine
    runtime = engine.load(str(project))
    state = runtime.run(export_structure=False, task_input=task_input)
    for _ in range(run_limit):
        status, run_id = state.get('status'), state['data']['run']['id']
        if status == 'awaiting_confirmation':
            state = runtime.resume(run_id, confirmed=True)
        elif status == 'awaiting_review':
            state = runtime.resume(run_id, review={'approved': True, 'notes': ''})
        else:
            break
    return state


def plan_case(case, folder, client, plan_rounds):
    from astra_designer.discovery.pipeline import explore, plan
    session = folder / 'session'
    first = explore(case['goal'], session, client=CannedModel(case['document']))
    if first.get('status') != 'awaiting_confirmation':
        raise ValueError(f"用例的需求记录无效：{first.get('status')} {first.get('failure') or first.get('summary')}")
    explore(None, session, action='confirm', revision=first['revision'])
    rounds, result = 0, None
    for rounds in range(1, plan_rounds + 1):
        result = plan(session, client=client)
        if result['status'] != 'failed':
            break
    return result, rounds


def evaluate_case(case, folder, client, project_profile, *, plan_rounds=3):
    folder.mkdir(parents=True, exist_ok=True)
    record = {'id': case['id'], 'title': case['title'], 'steps': {}, 'notes': []}
    started = time.monotonic()
    skipped = missing_needs(case)
    if skipped:
        record.update(status='skipped', score=None, notes=[f"未配置 {'、'.join(skipped)}，跳过"])
        return record
    try:
        result, rounds = plan_case(case, folder, client, plan_rounds)
        record['plan'] = {'status': result['status'], 'rounds': rounds, 'calls': result.get('calls'),
                          'seconds': round(time.monotonic() - started, 1),
                          'pending': [item.get('title') for item in result.get('pending_integrations') or []]}
        record['steps']['design'] = result['status'] == 'ready'
        if not record['steps']['design']:
            record['notes'] += [str(item) for item in (result.get('validation_feedback') or [result.get('summary')])[:3]]
            return finish(record, started)
        from astra_designer import generate_project, verify_run
        import yaml
        name = yaml.safe_load(Path(result['blueprint_path']).read_text(encoding='utf-8'))['project']['name']
        project = generate_project(result['blueprint_path'], folder / 'project' / name, model_config=project_profile)
        record['steps']['generate'] = True
        data = folder / 'data'
        data.mkdir(exist_ok=True)
        state, states = None, []
        for run in range(1, case['runs'] + 1):
            ran = time.monotonic()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                state = run_project(project, case['prepare'](data, run))
            states.append(state)
            record.setdefault('runs', []).append({'status': state.get('status'), 'seconds': round(time.monotonic() - ran, 1)})
            if state.get('status') != 'completed':
                break
        record['steps']['run'] = state.get('status') == 'completed'
        if not record['steps']['run']:
            record['notes'] += [str(error.get('error', error))[:300] for error in (state.get('errors') or [])[:2]]
            record['notes'] += [state.get('dependency_message') or ''] if state.get('status') == 'waiting_dependencies' else []
            return finish(record, started)
        acceptance = verify_run(project, state)
        record['steps']['acceptance'] = acceptance['passed']
        if not acceptance['passed']:
            record['notes'] += [f"验收未通过：{item['path']}" for item in acceptance['checks'] if not item.get('passed')][:3]
        checks = case['check'](state, data, states)
        record['checks'] = [{'name': name, 'passed': passed, 'detail': detail} for name, passed, detail in checks]
        record['steps']['checks'] = sum(passed for _, passed, _ in checks) / len(checks) if checks else 1
    except Exception as exc:  # one broken case must not stop the others
        record['notes'].append(f'{type(exc).__name__}: {exc}'[:500])
        (folder / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
    return finish(record, started)


def finish(record, started):
    steps = record['steps']
    record['score'] = round(sum(weight * float(steps.get(name, 0)) for name, weight in WEIGHTS.items()), 1)
    # Passing means the blueprint's acceptance and every content check held, not just that it ran.
    record['status'] = 'passed' if steps.get('acceptance') and steps.get('checks') == 1 else 'failed'
    record['seconds'] = round(time.monotonic() - started, 1)
    return record


def evaluate(output, client, project_profile, *, only=None, plan_rounds=3, cases=None):
    from astra_designer.evaluation.cases import CASES
    chosen = [case for case in (cases or CASES) if not only or case['id'] in only]
    if only and len(chosen) != len(only):
        known = '、'.join(case['id'] for case in (cases or CASES))
        raise ValueError(f'未知用例；可用：{known}')
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    root = Path(output).resolve() / stamp
    records = []
    for case in chosen:
        print(f"▸ {case['title']}（{case['id']}）", flush=True)
        record = evaluate_case(case, root / case['id'], client, project_profile, plan_rounds=plan_rounds)
        records.append(record)
        print(f"  {'通过' if record['status'] == 'passed' else '跳过' if record['status'] == 'skipped' else '未通过'}"
              + (f"，{record['score']} 分" if record['score'] is not None else ''), flush=True)
    report = {'created_at': stamp, 'model': getattr(client, 'model', None), 'cases': records, 'summary': summary(records)}
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (root / 'report.md').write_text(markdown(report), encoding='utf-8')
    return root, report


def summary(records):
    counted = [record for record in records if record['status'] != 'skipped']
    rate = lambda step: sum(1 for record in counted if record['steps'].get(step)) / len(counted) if counted else 0
    plans = [record['plan'] for record in counted if record.get('plan')]
    return {'cases': len(counted), 'skipped': len(records) - len(counted),
            'passed': sum(1 for record in counted if record['status'] == 'passed'),
            'average_score': round(sum(record['score'] for record in counted) / len(counted), 1) if counted else 0,
            'design_rate': round(rate('design'), 2), 'run_rate': round(rate('run'), 2),
            'acceptance_rate': round(rate('acceptance'), 2),
            'average_plan_calls': round(sum(plan.get('calls') or 0 for plan in plans) / len(plans), 1) if plans else 0,
            'average_plan_seconds': round(sum(plan['seconds'] for plan in plans) / len(plans), 1) if plans else 0}


def markdown(report):
    total = report['summary']
    lines = [f"# Astra 评测报告（{report['created_at']}）", '',
             f"设计模型：{report.get('model') or '未知'}", '',
             f"- 用例 {total['cases']} 个（跳过 {total['skipped']} 个），通过 {total['passed']} 个，平均 {total['average_score']} 分",
             f"- 设计成功率 {total['design_rate']:.0%}，运行完成率 {total['run_rate']:.0%}，验收通过率 {total['acceptance_rate']:.0%}",
             f"- 规划平均 {total['average_plan_calls']} 次模型调用、{total['average_plan_seconds']} 秒", '',
             '| 用例 | 结果 | 得分 | 设计 | 生成 | 运行 | 验收 | 成果检查 | 规划调用 | 用时（秒） |', '|---|---|---|---|---|---|---|---|---|---|']
    mark = lambda value: '—' if value is None else ('✓' if value is True else '✗' if value is False else f'{value:.0%}')
    for record in report['cases']:
        steps, plan = record['steps'], record.get('plan') or {}
        lines.append(f"| {record['title']} | {'通过' if record['status'] == 'passed' else '跳过' if record['status'] == 'skipped' else '未通过'} "
                     f"| {'—' if record['score'] is None else record['score']} "
                     + ' '.join(f"| {mark(steps.get(name))}" for name in WEIGHTS)
                     + f" | {plan.get('calls', '—')} | {record.get('seconds', '—')} |")
    lines += ['', '## 未通过的原因', '']
    for record in report['cases']:
        failed = [check for check in record.get('checks', []) if not check['passed']]
        if record['status'] == 'failed' or failed:
            lines.append(f"### {record['title']}")
            lines += [f'- {note}' for note in record['notes'] if note]
            lines += [f"- 成果检查未通过：{check['name']}（{check['detail']}）" for check in failed]
            lines.append('')
    lines += ['## 评分方法', '', '设计 40、生成 10、运行完成 20、蓝图验收 15、成果检查 15（按通过项比例）；蓝图验收和全部成果检查都通过才记为通过。',
              '需求记录由用例直接给出（相当于已确认的需求），评测的是规划、生成与运行；需求访谈的质量不在本评测中。', '']
    return '\n'.join(lines)
