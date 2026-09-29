"""Designer model usage recorded in a session (reported by the provider)."""
import json
from pathlib import Path


def design_usage_line(directory):
    root = Path(directory)
    files = [*(root / 'diagnostics').glob('call-*.json'), *(root / 'diagnostics').glob('input-contract-*.json'),
             *(root / 'plans').glob('*/revisions/*/response-*.json')]
    total = calls = 0
    for path in files:
        try:
            usage = json.loads(path.read_text(encoding='utf-8')).get('usage') or {}
        except (OSError, ValueError, AttributeError):
            continue
        value = usage.get('total_tokens') if isinstance(usage, dict) else None
        if value is None and isinstance(usage, dict) and all(type(usage.get(k)) is int for k in ('prompt_tokens', 'completion_tokens')):
            value = usage['prompt_tokens'] + usage['completion_tokens']
        if type(value) is int and value >= 0:
            total, calls = total + value, calls + 1
    if not calls:
        return '本会话设计器用量：服务尚未报告'
    return f'本会话设计器用量：{total:,} Token（{calls} 次请求有报告，未报告的请求不计入）'
