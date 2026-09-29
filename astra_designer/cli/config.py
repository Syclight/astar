"""Interactive model configuration wizard."""
import os
from pathlib import Path
from astra_designer.llm.client import ChatModel
from astra_core.llm.client import parse_stream
from astra_designer.llm.config import DEFAULT_CONFIG, FIELDS, preferences, configured_model  # noqa: F401 (re-exported for discovery.chat)

def configure(path=DEFAULT_CONFIG):
    values = preferences(path, environment=False)
    print('配置 Chat Completions 兼容服务。密钥不写入配置文件。')
    for key, title in [('base_url', 'API 基础地址 base_url（例如 https://api.openai.com/v1）'), ('model', '模型名'), ('timeout', '超时秒数'),
                       ('max_output_tokens', '输出 token 上限'), ('token_parameter', '上限字段（max_completion_tokens / max_tokens）'),
                       ('stream', '流式接收（true / false）'),
                       ('response_format', '响应格式（text / json_object / json_schema）'),
                       ('reasoning_effort', '思考设置（auto / none / low / medium / high / max）')]:
        current = values.get(key, '')
        answer = input(f'{title} [{current}]: ').strip()
        values[key] = answer or current
    values['timeout'] = float(values['timeout'])
    values['max_output_tokens'] = int(values['max_output_tokens'])
    values['stream'] = parse_stream(values['stream'])
    ChatModel(**values)  # Validate before touching an existing profile.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from astra_core.llm.profiles import save_connection
    save_connection(path, values)
    print(f'模型配置已保存: {path.resolve()}')
    if any(env in os.environ for env in FIELDS.values()):
        print('已设置的 ASTRA_DESIGNER_* 环境变量优先于此配置。')
    return values
