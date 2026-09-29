"""Designer behaviour settings, separate from model connections.

Optional file .astra-designer/settings.json, e.g. {"max_plan_calls": 10};
null means no limit. ASTRA_DESIGNER_MAX_PLAN_CALLS overrides the file.
"""
import json
import os
from pathlib import Path

DEFAULT_PATH = Path('.astra-designer/settings.json')
DEFAULTS = {'max_plan_calls': 6}


def load_settings(path=DEFAULT_PATH):
    values = dict(DEFAULTS)
    path = Path(path)
    if path.is_file():
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or set(data) - set(DEFAULTS):
            raise ValueError(f"{path} 只能包含：{'、'.join(DEFAULTS)}")
        values.update(data)
    if 'ASTRA_DESIGNER_MAX_PLAN_CALLS' in os.environ:
        raw = os.environ['ASTRA_DESIGNER_MAX_PLAN_CALLS'].strip()
        values['max_plan_calls'] = None if raw.lower() in {'', 'none', 'null', 'unlimited'} else int(raw)
    limit = values['max_plan_calls']
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError(f'max_plan_calls 必须是正整数，或 null 表示不限，当前为 {limit!r}')
    return values


def limit_message(kind, limit):
    return (f'本需求会话的{kind}已用完 {limit} 次模型请求（模型截断、连接失败不计入）；'
            f'可在 {DEFAULT_PATH} 调整 max_plan_calls，或用 /new 新建会话')
