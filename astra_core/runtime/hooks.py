import copy
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class HookEvent:
    PRE_STAGE = "PreStage"
    POST_STAGE = "PostStage"
    PRE_AGENT = "PreAgent"
    POST_AGENT = "PostAgent"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    ON_ERROR = "OnError"
    SESSION_END = "SessionEnd"


HookHandler = Callable[[Dict[str, Any]], Optional[Any]]


@dataclass
class HookResult:
    patch: Dict[str, Any] = field(default_factory=dict)
    state_patch: Dict[str, Any] = field(default_factory=dict)
    message: Optional[str] = None
    block: bool = False
    reason: Optional[str] = None


@dataclass
class HookRegistration:
    event_type: str
    handler: HookHandler
    priority: int = 0
    enabled: bool = True
    name: Optional[str] = None


class HookManager:
    """轻量生命周期 Hook 管理器。"""

    VALID_EVENTS = {
        HookEvent.PRE_STAGE,
        HookEvent.POST_STAGE,
        HookEvent.PRE_AGENT,
        HookEvent.POST_AGENT,
        HookEvent.PRE_TOOL_USE,
        HookEvent.POST_TOOL_USE,
        HookEvent.ON_ERROR,
        HookEvent.SESSION_END,
    }

    def __init__(self, profile: str = "standard", strict: bool = False) -> None:
        self.profile = profile
        self.strict = strict
        self._hooks: Dict[str, List[HookRegistration]] = {event: [] for event in self.VALID_EVENTS}
        self.events: List[Dict[str, Any]] = []

    def register_hook(
        self,
        event_type: str, # 事件类型，必须是 HookEvent 定义的有效事件
        handler: HookHandler,
        priority: int = 0, # 优先级，数值越大优先执行
        enabled: bool = True, # 是否启用
        name: Optional[str] = None,
    ) -> HookRegistration:
        if event_type not in self.VALID_EVENTS:
            raise ValueError(f"Unsupported hook event_type: {event_type}")

        registration = HookRegistration(
            event_type=event_type,
            handler=handler,
            priority=priority,
            enabled=enabled,
            name=name or getattr(handler, "__name__", handler.__class__.__name__),
        )
        self._hooks[event_type].append(registration)
        self._hooks[event_type].sort(key=lambda item: item.priority, reverse=True)
        return registration

    def has_hooks(self, event_type: str) -> bool:
        return any(hook.enabled for hook in self._hooks.get(event_type, []))

    def run(self, event_type: str, context: Dict[str, Any]) -> HookResult:
        combined = HookResult()
        if event_type not in self.VALID_EVENTS:
            return combined

        working_context = self._snapshot_context(context)
        for registration in self._hooks.get(event_type, []):
            if not registration.enabled:
                continue

            started_at = time.time()
            try:
                result = registration.handler(self._snapshot_context(working_context))
                hook_result = self._coerce_result(result)
                elapsed_ms = round((time.time() - started_at) * 1000, 3)
                self._record_event(event_type, registration, "success", elapsed_ms, hook_result, working_context)
                self._merge_result(combined, hook_result)
                self._apply_result_to_context(working_context, hook_result)
                if hook_result.block:
                    break
            except Exception as exc:
                elapsed_ms = round((time.time() - started_at) * 1000, 3)
                failure = HookResult(
                    message=f"Hook {registration.name} failed: {exc}",
                    block=self.strict,
                    reason=str(exc),
                )
                self._record_event(event_type, registration, "failed", elapsed_ms, failure, context)
                self._merge_result(combined, failure)
                if self.strict:
                    break

        return combined

    def apply_profile(self, profile: Optional[str] = None) -> None:
        profile_name = profile or self.profile
        self.profile = profile_name
        self.strict = profile_name == "strict"
        self._hooks = {event: [] for event in self.VALID_EVENTS}

        if profile_name == "minimal":
            self._register_minimal_profile()
            return
        if profile_name == "standard":
            self._register_standard_profile()
            return
        if profile_name == "strict":
            self._register_strict_profile()
            return
        raise ValueError(f"Unsupported hook profile: {profile_name}")

    def export_events(self) -> List[Dict[str, Any]]:
        return list(self.events)

    def _snapshot_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return copy.deepcopy(context)
        except Exception:
            return dict(context)

    def _coerce_result(self, value: Any) -> HookResult:
        if value is None:
            return HookResult()
        if isinstance(value, HookResult):
            return value
        if isinstance(value, dict):
            return HookResult(
                patch=value.get("patch") if isinstance(value.get("patch"), dict) else {},
                state_patch=value.get("state_patch") if isinstance(value.get("state_patch"), dict) else {},
                message=value.get("message"),
                block=bool(value.get("block", False)),
                reason=value.get("reason"),
            )
        if isinstance(value, str):
            return HookResult(message=value)
        return HookResult(message=str(value))

    def _merge_result(self, target: HookResult, source: HookResult) -> None:
        if source.patch:
            target.patch.update(source.patch)
        if source.state_patch:
            target.state_patch.update(source.state_patch)
        if source.message:
            if target.message:
                target.message += "\n" + source.message
            else:
                target.message = source.message
        if source.block:
            target.block = True
            target.reason = source.reason or source.message

    def _apply_result_to_context(self, context: Dict[str, Any], result: HookResult) -> None:
        state = context.get("state")
        if not isinstance(state, dict):
            return

        if result.patch:
            data = state.setdefault("data", {})
            if isinstance(data, dict):
                data.update(result.patch)

        artifact_records = result.state_patch.get("artifact_records")
        if isinstance(artifact_records, list):
            state["artifact_records"] = list(artifact_records)

    def _record_event(
        self,
        event_type: str,
        registration: HookRegistration,
        status: str,
        elapsed_ms: float,
        result: HookResult,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        context = context or {}
        self.events.append(
            {
                "event_type": event_type,
                "hook": registration.name,
                "priority": registration.priority,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "message": result.message,
                "blocked": result.block,
                "reason": result.reason,
                "patch_keys": list(result.patch.keys()),
                "state_patch_keys": list(result.state_patch.keys()),
                "stage": context.get("stage"),
                "role": context.get("role"),
                "attempt": context.get("attempt"),
                "tool_name": context.get("tool_name"),
            }
        )

    def _register_minimal_profile(self) -> None:
        self.register_hook(HookEvent.POST_STAGE, _stage_summary_hook, priority=-100, name="stage_summary")
        self.register_hook(HookEvent.ON_ERROR, _error_suggestion_hook, priority=-100, name="error_suggestion")
        self.register_hook(HookEvent.SESSION_END, _session_summary_hook, priority=-100, name="session_summary")

    def _register_standard_profile(self) -> None:
        self._register_minimal_profile()
        self.register_hook(HookEvent.PRE_STAGE, _pre_stage_context_hook, priority=-100, name="pre_stage_context")
        self.register_hook(HookEvent.PRE_AGENT, _pre_agent_context_hook, priority=-100, name="pre_agent_context")
        self.register_hook(HookEvent.POST_AGENT, _agent_summary_hook, priority=-100, name="agent_summary")
        self.register_hook(HookEvent.POST_STAGE, _stage_note_hook, priority=-90, name="stage_note")

    def _register_strict_profile(self) -> None:
        self._register_standard_profile()
        self.register_hook(HookEvent.PRE_TOOL_USE, _tool_input_guard_hook, priority=100, name="tool_input_guard")
        self.register_hook(HookEvent.POST_TOOL_USE, _tool_usage_summary_hook, priority=-100, name="tool_usage_summary")
        self.register_hook(HookEvent.POST_TOOL_USE, _tool_cost_hook, priority=-90, name="tool_cost")


def _event_patch_bucket(context: Dict[str, Any], bucket_name: str, item: Dict[str, Any]) -> HookResult:
    data = context.get("state", {}).get("data", {})
    existing = data.get(bucket_name)
    values = list(existing) if isinstance(existing, list) else []
    values.append(item)
    return HookResult(patch={bucket_name: values})


def _stage_summary_hook(context: Dict[str, Any]) -> HookResult:
    stage_id = context.get("stage")
    result = context.get("result") if isinstance(context.get("result"), dict) else {}
    item = {
        "stage": stage_id,
        "success": result.get("success"),
        "executed_roles": result.get("executed_roles", []),
        "next_stage": result.get("next_stage"),
        "failed_agent": result.get("failed_agent"),
    }
    return _event_patch_bucket(context, "hook_stage_summaries", item)


def _stage_note_hook(context: Dict[str, Any]) -> HookResult:
    result = context.get("result") if isinstance(context.get("result"), dict) else {}
    stage = context.get("stage") or "unknown_stage"
    if result.get("success"):
        next_stage = result.get("next_stage")
        if next_stage:
            note = f"阶段 {stage} 已完成，下一步进入 {next_stage}。"
        else:
            note = f"阶段 {stage} 已完成。"
    else:
        failed_agent = result.get("failed_agent") or context.get("role") or "unknown_agent"
        note = f"阶段 {stage} 未完成，建议复核 {failed_agent} 的上下文。"
    return _event_patch_bucket(context, "hook_stage_notes", {"stage": stage, "note": note})


def _agent_summary_hook(context: Dict[str, Any]) -> HookResult:
    result = context.get("result") if isinstance(context.get("result"), dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    item = {
        "stage": context.get("stage"),
        "role": context.get("role"),
        "attempt": context.get("attempt"),
        "status": result.get("status", "success"),
        "data_keys": list(data.keys()),
        "has_next_stage": bool(result.get("next_stage")),
    }
    return _event_patch_bucket(context, "hook_agent_summaries", item)


def _tool_cost_hook(context: Dict[str, Any]) -> HookResult:
    tool_name = context.get("tool_name") or "unknown_tool"
    params = context.get("tool_params")
    result = context.get("result")
    cost_hint = len(str(params)) if params is not None else 0
    result_size = len(str(result)) if result is not None else 0
    return _event_patch_bucket(
        context,
        "hook_tool_costs",
        {
            "tool_name": tool_name,
            "cost_hint": cost_hint,
            "result_size": result_size,
        },
    )


def _pre_stage_context_hook(context: Dict[str, Any]) -> HookResult:
    data = context.get("state", {}).get("data", {})
    stage_summaries = data.get("hook_stage_summaries", [])
    agent_summaries = data.get("hook_agent_summaries", [])
    reusable: List[Dict[str, Any]] = []

    if isinstance(stage_summaries, list) and stage_summaries:
        reusable.extend(stage_summaries[-2:])
    if isinstance(agent_summaries, list) and agent_summaries:
        reusable.extend(agent_summaries[-3:])

    if not reusable:
        return HookResult()

    return HookResult(
        patch={
            "hook_injected_stage_context": reusable,
            "hook_reusable_patterns": reusable,
        }
    )


def _pre_agent_context_hook(context: Dict[str, Any]) -> HookResult:
    data = context.get("state", {}).get("data", {})
    stage_context = data.get("hook_injected_stage_context")
    if not stage_context:
        return HookResult()
    return HookResult(patch={"hook_injected_agent_context": stage_context})


def _error_suggestion_hook(context: Dict[str, Any]) -> HookResult:
    error = str(context.get("error") or "")
    item = {
        "stage": context.get("stage"),
        "role": context.get("role"),
        "attempt": context.get("attempt"),
        "error": error,
        "suggestion": "检查输入完整性、输出格式和外部依赖可用性。",
    }
    return _event_patch_bucket(context, "hook_error_suggestions", item)


def _session_summary_hook(context: Dict[str, Any]) -> HookResult:
    state = context.get("state", {})
    summary = state.get("workflow_summary") or {}
    data = state.get("data", {})
    stage_summaries = data.get("hook_stage_summaries", [])
    stage_notes = data.get("hook_stage_notes", [])
    agent_summaries = data.get("hook_agent_summaries", [])
    error_suggestions = data.get("hook_error_suggestions", [])
    tool_costs = data.get("hook_tool_costs", [])

    reusable_patterns: List[str] = []
    if isinstance(stage_summaries, list):
        for item in stage_summaries[-3:]:
            if not isinstance(item, dict):
                continue
            reusable_patterns.append(
                f"stage={item.get('stage')}, success={item.get('success')}, next_stage={item.get('next_stage')}"
            )
    if isinstance(stage_notes, list):
        for item in stage_notes[-3:]:
            if not isinstance(item, dict):
                continue
            note = item.get("note")
            if note:
                reusable_patterns.append(str(note))
    if isinstance(agent_summaries, list):
        for item in agent_summaries[-3:]:
            if not isinstance(item, dict):
                continue
            reusable_patterns.append(
                f"role={item.get('role')}, status={item.get('status')}, data_keys={item.get('data_keys', [])}"
            )
    if isinstance(error_suggestions, list):
        for item in error_suggestions[-3:]:
            if not isinstance(item, dict):
                continue
            suggestion = item.get("suggestion")
            if suggestion:
                reusable_patterns.append(str(suggestion))
    if isinstance(tool_costs, list):
        for item in tool_costs[-3:]:
            if not isinstance(item, dict):
                continue
            tool_name = item.get("tool_name")
            cost_hint = item.get("cost_hint")
            if tool_name:
                reusable_patterns.append(f"tool={tool_name}, cost_hint={cost_hint}")

    item = {
        "task": summary.get("task") or state.get("task"),
        "status": summary.get("status") or state.get("status"),
        "executed_stage_count": len(summary.get("executed_stages", [])),
        "executed_role_count": len(summary.get("executed_roles", [])),
        "failed_role_count": len(summary.get("failed_roles", [])),
    }
    return HookResult(
        patch={
            "hook_session_summary": item,
            "hook_reusable_patterns": reusable_patterns[-10:],
        }
    )


def _tool_input_guard_hook(context: Dict[str, Any]) -> HookResult:
    params = context.get("tool_params")
    if params is None:
        return HookResult(block=True, reason="工具参数为空。")
    if isinstance(params, dict) and not params:
        return HookResult(block=True, reason="工具参数为空对象。")
    return HookResult()


def _tool_usage_summary_hook(context: Dict[str, Any]) -> HookResult:
    item = {
        "tool_name": context.get("tool_name"),
        "status": context.get("status", "success"),
        "has_error": bool(context.get("error")),
    }
    return _event_patch_bucket(context, "hook_tool_usage", item)


global_hook_manager = HookManager(profile="minimal")


active_hook_manager: ContextVar[HookManager] = ContextVar('hook_manager', default=global_hook_manager)
_active_execution_context: ContextVar[Dict[str, Any]] = ContextVar("active_execution_context", default={})


def set_active_hook_manager(manager: HookManager) -> None:
    active_hook_manager.set(manager)


def get_active_hook_manager() -> HookManager:
    return active_hook_manager.get()


def set_active_execution_context(context: Dict[str, Any]):
    return _active_execution_context.set(context)


def reset_active_execution_context(token) -> None:
    _active_execution_context.reset(token)


def get_active_execution_context() -> Dict[str, Any]:
    return _active_execution_context.get()
