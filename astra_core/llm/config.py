"""Workspace-local model preferences; credentials stay in environment/memory."""
import getpass
import os
from pathlib import Path

from astra_core.llm.client import ChatModel, parse_stream, reject_legacy_environment, DEFAULT_MAX_RESPONSE_BYTES, DEFAULT_MAX_CONTENT_BYTES

DEFAULT_CONFIG = Path('.astra-designer/model.json')
FIELDS = {'base_url': 'ASTRA_DESIGNER_BASE_URL', 'model': 'ASTRA_DESIGNER_MODEL',
          'timeout': 'ASTRA_DESIGNER_TIMEOUT', 'max_output_tokens': 'ASTRA_DESIGNER_MAX_OUTPUT_TOKENS',
          'token_parameter': 'ASTRA_DESIGNER_TOKEN_PARAMETER', 'stream': 'ASTRA_DESIGNER_STREAM',
          'response_format': 'ASTRA_DESIGNER_RESPONSE_FORMAT',
          'reasoning_effort': 'ASTRA_DESIGNER_REASONING_EFFORT',
          'max_response_bytes': 'ASTRA_DESIGNER_MAX_RESPONSE_BYTES',
          'max_content_bytes': 'ASTRA_DESIGNER_MAX_CONTENT_BYTES'}


def preferences(path=DEFAULT_CONFIG, *, environment=True):
    path = Path(path)
    values = {'timeout': 60, 'max_output_tokens': 8192, 'token_parameter': 'max_completion_tokens'}
    if path.exists():
        from astra_core.llm.profiles import active_connection
        stored = active_connection(path)
        values.update({key: value for key, value in stored.items() if key in FIELDS})
    if environment:
        reject_legacy_environment('ASTRA_DESIGNER_')
        values.update({key: os.environ[env] for key, env in FIELDS.items() if env in os.environ})
    values['timeout'] = float(values['timeout'])
    values['max_output_tokens'] = int(values['max_output_tokens'])
    for key, default in (('max_response_bytes', DEFAULT_MAX_RESPONSE_BYTES), ('max_content_bytes', DEFAULT_MAX_CONTENT_BYTES)):
        value = values.get(key, default)
        values[key] = int(value) if isinstance(value, str) else value
    values['stream'] = parse_stream(values.get('stream', False))
    values.setdefault('response_format', 'text')
    values.setdefault('reasoning_effort', 'auto')
    return values


def configured_model(path=DEFAULT_CONFIG, *, interactive=False, credentials=None):
    values = preferences(path)
    if not values.get('base_url') or not values.get('model'):
        raise ValueError('尚未配置模型，请运行 python -m astra_designer config')
    key_env = credential_environment(path)
    key = os.environ.get(key_env, '')
    identity = (values['base_url'].rstrip('/'), key_env)
    if key_env not in os.environ and credentials is not None and identity in credentials:
        key = credentials[identity]
    elif interactive and key_env not in os.environ:
        key = getpass.getpass(f'API Key [{key_env}]（隐藏输入，仅本次使用；免认证服务直接回车）: ')
        if credentials is not None:
            credentials[identity] = key
    return ChatModel(**values, api_key=key)


def credential_environment(path=DEFAULT_CONFIG):
    from astra_core.llm.profiles import active_connection
    # An explicit address override must not borrow a saved provider's credential.
    if 'ASTRA_DESIGNER_BASE_URL' in os.environ:
        return 'ASTRA_DESIGNER_API_KEY'
    return active_connection(path).get('api_key_env', 'ASTRA_DESIGNER_API_KEY')


def runtime_model():
    """Explicit runtime environment, then project profile, then legacy workspace fallback."""
    reject_legacy_environment('ASTRA_RUNTIME_')
    if 'ASTRA_RUNTIME_BASE_URL' in os.environ or 'ASTRA_RUNTIME_MODEL' in os.environ:
        values = {key: os.environ[env.replace('ASTRA_DESIGNER_', 'ASTRA_RUNTIME_')]
                  for key, env in FIELDS.items() if env.replace('ASTRA_DESIGNER_', 'ASTRA_RUNTIME_') in os.environ}
        if not values.get('base_url') or not values.get('model'):
            raise ValueError('请同时配置 ASTRA_RUNTIME_BASE_URL 和 ASTRA_RUNTIME_MODEL')
        values['timeout'] = float(values.get('timeout', 60))
        values['max_output_tokens'] = int(values.get('max_output_tokens', 8192))
        for key in ('max_response_bytes', 'max_content_bytes'):
            if key in values:
                values[key] = int(values[key])
        values['stream'] = parse_stream(values.get('stream', False))
        return ChatModel(**values, api_key=os.environ.get('ASTRA_RUNTIME_API_KEY', ''))
    from astra_core.runtime.context import project_model_config
    from astra_core.llm.project_config import profile_model
    profile = project_model_config.get()
    if profile is not None:
        return profile_model(profile)
    return configured_model()

