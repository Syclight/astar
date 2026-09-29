from astra_core.runtime.pause import WorkflowPause
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from astra_core.services.console import print_detail, print_info
from astra_core.extensions import load_project_extensions, validate_project_extensions
from astra_core.core.orchestrator import OrchestratorAgent
from astra_core.project import DEFAULT_PROJECT_CONFIG_PATH, BusinessProject, load_business_project
from astra_core.runtime.context import project_context
from astra_core.runtime.workflow_config import (
    build_orchestrator_from_config,
    describe_workflow_config,
    get_structure_figure_path,
    load_workflow_config,
)


@dataclass(frozen=True)
class ProjectRuntime:
    """A loaded business project and its engine-facing execution API."""

    project: BusinessProject

    def inspect(self) -> Dict[str, Any]:
        with _project_working_directory(self.project):
            missing_paths = [
                str(path)
                for path in (
                Path(self.project.workflow_config),
                Path(self.project.tools_dir),
                *((Path(self.project.hooks_dir),) if self.project.hooks_dir else ()),
                Path(self.project.data_dir),
                    Path(self.project.prompts_dir),
                )
                if not path.exists()
            ]
            if missing_paths:
                raise FileNotFoundError(f"业务项目缺少目录或配置: {', '.join(missing_paths)}")

            configure(self.project)
            extensions = list(self.project.extensions)
            bootstrap_orchestrator = None
            inspected_orchestrator = None
            runtime_status: Dict[str, str] = {"status": "ready"}
            try:
                validate_project_extensions(self.project)
                bootstrap_orchestrator = OrchestratorAgent(output_dir=self.project.output_dir)
                load_project_extensions(self.project, bootstrap_orchestrator, self.project.output_dir)
                inspected_orchestrator = build_orchestrator(
                    self.project, output_dir=self.project.output_dir
                )
                structure = inspected_orchestrator.get_structure_map()
            except ModuleNotFoundError as exc:
                runtime_status = {
                    "status": "dependency_missing",
                    "dependency": exc.name or "unknown",
                    "message": f"完整加载 Agent 需要安装依赖: {exc.name or 'unknown'}",
                }
                structure = describe_workflow_config(self.project.workflow_config)
            finally:
                for instance in (bootstrap_orchestrator, inspected_orchestrator):
                    if instance is not None:
                        instance.close()
            from astra_core.agents.pending import check_integrations
            try:
                check_integrations(self.project.workflow_config)
            except ValueError as exc:
                runtime_status = {'status': 'pending_integration', 'message': str(exc)}

        return {
            "name": self.project.name,
            "package": self.project.package,
            "project_file": self.project.project_file,
            "workflow_config": self.project.workflow_config,
            "tools_dir": self.project.tools_dir,
            "hooks_dir": self.project.hooks_dir,
            "data_dir": self.project.data_dir,
            "prompts_dir": self.project.prompts_dir,
            "output_dir": self.project.output_dir,
            "initial_task": self.project.initial_task,
            "task_input": self.project.task_input,
            "interaction": self.project.interaction,
            "distribution": self.project.distribution,
            "extensions": extensions,
            "runtime": runtime_status,
            "model_config": self.project.model_config,
            "structure": structure,
        }

    def run(self, initial_task: Optional[str] = None, export_structure: bool = True,
            *, task_input=None, confirmed=False) -> Dict[str, Any]:
        with _project_working_directory(self.project):
            session = _create_run_session(self.project)
            return self._execute_session(session, initial_task=initial_task, export_structure=export_structure,
                                         task_input=task_input, confirmed=confirmed)

    def run_stage(
        self,
        stage_id: str,
        initial_task: Optional[str] = None,
        initial_data: Optional[Dict[str, Any]] = None,
        continue_from_stage: bool = False,
        export_structure: bool = True,
    ) -> Dict[str, Any]:
        with _project_working_directory(self.project):
            session = _create_run_session(self.project)
            return self._run_stage(
                session,
                stage_id,
                initial_task=initial_task,
                initial_data=initial_data,
                continue_from_stage=continue_from_stage,
                export_structure=export_structure,
            )

    def resume(self, run_id: str, *, task_input=None, confirmed=False, review=None) -> Dict[str, Any]:
        with _project_working_directory(self.project):
            session = _load_run_session(self.project, run_id)
            state = _load_run_state(session)
            if (task_input is not None or confirmed) and state.get('status') not in {'waiting_input', 'awaiting_confirmation'}:
                raise ValueError('只有等待输入或确认的运行可补充任务资料；执行后请新建运行')
            if review is not None:
                pending = state.get('pending_review')
                if state.get('status') != 'awaiting_review' or not isinstance(pending, dict):
                    raise ValueError('这次运行没有在等待审阅')
                if not isinstance(review, dict) or type(review.get('approved')) is not bool:
                    raise ValueError('审阅结果需要 approved（true/false），可附 notes')
                # The answer belongs to this pass of the review step; a redo pass asks again.
                state.setdefault('review_answers', {})[pending['token']] = {
                    'approved': review['approved'], 'notes': str(review.get('notes') or '')}
            if state.get("status") == "completed":
                return state

            return self._execute_session(session, export_structure=False, saved_state=state,
                                         task_input=task_input, confirmed=confirmed)

    def _run_workflow(self, session, initial_task, export_structure):
        return self._execute_session(session, initial_task=initial_task, export_structure=export_structure)

    def _run_stage(self, session, stage_id, initial_task, initial_data,
                   continue_from_stage, export_structure):
        return self._execute_session(
            session, initial_task=initial_task, initial_data=initial_data,
            stage_id=stage_id, single_stage=not continue_from_stage,
            export_structure=export_structure,
        )

    def _execute_session(self, session, initial_task=None, initial_data=None,
                         stage_id=None, single_stage=False, export_structure=False,
                         saved_state=None, task_input=None, confirmed=False):
        # Persist a recoverable initial snapshot even if construction fails.
        state = saved_state if saved_state is not None else {
            "status": "running", "task": initial_task if initial_task is not None else self.project.initial_task,
            "data": dict(initial_data or {}), "current_stage": stage_id,
            "single_stage": single_stage,
        }
        state["data"]["run"] = dict(session)
        state["status"] = "running"
        _write_run_state(session, state)
        orchestrator = None
        try:
            if saved_state is None:
                state['task_contract'] = json.loads(json.dumps({
                    'schema': self.project.task_input, 'interaction': self.project.interaction or {}}))
            if task_input is not None:
                if not isinstance(task_input, dict):
                    raise ValueError('task_input 必须是 object')
                state['data']['task_input'] = json.loads(json.dumps(task_input, allow_nan=False))
            state['workflow_config'] = self.project.workflow_config
            pending = state.pop('pending_confirmation', None)
            if confirmed is True and isinstance(pending, dict) and pending.get('agent'):
                # A step paused to show what it will do (e.g. the files to delete); the user approved that step.
                state.setdefault('confirmed_actions', []).append(pending['agent'])
            else:
                state['input_confirmation'] = confirmed is True or state.get('input_confirmation', False)
            for key in ('dependency_message', 'input_errors', 'questions', 'pending_review'):
                state.pop(key, None)
            orchestrator, extension_names = self._prepare_session(session, export_structure)
            base_state = orchestrator.state
            base_state.update(state)
            state = orchestrator.state = base_state
            state["data"]["run"]["extensions"] = extension_names
            orchestrator.hook_manager.events = list(state.get("hook_events", []))
            orchestrator.checkpoint = lambda value: _write_run_state(session, value)
            start_stage = state.get("current_stage") or orchestrator._get_start_stage(None)
            if state.get("single_stage"):
                state = orchestrator.run_stage(start_stage, reset_state=False)
            else:
                state = orchestrator.run_workflow(state["task"], start_stage=start_stage, reset_state=False)
        except WorkflowPause as exc:
            state = orchestrator.state
            state.update(exc.details)
            return self._finalize_session(session, state)
        except BaseException as exc:
            state = orchestrator.state if orchestrator is not None else state
            state["status"] = "failed"
            state.setdefault("errors", []).append({
                "stage": state.get("current_stage"), "node": "runtime",
                "attempt": 0, "error": str(exc) or type(exc).__name__,
            })
            if orchestrator is not None:
                try:
                    orchestrator._finalize_run_state()
                except Exception as finalize_error:
                    # Keep the original failure primary, but do not hide the secondary one.
                    print_info(f"运行状态收尾失败: {finalize_error!r}")
            self._finalize_session(session, state)
            if not isinstance(exc, Exception):
                raise
            return state
        finally:
            if orchestrator is not None:
                orchestrator.close()
        return self._finalize_session(session, state)

    def _prepare_session(
        self,
        session: Dict[str, str],
        export_structure: bool,
    ) -> tuple[OrchestratorAgent, list[str]]:
        configure(self.project)
        _write_run_manifest(session, self.project, status="running")
        workflow = load_workflow_config(self.project.workflow_config)
        bootstrap_orchestrator = OrchestratorAgent(
            output_dir=session["output_dir"],
            hook_profile=workflow.get("orchestrator", {}).get("hook_profile", "minimal"),
        )
        try:
            extension_context = load_project_extensions(
                self.project, bootstrap_orchestrator, session["output_dir"]
            )
            orchestrator = build_orchestrator(
                self.project, output_dir=session["output_dir"],
                hook_manager=bootstrap_orchestrator.hook_manager,
            )
        finally:
            bootstrap_orchestrator.close()
        try:
            if export_structure:
                orchestrator.export_structure_figure(
                    get_structure_figure_path(self.project.workflow_config, output_dir_override=session["output_dir"])
                )
        except BaseException:
            orchestrator.close()
            raise
        return orchestrator, extension_context.loaded_extensions

    def _finalize_session(self, session: Dict[str, str], workflow_state: Dict[str, Any]) -> Dict[str, Any]:
        _write_run_state(session, workflow_state)
        _write_run_manifest(session, self.project, status=str(workflow_state.get("status", "unknown")), state=workflow_state)
        report_result(self.project, workflow_state)
        return workflow_state


