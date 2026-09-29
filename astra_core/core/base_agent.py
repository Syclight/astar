from typing import Dict, Any

class BaseAgent:
    """子 Agent 基类"""
    def __init__(self, name: str):
        self.name = name

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """所有子 Agent 必须实现此方法，接收全局状态，返回更新的数据字典"""
        raise NotImplementedError
    
