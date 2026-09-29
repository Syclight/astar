"""User-facing discovery before executable blueprint planning."""
import json
from pathlib import Path
from uuid import uuid4

from astra_designer.cli.config import DEFAULT_CONFIG, configured_model, configure
from astra_designer.cli.streaming import live_model, terminal_text, summary_text, question_text
from astra_designer.contracts.session import session_lock
from astra_designer.discovery.pipeline import explore, plan, load_session, persist
from astra_designer.discovery.versions import design_label


HELP = '''直接用文字描述业务目标或回答问题，无需先准备文件。

需求与方案
  /requirements            查看当前需求、待解决问题和任务输入规范
  /team                    查看团队方案与角色分工
  /select 方案ID           在多个方案中切换（切换后需重新确认）
  确认 或 /confirm         确认当前需求与团队方案
  继续                     需求整理完成后，获取团队方案
  /retry                   重试上一次未完成的处理

规划与运行
  /plan                    将已确认的需求规划为项目蓝图
  /show                    查看蓝图
  /generate                生成项目文件
  /run                     试运行已生成的项目
  /run-resume 运行ID       恢复指定运行
  /input                   添加本地 JSON 样例（一般不需要）
  /inputs                  查看已添加的样例

会话
  /new                     开始新的需求
  /resume 会话目录         打开已有会话
  /export 路径             导出对话为 TXT 文件

模型与技术设置
  /model                   选择模型（Ctrl+L）
  /config                  编辑模型连接参数
  /status                  查看当前模型和本会话用量
  /frameworks              查看支持的执行框架
  /framework ID [Agent]    设置执行框架（高级，默认 auto 自动选择）
  /execution               查看当前执行框架设置
  /capabilities [关键词]   查看可用能力

/help 查看帮助 · /quit 保存并退出'''
# Hidden aliases kept for compatibility: /continue = /retry, /recommend = 继续, /exit = /quit.
COMMANDS = [line.split()[0] for line in HELP.splitlines() if line.strip().startswith('/')] + [
    '/help', '/quit', '/continue', '/recommend', '/exit']

CATEGORY_LABELS = {'goal': '目标', 'scope': '范围', 'actor': '参与者', 'input': '输入', 'output': '输出',
                   'rule': '规则', 'constraint': '约束', 'preference': '偏好', 'acceptance': '验收',
                   'scenario': '场景'}
STATUS_LABELS = {'exploring': '澄清中', 'requirements_ready': '需求已整理', 'awaiting_confirmation': '待确认',
                 'confirmed': '已确认', 'failed': '待重试'}
PRIORITY_LABELS = {'delivery': '交付', 'review': '检查', 'exploration': '探索'}


def failure_reason(kind, message, details=None):
    """One actionable sentence about why the last model step failed."""
    details = details or {}
    message = terminal_text(message or '').strip()
    if details.get('finish_reason') == 'length' or 'finish_reason=length' in message:
        limit = details.get('max_output_tokens')
        text = '模型输出达到长度上限' + (f'（max_output_tokens={limit:,}）' if isinstance(limit, int) else '') + '，回复被截断。'
        thinking, answer = details.get('reasoning_chars'), details.get('content_chars')
        raise_limit = '，或调高 max_output_tokens（服务端上下文长度需足够）'
        if isinstance(thinking, int) and isinstance(answer, int):
            text += f'本次思考 {thinking:,} 字符、正文 {answer:,} 字符。'
            if thinking > answer:
                return text + f'大部分输出额度被思考占用，建议在 /config 把 reasoning_effort 设为 none{raise_limit}。'
        return text + f'建议在 /config 把 reasoning_effort 设为 none{raise_limit}，或在 /model 换用其他模型。'
    if kind == 'transport_error':
        return '模型服务请求失败：' + (message or '未返回错误信息') + '。请检查网络、地址和密钥（/status）。'
    if kind == 'validation_error':
        first = message.splitlines()[0] if message else ''
        return '模型回复未通过格式检查，自动修复后仍不合格' + (f'（{first[:160]}）' if first else '') + '。'
    return message or '原因未记录。'