def load(project_path: str = DEFAULT_PROJECT_CONFIG_PATH) -> ProjectRuntime:
    """Load a business project without starting a workflow."""
    return ProjectRuntime(load_business_project(project_path))


def inspect(project_path: str = DEFAULT_PROJECT_CONFIG_PATH) -> Dict[str, Any]:
    """One-shot project inspection convenience API."""
    return load(project_path).inspect()


def resume(run_id: str) -> Dict[str, Any]:
    """Find a local run by ID and resume it through its owning business project."""
    manifest_paths = list(Path("business").glob(f"*/output/runs/{run_id}/run.json"))
    if not manifest_paths:
        raise FileNotFoundError(f"未找到运行记录: {run_id}")
    if len(manifest_paths) > 1:
        raise ValueError(f"运行 ID 不唯一，请通过已加载项目调用 runtime.resume(): {run_id}")

    with manifest_paths[0].open("r", encoding="utf-8") as file:
        manifest = json.load(file)
    project_file = manifest.get("project", {}).get("project_file")
    if not isinstance(project_file, str) or not project_file:
        raise ValueError(f"运行记录缺少项目配置路径: {manifest_paths[0]}")
    return load(project_file).resume(run_id)


def configure(project: BusinessProject) -> None:
    Path(project.output_dir).mkdir(parents=True, exist_ok=True)


