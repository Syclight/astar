"""Installed by the project's normal tool loader."""
from astra_core.tools.base import ToolSpec, register_tool
from astra_core.runtime.pause import WorkflowPause
from astra_core.task_contract import input_issues

def collect(state):
    contract = state['task_contract']
    values = state['data'].get('task_input', {})
    rules = contract.get('interaction', {})
    errors, questions = input_issues(contract['schema'], values, rules)
    if errors:
        if rules.get('on_missing', 'ask') != 'ask':
            raise ValueError('；'.join(errors))
        raise WorkflowPause('waiting_input', input_errors=errors, questions=questions)
    if rules.get('confirm_before_run') and not state.get('input_confirmation'):
        raise WorkflowPause('awaiting_confirmation', input_errors=[], questions=[])
    return {'task_input': values}

def check(state):
    """Steps before a pending integration still run; the run pauses at the pending step itself.

    The declaration is still validated here, so a broken workflow file fails before any work.
    """
    from astra_core.agents.pending import check_integrations, PendingIntegrationRequired
    from astra_core.agents.pack import check_project_packs
    check_project_packs(state['workflow_config'])
    try:
        check_integrations(state['workflow_config'])
    except PendingIntegrationRequired:
        pass
    return {}

def register_tools():
    register_tool(ToolSpec('task.collect', '收集并验证本单资料，必要时请求用户补充', [], collect))
    register_tool(ToolSpec('integration.check', '检查工作流声明的待接入能力', [], check))