def last_failure(state):
    if state.get('status') not in {'failed', 'budget_exhausted'}:
        return None
    feedback = state.get('feedback') or []
    return failure_reason(state.get('failure_kind'), feedback[0] if feedback else '', state.get('failure_details'))


def show_requirements(state, directory=None):
    """The requirement ledger in business terms; evidence stays in requirements.yaml."""
    reason = last_failure(state)
    if reason:
        print('上次处理未完成：' + reason)
        print('已保存的需求如下，输入 /retry 重试。\n')
    doc = state.get('document')
    if not doc:
        print('尚未整理出需求。请先描述业务目标。')
        return
    status = STATUS_LABELS.get(state['status'], state['status'])
    print(f'需求（{design_label(state)} · {status}）')
    groups = {}
    for item in doc['items']:
        groups.setdefault(item['category'], []).append(item['statement'])
    for category, label in CATEGORY_LABELS.items():
        for index, statement in enumerate(groups.get(category, [])):
            # Full-width padding keeps CJK labels of 2–3 characters aligned.
            print(f"  {(label if index == 0 else ''):　<3}  {terminal_text(statement)}")
    if doc['issues']:
        print('\n待解决问题：')
        for issue in doc['issues']:
            print(f"  · {'需回答' if issue['blocking'] else '可稍后'}：{terminal_text(issue['description'])}")
    if doc.get('task_input'):
        print('\n每次运行需要的资料：')
        print_task_input(doc)
    if doc.get('deliverables'):
        print('\n每次运行交付的成果：')
        print_deliverables(doc)
    if directory is not None:
        print(f"\n完整记录（含每条需求的原话依据）：{Path(directory) / 'requirements.yaml'}")
        if reason and state.get('diagnostics_path'):
            print('调用诊断：' + state['diagnostics_path'])


def print_task_input(doc):
    schema = doc['task_input']
    when = {}
    for rule in schema.get('x-astra-required-when') or []:
        for key in rule['required']:
            when.setdefault(key, []).append(f"{rule['field']} 为 {'、'.join(map(str, rule['values']))} 时必填")
    for key, field in schema['properties'].items():
        required = '必填' if key in schema.get('required', []) else '；'.join(when[key]) if key in when else '可选'
        print(f"  · {terminal_text(field.get('description', key))}（{required}）")
    rules = doc.get('interaction', {})
    print('  资料不足时' + ('等待补充' if rules.get('on_missing', 'ask') == 'ask' else '直接报错，不开始执行')
          + '；' + ('执行前需确认本单资料' if rules.get('confirm_before_run') else '资料合格后直接执行') + '。')


FORMAT_LABELS = {'txt': 'TXT', 'md': 'Markdown', 'docx': 'Word（docx）', 'pdf': 'PDF', 'csv': 'CSV', 'xlsx': 'Excel（xlsx）', 'image': '图片'}


def print_deliverables(doc):
    for item in doc.get('deliverables', []):
        form = {'file': '文件：' + '、'.join(FORMAT_LABELS.get(f, f) for f in item.get('formats', [])),
                'action': '实际执行的动作', 'data': '运行记录中的数据'}[item['form']]
        print(f"  · {terminal_text(item['name'])}（{form}）：{terminal_text(item['description'])}")
        if item.get('evidence'):
            print(f"      验证方式：{terminal_text(item['evidence'])}")


