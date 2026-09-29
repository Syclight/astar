import json
from typing import Any, Dict, List

from qwen_agent.agents import Assistant

from astra_core.core.base_agent import BaseAgent
from astra_core.services.console import print_info
from astra_core.services.files import read_md_files
from astra_core.runtime.settings import get_default_llm_config
from astra_core.services.text import sanitize_final_answer


class QwenAssistantAgent(BaseAgent):
    """对 qwen Assistant 的统一封装，兼容主控 Agent 的 state 输入输出协议。"""

    def __init__(
        self,
        name: str,
        system_files: List[str],
        tool_names: List[str],
    ):
        super().__init__(name)
        self.system_files = system_files
        self.tool_names = tool_names
        self.agent = Assistant(
            llm=get_default_llm_config(),
            system_message=read_md_files(*system_files),
            function_list=tool_names,
        )

    def build_query(self, state: Dict[str, Any]) -> str:
        raise NotImplementedError

    def _get_manual_rerun_context(self, state: Dict[str, Any]) -> Dict[str, Any]:
        data = state.get("data", {})
        contexts = data.get("manual_rerun_contexts")
        if not isinstance(contexts, dict):
            return {}

        for key in (state.get("current_role"), state.get("current_stage")):
            if not key:
                continue
            context = contexts.get(key)
            if isinstance(context, dict):
                return context
        return {}

    def _build_query_context_suffix(self, state: Dict[str, Any]) -> str:
        notes = []

        manual_rerun_context = self._get_manual_rerun_context(state)
        manual_instruction = str(manual_rerun_context.get("instruction") or "").strip()
        if manual_instruction:
            notes.append(f"手动重跑补充：{manual_instruction}")

        retry_context = state.get("retry_context") or {}
        retry_instruction = str(retry_context.get("instruction") or "").strip()
        if retry_instruction:
            notes.append(f"系统重试补充：{retry_instruction}")

        if not notes:
            return ""
        return "补充要求：\n" + "\n".join(f"- {note}" for note in notes)

    def build_success_payload(self, answer: str, state: Dict[str, Any]) -> Dict[str, Any]:
        return {"answer": answer}

    def build_failure_message(self, query: str) -> str:
        return f"{query} 生成未成功，请稍后重试。" if query else "生成未成功，请稍后重试。"

    def _run_agent(self, query: str) -> Dict[str, Any]:
        messages = [{"role": "user", "content": query}]
        print_info(f"[👤 User] {query}")

        has_called_tool = False
        last_tool_name = None
        final_content = ""
        fallback_content = ""

        for response in self.agent.run(messages):
            latest_message = response[-1]

            if "function_call" in latest_message:
                fn_call = latest_message["function_call"]
                tool_name = fn_call.get("name")
                if tool_name and tool_name != last_tool_name:
                    print(f"[🔧 Tool] 正在调用工具: {tool_name} ...")
                    last_tool_name = tool_name
                    has_called_tool = True
                continue

            content = latest_message.get("content", "")
            if not content:
                continue

            last_tool_name = None
            fallback_content = content
            if has_called_tool:
                final_content = content

        answer = sanitize_final_answer(final_content or fallback_content)
        if not answer:
            return {
                "status": "failed",
                "error": self.build_failure_message(query),
            }

        try:
            json_answer = json.loads(answer)
            print_info(json_answer, tip="[🤖 Assistant] ", use_json_format=True)
        except json.JSONDecodeError:
            print_info(answer, tip="[🤖 Assistant] ")

        return {
            "status": "success",
            "answer": answer,
        }

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        query = self.build_query(state)
        result = self._run_agent(query)
        if result.get("status") != "success":
            retry_context = state.get("retry_context") or {}
            if retry_context:
                result["strategy"] = (
                    f"针对 {retry_context.get('role', self.name)} 的最近错误进行定向重试，"
                    "并补充更明确的输入与输出约束。"
                )
            return result

        answer = result["answer"]
        payload = self.build_success_payload(answer, state)
        return {
            **payload,
            "status": "success",
        }
