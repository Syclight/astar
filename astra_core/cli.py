"""Astra command-line entry point for external business projects."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

from astra_core import engine
from astra_core.project import DEFAULT_PROJECT_CONFIG_PATH


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments.append("run")
    elif arguments[0] not in {"run", "inspect", "resume", "schedule", "migrate-state", "-h", "--help"}:
        arguments.insert(0, "run")

    parser = argparse.ArgumentParser(prog="astra", description="Astra 业务项目运行器")
    subparsers = parser.add_subparsers(dest="command")
    migrate_parser = subparsers.add_parser('migrate-state', help='复制旧状态到统一对象存储，保留旧编号映射和原文件')
    migrate_parser.add_argument('project', help='项目目录或 project.yaml')
    migrate_parser.add_argument('legacy_key', help='已存在的旧状态编号')
    migrate_parser.add_argument('--key-path', help='将新 ID 写入业务状态的 JSON Pointer，如 /work_id')
    migrate_parser.add_argument('--document', action='append', default=[], metavar='NAME=PATH',
                                help='目标文件名=项目输出目录中的旧文档路径，可重复指定；同时迁移对应累积记录')

    run_parser = subparsers.add_parser("run", help="运行完整业务工作流")
    run_parser.add_argument("project", nargs="?", default=DEFAULT_PROJECT_CONFIG_PATH, help="project.yaml 路径或项目目录")
    _add_json_option(run_parser)
    _add_input_options(run_parser)

    inspect_parser = subparsers.add_parser("inspect", help="检查业务项目配置与依赖")
    inspect_parser.add_argument("project", nargs="?", default=DEFAULT_PROJECT_CONFIG_PATH, help="project.yaml 路径或项目目录")
    _add_json_option(inspect_parser)

    resume_parser = subparsers.add_parser("resume", help="恢复指定业务项目的一次运行")
    resume_parser.add_argument("project", help="project.yaml 路径或项目目录")
    resume_parser.add_argument("run_id", help="待恢复的运行 ID")
    _add_json_option(resume_parser)
    _add_input_options(resume_parser)
    resume_parser.add_argument('--approve', action='store_true', help='审阅通过（运行在人工审阅处等待时）')
    resume_parser.add_argument('--notes', help='审阅不通过，并给出修改意见')

    run_parser.add_argument('--yes', action='store_true',
                            help='自动确认本单资料（定时运行用）；清理文件清单和人工审阅仍会等待人处理')
    schedule_parser = subparsers.add_parser('schedule', help='按时间表自动运行（Windows 计划任务；Linux/macOS 给出 cron 行）')
    schedule_parser.add_argument('project', help='project.yaml 路径或项目目录')
    schedule_parser.add_argument('--daily', metavar='HH:MM', help='每天几点运行')
    schedule_parser.add_argument('--weekly', metavar='MON,FRI', help='每周哪几天运行，配合 --at')
    schedule_parser.add_argument('--at', metavar='HH:MM', help='--weekly 的运行时间')
    schedule_parser.add_argument('--hourly', type=int, metavar='N', help='每 N 小时运行一次')
    schedule_parser.add_argument('--input', help='每次运行使用的任务资料 JSON（会复制一份到项目 schedules 目录）')
    schedule_parser.add_argument('--name', help='计划任务名称，默认用项目名')
    schedule_parser.add_argument('--remove', action='store_true', help='删除这个计划任务')
    schedule_parser.add_argument('--print-only', action='store_true', help='只生成运行脚本并显示命令，不注册')

    for sub in (run_parser, inspect_parser, resume_parser):
        sub.add_argument("--verbose", "-v", action="store_true", help="显示详细运行日志（时间、源码位置和内部步骤）")
    args = parser.parse_args(arguments)
    if getattr(args, "verbose", False):
        from astra_core.services.console import set_verbose
        set_verbose(True)
    try:
        return _dispatch(args)
    except (FileNotFoundError, ValueError) as exc:
        # Configuration mistakes are user errors: report them without a traceback.
        print(f"astra: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    command = args.command or "run"
    if command == 'migrate-state':
        from astra_core.project import load_business_project
        from astra_core.object_identity import migrate_legacy_state
        project = load_business_project(args.project)
        documents = {}
        for item in args.document:
            name, separator, path = item.partition('=')
            if not separator or not path or name in documents:
                raise ValueError('--document 必须为 NAME=PATH，且目标文件名不能重复')
            documents[name] = path
        result = migrate_legacy_state(project.output_dir, args.legacy_key, key_path=args.key_path, documents=documents)
        print(f"已迁移：{result['legacy_key']} → {result['id']}\n对象目录：{result['path']}\n原文件已保留。团队配置需使用 object.resolve@1 和 object_context=true。")
        return 0

    if command == 'schedule':
        return _schedule(args)

    if command == "inspect":
        _print_result(engine.inspect(args.project), args.json)
        return 0

    if command == "resume":
        runtime = engine.load(args.project)
        values = _read_input(args.input)
        extra = {'task_input': values} if values is not None else {}
        if args.approve or args.notes is not None:
            extra['review'] = {'approved': bool(args.approve), 'notes': args.notes or ''}
        state = runtime.resume(args.run_id, **extra)
        if args.interactive or (not args.json and sys.stdin.isatty()):
            state = _collect_input(runtime, state)
        _print_result(state, args.json)
        return 0 if state.get("status") == "completed" else 1

    runtime = engine.load(args.project)
    values = _read_input(args.input)
    state = runtime.run(**({'task_input': values} if values is not None else {}))
    for _ in range(2):  # --yes confirms the task data only; file lists and reviews still wait for a person
        if not (args.yes and state.get('status') == 'awaiting_confirmation' and not state.get('pending_confirmation')):
            break
        state = runtime.resume(state['data']['run']['id'], confirmed=True)
    if args.interactive or (not args.json and sys.stdin.isatty()):
        state = _collect_input(runtime, state)
    _print_result(state, args.json)
    return 0 if state.get("status") == "completed" else 1


def _schedule(args) -> int:
    from astra_core import schedule
    if args.remove:
        from astra_core.project import resolve_project_file
        name = schedule.job_name(resolve_project_file(args.project), args.name)  # the same name create used
        result = schedule.remove(name)
        print(f'已删除计划任务 Astra\\{name}' if result.get('removed') else result['hint'])
        return 0
    job = schedule.plan(args.project, daily=args.daily, weekly=args.weekly, at=args.at, hourly=args.hourly,
                        input_file=args.input, name=args.name)
    result = schedule.create(job, register=not args.print_only)
    print(f"运行脚本：{job['script']}")
    print(f"运行日志：{job['log']}")
    if result.get('registered'):
        print(f"已创建计划任务 Astra\\{job['name']}（在“任务计划程序”中可查看、停用或删除）。")
    elif result.get('command'):
        print('注册命令：' + ' '.join(result['command']))
    else:
        print('把下面这一行加入 crontab（运行 crontab -e）：')
        print(result['cron'])
    print('提示：模型密钥、发信服务器等环境变量需用“setx 变量名 值”设为永久，计划任务才能读到。')
    return 0


def _add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="以 JSON 输出执行结果")


def _add_input_options(parser):
    parser.add_argument('--input', help='本次任务资料 JSON 文件，不修改项目配置')
    parser.add_argument('--interactive', action='store_true', help='呈现工作流 Agent 请求的资料补充与确认')


def _read_input(path):
    if path is None:
        return None
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('--input 必须为 JSON object')
    return value


def _collect_input(runtime, state):
    try:
        while state.get('status') in {'waiting_input', 'awaiting_confirmation', 'awaiting_review'}:
            run_id = state['data']['run']['id']
            if state['status'] == 'awaiting_review':
                review = _ask_review(state['pending_review'])
                if review is None:
                    return state
                state = runtime.resume(run_id, review=review)
                continue
            if state['status'] == 'awaiting_confirmation':
                pending = state.get('pending_confirmation')
                if isinstance(pending, dict) and pending.get('kind') == 'cleanup':
                    action = {'quarantine': '移入隔离区（可还原）', 'delete': '永久删除', 'restore': '还原到原位置'}.get(
                        pending.get('mode'), pending.get('mode'))
                    if pending['count']:
                        print(f"即将{action}以下 {pending['count']} 个文件，共 {pending['mb']} MB：")
                    for path in pending.get('items', []):
                        print('  ' + path)
                    if pending['count'] > len(pending.get('items', [])):
                        print(f"  ……其余 {pending['count'] - len(pending['items'])} 个见完整清单：{pending['plan']}")
                    if pending.get('purge_count'):
                        print(f"另将永久删除 {pending['purge_count']} 个已到期的隔离批次，共 {pending['purge_mb']} MB（清单同上）。")
                    prompt = '输入“确认”按这份清单执行，其余输入暂不执行（之后可用 astra resume 继续）：'
                else:
                    print(json.dumps(state['data']['task_input'], ensure_ascii=False, indent=2))
                    prompt = '任务资料已齐全，输入“确认”开始执行，其余输入保留待确认状态：'
                if input(prompt).strip() != '确认':
                    return state
                state = runtime.resume(run_id, confirmed=True)
                continue
            questions = state.get('questions', [])
            if not questions:
                print('资料结构不符合规范，请修正 --input 文件：' + '；'.join(state.get('input_errors', [])))
                return state
            question = questions[0]
            spec = question['schema']
            print(f"{runtime.project.name}[{state.get('current_role') or '接单'}]> {question['question']}")
            if 'enum' in spec:
                print('可选值：' + json.dumps(spec['enum'], ensure_ascii=False))
            answer = input('你> ')
            if answer.strip() == '/quit':
                return state
            try:
                value = answer if spec.get('type') == 'string' else json.loads(answer)
                from jsonschema import Draft202012Validator
                errors = list(Draft202012Validator(spec).iter_errors(value))
                if errors:
                    print('输入不符合要求：' + errors[0].message)
                    continue
            except ValueError:
                print('此字段请使用 JSON 值，例如数字、true/false、数组或对象。')
                continue
            state = runtime.resume(run_id, task_input={**state['data'].get('task_input', {}), question['field']: value})
    except (EOFError, KeyboardInterrupt):
        print('\n已保存本次任务，可通过 astra resume 恢复。')
    return state


def _ask_review(pending: Dict[str, Any]):
    """Show what to review; “通过” approves, any other text is the revision notes, /quit reviews later."""
    print(f"\n【{pending.get('title') or '请审阅'}】")
    print(pending.get('excerpt', ''))
    if pending.get('file'):
        print(f"（完整内容：{pending['file']}）")
    images = [path for path in pending.get('images') or [] if isinstance(path, str)]
    if images:
        print(f"共 {len(images)} 张图片：")
        for path in images:
            print('  ' + path)
        _open_images(images)
    answer = input('输入“通过”确认；需要修改就直接写修改意见（/quit 稍后再审）：').strip()
    if not answer or answer == '/quit':
        return None
    if answer in {'通过', '确认', '同意', 'ok', 'OK'}:
        return {'approved': True, 'notes': ''}
    return {'approved': False, 'notes': answer}


def _open_images(paths, limit=8):
    """Open the pictures in the system viewer so they can be looked at before answering (Windows and macOS)."""
    if os.environ.get('ASTRA_REVIEW_OPEN', '1') == '0':
        return
    import subprocess
    for path in paths[:limit]:
        try:
            if os.name == 'nt':
                os.startfile(path)  # the default image viewer
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
        except OSError:
            return  # a missing viewer must not block the review; the paths are printed above
    if len(paths) > limit:
        print(f"（已打开前 {limit} 张，其余请按上面的路径查看）")


def _print_result(value: Any, enabled: bool) -> None:
    if enabled:
        _print_json(value)
    elif isinstance(value, dict) and value.get('status') == 'awaiting_review':
        print(f"运行在“{(value.get('pending_review') or {}).get('title') or '人工审阅'}”处等待审阅。运行 ID：{value['data']['run']['id']}")
        print('继续：astra resume <project.yaml> <运行ID>（交互审阅），或加 --approve / --notes "修改意见"')
    elif isinstance(value, dict) and value.get('status') == 'waiting_dependencies':
        print(value['dependency_message'])
        print('运行 ID：' + value['data']['run']['id'])
        print('接入后使用 astra resume <project.yaml> <运行ID> 继续，无需重新填写资料。')
    elif isinstance(value, dict) and isinstance((value.get('data') or {}).get('run'), dict):
        _print_run_summary(value)


def _output_files(value: Any, root: Path) -> List[str]:
    """Business files the run wrote, e.g. a report path or document.write's files."""
    if isinstance(value, dict):
        return [path for item in value.values() for path in _output_files(item, root)]
    if isinstance(value, list):
        return [path for item in value for path in _output_files(item, root)]
    if isinstance(value, str) and len(value) < 1000 and '\n' not in value:
        try:
            path = Path(value).resolve()
            if path.is_relative_to(root) and path.is_file():
                return [str(path)]
        except (OSError, ValueError):
            pass
    return []


