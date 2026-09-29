import argparse
import json
from pathlib import Path

from astra_designer import design_project, generate_project, validate_blueprint
from astra_designer.contracts.validation import BlueprintError


def main(argv=None):
    parser = argparse.ArgumentParser(prog='astra-designer', description='Astra 项目设计器：需求澄清、蓝图规划与项目生成')
    parser.add_argument('--session', nargs='?', const='workspace/designs', help='配合 --list 指定会话搜索目录')
    parser.add_argument('--list', '-l', action='store_true', dest='list_sessions', help='列出历史会话与保存时间')
    commands = parser.add_subparsers(dest='command')
    packs = commands.add_parser('packs', help='安装、卸载、列出、自检本地可执行能力包')
    pack_actions = packs.add_subparsers(dest='pack_action', required=True)
    pack_install = pack_actions.add_parser('install', help='安装本地受信任的能力包并激活该版本')
    pack_install.add_argument('path')
    pack_uninstall = pack_actions.add_parser('uninstall', help='按包名卸载全部已安装版本；保留已生成团队的副本')
    pack_uninstall.add_argument('name', help='包名，例如 web_security，不是 capability ID 或路径')
    pack_actions.add_parser('list', help='列出已安装能力及依赖状态')
    pack_check = pack_actions.add_parser('check', help='检查包格式、完整性和依赖，不执行实现代码')
    pack_check.add_argument('path')
    frameworks = commands.add_parser('frameworks', help='查看框架契约与生成支持情况')
    frameworks.add_argument('--show', help='框架 ID，例如 astra.configured@1')
    catalog = commands.add_parser('capabilities', help='查看与检索已登记能力')
    catalog.add_argument('query', nargs='?', default='')
    catalog.add_argument('--show', help='查看完整能力 ID 的契约与默认参数')
    catalog.add_argument('--limit', type=int, default=20)
    configuration = commands.add_parser('config', help='交互配置模型（不保存密钥）')
    configuration.add_argument('--file', default='.astra-designer/model.json')
    configuration.add_argument('--advanced', action='store_true', help='逐项编辑当前连接的高级参数')
    conversation = commands.add_parser('chat', help='交互式业务设计问答')
    conversation.add_argument('--config', default='.astra-designer/model.json')
    conversation.add_argument('--session', nargs='?', const='workspace/designs', help='恢复会话；配合 --list 搜索该目录')
    conversation.add_argument('--list', '-l', action='store_true', dest='list_sessions', help='查看历史会话，不调用模型')
    export = commands.add_parser('export', help='将已保存的对话导出为易读 TXT，不调用模型')
    export.add_argument('--session', required=True, help='已有会话目录')
    export.add_argument('--output', required=True, help='导出 TXT 文件路径')
    discovery = commands.add_parser('explore', help='通用需求探索、确认与蓝图规划')
    discovery.add_argument('--session', required=True)
    discovery.add_argument('--message-file', help='UTF-8 业务描述或回答')
    discovery.add_argument('--action', choices=['message', 'retry', 'continue', 'confirm', 'select', 'show', 'plan'], default='message')
    discovery.add_argument('--choice', help='选择团队方案 ID，配合 --action select')
    discovery.add_argument('--revision', type=int, help='当前记录编号（不是对外展示的设计版本），用于确认或选择方案')
    design = commands.add_parser('design', help='从自然语言目标设计蓝图，不自动执行项目')
    design.add_argument('--goal-file', help='UTF-8 目标文本；继续已有会话时可省略')
    design.add_argument('--inputs', help='JSON 输入清单，文件路径相对该清单')
    design.add_argument('--session', required=True, help='设计会话目录')
    design.add_argument('--answers', help='问题标识到答案的 JSON 文件')
    design.add_argument('--max-attempts', type=int, default=2)
    validation = commands.add_parser('validate', help='校验蓝图，输出 JSON 结果')
    validation.add_argument('blueprint')
    generation = commands.add_parser('generate', help='由已校验蓝图生成项目文件')
    generation.add_argument('blueprint')
    generation.add_argument('--output', required=True)
    generation.add_argument('--framework', help='模型 Agent 默认框架，默认自动匹配')
    generation.add_argument('--run', action='store_true', help='生成后运行可信示例并验收')
    model_options = generation.add_mutually_exclusive_group()
    model_options.add_argument('--model-config', help='复制指定项目模型 JSON 配置（仅允许密钥环境变量名）')
    model_options.add_argument('--use-designer-model', action='store_true', help='复制设计器模型参数，不复制密钥')
    generation.add_argument('--designer-config', default='.astra-designer/model.json', help='复制设计器模型时使用的配置文件')
    evaluation = commands.add_parser('evaluate', help='用真实模型跑一组典型业务用例，统计设计、运行与验收的成功率')
    evaluation.add_argument('--config', default='.astra-designer/model.json', help='设计器模型配置')
    evaluation.add_argument('--project-config', help='运行生成项目用的模型配置 JSON；默认复制设计器模型参数')
    evaluation.add_argument('--cases', help='只跑这些用例（逗号分隔）；--show-cases 查看全部')
    evaluation.add_argument('--show-cases', action='store_true', help='列出用例，不调用模型')
    evaluation.add_argument('--out', default='workspace/evaluations', help='报告与过程文件保存目录')
    evaluation.add_argument('--plan-rounds', type=int, default=3, help='每个用例最多规划几轮（每轮含修复），默认 3')
    trial = commands.add_parser('trial', help='限时试运行、检查点恢复与业务验收')
    trial.add_argument('project')
    trial.add_argument('--timeout', type=float, default=600, help='本次试运行总秒数，最多 3600')
    trial.add_argument('--max-attempts', type=int, default=1, help='含首次执行，最多 3 次')
    trial.add_argument('--resume', help='恢复本项目指定运行 ID')
    args = parser.parse_args(argv)
    try:
        if args.command == 'packs':
            from astra_core.capability_packs import install_pack, uninstall_pack, installed_packs, load_pack, pack_summary
            if args.pack_action == 'install':
                result = install_pack(args.path)
            elif args.pack_action == 'uninstall':
                result = uninstall_pack(args.name)
            elif args.pack_action == 'check':
                result = pack_summary(load_pack(args.path))
            else:
                result = [pack_summary(pack) for pack in installed_packs()]
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if args.pack_action == 'check' and any(c['status'] != 'ready' for c in result['capabilities']):
                return 1
            return 0
        if args.list_sessions:
            if args.command not in (None, 'chat'):
                parser.error('--list 仅用于顶层或 chat 命令')
            from astra_designer.cli.sessions import print_sessions
            return print_sessions(args.session or 'workspace/designs')
        if args.command is None:
            parser.error('请指定子命令，或使用 --session --list 查看历史会话')
        if args.command == 'export':
            from astra_designer.cli.export import export_conversation
            target = export_conversation(args.session, args.output)
            print(f'对话已导出：{target}')
            return 0
        if args.command == 'frameworks':
            from astra_designer.frameworks.registry import describe
            entries = describe()
            if args.show and args.show not in entries:
                raise ValueError('未知框架: ' + args.show)
            print(json.dumps(entries[args.show] if args.show else entries, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'explore':
            from astra_designer.discovery.pipeline import explore, plan, load_session
            if args.action == 'show':
                result = load_session(args.session)
            elif args.action == 'plan':
                result = plan(args.session)
            else:
                message = Path(args.message_file).read_text(encoding='utf-8') if args.message_file else None
                result = explore(message, args.session, action=args.action, revision=args.revision, choice=args.choice)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result['status'] in {'failed', 'budget_exhausted', 'unsupported'} else 0
        if args.command == 'evaluate':
            from astra_designer.evaluation.cases import CASES
            if args.show_cases:
                for case in CASES:
                    extra = f"（需配置 {'、'.join(case['needs'])}）" if case['needs'] else ''
                    print(f"{case['id']:20} {case['title']}{extra}")
                return 0
            from astra_designer.cli.config import configured_model
            from astra_designer.cli.project_model import designer_profile
            from astra_designer.evaluation.runner import evaluate
            client = configured_model(args.config)
            profile = (json.loads(Path(args.project_config).read_text(encoding='utf-8')) if args.project_config
                       else designer_profile(args.config))
            only = [item.strip() for item in args.cases.split(',') if item.strip()] if args.cases else None
            folder, report = evaluate(args.out, client, profile, only=only, plan_rounds=args.plan_rounds)
            total = report['summary']
            print(f"\n通过 {total['passed']}/{total['cases']}，平均 {total['average_score']} 分；"
                  f"设计成功率 {total['design_rate']:.0%}，验收通过率 {total['acceptance_rate']:.0%}")
            print(f'报告：{folder / "report.md"}')
            return 0
        if args.command == 'trial':
            from astra_designer.validation.trial import trial_project
            report = trial_project(args.project, timeout=args.timeout, max_attempts=args.max_attempts, resume=args.resume)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['passed'] else 1
        if args.command == 'capabilities':
            from astra_designer.catalog.registry import capabilities
            from astra_designer.catalog.search import search_capabilities
            if args.show:
                entry = capabilities().get(args.show)
                if entry is None:
                    raise ValueError(f'未找到能力: {args.show}')
                value = {'id': args.show, **entry}
            else:
                value = search_capabilities(args.query, args.limit)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        if args.command == 'config':
            if args.advanced:
                from astra_designer.cli.config import configure
                configure(args.file)
            else:
                from astra_designer.cli.models import select_model
                select_model(args.file)
            return 0
        if args.command == 'chat':
            if args.session and (Path(args.session) / 'session.json').exists() and not (Path(args.session) / 'discovery.json').exists():
                raise ValueError('该目录是旧版蓝图设计会话，交互对话已不再支持；请用 design 子命令继续，或新建会话')
            from astra_designer.discovery.chat import chat
            return chat(args.config, args.session)
        if args.command == 'design':
            goal = Path(args.goal_file).read_text(encoding='utf-8').strip() if args.goal_file else None
            inputs = None
            if args.inputs:
                manifest = Path(args.inputs).resolve()
                inputs = json.loads(manifest.read_text(encoding='utf-8'))
                if not isinstance(inputs, dict):
                    raise ValueError('输入清单必须是 JSON 对象')
                for spec in inputs.values():
                    if not isinstance(spec, dict) or not isinstance(spec.get('path'), str):
                        raise ValueError('每项输入必须声明字符串 path')
                    spec['path'] = str((manifest.parent / spec['path']).resolve())
            answers = json.loads(Path(args.answers).read_text(encoding='utf-8')) if args.answers else None
            result = design_project(goal, args.session, inputs=inputs, answers=answers, max_attempts=args.max_attempts)
            print(json.dumps({k: result.get(k) for k in ('status', 'summary', 'questions', 'blueprint_path', 'calls')}, ensure_ascii=False, indent=2))
            return {'ready': 0, 'needs_clarification': 2, 'unsupported': 3}.get(result['status'], 1)
        if args.command == 'validate':
            result = validate_blueprint(args.blueprint)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0 if result.valid else 1
        profile = None
        if args.model_config:
            profile = json.loads(Path(args.model_config).read_text(encoding='utf-8-sig'))
        elif args.use_designer_model:
            from astra_designer.cli.project_model import designer_profile
            profile = designer_profile(args.designer_config)
        project = generate_project(args.blueprint, args.output, framework=args.framework, model_config=profile)
        print(f'项目已生成: {project}')
        if args.run:
            from astra_designer.validation.trial import trial_project
            report = trial_project(project)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['passed'] else 1
        return 0
    except BlueprintError as exc:
        print(json.dumps(exc.result.to_dict(), ensure_ascii=False, indent=2))
        return 1
    except (OSError, ValueError) as exc:
        print(f'操作失败：{exc}')
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\n已取消。')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())

