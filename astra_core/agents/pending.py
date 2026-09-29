"""Loadable declarations of integrations which cannot yet execute."""
from astra_core.core.base_agent import BaseAgent


class PendingIntegrationRequired(ValueError):
    pass


class PendingIntegrationAgent(BaseAgent):
    def __init__(self, name, title, description, setup, input_schemas, output_schemas, stub=None):
        super().__init__(name)
        self.title, self.setup, self.stub = title, list(setup), stub

    def run(self, *args, **kwargs):
        from astra_core.runtime.pause import WorkflowPause
        steps = '\n'.join(f'  {number}. {step}' for number, step in enumerate(self.setup, 1))
        where = (f'\n实现模板：{self.stub}（填写其中的 call 方法），然后把 configs/workflow.yaml 中 {self.name} 的 class '
                 '改为模板里写明的类名。') if self.stub else ''
        raise WorkflowPause('waiting_dependencies', dependency_message=(
            f'前面的步骤已完成并保存。“{self.title}”尚未接入，运行在此暂停。\n接入步骤：\n{steps}{where}'))


def check_integrations(workflow_path):
    from pathlib import Path
    import yaml
    workflow = yaml.safe_load(Path(workflow_path).read_text(encoding='utf-8'))
    if not isinstance(workflow, dict) or not isinstance(workflow.get('agents', []), list):
        raise ValueError('工作流配置必须是包含 agents 列表的对象')
    if any(not isinstance(agent, dict) for agent in workflow.get('agents', [])):
        raise ValueError('工作流 Agent 配置必须是对象')
    pending = [agent for agent in workflow.get('agents', [])
               if agent.get('class') == 'astra_core.agents.pending.PendingIntegrationAgent']
    if pending:
        details = []
        for agent in pending:
            spec = agent.get('kwargs', {})
            details.append(str(agent.get('role')) + '：' + str(spec.get('title', '待接入能力'))
                           + '；' + '；'.join(spec.get('setup', [])))
        raise PendingIntegrationRequired('以下能力尚未接入，暂不能制作业务成果：\n'
                         + '\n'.join(details)
                         + '\n请实现相应 Agent/工具、配置服务并验证输入输出，然后替换工作流中的待接入节点。')