def _print_run_summary(state: Dict[str, Any]) -> None:
    run = state['data']['run']
    status = state.get('status')
    print(('运行完成' if status == 'completed' else f'运行结束（状态：{status}）') + f"。运行 ID：{run.get('id')}")
    root = Path(run.get('output_dir') or '.').resolve()
    business = {key: item for key, item in state['data'].items()
                if key not in {'run', 'task', 'task_input'} and not key.startswith('hook_')}
    files = list(dict.fromkeys(_output_files(business, root)))
    for path in files:
        print('输出文件：' + path)
    for item in business.values():
        if isinstance(item, dict) and {'mode', 'handled', 'skipped', 'affected_mb'} <= set(item):  # a cleanup record
            label = {'dry_run': '试算，未改动文件', 'quarantine': '已移入隔离区', 'delete': '已删除', 'restore': '已还原'}.get(item['mode'], item['mode'])
            print(f"清理结果：{label}，处理 {len(item['handled'])} 个文件（{item['affected_mb']} MB），"
                  f"跳过 {len(item['skipped'])} 个" + (f"；隔离区：{item['quarantine_dir']}" if item.get('quarantine_dir') else ''))
    shown_ids = set()
    for item in business.values():
        if isinstance(item, dict) and set(item) == {'id', 'is_new'} and isinstance(item.get('id'), str) and item['id'].startswith('obj_'):
            print(f"业务对象编号：{item['id']}（以后继续时使用此编号）")
            shown_ids.add(item['id'])
    for item in business.values():
        if isinstance(item, dict) and item.get('generated') is True and {'key', 'saved_at'} <= set(item) and item['key'] not in shown_ids:  # a new saved record
            field = item.get('field')
            spec = ((state.get('task_contract') or {}).get('schema') or {}).get('properties', {}).get(field) or {}
            where = f"“{spec.get('description') or field}”" if field else '编号'
            print(f"新建的编号：{item['key']}（请记下，以后继续时在{where}中输入）")
    print('运行记录：' + str(root) + ('（业务结果在 state.json 的 data 中）' if not files else ''))
    for error in (state.get('errors') or [])[:3]:
        print('错误：' + str(error))


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
