"""Optional Qwen runtime adapter exposed through the extension API."""

from astra_core.extensions.api import ExtensionContext


def register_extension(context: ExtensionContext) -> None:
    try:
        from astra_qwen.tools import register_all_qwen_tools
    except ModuleNotFoundError as exc:
        if exc.name == "qwen_agent":
            raise RuntimeError("项目启用了 Qwen Adapter，但当前环境未安装 qwen_agent。") from exc
        raise

    register_all_qwen_tools()
