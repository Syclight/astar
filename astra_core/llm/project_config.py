"""Portable project model profiles; credentials are environment references only."""
import json
import os
from pathlib import Path
import re

from astra_core.llm.client import ChatModel, DEFAULT_MAX_RESPONSE_BYTES, DEFAULT_MAX_CONTENT_BYTES

MODEL_FIELDS = ('base_url', 'model', 'timeout', 'max_output_tokens', 'token_parameter',
                'stream', 'response_format', 'reasoning_effort', 'max_response_bytes', 'max_content_bytes')


def blank_profile():
    return {'base_url': '', 'model': '', 'api_key_env': 'ASTRA_RUNTIME_API_KEY',
            'timeout': 300, 'max_output_tokens': 8192, 'token_parameter': 'max_tokens',
            'stream': True, 'response_format': 'text', 'reasoning_effort': 'auto',
            'max_response_bytes': DEFAULT_MAX_RESPONSE_BYTES, 'max_content_bytes': DEFAULT_MAX_CONTENT_BYTES}


def validate_profile(value):
    if isinstance(value, dict) and 'endpoint' in value:
        raise ValueError('endpoint 已移除，请改用 base_url，填写不含 /chat/completions 的 API 基础地址')
    if not isinstance(value, dict) or set(value) - {*MODEL_FIELDS, 'api_key_env'}:
        raise ValueError('项目模型配置必须是 object，密钥只能通过 api_key_env 引用环境变量')
    result = {**blank_profile(), **value}
    if not isinstance(result['api_key_env'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', result['api_key_env']):
        raise ValueError('api_key_env 必须是有效环境变量名，不能填写密钥')
    for key in ('base_url', 'model'):
        if not isinstance(result[key], str):
            raise ValueError(f'项目模型 {key} 必须是字符串，可留空稍后配置')
        result[key] = result[key].strip()
    # Validate parameters even in a not-yet-configured project.
    values = {key: result[key] for key in MODEL_FIELDS}
    values['base_url'] = values['base_url'] or 'http://localhost/v1'
    values['model'] = values['model'] or 'unconfigured'
    try:
        ChatModel(**values)
    except (TypeError, AttributeError) as exc:
        raise ValueError('项目模型参数类型无效') from exc
    return result


def load_profile(root, filename):
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError('model_config 必须是项目内 JSON 文件的相对路径')
    relative = Path(filename)
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root):
        raise ValueError('model_config 必须位于当前项目目录内')
    return validate_profile(json.loads(path.read_text(encoding='utf-8-sig')))


def profile_model(profile):
    if not profile['base_url'] or not profile['model']:
        raise ValueError('项目模型尚未配置，请填写 project.yaml 的 model_config 文件中的 base_url 和 model，或同时设置 ASTRA_RUNTIME_BASE_URL 与 ASTRA_RUNTIME_MODEL')
    return ChatModel(**{key: profile[key] for key in MODEL_FIELDS},
                     api_key=os.environ.get(profile['api_key_env'], ''))