# Kept as an internal compatibility name; no process-global chdir occurs.
_project_working_directory = project_context


def build_orchestrator(
    project: BusinessProject,
    output_dir: Optional[str] = None,
    hook_manager: Optional[Any] = None,
) -> OrchestratorAgent:
    return build_orchestrator_from_config(
        project.workflow_config,
        output_dir_override=output_dir,
        hook_manager=hook_manager,
    )


def _create_run_session(project: BusinessProject) -> Dict[str, str]:
    run_id = _new_run_id()
    output_dir = Path(project.output_dir) / "runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    return {
        "id": run_id,
        "output_dir": str(output_dir),
        "manifest_path": str(output_dir / "run.json"),
        "state_path": str(output_dir / "state.json"),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_run_session(project: BusinessProject, run_id: str) -> Dict[str, str]:
    if not run_id or Path(run_id).name != run_id:
        raise ValueError("run_id 必须是单个目录名，不能包含路径分隔符。")
    output_dir = Path(project.output_dir) / "runs" / run_id
    manifest_path = output_dir / "run.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"未找到运行记录: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as file:
        manifest = json.load(file)
    return {
        "id": run_id,
        "output_dir": str(output_dir),
        "manifest_path": str(manifest_path),
        "state_path": str(output_dir / "state.json"),
        "started_at": str(manifest.get("started_at") or datetime.now(timezone.utc).isoformat()),
    }


