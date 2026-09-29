"""Trial-run a generated project from the designer chat."""


def run_project(project, model=None, resume=None):
    from astra_designer.validation.trial import trial_project
    report = trial_project(project, model=model, resume=resume)
    if report['status'] == 'waiting_dependencies':
        attempt = report['attempts'][-1]
        print(attempt.get('dependency_message') or '本单资料已保存，项目中仍有待接入的能力。')
        print(f"完成接入后继续：uv run astra resume \"{project}\" {attempt['run_id']} --interactive")
        return report
    if report['status'] in {'waiting_input', 'awaiting_confirmation', 'awaiting_review'}:
        run_id = report['attempts'][-1]['run_id']
        print('项目已启动，正在等待审阅。请在运行器中继续：' if report['status'] == 'awaiting_review'
              else '项目已启动，正在等待本次任务资料。请在运行器中填写：')
        print(f'uv run astra resume "{project}" {run_id} --interactive')
        return report
    print('试运行通过。' if report['passed'] else f"试运行未通过（运行状态：{report['status']}）。")
    print(f"报告：{report['report_path']}")
    if report['attempts'] and report['attempts'][-1].get('run_id'):
        print(f"运行 ID：{report['attempts'][-1]['run_id']}")
    return report
