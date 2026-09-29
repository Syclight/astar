from astra_core.core.base_agent import BaseAgent
from astra_core.tools.base import tool_registry

class TaskIntakeAgent(BaseAgent):
    def __init__(self, name, tool_name='task.collect'):
        super().__init__(name)
        self.tool_names = [tool_name]
    def run(self, state):
        return tool_registry.get(self.tool_names[0]).handler(state)

class CapabilityCheckAgent(TaskIntakeAgent):
    def __init__(self, name, tool_name='integration.check'):
        super().__init__(name, tool_name)
