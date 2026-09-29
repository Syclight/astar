from typing import Any, Dict

from astra_core.core.base_agent import BaseAgent


class DraftAgent(BaseAgent):
    def __init__(self, name: str = "example_draft_agent"):
        super().__init__(name=name)

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        task = state.get("task") or "未命名任务"
        return {
            "status": "success",
            "data": {
                "draft": {
                    "title": "示例项目草稿",
                    "task": task,
                    "points": [
                        "业务项目由 project.yaml 描述。",
                        "Agent 放在业务包内，引擎只负责加载和编排。",
                        "这个示例不依赖大模型或外部工具。",
                    ],
                },
                "draft_status": "completed",
            },
        }


class ReviewAgent(BaseAgent):
    def __init__(self, name: str = "example_review_agent"):
        super().__init__(name=name)

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        draft = state.get("data", {}).get("draft", {})
        points = draft.get("points", []) if isinstance(draft, dict) else []
        return {
            "status": "success",
            "data": {
                "review": {
                    "accepted": True,
                    "summary": f"示例项目已完成 {len(points)} 个要点的演示。",
                },
                "review_status": "completed",
            },
        }