def _load_run_state(session: Dict[str, str]) -> Dict[str, Any]:
    state_path = Path(session["state_path"])
    if not state_path.is_file():
        raise FileNotFoundError(f"运行记录缺少状态快照，无法恢复: {state_path}")
    with state_path.open("r", encoding="utf-8") as file:
        state = json.load(file)
    if not isinstance(state, dict):
        raise ValueError(f"运行状态快照必须是 object: {state_path}")
    return state


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_run_state(session: Dict[str, str], state: Dict[str, Any]) -> None:
    _atomic_json(session["state_path"], state)


def _write_run_manifest(
    session: Dict[str, str],
    project: BusinessProject,
    status: str,
    state: Optional[Dict[str, Any]] = None,
) -> None:
    manifest: Dict[str, Any] = {
        "run_id": session["id"],
        "status": status,
        "project": {
            "name": project.name,
            "package": project.package,
            "project_file": project.project_file,
            "workflow_config": project.workflow_config,
        },
        "output_dir": session["output_dir"],
        "started_at": session["started_at"],
    }
    if state is not None:
        manifest["workflow_summary"] = state.get("workflow_summary", {})
        manifest["artifacts"] = state.get("artifact_records", [])
        if status in {'completed', 'failed'}:
            manifest["finished_at"] = datetime.now(timezone.utc).isoformat()

    _atomic_json(session["manifest_path"], manifest)


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def report_result(project: BusinessProject, workflow_state: Dict[str, Any]) -> None:
    run = workflow_state.get("data", {}).get("run", {})
    output_dir = run.get("output_dir", project.output_dir) if isinstance(run, dict) else project.output_dir
    if workflow_state.get('status') in {'waiting_input', 'awaiting_confirmation'}:
        print_detail(f"任务等待补充资料或确认，运行 ID: {run.get('id')}。")
    elif workflow_state.get('status') == 'awaiting_review':
        print_detail(f"任务等待人工审阅，运行 ID: {run.get('id')}。")
    elif workflow_state.get('status') == 'waiting_dependencies':
        print_detail(f"任务等待接入能力，资料已保存，运行 ID: {run.get('id')}。")
    elif workflow_state.get('input_errors'):
        print_info('任务资料不符合规范：' + '；'.join(workflow_state['input_errors']))
    elif workflow_state.get("status") != "completed":
        report_path = os.path.join(output_dir, "orchestrator_report.json")
        print_info(f"工作流执行失败，请查看 {report_path} 中的错误信息和改进建议。")
    else:
        print_detail(f"工作流执行完成，结果已输出到 {output_dir} 目录。")
