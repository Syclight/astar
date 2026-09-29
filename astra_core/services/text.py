import re


def sanitize_final_answer(text: str) -> str:
    """清理模型偶发泄露的 think/tool 标签、提示词残片和日志行。"""
    cleaned = text or ""
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<tool_call>.*?</tool_call>", "", cleaned, flags=re.DOTALL)
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>")[-1]

    for token in ("<think>", "</think>", "<tool_call>", "</tool_call>"):
        cleaned = cleaned.replace(token, "")

    cleaned = re.sub(
        r"\d{4}-\d{2}-\d{2} [\d:, -]+INFO - [^\r\n]*",
        "",
        cleaned,
    )

    cleaned = cleaned.replace("```json", "")
    cleaned = cleaned.replace("```", "")

    bad_fragments = (
        "收到工具结果后，请直接给出最终答案，不要解释你的处理步骤。",
        "收到工具结果后，请直接给出最终答案",
        "直接给出最终答案，不要解释你的处理步骤。",
        "直接给出最终答案",
    )
    for fragment in bad_fragments:
        cleaned = cleaned.replace(fragment, "")

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return "\n".join(lines).strip()