def show_team(state):
    proposal = (state.get('document') or {}).get('team_proposal')
    if not proposal:
        print('尚无团队方案。需求整理完成后输入“继续”获取。')
        return
    selected = state.get('team_choice') or proposal['recommended_id']
    print('团队方案')
    if proposal.get('reason'):
        print('推荐理由：' + terminal_text(proposal['reason']))
    for option in proposal['options']:
        marks = [mark for mark, on in (('推荐', option['id'] == proposal['recommended_id']),
                                       ('当前选择', option['id'] == selected)) if on]
        print(f"\n{terminal_text(option['label'])}（ID：{option['id']}{'，' + '，'.join(marks) if marks else ''}）")
        print('  交付：' + terminal_text('；'.join(option['deliverables'])))
        for role in option['roles']:
            print(f"  · {role['id']}［{PRIORITY_LABELS.get(role['priority'], role['priority'])}］"
                  + terminal_text(role['responsibility']))
        if option.get('tradeoffs'):
            print('  取舍：' + terminal_text('；'.join(option['tradeoffs'])))
    if len(proposal['options']) > 1:
        print('\n输入 /select 方案ID 切换方案。')


def display(state, *, preview=''):
    """Show the next conversational step, not the internal requirements ledger."""
    doc = state.get('document')
    status = state['status']
    reason = last_failure(state)
    if reason:
        print('Astra-Designer> 本轮未完成，你的输入已保存。')
        print('原因：' + reason)
        print('输入 /retry 重试。')
        return
    if not doc:
        return
    summary = summary_text(doc['summary'])
    if summary[:400] != summary_text(preview):
        stage = '方案' if status == 'awaiting_confirmation' else '需求'
        print(f'Astra-Designer[{stage}]> ' + (summary if len(summary) <= 400 else summary[:400] + '…'))
    if doc['questions']:
        print()
        for index, question in enumerate(doc['questions'][:2], 1):
            prefix = f'{index}. ' if len(doc['questions']) > 1 else ''
            print(prefix + question_text(question['question']))
        print('\n直接回答即可，也可以举一个具体例子。')
        return
    if status in {'requirements_ready', 'awaiting_confirmation'}:
        print(f'\n{design_label(state)}，目前整理的需求：')
        for item in doc['items']:
            print('  · ' + terminal_text(item['statement']))
        if doc.get('task_input'):
            print('\n每次运行需要的资料：')
            print_task_input(doc)
        if doc.get('deliverables'):
            print('\n每次运行交付的成果：')
            print_deliverables(doc)
    proposal = doc.get('team_proposal')
    if proposal and status == 'awaiting_confirmation':
        selected = state.get('team_choice') or proposal['recommended_id']
        print('\n团队方案：')
        for option in proposal['options']:
            tag = '（当前选择）' if option['id'] == selected and len(proposal['options']) > 1 else ''
            roles = '、'.join(role['id'] for role in option['roles'])
            print(f"  {terminal_text(option['label'])}{tag}：{len(option['roles'])} 个角色（{roles}）")
            print('    交付：' + terminal_text('；'.join(option['deliverables'])))
        print('角色分工见 /team。')
    if status == 'requirements_ready':
        print('\n以上理解是否准确？可以直接修改，或输入“继续”获取团队方案。')
    elif status == 'awaiting_confirmation':
        print('\n输入“确认”接受需求和团队方案，或直接说明需要修改的地方。')
    elif status == 'confirmed':
        print('需求已确认。输入 /plan 生成项目蓝图。')


def show_frameworks():
    labels = {'generatable': '可生成', 'runtime_only': '仅运行时，需手写 Agent'}
    from astra_designer.frameworks.registry import describe
    for identifier, entry in describe().items():
        print(f"{identifier}  {entry['title']}（{labels.get(entry['status'], entry['status'])}）")
        if entry.get('limitations'):
            print('  限制：' + '；'.join(entry['limitations']))
        if entry.get('dependencies'):
            print('  依赖：' + '、'.join(entry['dependencies']))


def show_capabilities(query):
    from astra_designer.catalog.search import search_capabilities
    rows = search_capabilities(query)
    if not rows:
        print('没有匹配的能力。')
    for row in rows:
        print(f"{row['id']}  {row['title']}：{row['description']}")


