import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from astra_core.agents.default.selection_interaction import ConsoleSelectionInteractor, SelectionInteractor
from astra_core.core.base_agent import BaseAgent
from astra_core.services.console import print_info


@dataclass
class SelectionAgentConfig:
    """通用人机协同选择 Agent 的字段和流程配置。"""

    source_data_key: str
    option_keys: Sequence[str]
    recommendation_keys: Sequence[str]
    selected_key: str
    selected_index_key: str
    source_key: str
    item_label: str = "方案"
    options_title: str = "候选方案列表"
    recommendation_title: str = "系统推荐方案如下"
    missing_error: str = "未找到可供选择的数据，请先完成推荐生成阶段。"
    display_fields: Sequence[Tuple[str, str, str]] = field(default_factory=tuple)
    static_options: Sequence[Dict[str, Any]] = field(default_factory=tuple)
    static_recommendation: Optional[Dict[str, Any]] = None
    enable_custom_input: bool = True


class GenericSelectionAgent(BaseAgent):
    """通用选择 Agent：展示候选项/推荐项，并支持选择、采纳推荐、自定义输入或请求重跑。"""

    def __init__(
        self,
        name: str,
        config: SelectionAgentConfig,
        interactor: Optional[SelectionInteractor] = None,
    ):
        super().__init__(name)
        self.config = config
        self.interactor = interactor or ConsoleSelectionInteractor()

    @property
    def selection_mode(self) -> str:
        return "single"

    def _load_source_payload(self, state: Dict[str, Any]) -> Dict[str, Any]:
        payload = state.get("data", {}).get(self.config.source_data_key)
        if not payload:
            return self._build_static_payload()

        if isinstance(payload, dict):
            return payload

        try:
            loaded_payload = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return self._build_static_payload()

        return loaded_payload if isinstance(loaded_payload, dict) else self._build_static_payload()

    def _build_static_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if self.config.static_options:
            option_key = self.config.option_keys[0] if self.config.option_keys else "options"
            payload[option_key] = list(self.config.static_options)
        if self.config.static_recommendation:
            recommendation_key = self.config.recommendation_keys[0] if self.config.recommendation_keys else "recommendation"
            payload[recommendation_key] = self.config.static_recommendation
        return payload

    def _get_first_dict_value(self, data: Dict[str, Any], keys: Iterable[str]) -> Optional[Dict[str, Any]]:
        for key in keys:
            value = data.get(key)
            if isinstance(value, dict):
                return value
        return None

    def _get_first_list_value(self, data: Dict[str, Any], keys: Iterable[str]) -> List[Dict[str, Any]]:
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _extract_options(self, payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            return [], None

        options = self._get_first_list_value(data, self.config.option_keys)
        recommendation = self._get_first_dict_value(data, self.config.recommendation_keys)
        return options, recommendation

    def _format_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _parse_custom_value(self, value: str) -> Any:
        text = value.strip()
        if not text:
            return None
        if text[0] in "{[":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text

    def _get_nested_value(self, item: Dict[str, Any], path: str) -> Any:
        current: Any = item
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def _set_nested_value(self, item: Dict[str, Any], path: str, value: Any) -> None:
        current = item
        parts = path.split(".")
        for part in parts[:-1]:
            next_value = current.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                current[part] = next_value
            current = next_value
        current[parts[-1]] = value

    def _render_item_brief(self, item: Dict[str, Any], index: Optional[int] = None) -> str:
        title = self.config.item_label
        if index is not None:
            title = f"{title} {index}"

        lines = [f"{title}:"]
        for path, label, default in self.config.display_fields:
            value = self._get_nested_value(item, path)
            formatted_value = self._format_value(value) or default
            lines.append(f"  {label}: {formatted_value}")

        if len(lines) == 1:
            lines.append("  " + json.dumps(item, ensure_ascii=False))
        return "\n".join(lines)

    def _print_options(self, options: List[Dict[str, Any]], recommendation: Optional[Dict[str, Any]]) -> None:
        self._print_candidate_items(options, recommendation)
        input_lines = [
            "\n请输入你的选择：",
            f"  1..N : 选择对应编号的候选{self.config.item_label}",
        ]
        if recommendation:
            input_lines.append(f"  r    : 直接采用推荐{self.config.item_label}")
        if self.config.enable_custom_input:
            input_lines.append(f"  c    : 自定义输入{self.config.item_label}")
        input_lines.append("  g    : 重新运行推荐生成阶段")
        print_info("\n".join(input_lines))

    def _print_candidate_items(self, options: List[Dict[str, Any]], recommendation: Optional[Dict[str, Any]]) -> None:
        if options:
            option_lines = [self._render_item_brief(item, index + 1) for index, item in enumerate(options)]
            print_info(f"\n{self.config.options_title}：\n" + "\n\n".join(option_lines))

        if recommendation:
            print_info(
                f"{self.config.recommendation_title}：\n"
                + self._render_item_brief(recommendation).replace(f"{self.config.item_label}:\n", "")
            )

    def _prompt_user_choice(self, options: List[Dict[str, Any]], recommendation: Optional[Dict[str, Any]]) -> str:
        while True:
            user_input = self.interactor.read("请输入你的选择: ").strip().lower()
            if user_input == "g":
                return "rerun"
            if user_input == "r" and recommendation:
                return "recommended"
            if user_input == "c" and self.config.enable_custom_input:
                return "custom"
            if user_input.isdigit():
                selected_index = int(user_input) - 1
                if 0 <= selected_index < len(options):
                    return user_input
            print_info("输入无效，请重新输入。")

    def _prompt_rerun_instruction(self) -> str:
        print_info(
            "\n你选择了重新运行推荐生成阶段。\n"
            "可输入补充说明，让大模型结合新的约束重新生成；直接回车则按原输入重跑。"
        )
        return self.interactor.read("补充说明: ").strip()

    def _prompt_custom_item(self) -> Dict[str, Any]:
        while True:
            print_info(
                "\n进入自定义输入向导：\n"
                f"  - 可直接粘贴完整 JSON 作为自定义{self.config.item_label}\n"
                "  - 或直接回车，按字段逐项填写\n"
                "  - 字段值以 { 或 [ 开头时会按 JSON 解析"
            )
            raw_json = self.interactor.read(f"请粘贴 JSON，或回车逐项输入自定义{self.config.item_label}: ").strip()
            if raw_json:
                try:
                    custom_item = json.loads(raw_json)
                except json.JSONDecodeError:
                    print_info("JSON 格式无效，请重新输入，或回车改用逐项填写。")
                    continue
                if isinstance(custom_item, dict):
                    print_info(f"已读取自定义{self.config.item_label}。")
                    return custom_item
                print_info("自定义 JSON 必须是对象格式，请重新输入。")
                continue

            custom_item: Dict[str, Any] = {}
            for path, label, default in self.config.display_fields:
                prompt = f"{label}"
                if default:
                    prompt += f"（默认提示：{default}）"
                value = self.interactor.read(f"{prompt}: ").strip()
                parsed_value = self._parse_custom_value(value)
                if parsed_value is not None:
                    self._set_nested_value(custom_item, path, parsed_value)

            if custom_item:
                print_info(
                    f"自定义{self.config.item_label}预览：\n"
                    + self._render_item_brief(custom_item).replace(f"{self.config.item_label}:\n", "")
                )
                confirmed = self.interactor.read("确认使用该自定义输入？([y]/n): ").strip().lower()
                if confirmed in {"y", "Y", "yes", ""}:
                    return custom_item
                continue

            print_info("自定义内容不能为空，请重新输入。")

    def _build_manual_rerun_context(self, state: Dict[str, Any], instruction: str) -> Dict[str, Any]:
        return {
            "instruction": instruction.strip(),
            "source_agent": self.name,
            "source_stage": state.get("current_stage"),
            "source_role": state.get("current_role"),
        }

    def _build_success_data(
        self,
        status: str,
        source: str,
        selected_item: Optional[Dict[str, Any]] = None,
        selected_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        data = {
            "role_status": status,
            self.config.source_key: source,
            "selection": {
                "mode": self.selection_mode,
                "outcome": "selected" if status == "selected" else status,
                "source": source,
            },
        }
        if selected_item is not None:
            data[self.config.selected_key] = selected_item
            data[self.config.selected_index_key] = selected_index
            data["selection"].update({"items": [selected_item], "indices": [selected_index]})
        return data

    def _build_rerun_result(self, state: Dict[str, Any], data: Dict[str, Any], instruction: str) -> Dict[str, Any]:
        manual_rerun_contexts: Dict[str, Any] = {}
        existing_contexts = state.get("data", {}).get("manual_rerun_contexts")
        if isinstance(existing_contexts, dict):
            manual_rerun_contexts.update(existing_contexts)

        rerun_context = self._build_manual_rerun_context(state, instruction)
        rerun_context["outcome"] = "rerun"
        manual_rerun_contexts[self.name] = rerun_context

        data["manual_rerun_contexts"] = manual_rerun_contexts
        data["selection"] = {
            "mode": self.selection_mode,
            "outcome": "rerun",
            "source": "rerun_requested",
            "instruction": instruction.strip(),
        }
        return {
            "status": "success",
            "data": data,
            "outcome": "rerun",
        }

    def _build_selection_result(
        self,
        state: Dict[str, Any],
        source: str,
        selected_item: Dict[str, Any],
        selected_index: Optional[int],
    ) -> Dict[str, Any]:
        del state
        data = self._build_success_data("selected", source, selected_item, selected_index)
        return {
            "status": "success",
            "data": data,
            "outcome": "selected",
        }

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._load_source_payload(state)
        options, recommendation = self._extract_options(payload)

        if not options and not recommendation:
            return {
                "status": "failed",
                "error": self.config.missing_error,
            }

        self._print_options(options, recommendation)
        user_choice = self._prompt_user_choice(options, recommendation)

        if user_choice == "rerun":
            rerun_instruction = self._prompt_rerun_instruction()
            print_info("已收到重新生成请求，工作流将回到推荐生成阶段。")
            return self._build_rerun_result(
                state,
                self._build_success_data("rerun_requested", "rerun_requested"),
                rerun_instruction,
            )

        if user_choice == "recommended":
            print_info(f"已采用系统推荐{self.config.item_label}。")
            return self._build_selection_result(state, "recommended", recommendation, None)

        if user_choice == "custom":
            custom_item = self._prompt_custom_item()
            print_info(f"已采用自定义{self.config.item_label}。")
            return self._build_selection_result(state, "custom_input", custom_item, None)

        selected_index = int(user_choice) - 1
        selected_item = options[selected_index]
        print_info(f"已选择第 {selected_index + 1} 个候选{self.config.item_label}。")
        return self._build_selection_result(state, "user_selected", selected_item, selected_index + 1)


class MultiSelectionAgent(GenericSelectionAgent):
    """通用多选 Agent：展示候选项，并将用户选择的多个项目传递给后续 Agent。"""

    @property
    def selection_mode(self) -> str:
        return "multiple"

    def _print_options(self, options: List[Dict[str, Any]], recommendation: Optional[Dict[str, Any]]) -> None:
        self._print_candidate_items(options, recommendation)
        input_lines = [
            "\n多选输入格式：",
            f"  1,3,5 : 选择多个候选{self.config.item_label}",
            "  1-3   : 选择连续编号范围",
            f"  all   : 选择全部候选{self.config.item_label}",
        ]
        if recommendation:
            input_lines.append(f"  r     : 直接采用推荐{self.config.item_label}")
        if self.config.enable_custom_input:
            input_lines.append(f"  c     : 自定义输入{self.config.item_label}")
        input_lines.append("  g     : 重新运行推荐生成阶段")
        print_info("\n".join(input_lines))

    def _parse_multi_choice(self, user_input: str, option_count: int) -> Optional[List[int]]:
        normalized = user_input.strip().lower().replace("，", ",")
        if not normalized:
            return None
        if normalized in {"all", "a"}:
            return list(range(option_count))

        selected_indices: List[int] = []
        for chunk in normalized.split(","):
            token = chunk.strip()
            if not token:
                continue
            if "-" in token:
                start_text, end_text = token.split("-", 1)
                if not start_text.isdigit() or not end_text.isdigit():
                    return None
                start = int(start_text)
                end = int(end_text)
                if start > end:
                    return None
                selected_indices.extend(range(start - 1, end))
                continue
            if not token.isdigit():
                return None
            selected_indices.append(int(token) - 1)

        if not selected_indices:
            return None
        if any(index < 0 or index >= option_count for index in selected_indices):
            return None

        deduped_indices = []
        seen = set()
        for index in selected_indices:
            if index in seen:
                continue
            seen.add(index)
            deduped_indices.append(index)
        return deduped_indices

    def _prompt_user_choice(self, options: List[Dict[str, Any]], recommendation: Optional[Dict[str, Any]]) -> str:
        while True:
            user_input = self.interactor.read("请输入你的选择: ").strip().lower()
            if user_input == "g":
                return "rerun"
            if user_input == "r" and recommendation:
                return "recommended"
            if user_input == "c" and self.config.enable_custom_input:
                return "custom"
            selected_indices = self._parse_multi_choice(user_input, len(options))
            if selected_indices is not None:
                return ",".join(str(index + 1) for index in selected_indices)
            print_info("输入无效，请重新输入。")

    def _build_multi_success_data(
        self,
        status: str,
        source: str,
        selected_items: Optional[List[Dict[str, Any]]] = None,
        selected_indices: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        data = {
            "role_status": status,
            self.config.source_key: source,
            "selection": {
                "mode": "multiple",
                "outcome": "selected" if status == "selected" else status,
                "source": source,
            },
        }
        if selected_items is not None:
            data[self.config.selected_key] = selected_items
            data[self.config.selected_index_key] = selected_indices or []
            data["selection"].update({"items": selected_items, "indices": selected_indices or []})
        return data

    def _build_multi_selection_result(
        self,
        state: Dict[str, Any],
        source: str,
        selected_items: List[Dict[str, Any]],
        selected_indices: List[int],
    ) -> Dict[str, Any]:
        del state
        data = self._build_multi_success_data("selected", source, selected_items, selected_indices)
        return {
            "status": "success",
            "data": data,
            "outcome": "selected",
        }

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        payload = self._load_source_payload(state)
        options, recommendation = self._extract_options(payload)

        if not options and not recommendation:
            return {
                "status": "failed",
                "error": self.config.missing_error,
            }

        self._print_options(options, recommendation)
        user_choice = self._prompt_user_choice(options, recommendation)

        if user_choice == "rerun":
            rerun_instruction = self._prompt_rerun_instruction()
            print_info("已收到重新生成请求，工作流将回到推荐生成阶段。")
            return self._build_rerun_result(
                state,
                self._build_multi_success_data("rerun_requested", "rerun_requested"),
                rerun_instruction,
            )

        if user_choice == "recommended":
            print_info(f"已采用系统推荐{self.config.item_label}。")
            return self._build_multi_selection_result(
                state=state,
                source="recommended",
                selected_items=[recommendation],
                selected_indices=[],
            )

        if user_choice == "custom":
            custom_item = self._prompt_custom_item()
            print_info(f"已采用自定义{self.config.item_label}。")
            return self._build_multi_selection_result(
                state=state,
                source="custom_input",
                selected_items=[custom_item],
                selected_indices=[],
            )

        selected_indices = [int(item) for item in user_choice.split(",") if item]
        selected_items = [options[index - 1] for index in selected_indices]
        print_info(f"已选择候选{self.config.item_label}：{', '.join(str(index) for index in selected_indices)}。")
        return self._build_multi_selection_result(
            state=state,
            source="user_multi_selected",
            selected_items=selected_items,
            selected_indices=selected_indices,
        )


