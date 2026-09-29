"""Bounded subprocess trials for unchanged generated projects (not an OS sandbox)."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from astra_designer.contracts.session import save_json, session_lock


def check_generated(project):
    root = project.parent
    manifest = json.loads((root / 'generation.json').read_text(encoding='utf-8'))
    files = manifest.get('files')
    if not isinstance(files, dict) or 'project.yaml' not in files:
        raise ValueError('无效的生成记录')
    changed = []
    for name, expected in files.items():
        path = (root / name).resolve()
        if name == 'configs/model.json' and manifest.get('editable_model_config') == name:
            from astra_core.llm.project_config import load_profile
            load_profile(root, name)
            continue
        if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            changed.append(name)
    if changed:
        raise ValueError('生成文件已变化，请从蓝图生成新版本: ' + ', '.join(changed))
    return hashlib.sha256((root / 'generation.json').read_bytes()).hexdigest()


def trial_project(project_file, *, timeout=600, max_attempts=1, resume=None, model=None):
    """Retry only failed runtime states with a known checkpoint; never repair assertions."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        raise ValueError('timeout 必须在 0 到 3600 秒之间')
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError('max_attempts 必须为 1 到 3')
    project = Path(project_file).resolve()
    if project.name != 'project.yaml':
        raise ValueError('请指定生成项目的 project.yaml')
    if resume is not None and (not resume or '/' in resume or '\\' in resume or resume in {'.', '..'}):
        raise ValueError('resume 必须是单个运行 ID')
    with session_lock(project.parent / 'output' / 'trials'):
        digest = check_generated(project)
        directory = project.parent / 'output' / 'trials' / uuid4().hex
        directory.mkdir()
        report = {'version': 1, 'project': str(project), 'generation_sha256': digest,
                  'status': 'running', 'passed': False, 'attempts': [], 'report_path': str(directory / 'report.json')}
        save_json(directory / 'report.json', report)
        import yaml
        project_manifest = yaml.safe_load(project.read_text(encoding='utf-8'))
        if 'model_config' in project_manifest:
            from astra_core.llm.project_config import load_profile
            profile = load_profile(project.parent, project_manifest['model_config'])
            report['model_config_sha256'] = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
        environment = dict(os.environ)
        if 'model_config' not in project_manifest and model is not None and not any(k in environment for k in ('ASTRA_RUNTIME_BASE_URL', 'ASTRA_RUNTIME_MODEL')):
            for field in ('base_url', 'model', 'timeout', 'max_output_tokens', 'token_parameter', 'api_key', 'stream', 'response_format', 'reasoning_effort', 'max_response_bytes', 'max_content_bytes'):
                environment['ASTRA_RUNTIME_' + field.upper()] = str(getattr(model, field))
        started = time.monotonic()
        for index in range(max_attempts):
            result_file = directory / f'attempt-{index + 1}.json'
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                report['status'] = 'timeout'
                break
            command = [sys.executable, '-X', 'utf8', '-c',
                       'import sys; sys.path.insert(0, sys.argv.pop(1)); from astra_designer.validation.trial import worker; worker()',
                       str(Path(__file__).resolve().parents[2]), str(project), str(result_file), resume or '']
            try:
                completed = subprocess.run(command, capture_output=True, encoding='utf-8', errors='replace',
                                           timeout=remaining, env=environment)
                if result_file.is_file():
                    attempt = json.loads(result_file.read_text(encoding='utf-8'))
                else:
                    attempt = {'status': 'process_failed', 'passed': False, 'returncode': completed.returncode}
            except subprocess.TimeoutExpired:
                attempt = {'status': 'timeout', 'passed': False}
            except OSError as exc:
                attempt = {'status': 'process_failed', 'passed': False, 'error_type': type(exc).__name__}
            report['attempts'].append(attempt)
            report.update(status=attempt['status'], passed=attempt['passed'])
            save_json(directory / 'report.json', report)
            if attempt['status'] != 'runtime_failed' or not attempt.get('run_id'):
                break
            resume = attempt['run_id']
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        save_json(directory / 'report.json', report)
        return report


def worker():
    from astra import engine
    from astra_designer.validation.acceptance import verify
    project, destination, resume = sys.argv[1:]
    result = {'status': 'load_failed', 'passed': False}
    try:
        runtime = engine.load(project)
        state = runtime.resume(resume) if resume else runtime.run(export_structure=False)
        result['run_id'] = state.get('data', {}).get('run', {}).get('id')
        result['runtime_status'] = state.get('status')
        if state.get('status') in {'waiting_input', 'awaiting_confirmation', 'awaiting_review', 'waiting_dependencies'}:
            result['status'] = state['status']
            result['dependency_message'] = state.get('dependency_message')
            save_json(Path(destination), result)
            return
        result['current_stage'] = state.get('current_stage')
        result['failed_nodes'] = [{'stage': item.get('stage'), 'node': item.get('node')}
                                  for item in state.get('errors', []) if isinstance(item, dict)]
        result['status'] = 'runtime_failed' if state.get('status') != 'completed' else 'acceptance_failed'
        acceptance = verify(Path(project), state)
        result['acceptance'] = acceptance
        result['passed'] = acceptance['passed']
        if result['passed']:
            result['status'] = 'passed'
        save_json(Path(state['data']['run']['output_dir']) / 'acceptance_report.json', acceptance)
    except Exception as exc:
        # Avoid copying provider responses or credentials into diagnostics.
        result['error_type'] = type(exc).__name__
    save_json(Path(destination), result)
