from astra_core.runtime.pause import WorkflowPause
import copy
import logging
import os
from typing import Any, Callable, Dict, List, Optional, cast

from astra_core.runtime.hooks import (
    HookEvent,
    HookManager,
    HookResult,
    reset_active_execution_context,
    set_active_execution_context,
    set_active_hook_manager,
)
from astra_core.core.state import OrchestratorData, merge_orchestrator_data, new_orchestrator_data
from astra_core.core.base_agent import BaseAgent
from astra_core.core.errors import AgentExecutionError
from astra_core.services.console import is_verbose, print_detail, print_info

class OrchestratorAgent:
    """负责统一编排、定向重试和结果复盘的主控 Agent。"""

    WORKFLOW_FLOW_TYPES = {"serial", "sync", "async"}
    WORKFLOW_FLOW_ALIASES = {
        "sequence": "serial",
        "sequential": "serial",
        "顺序": "serial",
        "同步": "sync",
        "异步": "async",
    }

    CONTROL_FIELDS = {
        "data",
        "error",
        "status",
        "next_role",
        "next_stage",
        "outcome",
        "retryable",
        "suggestions",
        "strategy",
        "recommendation",
        "recommendations",
    }

    def __init__(
        self,
        stage_routing: Optional[Dict[str, Optional[str]]] = None,
        max_retries: int = 3,
        hook_profile: str = "minimal",
        hook_manager: Optional[HookManager] = None,
        workflow_flow: Optional[Dict[str, Any]] = None,
        output_dir: str = "output",
    ):
        self.stage_routing: Dict[str, Optional[str]] = stage_routing or {}
        self.workflow_flow: Dict[str, Any] = {"type": "serial"}
        self.max_retries = max_retries
        self.agents: Dict[str, BaseAgent] = {}
        self.retry_policy: Dict[str, int] = {}
        self.stage_messages: Dict[str, str] = {}
        self.stages: Dict[str, Dict[str, Any]] = {}
        self.output_dir = output_dir
        self.state: Dict[str, Any] = self._build_initial_state()
        self.logger = self._build_logger(output_dir)
        self.hook_manager = hook_manager or HookManager(profile=hook_profile)
        if hook_manager is None:
            self.hook_manager.apply_profile(hook_profile)
        set_active_hook_manager(self.hook_manager)
        self.stage_flow_handlers: Dict[str, Callable[[str, Dict[str, Any]], Dict[str, Any]]] = {
            "serial": self._execute_serial_stage,
        }
        self.workflow_flow_handlers: Dict[str, Callable[[Optional[str], bool], Dict[str, Any]]] = {
            "serial": self._execute_serial_workflow,
            "sync": self._execute_sync_workflow,
            "async": self._execute_async_workflow,
        }
        self.set_workflow_flow(workflow_flow)
        self.checkpoint = None

    @classmethod
    def _build_logger(cls, output_dir: str = "output") -> logging.Logger:
        logger = logging.Logger(f"orchestrator_agent.{id(output_dir)}")
        if logger.handlers and getattr(logger, "_output_dir", None) == output_dir:
            return logger

        os.makedirs(output_dir, exist_ok=True)
        try:
            handler: logging.Handler = logging.FileHandler(
                os.path.join(output_dir, "orchestrator.log"), encoding="utf-8"
            )
        except PermissionError:
            # 日志目录不可写时不应阻断业务项目的加载与执行。
            handler = logging.NullHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s - [%(levelname)s] - %(message)s"))

        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.propagate = False
        logger._output_dir = output_dir
        return logger

    def close(self):
        for handler in list(self.logger.handlers):
            handler.close()
            self.logger.removeHandler(handler)

    def _checkpoint(self):
        if self.checkpoint is not None:
            self.checkpoint(self.state)

    def _build_initial_state(self) -> Dict[str, Any]:
        return {
            "status": "init",
            "task": None,
            "current_stage": None,
            "current_role": None,
            "history": [],
            "executions": [],
            "stage_executions": [],
            "data": new_orchestrator_data(),
            "errors": [],
            "suggestions": [],
            "strategies": [],
            "agent_results": {},
            "retry_context": {},
            "workflow_summary": {},
            "hook_events": [],
            "artifact_records": [],
        }

    def reset(self) -> None:
        self.state = self._build_initial_state()

    def _data(self) -> OrchestratorData:
        data = self.state.setdefault("data", new_orchestrator_data())
        if not isinstance(data, dict):
            raise AgentExecutionError(f'state["data"] 必须为 dict，当前类型为 {type(data).__name__}')
        return cast(OrchestratorData, data)

    def _merge_data_patch(self, patch: Dict[str, Any]) -> OrchestratorData:
        return merge_orchestrator_data(self._data(), patch)

    def _prepare_run_state(
        self,
        initial_task: Optional[str] = None,
        initial_data: Optional[Dict[str, Any]] = None,
        reset_state: bool = True,
    ) -> None:
        if reset_state:
            self.reset()

        if initial_task is not None:
            self.state["task"] = initial_task
        elif not self.state.get("task"):
            self.state["task"] = self._data().get("task")

        if self.state.get("task") is not None:
            self._data()["task"] = self.state["task"]

        if initial_data:
            self._merge_data_patch(initial_data)

        self.state["status"] = "running"
        self.state["current_role"] = None
        self.state["retry_context"] = {}

    def _unique_in_order(self, values: List[Any]) -> List[Any]:
        unique_values: List[Any] = []
        seen = set()
        for value in values:
            marker = str(value)
            if marker in seen:
                continue
            seen.add(marker)
            unique_values.append(value)
        return unique_values

    def _finalize_run_state(self) -> Dict[str, Any]:
        if self.state["status"] != "failed":
            self.state["status"] = "completed"

        self.state["workflow_summary"] = {
            "task": self.state.get("task"),
            "status": self.state["status"],
            "executed_stages": self._unique_in_order(
                [record["stage"] for record in self.state["stage_executions"] if record["status"] == "success"]
            ),
            "executed_roles": self._unique_in_order(
                [record["role"] for record in self.state["executions"] if record["status"] == "success"]
            ),
            "failed_roles": self._unique_in_order([error["node"] for error in self.state["errors"]]),
            "failed_attempt_count": len([record for record in self.state["executions"] if record["status"] == "failed"]),
        }
        self._build_improvement_report()
        self._run_hooks(
            HookEvent.SESSION_END,
            self._build_hook_context(HookEvent.SESSION_END, result={"status": self.state["status"]}),
        )
        self._persist_session_report()
        return self.state

    def _persist_session_report(self) -> None:
        try:
            from astra_core.runtime.artifacts import persist_session_artifacts

            data = self._data()
            output_dir = data.get("hook_artifact_output_dir") or self.output_dir
            persist_session_artifacts(self.state, output_dir=output_dir)
        except Exception as exc:
            self.logger.warning("Session report persistence failed: %s", exc)

    def register_agent(
        self,
        role: str,
        agent: BaseAgent,
        next_role: Optional[str] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        self.agents[role] = agent
        if next_role is not None:
            self.stage_routing[role] = next_role
        if max_retries is not None:
            self.retry_policy[role] = max_retries

    def register_stage(
        self,
        stage_id: str,
        agent_roles: List[str],
        next_stage: Optional[str] = None,
        mode: str = "serial",
        message: Optional[str] = None,
        flow: Optional[Dict[str, Any]] = None,
    ) -> None:
        flow_config = dict(flow or {})
        flow_type = str(flow_config.get("type") or mode or "serial")
        flow_config["type"] = flow_type
        self.stages[stage_id] = {
            "stage_id": stage_id,
            "agents": list(agent_roles),
            "mode": flow_type,
            "flow": flow_config,
            "next_stage": next_stage,
        }
        if next_stage is not None:
            self.stage_routing[stage_id] = next_stage
        if message is not None:
            self.stage_messages[stage_id] = message

    def register_stage_flow_handler(
        self,
        flow_type: str,
        handler: Callable[[str, Dict[str, Any]], Dict[str, Any]],
    ) -> None:
        normalized_flow_type = self._normalize_stage_flow_type(flow_type)
        if not normalized_flow_type:
            raise ValueError("stage flow type 不能为空")
        self.stage_flow_handlers[normalized_flow_type] = handler

    def _normalize_workflow_flow_type(self, flow_type: Any) -> str:
        normalized = str(flow_type or "").strip().lower()
        return self.WORKFLOW_FLOW_ALIASES.get(normalized, normalized)

    def set_workflow_flow(self, flow: Optional[Dict[str, Any]] = None) -> None:
        flow_config = dict(flow or {})
        flow_type = self._normalize_workflow_flow_type(flow_config.get("type") or "serial")
        if flow_type not in self.WORKFLOW_FLOW_TYPES:
            allowed = ", ".join(sorted(self.WORKFLOW_FLOW_TYPES))
            raise ValueError(f"workflow flow type 仅支持: {allowed}，当前为: {flow_type}")
        flow_config["type"] = flow_type
        self.workflow_flow = flow_config

    def register_workflow_flow_handler(
        self,
        flow_type: str,
        handler: Callable[[Optional[str], bool], Dict[str, Any]],
    ) -> None:
        normalized_flow_type = self._normalize_workflow_flow_type(flow_type)
        if normalized_flow_type not in self.WORKFLOW_FLOW_TYPES:
            allowed = ", ".join(sorted(self.WORKFLOW_FLOW_TYPES))
            raise ValueError(f"workflow flow type 仅支持: {allowed}，当前为: {flow_type}")
        self.workflow_flow_handlers[normalized_flow_type] = handler

    def set_stage_routing(self, stage_routing: Dict[str, Optional[str]]) -> None:
        self.stage_routing = dict(stage_routing)

    def set_retry_policy(self, retry_policy: Dict[str, int]) -> None:
        self.retry_policy = dict(retry_policy)

    def set_stage_messages(self, stage_messages: Dict[str, str]) -> None:
        self.stage_messages = dict(stage_messages)

    def register_hook(self, event_type, handler, priority: int = 0, enabled: bool = True, name: Optional[str] = None):
        return self.hook_manager.register_hook(
            event_type,
            handler,
            priority=priority,
            enabled=enabled,
            name=name,
        )

    def _build_hook_context(
        self,
        event_type: str,
        stage: Optional[str] = None,
        role: Optional[str] = None,
        attempt: Optional[int] = None,
        agent: Optional[BaseAgent] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[Any] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        context = {
            "event_type": event_type,
            "state": self.state,
            "stage": stage if stage is not None else self.state.get("current_stage"),
            "role": role if role is not None else self.state.get("current_role"),
            "attempt": attempt,
            "agent_name": agent.name if agent else None,
            "agent_class": agent.__class__.__name__ if agent else None,
            "result": result,
            "error": str(error) if error is not None else None,
        }
        if extra:
            context.update(extra)
        return context

    def _merge_hook_result(self, event_type: str, hook_result: HookResult) -> None:
        if hook_result.patch:
            if hook_result.patch:
                self._merge_data_patch(hook_result.patch)
        if hook_result.state_patch:
            self._merge_state_patch(hook_result.state_patch)
        if hook_result.message:
            self.state["history"].append(
                {
                    "stage": self.state.get("current_stage"),
                    "role": "hook",
                    "attempt": 0,
                    "status": "info",
                    "message": f"{event_type}: {hook_result.message}",
                }
            )
        self.state["hook_events"] = self.hook_manager.export_events()

    def _merge_state_patch(self, state_patch: Dict[str, Any]) -> None:
        artifact_records = state_patch.get("artifact_records")
        if isinstance(artifact_records, list):
            records = self.state.setdefault("artifact_records", [])
            if not isinstance(records, list):
                records = []
                self.state["artifact_records"] = records

            by_name = {record.get("name"): record for record in records if isinstance(record, dict)}
            for record in artifact_records:
                if not isinstance(record, dict):
                    continue
                name = record.get("name")
                if not name:
                    continue
                if name in by_name:
                    by_name[name].update(record)
                else:
                    records.append(record)
                    by_name[name] = record

    def _run_hooks(self, event_type: str, context: Dict[str, Any]) -> HookResult:
        hook_result = self.hook_manager.run(event_type, context)
        self._merge_hook_result(event_type, hook_result)
        if hook_result.block:
            raise AgentExecutionError(hook_result.reason or hook_result.message or f"{event_type} hook blocked execution")
        return hook_result

    def _get_agent_tool_names(self, agent: BaseAgent) -> List[str]:
        tool_names = getattr(agent, "tool_names", None)
        if isinstance(tool_names, list):
            return [str(item) for item in tool_names]
        return []

    def _get_agent_kind(self, agent: Optional[BaseAgent]) -> str:
        if agent is None:
            return "unknown_agent"
        class_names = {cls.__name__ for cls in agent.__class__.mro()}
        if class_names & {"QwenAssistantAgent", "ConfiguredLLMAgent"}:
            return "llm_agent"
        return "base_agent"

    def get_structure_map(self) -> Dict[str, Any]:
        stages: List[Dict[str, Any]] = []
        stage_order: List[str] = []

        start_stage = self.stage_routing.get("init")
        visited_stages = set()
        current_stage = start_stage
        while current_stage and current_stage != "end" and current_stage not in visited_stages:
            stage_order.append(current_stage)
            visited_stages.add(current_stage)
            current_stage = self.stage_routing.get(current_stage)

        for stage_id in self.stages:
            if stage_id not in visited_stages:
                stage_order.append(stage_id)

        for stage_id in stage_order:
            stage_config = self.stages.get(stage_id, {})
            agent_roles = stage_config.get("agents", [])
            agents: List[Dict[str, Any]] = []
            for role in agent_roles:
                agent = self.agents.get(role)
                agents.append(
                    {
                        "role": role,
                        "name": agent.name if agent else role,
                        "class_name": agent.__class__.__name__ if agent else None,
                        "agent_kind": self._get_agent_kind(agent),
                        "max_retries": self._get_agent_retry_limit(role),
                        "tools": self._get_agent_tool_names(agent) if agent else [],
                    }
                )

            stages.append(
                {
                    "stage_id": stage_id,
                    "message": self.stage_messages.get(stage_id),
                    "mode": stage_config.get("mode"),
                    "next_stage": stage_config.get("next_stage", self.stage_routing.get(stage_id)),
                    "agents": agents,
                }
            )

        return {
            "orchestrator": {
                "class_name": self.__class__.__name__,
                "max_retries": self.max_retries,
                "start_stage": start_stage,
            },
            "stages": stages,
            "stage_routing": dict(self.stage_routing),
        }

    def render_structure_tree(self) -> str:
        structure = self.get_structure_map()
        orchestrator_info = structure["orchestrator"]
        lines = [
            f"{orchestrator_info['class_name']}",
            f"  start_stage: {orchestrator_info['start_stage']}",
            f"  default_max_retries: {orchestrator_info['max_retries']}",
        ]

        for stage in structure["stages"]:
            header = f"  |- stage: {stage['stage_id']}"
            if stage.get("next_stage") is not None:
                header += f" -> next_stage: {stage['next_stage']}"
            lines.append(header)

            if stage.get("message"):
                lines.append(f"  |  description: {stage['message']}")
            if stage.get("mode"):
                lines.append(f"  |  mode: {stage['mode']}")

            for agent in stage["agents"]:
                lines.append(
                    f"  |  |- agent: {agent['name']} (role={agent['role']}, class={agent['class_name']}, retries={agent['max_retries']})"
                )
                tools = agent.get("tools", [])
                if tools:
                    for tool_name in tools:
                        lines.append(f"  |  |  |- tool: {tool_name}")

        return "\n".join(lines)

    def print_structure_tree(self) -> str:
        tree = self.render_structure_tree()
        print_detail(tree, tip="[组织结构图]\n")
        return tree

    def _build_visual_nodes(self) -> List[Dict[str, Any]]:
        structure = self.get_structure_map()
        nodes: List[Dict[str, Any]] = []

        nodes.append(
            {
                "id": "orchestrator",
                "label": structure["orchestrator"]["class_name"],
                "level": 0,
                "group": "orchestrator",
                "parent": None,
            }
        )

        for stage_index, stage in enumerate(structure["stages"]):
            stage_id = stage["stage_id"]
            nodes.append(
                {
                    "id": f"stage:{stage_id}",
                    "label": f"Stage\n{stage_id}",
                    "level": 1,
                    "group": "stage",
                    "parent": "orchestrator",
                    "order": stage_index,
                }
            )

            for agent_index, agent in enumerate(stage["agents"]):
                agent_id = f"agent:{stage_id}:{agent['role']}"
                nodes.append(
                    {
                        "id": agent_id,
                        "label": f"Agent\n{agent['name']}",
                        "level": 2,
                        "group": agent.get("agent_kind", "base_agent"),
                        "parent": f"stage:{stage_id}",
                        "order": agent_index,
                    }
                )

                tools = agent.get("tools", [])
                for tool_index, tool_name in enumerate(tools):
                    nodes.append(
                        {
                            "id": f"tool:{stage_id}:{agent['role']}:{tool_name}",
                            "label": f"Tool\n{tool_name}",
                            "level": 3,
                            "group": "tool",
                            "parent": agent_id,
                            "order": tool_index,
                        }
                    )

        return nodes

    def export_structure_figure(
        self,
        output_path: Optional[str] = None,
        figsize: tuple = (14, 8),
        dpi: int = 200,
    ) -> str:
        import os

        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.patches import FancyBboxPatch

        if output_path is None:
            output_path = os.path.join(self.output_dir, "orchestrator_structure.png")

        nodes = self._build_visual_nodes()
        column_x = {0: 0.8, 1: 4.0, 2: 8.2, 3: 12.6}
        row_gap = 1.6
        box_width = {0: 2.3, 1: 2.6, 2: 2.5, 3: 2.4}
        box_height = 0.9
        colors = {
            "orchestrator": "#264653",
            "stage": "#2A9D8F",
            "llm_agent": "#E76F51",
            "base_agent": "#E9C46A",
            "unknown_agent": "#94A3B8",
            "tool": "#F4A261",
        }
        text_colors = {
            "orchestrator": "white",
            "stage": "white",
            "llm_agent": "white",
            "base_agent": "#1F2937",
            "unknown_agent": "#1F2937",
            "tool": "#1F2937",
        }

        levels: Dict[int, List[Dict[str, Any]]] = {0: [], 1: [], 2: [], 3: []}
        for node in nodes:
            levels[node["level"]].append(node)
        has_tool_nodes = bool(levels[3])

        positions: Dict[str, tuple] = {}
        for level, level_nodes in levels.items():
            for index, node in enumerate(level_nodes):
                positions[node["id"]] = (column_x[level], -(index * row_gap))

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        fig = Figure(figsize=figsize, dpi=dpi)
        FigureCanvasAgg(fig)
        ax = fig.subplots()
        ax.set_facecolor("#F8FAFC")
        fig.patch.set_facecolor("#F8FAFC")

        for node in nodes:
            parent = node.get("parent")
            if not parent:
                continue
            x0, y0 = positions[parent]
            x1, y1 = positions[node["id"]]
            parent_level = next(item["level"] for item in nodes if item["id"] == parent)
            start_x = x0 + box_width[parent_level] / 2
            end_x = x1 - box_width[node["level"]] / 2
            ax.plot([start_x, end_x], [y0, y1], color="#94A3B8", linewidth=1.4, zorder=1)

        for node in nodes:
            x, y = positions[node["id"]]
            width = box_width[node["level"]]
            patch = FancyBboxPatch(
                (x - width / 2, y - box_height / 2),
                width,
                box_height,
                boxstyle="round,pad=0.03,rounding_size=0.08",
                linewidth=1.2,
                edgecolor="#CBD5E1",
                facecolor=colors[node["group"]],
                zorder=2,
            )
            ax.add_patch(patch)
            ax.text(
                x,
                y,
                node["label"],
                ha="center",
                va="center",
                fontsize=9,
                color=text_colors[node["group"]],
                zorder=3,
            )

        ax.text(
            0.2,
            1.02,
            "Orchestrator -> Stage -> Agent -> Tool" if has_tool_nodes else "Orchestrator -> Stage -> Agent",
            transform=ax.transAxes,
            fontsize=14,
            fontweight="bold",
            color="#0F172A",
        )
        legend_items = [
            ("LLM Agent", colors["llm_agent"], text_colors["llm_agent"]),
            ("Base Agent", colors["base_agent"], text_colors["base_agent"]),
        ]
        for index, (label, face_color, _text_color) in enumerate(legend_items):
            y = 1.03 - index * 0.06
            patch = FancyBboxPatch(
                (0.72, y - 0.025),
                0.035,
                0.035,
                boxstyle="round,pad=0.004,rounding_size=0.006",
                linewidth=0.8,
                edgecolor="#CBD5E1",
                facecolor=face_color,
                transform=ax.transAxes,
                clip_on=False,
                zorder=4,
            )
            ax.add_patch(patch)
            ax.text(
                0.762,
                y - 0.008,
                label,
                transform=ax.transAxes,
                ha="left",
                va="center",
                fontsize=9,
                color="#334155",
                clip_on=False,
                zorder=4,
            )

        all_y = [position[1] for position in positions.values()]
        ax.set_xlim(-0.8, 15.0 if has_tool_nodes else 10.2)
        ax.set_ylim(min(all_y) - 1.5, 1.8)
        ax.axis("off")

        fig.tight_layout()
        fig.savefig(output_path, bbox_inches="tight")
        fig.clear()

        print_detail(f"组织结构图已保存到 {output_path}", tip="[主控信息：结构可视化] ")
        return output_path

    def _get_start_stage(self, start_stage: Optional[str]) -> Optional[str]:
        if start_stage:
            return start_stage
        if "init" in self.stage_routing:
            return self.stage_routing.get("init")
        if self.stages:
            return next(iter(self.stages))
        return None

    def _get_agent_retry_limit(self, role: str) -> int:
        return self.retry_policy.get(role, self.max_retries)

    def _build_retry_context(self, role: str, attempt: int, last_error: str) -> Dict[str, Any]:
        return {
            "role": role,
            "attempt": attempt,
            "last_error": last_error,
            "instruction": (
                f"{role} 上一次执行失败，错误信息：{last_error}。"
                "本次仅针对该错误进行修正，优先检查输入完整性、输出格式和关键业务逻辑。"
            ),
        }

    def _build_agent_state(
        self,
        role: str,
        attempt: int,
        last_error: Optional[str] = None,
    ) -> Dict[str, Any]:
        state_snapshot = copy.deepcopy(self.state)
        state_snapshot["current_role"] = role
        state_snapshot["current_attempt"] = attempt
        if last_error:
            state_snapshot["retry_context"] = self._build_retry_context(role, attempt, last_error)
        else:
            state_snapshot["retry_context"] = {}
        return state_snapshot

    def _validate_agent_result(self, role: str, result: Any) -> Dict[str, Any]:
        if not isinstance(result, dict):
            raise AgentExecutionError(f"{role} 返回结果必须为 dict，当前类型为 {type(result).__name__}")
        if result.get("error"):
            raise AgentExecutionError(str(result["error"]))

        status = str(result.get("status", "success")).lower()
        if status in {"failed", "failure", "error"}:
            message = result.get("message") or result.get("reason") or f"{role} 执行失败"
            raise AgentExecutionError(str(message))
        return result

    def _extract_data_payload(self, result: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(result.get("data"), dict):
            return dict(result["data"])
        return {k: v for k, v in result.items() if k not in self.CONTROL_FIELDS}

    def _normalize_to_list(self, value: Any) -> List[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if item]
        return [str(value)]

    def _merge_agent_result(self, role: str, result: Dict[str, Any], attempt: int) -> None:
        data_payload = self._extract_data_payload(result)
        if data_payload:
            self._merge_data_patch(data_payload)

        self.state["agent_results"][role] = {
            "attempt": attempt,
            "status": result.get("status", "success"),
            "data": data_payload,
            "raw_result": result,
        }
        self.state["history"].append(
            {
                "stage": self.state.get("current_stage"),
                "role": role,
                "attempt": attempt,
                "status": "success",
                "message": f"{role} 执行成功",
            }
        )

        for suggestion in self._normalize_to_list(
            result.get("suggestions") or result.get("recommendations") or result.get("recommendation")
        ):
            self.state["suggestions"].append({"role": role, "content": suggestion})

        for strategy in self._normalize_to_list(result.get("strategy")):
            self.state["strategies"].append({"role": role, "content": strategy})

    def _record_execution(
        self,
        role: str,
        attempt: int,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        self.state["executions"].append(
            {
                "stage": self.state.get("current_stage"),
                "role": role,
                "attempt": attempt,
                "status": status,
                "error": error,
                "result": result,
            }
        )

    def execute_with_retry(self, role: str, agent: BaseAgent) -> Dict[str, Any]:
        retry_limit = self._get_agent_retry_limit(role)
        last_error: Optional[str] = None

        offset = max((record.get("attempt", 0) for record in self.state["executions"]
                      if record.get("role") == role and record.get("stage") == self.state.get("current_stage")), default=0)
        final_attempt = offset + retry_limit
        for attempt in range(offset + 1, final_attempt + 1):
            try:
                self._announce_agent(self.state.get("current_stage"), role, attempt, final_attempt)
                self.logger.info("[%s] 开始执行 (尝试次数: %s/%s)", role, attempt, retry_limit)
                self._run_hooks(
                    HookEvent.PRE_AGENT,
                    self._build_hook_context(
                        HookEvent.PRE_AGENT,
                        role=role,
                        attempt=attempt,
                        agent=agent,
                        extra={"retry_limit": retry_limit, "last_error": last_error},
                    ),
                )
                run_state = self._build_agent_state(role, attempt, last_error)
                execution_context_token = set_active_execution_context(
                    self._build_hook_context(
                        HookEvent.PRE_AGENT,
                        role=role,
                        attempt=attempt,
                        agent=agent,
                        extra={"retry_limit": retry_limit, "last_error": last_error},
                    )
                )
                try:
                    result = agent.run(run_state)
                finally:
                    reset_active_execution_context(execution_context_token)
                validated_result = self._validate_agent_result(role, result)
                self._merge_agent_result(role, validated_result, attempt)
                self._record_execution(role, attempt, "success", result=validated_result)
                self._run_hooks(
                    HookEvent.POST_AGENT,
                    self._build_hook_context(
                        HookEvent.POST_AGENT,
                        role=role,
                        attempt=attempt,
                        agent=agent,
                        result=validated_result,
                        extra={"retry_limit": retry_limit},
                    ),
                )
                self.state["retry_context"] = {}
                self.logger.info("[%s] 执行成功。", role)
                return {"success": True, "result": validated_result, "attempts": attempt}
            except WorkflowPause:
                raise
            except Exception as exc:
                last_error = str(exc)
                self.logger.warning("[%s] 执行异常拦截: %s", role, last_error)
                self.state["errors"].append(
                    {
                        "stage": self.state.get("current_stage"),
                        "node": role,
                        "attempt": attempt,
                        "error": last_error,
                    }
                )
                self.state["history"].append(
                    {
                        "stage": self.state.get("current_stage"),
                        "role": role,
                        "attempt": attempt,
                        "status": "failed",
                        "message": last_error,
                    }
                )
                self._record_execution(role, attempt, "failed", error=last_error)
                self._run_hooks(
                    HookEvent.ON_ERROR,
                    self._build_hook_context(
                        HookEvent.ON_ERROR,
                        role=role,
                        attempt=attempt,
                        agent=agent,
                        error=last_error,
                        extra={"retry_limit": retry_limit},
                    ),
                )

                if attempt >= final_attempt:
                    self.logger.error("[%s] 达到最大重试次数，节点执行彻底失败。", role)
                    self.state["retry_context"] = self._build_retry_context(role, attempt, last_error)
                    return {"success": False, "result": None, "attempts": attempt, "error": last_error}

        return {"success": False, "result": None, "attempts": retry_limit, "error": last_error or "未知异常"}

    def _announce_stage(self, stage_id: str, stage_config: Dict[str, Any]) -> None:
        message = self.stage_messages.get(stage_id, stage_id)
        agent_names = [self.agents[role].name for role in stage_config.get("agents", []) if role in self.agents]
        if not is_verbose():
            if stage_id in self.stage_messages:
                print_info(f"▸ {self.stage_messages[stage_id]}")
            else:
                print_info(f"▸ {stage_id}" + (f"：{'、'.join(agent_names)}" if agent_names else ""))
            return
        tip = f"[主控信息：主控阶段] {stage_id}"
        if agent_names:
            tip += f" | 计划调用: {', '.join(agent_names)}"
        tip += "\n"
        print_info(message, tip=tip)

    def _announce_agent(self, stage_id: Optional[str], role: str, attempt: int, retry_limit: int) -> None:
        agent = self.agents.get(role)
        agent_name = agent.name if agent else role
        if attempt == 1:
            print_detail(f"正在调用 Agent：{agent_name}", tip=f"[主控信息：阶段执行] {stage_id} - ")
            return
        print_info(f"正在重试 {agent_name}（第 {attempt}/{retry_limit} 次）", tip=f"  {stage_id} - ")

    def _resolve_next_stage(self, current_stage: str, stage_result: Dict[str, Any]) -> Optional[str]:
        next_stage = stage_result.get("next_stage")
        if next_stage is not None:
            return next_stage

        result = stage_result.get("last_result") or {}
        next_stage = result.get("next_stage")
        if next_stage is not None:
            return next_stage

        stage_config = self.stages.get(current_stage, {})
        if "next_stage" in stage_config:
            return stage_config.get("next_stage")
        return self.stage_routing.get(current_stage)

    def _validate_agent_transition(self, stage_id: str, agent_roles: List[str], result: Dict[str, Any]) -> None:
        next_role = result.get("next_role")
        if next_role is not None and next_role not in agent_roles:
            raise AgentExecutionError(
                f"Agent 返回了跨阶段 next_role={next_role}。"
                "next_role 只允许指向当前 stage 内的 agent；跨 stage 调度请使用 next_stage 或 stage_routing。"
            )

    def _apply_outcome_route(self, stage_config: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        """Translate a business-declared Agent outcome into a workflow transition."""
        outcome = result.get("outcome")
        routes = stage_config.get("outcome_routes") or {}
        route = routes.get(outcome) if isinstance(routes, dict) else None
        if not isinstance(route, dict):
            return result

        routed_result = dict(result)
        if "next_role" in route:
            routed_result["next_role"] = route["next_role"]
        if "next_stage" in route:
            routed_result["next_stage"] = route["next_stage"]
        return routed_result

    def _get_default_next_role(self, agent_roles: List[str], current_role: str) -> Optional[str]:
        try:
            current_index = agent_roles.index(current_role)
        except ValueError as exc:
            raise AgentExecutionError(f"当前角色 {current_role} 不在本阶段配置中。") from exc

        next_index = current_index + 1
        if next_index >= len(agent_roles):
            return None
        return agent_roles[next_index]

    def _is_rerun_requested(self, result: Dict[str, Any]) -> bool:
        data = self._extract_data_payload(result)
        return any(value == "rerun_requested" for value in data.values())

    def _trim_visited_roles_for_rerun(self, visited_roles: List[str], next_role: str) -> List[str]:
        if next_role not in visited_roles:
            return visited_roles
        return visited_roles[:visited_roles.index(next_role)]

    def _normalize_stage_flow_type(self, flow_type: Any) -> str:
        return str(flow_type or "").strip().lower()

    def _get_stage_flow_type(self, stage_config: Dict[str, Any]) -> str:
        flow_config = stage_config.get("flow")
        if isinstance(flow_config, dict) and flow_config.get("type"):
            return self._normalize_stage_flow_type(flow_config.get("type"))
        return self._normalize_stage_flow_type(stage_config.get("mode") or "serial")

    def _build_unsupported_stage_flow_result(self, stage_id: str, flow_type: str) -> Dict[str, Any]:
        supported_types = ", ".join(sorted(self.stage_flow_handlers)) or "无"
        error_message = f"阶段 {stage_id} 配置了暂未实现的流程类型: {flow_type}。当前已注册流程类型: {supported_types}"
        self.state["errors"].append({"stage": stage_id, "node": "orchestrator", "attempt": 0, "error": error_message})
        return {"success": False, "error": error_message, "flow_type": flow_type}

    def _execute_serial_stage(self, stage_id: str, stage_config: Dict[str, Any]) -> Dict[str, Any]:
        agent_roles = stage_config.get("agents", [])
        progress = self.state.get("stage_progress") or {}
        if progress.get("stage") != stage_id:
            progress = {"stage": stage_id, "roles": [], "visited": [], "last_result": None,
                        "next_role": agent_roles[0] if agent_roles else None}
        stage_roles = list(progress["roles"])
        visited_roles = list(progress["visited"])
        last_result = progress["last_result"]
        current_role = progress["next_role"]
        self.state["stage_progress"] = progress
        self._checkpoint()

        while current_role is not None:
            role = current_role
            agent = self.agents.get(role)
            if not agent:
                error_message = f"阶段 {stage_id} 未找到已注册的 Agent 角色: {role}"
                self.state["errors"].append({"stage": stage_id, "node": role, "attempt": 0, "error": error_message})
                return {"success": False, "error": error_message, "failed_agent": role}

            if role in visited_roles:
                error_message = f"阶段 {stage_id} 内检测到 next_role 循环跳转: {' -> '.join(visited_roles + [role])}"
                self.state["errors"].append({"stage": stage_id, "node": role, "attempt": 0, "error": error_message})
                self.state["stage_executions"].append(
                    {
                        "stage": stage_id,
                        "status": "failed",
                        "failed_agent": role,
                        "executed_roles": stage_roles,
                    }
                )
                return {"success": False, "error": error_message, "failed_agent": role, "last_result": last_result}

            self.state["current_role"] = role
            self._checkpoint()
            execution = self.execute_with_retry(role, agent)
            if not execution["success"]:
                self.state["stage_executions"].append(
                    {
                        "stage": stage_id,
                        "status": "failed",
                        "failed_agent": role,
                        "executed_roles": stage_roles,
                    }
                )
                return {
                    "success": False,
                    "error": execution.get("error"),
                    "failed_agent": role,
                    "last_result": execution.get("result"),
                }

            execution["result"] = self._apply_outcome_route(stage_config, execution["result"])
            self._validate_agent_transition(stage_id, agent_roles, execution["result"])
            visited_roles.append(role)
            stage_roles.append(role)
            last_result = execution["result"]

            current_role = execution["result"].get("next_role")
            if current_role is not None and current_role in visited_roles and self._is_rerun_requested(execution["result"]):
                visited_roles = self._trim_visited_roles_for_rerun(visited_roles, current_role)
            if current_role is None:
                current_role = self._get_default_next_role(agent_roles, role)
            self.state["stage_progress"] = {
                "stage": stage_id, "roles": list(stage_roles), "visited": list(visited_roles),
                "last_result": last_result, "next_role": current_role,
            }
            self._checkpoint()


        self.state["stage_executions"].append(
            {
                "stage": stage_id,
                "status": "success",
                "failed_agent": None,
                "executed_roles": stage_roles,
            }
        )
        return {
            "success": True,
            "executed_roles": stage_roles,
            "last_result": last_result,
            "next_stage": (last_result or {}).get("next_stage", stage_config.get("next_stage")),
        }

    def execute_stage(self, stage_id: str) -> Dict[str, Any]:
        stage_config = self.stages.get(stage_id)
        if not stage_config:
            error_message = f"未配置阶段: {stage_id}"
            self.state["errors"].append({"stage": stage_id, "node": "orchestrator", "attempt": 0, "error": error_message})
            return {"success": False, "error": error_message}

        flow_type = self._get_stage_flow_type(stage_config)
        handler = self.stage_flow_handlers.get(flow_type)
        if handler is None:
            return self._build_unsupported_stage_flow_result(stage_id, flow_type)

        agent_roles = stage_config.get("agents", [])
        self._announce_stage(stage_id, stage_config)
        self._run_hooks(
            HookEvent.PRE_STAGE,
            self._build_hook_context(
                HookEvent.PRE_STAGE,
                stage=stage_id,
                extra={"stage_config": stage_config, "agent_roles": agent_roles, "flow_type": flow_type},
            ),
        )

        stage_result: Dict[str, Any]
        try:
            stage_result = handler(stage_id, stage_config)
        finally:
            self._run_hooks(
                HookEvent.POST_STAGE,
                self._build_hook_context(HookEvent.POST_STAGE, stage=stage_id, result=locals().get("stage_result")),
            )

        return stage_result

    def _execute_serial_workflow(self, start_stage: Optional[str], stop_after_first_stage: bool = False) -> Dict[str, Any]:
        current_stage = start_stage
        last_stage_execution: Optional[Dict[str, Any]] = None
        executed_stages: List[str] = []

        while current_stage and current_stage != "end":
            self.state["current_stage"] = current_stage
            self._checkpoint()
            stage_execution = self.execute_stage(current_stage)
            last_stage_execution = stage_execution
            if not stage_execution["success"]:
                self.state["status"] = "failed"
                return {
                    "success": False,
                    "flow_type": "serial",
                    "executed_stages": executed_stages,
                    "last_stage": current_stage,
                    "last_stage_execution": stage_execution,
                }

            executed_stages.append(current_stage)
            current_stage = "end" if stop_after_first_stage else self._resolve_next_stage(current_stage, stage_execution)
            self.state["current_stage"] = current_stage or "end"
            self.state.pop("stage_progress", None)
            self._checkpoint()
            if stop_after_first_stage:
                break

        return {
            "success": True,
            "flow_type": "serial",
            "executed_stages": executed_stages,
            "last_stage": executed_stages[-1] if executed_stages else None,
            "last_stage_execution": last_stage_execution,
        }

    def _build_deferred_workflow_flow_result(self, flow_type: str) -> Dict[str, Any]:
        error_message = f"workflow flow type={flow_type} 的接口已预留，但执行语义尚未实现。当前可运行流程为 serial。"
        self.state["errors"].append({"stage": None, "node": "orchestrator", "attempt": 0, "error": error_message})
        self.state["status"] = "failed"
        return {"success": False, "flow_type": flow_type, "error": error_message}

    def _execute_sync_workflow(self, start_stage: Optional[str], stop_after_first_stage: bool = False) -> Dict[str, Any]:
        return self._build_deferred_workflow_flow_result("sync")

    def _execute_async_workflow(self, start_stage: Optional[str], stop_after_first_stage: bool = False) -> Dict[str, Any]:
        return self._build_deferred_workflow_flow_result("async")

    def _get_workflow_flow_type(self) -> str:
        return self._normalize_workflow_flow_type(self.workflow_flow.get("type") or "serial")

    def _execute_workflow_flow(self, start_stage: Optional[str], stop_after_first_stage: bool = False) -> Dict[str, Any]:
        flow_type = self._get_workflow_flow_type()
        handler = self.workflow_flow_handlers.get(flow_type)
        if handler is None:
            return self._build_deferred_workflow_flow_result(flow_type)
        return handler(start_stage, stop_after_first_stage)

    def run_stage(
        self,
        stage_id: str,
        initial_task: Optional[str] = None,
        initial_data: Optional[Dict[str, Any]] = None,
        continue_from_stage: bool = False,
        reset_state: bool = False,
    ) -> Dict[str, Any]:
        """
        单独运行指定阶段。

        - reset_state=True: 从干净状态启动，只运行该阶段或从该阶段继续。
        - reset_state=False: 复用当前 state，适合“stage1 满意，只重跑 stage2”。
        - continue_from_stage=True: 指定阶段执行完成后，继续按 stage_routing 往后执行。
        """
        self._prepare_run_state(initial_task=initial_task, initial_data=initial_data, reset_state=reset_state)
        self.state["current_stage"] = stage_id

        self.logger.info("========== 阶段执行启动 ==========")

        stage_flow_result = self._execute_serial_workflow(
            stage_id,
            stop_after_first_stage=not continue_from_stage,
        )
        if not stage_flow_result["success"]:
            self.logger.error("阶段执行失败并中断。")

        self._finalize_run_state()
        self.logger.info("========== 阶段执行结束 ==========")
        return self.state

    def _build_improvement_report(self) -> Dict[str, Any]:
        data = self._data()
        suggestions: List[Dict[str, Any]] = list(self.state.get("suggestions", []))
        strategies: List[Dict[str, Any]] = list(self.state.get("strategies", []))

        hook_reusable_patterns = data.get("hook_reusable_patterns", [])
        if isinstance(hook_reusable_patterns, list) and hook_reusable_patterns:
            existing_suggestion_texts = {item.get("content") for item in suggestions if isinstance(item, dict)}
            for pattern in hook_reusable_patterns[-5:]:
                text = str(pattern)
                if text not in existing_suggestion_texts:
                    suggestions.append({"role": "hook", "content": f"可复用上下文：{text}"})

        hook_error_suggestions = data.get("hook_error_suggestions", [])
        if isinstance(hook_error_suggestions, list):
            for item in hook_error_suggestions:
                if not isinstance(item, dict):
                    continue
                suggestion = item.get("suggestion")
                if suggestion:
                    text = f"阶段 {item.get('stage') or 'unknown_stage'} / 角色 {item.get('role') or 'unknown_role'}：{suggestion}"
                    if text not in {entry.get("content") for entry in suggestions if isinstance(entry, dict)}:
                        suggestions.append({"role": item.get("role") or "hook", "content": text})

        hook_stage_summaries = data.get("hook_stage_summaries", [])
        if isinstance(hook_stage_summaries, list):
            for item in hook_stage_summaries:
                if not isinstance(item, dict):
                    continue
                if item.get("success"):
                    summary_text = f"阶段 {item.get('stage') or 'unknown_stage'} 已稳定完成。"
                else:
                    summary_text = (
                        f"阶段 {item.get('stage') or 'unknown_stage'} 执行失败，"
                        f"建议保留 {item.get('failed_agent') or 'unknown_agent'} 的上下文与错误信息。"
                    )
                if summary_text not in {entry.get("content") for entry in strategies if isinstance(entry, dict)}:
                    strategies.append({"role": "hook", "content": summary_text})

        self.state["suggestions"] = suggestions
        self.state["strategies"] = strategies
        return {"suggestions": suggestions, "strategies": strategies}

    def run_workflow(
        self,
        initial_task: str,
        initial_data: Optional[Dict[str, Any]] = None,
        start_stage: Optional[str] = None,
        reset_state: bool = True,
    ) -> Dict[str, Any]:
        self._prepare_run_state(initial_task=initial_task, initial_data=initial_data, reset_state=reset_state)

        current_stage = self._get_start_stage(start_stage)
        self.state["current_stage"] = current_stage

        self.logger.info("========== 工作流启动 ==========")

        if not current_stage:
            self.state["status"] = "failed"
            self.state["errors"].append({"stage": None, "node": "orchestrator", "attempt": 0, "error": "未配置可执行的起始阶段"})
            self._finalize_run_state()
            self.logger.error("未配置可执行的起始阶段。")
            self.logger.info("========== 工作流结束 ==========")
            return self.state

        workflow_flow_result = self._execute_workflow_flow(current_stage)
        if not workflow_flow_result["success"]:
            self.logger.error("工作流因关键阶段失败而中断。")

        self._finalize_run_state()
        self.logger.info("========== 工作流结束 ==========")
        return self.state