def chat(config_path=DEFAULT_CONFIG, session_dir=None):
    print('Astra 项目设计器\n描述你希望业务项目完成什么，我会逐步澄清需求并设计项目。\n/help 查看命令 · /quit 保存并退出\n')
    directory = Path(session_dir).resolve() if session_dir else None
    state, client = None, None
    credentials = {}
    from astra_designer.cli.models import select_model, show_status, status
    from astra_designer.cli.tui import ChatInput
    reader = ChatInput(lambda: status(config_path, client), COMMANDS)
    show_status(config_path)

    def model():
        nonlocal client
        if client is None:
            client = live_model(configured_model(config_path, interactive=True, credentials=credentials))
        return client

    try:
        if directory and (directory / 'discovery.json').exists():
            state = load_session(directory)
            display(state)
        while True:
            text = reader.read().strip()
            if not text:
                continue
            from contextlib import nullcontext
            from astra_designer.cli.history import record_turn
            recording = nullcontext() if text.split(' ', 1)[0] in {'/config', '/model', '/status'} else record_turn(lambda: directory, text)
            with recording:
                try:
                    command, _, argument = text.partition(' ')
                    if directory and (directory / 'discovery.json').exists():
                        state = load_session(directory)
                    if state and text == '确认' and state['status'] == 'awaiting_confirmation':
                        command, argument = '/confirm', str(state['revision'])
                    elif state and text == '继续' and state['status'] == 'requirements_ready':
                        command = '/recommend'
                    elif state and text in {'确认', '继续'}:
                        # A bare keyword at the wrong step is a navigation slip, not a new requirement.
                        hint = {'requirements_ready': '需求已整理。输入“继续”获取团队方案，方案给出后再确认。',
                                'awaiting_confirmation': '团队方案已给出。输入“确认”接受，或直接说明需要修改的地方。',
                                'confirmed': '需求与团队方案已确认。输入 /plan 生成项目蓝图。',
                                'failed': '上次处理未完成，输入 /retry 重试。'}.get(state['status'])
                        if hint:
                            print(hint)
                            continue
                    if command in {'/quit', '/exit'}:
                        return 0
                    if command == '/help':
                        print(HELP)
                        continue
                    if command == '/export':
                        from astra_designer.cli.export import export_conversation
                        target = export_conversation(directory, argument.strip().strip('"'))
                        print(f'对话已导出：{target}')
                        continue
                    if command == '/config':
                        configure(config_path)
                        client = None
                        show_status(config_path)
                        continue
                    if command == '/model':
                        if select_model(config_path, credentials=credentials):
                            client = None
                        continue
                    if command == '/status':
                        show_status(config_path, client)
                        if directory is not None:
                            from astra_designer.cli.usage import design_usage_line
                            print(design_usage_line(directory))
                        continue
                    if command == '/new':
                        directory, state = None, None
                        print('已开始新会话，请描述新的业务目标。')
                        continue
                    if command == '/resume':
                        target = Path(argument.strip().strip('"')).resolve()
                        restored = load_session(target)
                        directory, state = target, restored
                        display(state)
                        continue
                    if command == '/frameworks':
                        show_frameworks()
                        continue
                    if command == '/capabilities':
                        show_capabilities(argument)
                        continue
                    if command.startswith('/') and state is None:
                        print('请先描述业务目标，建立需求会话。')
                        continue
                    if command == '/execution':
                        execution = state.get('execution', {'default_framework': 'auto'})
                        print('默认执行框架：' + execution.get('default_framework', 'auto'))
                        for agent, framework in execution.get('agents', {}).items():
                            print(f'  {agent}：{framework}')
                        continue
                    if command == '/framework':
                        from astra_designer.frameworks.registry import check_choice
                        values = argument.split()
                        if len(values) not in (1, 2):
                            raise ValueError('用法：/framework 框架ID [AgentID]')
                        check_choice(values[0])
                        with session_lock(directory):
                            state = load_session(directory)
                            execution = state.setdefault('execution', {'default_framework': 'auto'})
                            if len(values) == 1:
                                execution['default_framework'] = values[0]
                            else:
                                execution.setdefault('agents', {})[values[1]] = values[0]
                            state['plan'] = None
                            persist(directory, state)
                        print('执行框架已保存，需求确认不受影响。请重新 /plan 生成蓝图。')
                        continue
                    if command == '/requirements':
                        show_requirements(state, directory)
                        continue
                    if command == '/team':
                        show_team(state)
                        continue
                    if command == '/select':
                        if len(argument.split()) != 1:
                            raise ValueError('用法：/select 方案ID，方案 ID 见 /team')
                        state = explore(None, directory, action='select', revision=state['revision'], choice=argument.strip())
                        chosen = next(o for o in state['document']['team_proposal']['options'] if o['id'] == state['team_choice'])
                        print(f"已切换到方案「{terminal_text(chosen['label'])}」。输入“确认”接受，或 /team 查看角色分工。")
                        continue
                    if command == '/recommend':
                        state = explore('请根据已整理的需求给出团队方案。', directory, client=model(), command='继续')
                        display(state, preview=getattr(client, 'last_summary', ''))
                        continue
                    if command == '/inputs':
                        if not state['samples']:
                            print('尚未添加样例。可用 /input 添加本地 JSON 样例。')
                        for key, item in state['samples'].items():
                            print(f"{key}：{item['path']}（Schema：{item['schema']}）")
                        continue
                    if command == '/input':
                        from astra_designer.pipeline import normalize_inputs
                        from astra_designer.catalog.registry import schemas
                        from jsonschema import Draft202012Validator
                        key = input('样例资源标识 [customer_feedback]: ').strip() or 'customer_feedback'
                        path = input('本地 JSON 路径: ').strip().strip('"')
                        schema = input('Schema [feedback_records]: ').strip() or 'feedback_records'
                        value = normalize_inputs({key: {'path': path, 'schema': schema}})
                        Draft202012Validator(schemas()[schema]).validate(json.loads(Path(path).read_text(encoding='utf-8')))
                        with session_lock(directory):
                            state = load_session(directory)
                            state['samples'].update(value)
                            state['plan'] = None  # sample changes require fresh planning
                            persist(directory, state)
                        print('样例已添加，需求确认不受影响；请重新 /plan 生成蓝图。')
                        continue
                    if command == '/confirm':
                        state = explore(None, directory, action='confirm', revision=state['revision'])
                        print('需求与团队方案已确认。输入 /plan 生成项目蓝图。')
                        continue
                    if command == '/plan':
                        result = plan(directory, client=model())
                        state = load_session(directory)
                        if result['status'] == 'needs_confirmation':
                            print(result['summary'])
                            display(state, preview=state['document']['summary'])
                            continue
                        summary = summary_text(result.get('summary') or '本次规划未完成。')
                        if result['status'] == 'failed':
                            print('Astra-Designer[蓝图]> 蓝图规划未完成。')
                            if result.get('failure') or not result.get('validation_feedback'):
                                print('原因：' + failure_reason('transport_error' if result.get('failure') else None, summary, result.get('failure')))
                        elif summary[:400] != summary_text(getattr(client, 'last_summary', '')):
                            print('Astra-Designer[蓝图]> ' + summary)
                        if result.get('validation_feedback'):
                            print('上一版蓝图待修复的问题：' if result.get('failure') else '蓝图未通过检查，主要问题（技术细节，一般再次 /plan 即可自动修复）：')
                            for issue in result['validation_feedback'][:4]:
                                print('  · ' + terminal_text(issue))
                        for note in (result.get('repair_notes') or [])[:1]:
                            print('说明：' + terminal_text(note[:300]))
                        if result['status'] == 'failed':
                            print('规划记录：' + str(directory / 'plans') + '。')
                            if result.get('candidate'):
                                print('再次输入 /plan 会基于这版蓝图和上述问题继续修复，不从头重新设计。')
                            else:
                                print('可再次 /plan 重试，或用 /model 换用模型。')
                        for question in result.get('questions', []):
                            print(question_text(question['question']))
                        if result.get('blueprint_path'):
                            if result.get('pending_integrations'):
                                print('项目蓝图已保存，以下能力待接入，暂不能运行：')
                                for item in result['pending_integrations']:
                                    print('  · ' + terminal_text(item['title']))
                                print('输入 /generate 保存项目及接入清单，或 /show 查看蓝图。')
                            else:
                                print('项目蓝图已通过检查。输入 /generate 生成项目，或 /show 查看蓝图。')
                        if result['status'] == 'needs_clarification':
                            print('请直接回答上面的问题；需求更新后输入“确认”，再 /plan。')
                        continue
                    if command in {'/show', '/generate', '/run', '/run-resume'}:
                        result = state.get('plan') or {}
                        if state['status'] != 'confirmed' or result.get('status') != 'ready':
                            print('请先确认需求，再用 /plan 生成蓝图。')
                            continue
                        blueprint = Path(result['blueprint_path'])
                        if command == '/show':
                            print(blueprint.read_text(encoding='utf-8'))
                        elif command == '/generate':
                            import yaml
                            from astra_designer import generate_project
                            name = yaml.safe_load(blueprint.read_text(encoding='utf-8'))['project']['name']
                            default = blueprint.parent / 'candidate' / name
                            target = input(f'新项目目录 [{default}]: ').strip().strip('"') or str(default)
                            from astra_designer.cli.project_model import choose_project_model
                            project_profile = choose_project_model(config_path, client)
                            project = generate_project(blueprint, target, model_config=project_profile)
                            with session_lock(directory):
                                state = load_session(directory)
                                state['plan']['project_path'] = str(project)
                                persist(directory, state)
                            print('项目已生成：' + str(project))
                        else:
                            from astra_designer.cli.chat import run_project
                            project = result.get('project_path')
                            if not project:
                                print('请先 /generate。')
                                continue
                            import os
                            import yaml
                            project_manifest = yaml.safe_load(Path(project).read_text(encoding='utf-8'))
                            needs_model = 'model_config' not in project_manifest and (Path(project).parent / 'configs/agents.yaml').exists() and not any(
                                k in os.environ for k in ('ASTRA_RUNTIME_BASE_URL', 'ASTRA_RUNTIME_MODEL'))
                            run_project(project, model() if needs_model else None, argument if command == '/run-resume' else None)
                        continue
                    if command.startswith('/') and command not in {'/retry', '/continue'}:
                        print(f'未知命令：{command}。输入 /help 查看可用命令。')
                        continue
                    directory = directory or (Path('workspace/designs') / f'discovery-{uuid4().hex[:8]}').resolve()
                    action = {'/retry': 'retry', '/continue': 'continue'}.get(command, 'message')
                    state = explore(text if action == 'message' else None, directory, client=model(), action=action)
                    display(state, preview=getattr(client, 'last_summary', ''))
                except (ValueError, OSError) as exc:
                    print('操作失败：' + str(exc))
                except (KeyError, TypeError) as exc:
                    print(f'操作失败：会话数据不完整（{type(exc).__name__}: {exc}）。可用 /new 新建会话。')
                except Exception as exc:
                    from jsonschema.exceptions import ValidationError
                    if not isinstance(exc, ValidationError):
                        raise
                    print('样例结构不符合 Schema：' + exc.message)
    except (EOFError, KeyboardInterrupt):
        print('\n已退出，会话已保存' + (f'：{directory}' if directory else '。'))
        return 0
