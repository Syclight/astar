import json
import os
from typing import Any, Dict, List, Optional

from astra_core.runtime.hooks import HookEvent, HookManager, HookResult
from astra_core.services.console import print_detail
from astra_core.services.files import write_json_file, write_text_file


DEFAULT_OUTPUT_DIR = "output"


def _output_path(output_dir: str, filename: str) -> str:
    return os.path.join(output_dir, filename)


def _artifact_records(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    records = state.setdefault("artifact_records", [])
    if not isinstance(records, list):
        records = []
        state["artifact_records"] = records
    return records


def _register_artifact(
    state: Dict[str, Any],
    name: str,
    path: str,
    source: Optional[str] = None,
    kind: str = "file",
) -> None:
    if not name or not path:
        return

    records = _artifact_records(state)
    normalized_name = str(name)
    normalized_path = str(path)
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("name") == normalized_name:
            record["path"] = normalized_path
            if source is not None:
                record["source"] = source
            record["kind"] = kind
            return

    record: Dict[str, Any] = {
        "name": normalized_name,
        "path": normalized_path,
        "kind": kind,
    }
    if source is not None:
        record["source"] = source
    records.append(record)


def _artifact_name_from_state_key(key: str) -> Optional[str]:
    if not key or not key.endswith("_path"):
        return None

    name = key[:-5]
    if name.endswith("_output"):
        name = name[:-7]
    return name or None


def _register_state_path_artifacts(state: Dict[str, Any]) -> None:
    data = state.get("data", {})
    if not isinstance(data, dict):
        return

    for key, value in data.items():
        if not isinstance(value, str):
            continue
        artifact_name = _artifact_name_from_state_key(key)
        if not artifact_name:
            continue
        if os.path.exists(value):
            _register_artifact(state, artifact_name, value, source=key, kind="workflow_path")


def persist_answer(
    output_json_path: str,
    output_text_path: str,
    answer: Any,
) -> bool:
    os.makedirs(os.path.dirname(output_json_path) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(output_text_path) or ".", exist_ok=True)

    if isinstance(answer, (dict, list)):
        write_json_file(output_json_path, answer)
        return True

    if isinstance(answer, (str, bytes, bytearray)):
        try:
            answer_json = json.loads(answer)
            write_json_file(output_json_path, answer_json)
            return True
        except Exception:
            print_detail(f"生成的答案不是有效的 JSON 格式，已保存为文本文件。位置：{output_text_path}")
            write_text_file(
                output_text_path,
                answer.decode("utf-8", errors="replace") if isinstance(answer, (bytes, bytearray)) else answer,
            )
            return False

    print_detail(f"生成的答案类型为 {type(answer).__name__}，已按文本保存。位置：{output_text_path}")
    write_text_file(output_text_path, str(answer))
    return False


def _result_brief(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict):
        return None

    data_payload = result.get("data") if isinstance(result.get("data"), dict) else None
    data_keys = list(data_payload.keys()) if data_payload else []
    top_level_data_keys = [
        key
        for key in result.keys()
        if key not in {"status", "error", "message", "data", "next_stage", "next_role"}
    ]

    brief: Dict[str, Any] = {
        "status": result.get("status", "success"),
        "data_keys": data_keys or top_level_data_keys,
    }
    if result.get("next_stage") is not None:
        brief["next_stage"] = result.get("next_stage")
    if result.get("next_role") is not None:
        brief["next_role"] = result.get("next_role")
    if result.get("message"):
        brief["message"] = result.get("message")
    return brief


def _build_execution_timeline(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    timeline: List[Dict[str, Any]] = []
    for record in state.get("executions", []):
        item = {
            "stage": record.get("stage"),
            "role": record.get("role"),
            "attempt": record.get("attempt"),
            "status": record.get("status"),
        }
        if record.get("error"):
            item["error"] = record.get("error")
        result_summary = _result_brief(record.get("result"))
        if result_summary:
            item["result"] = result_summary
        timeline.append(item)
    return timeline


def _build_failure_summary(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for error in errors:
        stage = error.get("stage")
        node = error.get("node")
        key = f"{stage}:{node}"
        item = grouped.setdefault(
            key,
            {
                "stage": stage,
                "role": node,
                "attempts": [],
                "last_error": None,
            },
        )
        item["attempts"].append(error.get("attempt"))
        item["last_error"] = error.get("error")
    return list(grouped.values())


def _build_artifact_index(state: Dict[str, Any], output_dir: str) -> Dict[str, str]:
    _register_state_path_artifacts(state)
    records = state.get("artifact_records", [])
    if not isinstance(records, list):
        records = []

    index: Dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        name = record.get("name")
        path = record.get("path")
        if not name or not path:
            continue
        if record.get("kind") == "agent_raw_output":
            continue
        if os.path.exists(path):
            index[str(name)] = str(path)

    default_artifacts = {
        "orchestrator_log": _output_path(output_dir, "orchestrator.log"),
        "orchestrator_structure": _output_path(output_dir, "orchestrator_structure.png"),
    }
    for name, path in default_artifacts.items():
        if os.path.exists(path) and name not in index:
            index[name] = path

    agents_root = _output_path(output_dir, "agents")
    if os.path.isdir(agents_root):
        agent_dirs: Dict[str, str] = {}
        for root, _, files in os.walk(agents_root):
            if not any(filename.endswith("_raw.json") for filename in files):
                continue
            rel_dir = os.path.relpath(root, agents_root)
            parts = rel_dir.split(os.sep)
            if len(parts) < 2:
                continue
            role_name = parts[1]
            agent_dirs.setdefault(role_name, root + os.sep)

        for role_name, agent_dir in sorted(agent_dirs.items()):
            index.setdefault(role_name, agent_dir)

    return index


def _safe_name(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        text = fallback

    cleaned: List[str] = []
    for char in text:
        if char.isalnum() or char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            cleaned.append("_")

    normalized = "".join(cleaned).strip("._")
    return normalized or fallback


def _strip_answer_from_data(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: _strip_answer_from_data(value)
            for key, value in payload.items()
            if not str(key).endswith("_answer")
        }
    if isinstance(payload, list):
        return [_strip_answer_from_data(item) for item in payload]
    return payload


def _sanitize_agent_raw_result(result: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = dict(result)
    data = result.get("data")
    if not isinstance(data, dict):
        if data in (None, {}):
            sanitized.pop("data", None)
        return sanitized

    cleaned_data: Dict[str, Any] = {}
    for key, value in data.items():
        if str(key).endswith("_answer"):
            continue
        if key == "role_status":
            sanitized["role_status"] = value
            continue
        if key in result and result.get(key) == value:
            continue
        cleaned_data[key] = _strip_answer_from_data(value)

    if cleaned_data:
        sanitized["outputs"] = cleaned_data
    sanitized.pop("data", None)
    return sanitized


def _persist_agent_raw_output(
    state: Dict[str, Any],
    output_dir: str,
    stage: Optional[str],
    role: Optional[str],
    attempt: Optional[int],
    result: Dict[str, Any],
) -> Optional[str]:
    if not isinstance(result, dict):
        return None

    stage_name = _safe_name(stage, "unknown_stage")
    role_name = _safe_name(role, "unknown_agent")
    attempt_no = attempt if isinstance(attempt, int) and attempt > 0 else 1
    agent_dir = _output_path(output_dir, os.path.join("agents", stage_name, role_name))
    os.makedirs(agent_dir, exist_ok=True)

    output_path = _output_path(agent_dir, f"attempt_{attempt_no}_raw.json")
    payload = {
        "stage": stage,
        "role": role,
        "attempt": attempt_no,
        "result": _sanitize_agent_raw_result(result),
    }
    write_json_file(output_path, payload)
    _register_artifact(
        state,
        f"agent_raw_output_{stage_name}_{role_name}_attempt_{attempt_no}",
        output_path,
        source=f"post_agent:{stage}:{role}:attempt_{attempt_no}",
        kind="agent_raw_output",
    )
    state.setdefault("data", {})
    if isinstance(state["data"], dict):
        state["data"]["last_agent_output_dir"] = agent_dir
        state["data"]["last_agent_output_path"] = output_path
    return output_path


def _iter_artifact_source_entries(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []

    records = state.get("artifact_records", [])
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            entries.append(
                {
                    "name": record.get("name"),
                    "path": record.get("path"),
                    "source": record.get("source"),
                    "kind": record.get("kind", "file"),
                }
            )

    data = state.get("data", {})
    if isinstance(data, dict):
        for key, value in data.items():
            if not isinstance(value, str) or not os.path.exists(value):
                continue
            artifact_name = _artifact_name_from_state_key(key)
            if not artifact_name:
                continue
            entries.append(
                {
                    "name": artifact_name,
                    "path": value,
                    "source": key,
                    "kind": "workflow_path",
                }
            )

    return entries


def _build_report_improvements(state: Dict[str, Any]) -> Dict[str, List[str]]:
    data = state.get("data", {})
    failed_roles = state.get("workflow_summary", {}).get("failed_roles", [])
    suggestions: List[str] = []
    strategies: List[str] = []

    if failed_roles:
        suggestions.append(f"优先排查失败 Agent：{', '.join(str(item) for item in failed_roles)}。")
    else:
        suggestions.append("当前未发现失败 Agent，工作流执行链路稳定。")

    hook_error_suggestions = data.get("hook_error_suggestions", [])
    if isinstance(hook_error_suggestions, list):
        for item in hook_error_suggestions[:3]:
            if not isinstance(item, dict):
                continue
            suggestion = item.get("suggestion")
            if suggestion:
                stage = item.get("stage") or "unknown_stage"
                role = item.get("role") or "unknown_role"
                suggestions.append(f"{stage}/{role}：{suggestion}")

    stage_summaries = data.get("hook_stage_summaries", [])
    stage_notes = data.get("hook_stage_notes", [])
    if isinstance(stage_summaries, list) and stage_summaries:
        success_count = len([item for item in stage_summaries if isinstance(item, dict) and item.get("success")])
        strategies.append(f"阶段摘要：{success_count}/{len(stage_summaries)} 个阶段成功完成。")
    if isinstance(stage_notes, list) and stage_notes:
        strategies.append(f"阶段备注：已记录 {len(stage_notes)} 条阶段备注。")

    agent_summaries = data.get("hook_agent_summaries", [])
    if isinstance(agent_summaries, list) and agent_summaries:
        strategies.append(f"Agent 摘要：已记录 {len(agent_summaries)} 次 Agent 输出。")

    tool_costs = data.get("hook_tool_costs", [])
    if isinstance(tool_costs, list) and tool_costs:
        strategies.append(f"工具审计：已记录 {len(tool_costs)} 次工具使用成本提示。")

    hook_events = state.get("hook_events", [])
    if hook_events:
        output_dir = data.get("hook_artifact_output_dir") or DEFAULT_OUTPUT_DIR
        strategies.append(f"Hook 明细已单独写入 {output_dir}/hook_events.json，主报告仅保留统计摘要。")

    return {
        "suggestions": _unique_strings(suggestions),
        "strategies": _unique_strings(strategies),
    }


def _unique_strings(values: List[str]) -> List[str]:
    unique_values: List[str] = []
    seen = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


def _build_hook_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "invocation_count": len(events),
        "events": {},
    }
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type") or "unknown"
        hook_name = event.get("hook") or "unknown"
        event_summary = summary["events"].setdefault(
            event_type,
            {
                "invocation_count": 0,
                "hooks": {},
            },
        )
        event_summary["invocation_count"] += 1
        event_summary["hooks"][hook_name] = event_summary["hooks"].get(hook_name, 0) + 1
    return summary


def build_orchestrator_report(state: Dict[str, Any], output_dir: str = DEFAULT_OUTPUT_DIR) -> Dict[str, Any]:
    """生成面向排障的精简报告，避免把完整业务数据重复嵌入执行记录。"""
    improvements = _build_report_improvements(state)

    return {
        "summary": state.get("workflow_summary") or {},
        "failures": _build_failure_summary(state.get("errors", [])),
        "timeline": {
            "stages": state.get("stage_executions", []),
            "agents": _build_execution_timeline(state),
        },
        "artifacts": _build_artifact_index(state, output_dir),
        "improvements": improvements,
        "hooks": {
            "summary": _build_hook_summary(state.get("hook_events", [])),
            "session_summary": state.get("data", {}).get("hook_session_summary", {}),
        },
    }


def persist_session_artifacts(state: Dict[str, Any], output_dir: str = DEFAULT_OUTPUT_DIR) -> None:
    os.makedirs(output_dir, exist_ok=True)

    hook_events = state.get("hook_events")
    if hook_events:
        hook_events_path = _output_path(output_dir, "hook_events.json")
        write_json_file(hook_events_path, hook_events)
        _register_artifact(state, "hook_events", hook_events_path, source="hook_events", kind="hook_log")
    report_path = _output_path(output_dir, "orchestrator_report.json")
    write_json_file(report_path, build_orchestrator_report(state, output_dir=output_dir))
    _register_artifact(state, "orchestrator_report", report_path, source="orchestrator_report", kind="report")
    write_json_file(report_path, build_orchestrator_report(state, output_dir=output_dir))


def persist_workflow_outputs(
    state: Dict[str, Any],
    output_dir: str = DEFAULT_OUTPUT_DIR,
    verbose_missing: bool = True,
    include_session_artifacts: bool = True,
    persist_answer_summaries: bool = False,
) -> None:
    """将工作流中各阶段的关键输出结果持久化到文件，方便后续查看和分析。"""
    os.makedirs(output_dir, exist_ok=True)

    data = state.get("data", {})
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "task":
                continue

            if key.endswith("_output_path") or key.endswith("_path"):
                if not isinstance(value, str):
                    continue
                artifact_name = _artifact_name_from_state_key(key)
                if artifact_name and os.path.exists(value):
                    _register_artifact(state, artifact_name, value, source=key, kind="workflow_path")
                continue

            if not key.endswith("_answer"):
                continue
            if not persist_answer_summaries:
                continue
            if not isinstance(value, (dict, list, str, bytes, bytearray)):
                continue

            base_name = key[:-7]
            json_path = _output_path(output_dir, f"{base_name}.json")
            md_path = _output_path(output_dir, f"{base_name}.md")
            persist_answer(json_path, md_path, value)
            _register_artifact(state, base_name, json_path, source=key)
            if os.path.exists(md_path):
                _register_artifact(state, f"{base_name}_md", md_path, source=key)

    for entry in _iter_artifact_source_entries(state):
        name = entry.get("name")
        path = entry.get("path")
        if not name or not path:
            continue
        _register_artifact(state, str(name), str(path), source=entry.get("source"), kind=entry.get("kind", "file"))

    if include_session_artifacts:
        persist_session_artifacts(state, output_dir=output_dir)


def register_workflow_artifact_hook(
    hook_manager: HookManager,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    verbose_missing: bool = True,
) -> None:
    def _persist_agent_raw_output_hook(context: Dict[str, Any]) -> HookResult:
        state = context.get("state")
        result = context.get("result")
        if not isinstance(state, dict) or not isinstance(result, dict):
            return HookResult()

        output_path = _persist_agent_raw_output(
            state,
            output_dir=output_dir,
            stage=context.get("stage"),
            role=context.get("role"),
            attempt=context.get("attempt"),
            result=result,
        )
        if not output_path:
            return HookResult()

        return HookResult(
            state_patch={
                "artifact_records": list(state.get("artifact_records", [])),
            }
        )

    hook_manager.register_hook(
        HookEvent.POST_AGENT,
        _persist_agent_raw_output_hook,
        priority=-100,
        name="agent_raw_output_persistence",
    )

    def _persist_workflow_artifacts(context: Dict[str, Any]) -> HookResult:
        state = context.get("state")
        if not isinstance(state, dict): # 理论上不应该发生，除非 runtime 内部出现严重错误，此时不再尝试持久化以避免覆盖掉可能还未写入的日志等重要调试信息。
            return HookResult()
        persist_workflow_outputs(
            state,
            output_dir=output_dir,
            verbose_missing=verbose_missing,
            include_session_artifacts=False,
        )
        return HookResult(
            patch={
                "hook_artifact_output_dir": output_dir,
            },
        )

    hook_manager.register_hook(
        HookEvent.SESSION_END,
        _persist_workflow_artifacts,
        priority=0,
        name="artifact_persistence",
    )
