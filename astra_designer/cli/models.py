"""Provider/model selection over the shared Chat Completions transport."""
import json
import os
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from urllib.parse import urlparse

from astra_core.llm.client import ChatModel, NoRedirect
from astra_core.llm.config import FIELDS, preferences, credential_environment
from astra_core.llm.profiles import read_store, active_connection, validate_connection, connection, add_connection, save_store
from astra_designer.cli.streaming import summary_text
from astra_designer.cli.tui import choose

# Model IDs come from the service; no stale hard-coded model catalog.
PROVIDERS = {
    'openai': ('OpenAI', 'https://api.openai.com/v1', 'OPENAI_API_KEY', 'max_completion_tokens'),
    'deepseek': ('DeepSeek', 'https://api.deepseek.com', 'DEEPSEEK_API_KEY', 'max_tokens'),
    'openrouter': ('OpenRouter（含 Claude 等模型）', 'https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY', 'max_tokens'),
    'ollama': ('Ollama 本地', 'http://localhost:11434/v1', 'OLLAMA_API_KEY', 'max_tokens'),
    'lmstudio': ('LM Studio 本地', 'http://localhost:1234/v1', 'LM_STUDIO_API_KEY', 'max_tokens'),
    'custom': ('自定义 OpenAI Compatible', '', 'ASTRA_CUSTOM_API_KEY', 'max_tokens'),
}


def provider_label(profile):
    if profile.get('provider_name'):
        return profile['provider_name']
    provider = profile.get('provider')
    if provider in PROVIDERS:
        return PROVIDERS[provider][0]
    if not provider:
        base = profile.get('base_url', '').rstrip('/')
        for label, address, _, _ in PROVIDERS.values():
            if address and base == address:
                return label
        parsed = urlparse(base)
        if parsed.hostname in {'localhost', '127.0.0.1', '::1'}:
            if parsed.port == 11434:
                return 'Ollama 本地'
            if parsed.port == 1234:
                return 'LM Studio 本地'
    return provider or '自定义服务'


def status(path, client=None):
    try:
        profile = active_connection(path)
        values = preferences(path)
        source = '环境覆盖' if any(name in os.environ for name in FIELDS.values()) else '配置文件'
        # Once constructed, report the actual client rather than a later file edit.
        if isinstance(getattr(client, 'model', None), str):
            values = {key: getattr(client, key, value) for key, value in values.items()}
            source = '当前连接'
        label = provider_label(profile)
        if values.get('base_url') != profile.get('base_url'):
            label = '自定义服务／环境覆盖'
        model = values.get('model') or '未配置 · /model 选择'
        stream = '流式' if values.get('stream') else '非流式'
        return summary_text(f"{label} | {model} | {stream} | 思考 {values.get('reasoning_effort', 'auto')} | {source} | /model 切换")
    except (OSError, ValueError, TypeError) as exc:
        return f'模型配置无效：{exc} · /model 或 /config 修改'


def show_status(path, client=None):
    print('模型 ▸ ' + status(path, client))


def fetch_models(profile, *, api_key=None):
    validate_connection(profile)
    values = {key: value for key, value in profile.items() if key in FIELDS}
    model = ChatModel(**{'model': 'catalog', **values})
    headers = {'Accept': 'application/json'}
    key = os.environ.get(profile.get('api_key_env', 'ASTRA_DESIGNER_API_KEY'), '') if api_key is None else api_key
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = Request(model.base_url + '/models', headers=headers)
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError('模型列表过大，请手动填写模型 ID')
        body = json.loads(raw)
        models = body['data']
        if not isinstance(models, list):
            raise ValueError('模型列表格式无效')
        ids = sorted({row['id'] for row in models if isinstance(row, dict) and isinstance(row.get('id'), str)
                      and row['id'].strip() and len(row['id']) <= 300 and summary_text(row['id']) == row['id']})
        if not ids:
            raise ValueError('服务未返回可选模型，请手动填写模型 ID')
        return ids
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise ValueError(f'获取模型列表失败（HTTP {code}）；请检查密钥环境变量，或手动填写模型 ID') from None
    except (URLError, OSError, HTTPException):
        raise ValueError('无法获取模型列表；请检查地址或手动填写模型 ID') from None
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError('服务不支持标准模型列表，请手动填写模型 ID') from None


def select_model(path, *, credentials=None):
    """Atomic selection: cancel or failure leaves all saved profiles unchanged."""
    path = Path(path)
    store = read_store(path)
    options = []
    for key, provider in store['providers'].items():
        for model in provider['models']:
            selected = {'provider': key, 'model': model['id']}
            label = provider.get('name', key) + f" [{key}] / " + model.get('name', model['id'])
            if model.get('name') and model['name'] != model['id']:
                label += f" ({model['id']})"
            options.append(((key, model['id']), ('● ' if selected == store['active'] else '') + label))
    actions = [('__add__', '添加供应商 / 选择其他模型')]
    current = connection(store)
    if current.get('base_url') and current.get('model'):
        actions.insert(0, ('__same__', '从当前供应商选择其他模型'))
    choice = choose('模型 · 已保存连接', options + actions)
    if choice is None:
        return False
    if choice in {'__add__', '__same__'}:
        provider_key = store['active']['provider'] if choice == '__same__' else None
        if choice == '__same__':
            profile = dict(current)
            label, base = provider_label(profile), profile['base_url']
            env = profile.get('api_key_env', 'ASTRA_DESIGNER_API_KEY')
        else:
            provider = choose('供应商 · OpenAI Compatible', [(key, value[0]) for key, value in PROVIDERS.items()])
            if provider is None:
                return False
            label, base, env, parameter = PROVIDERS[provider]
            base = input(f'API 基础地址 [{base}]: ').strip() or base
            env = input(f'密钥环境变量 [{env}]: ').strip() or env
            profile = {'provider': provider, 'provider_name': label, 'base_url': base, 'api_key_env': env,
                       'stream': True, 'token_parameter': parameter}
        validate_connection(profile)
        ChatModel(base, 'catalog')  # Validate the address before any request.
        mode = choose('模型来源', [('remote', '从服务读取模型列表（GET /models）'), ('manual', '手动输入模型 ID')])
        if mode is None:
            return False
        selected = None
        if mode == 'remote':
            try:
                identity = (base.rstrip('/'), env)
                key = os.environ.get(env)
                if key is None:
                    if credentials is not None and identity in credentials:
                        key = credentials[identity]
                    else:
                        import getpass
                        key = getpass.getpass(f'API Key [{env}]（仅本次进程使用；免认证服务回车）: ')
                        if credentials is not None:
                            credentials[identity] = key
                print('正在读取模型列表…')
                ids = fetch_models(profile, api_key=key)
                selected = choose(label + ' · 模型（列表不保证都支持对话）',
                                  [(value, value) for value in ids] + [('__manual__', '手动填写其他模型 ID')])
                if selected is None:
                    return False
            except ValueError as exc:
                print(str(exc))
        if selected in (None, '__manual__'):
            selected = input('模型 ID（留空取消）: ').strip()
            if not selected:
                return False
        if selected != profile.get('model'):
            profile.pop('model_name', None)
        profile['model'] = selected
        # Preserve user timeout/size limits, but not provider-specific protocol flags.
        old = preferences(path, environment=False)
        for key in ('timeout', 'max_output_tokens', 'max_response_bytes', 'max_content_bytes'):
            profile[key] = old[key]
        ChatModel(**{key: value for key, value in profile.items() if key in FIELDS})
        selection = add_connection(store, profile, provider_key=provider_key)
    else:
        selection = {'provider': choice[0], 'model': choice[1]}
    selected_profile = connection(store, selection)
    ChatModel(**{key: value for key, value in selected_profile.items() if key in FIELDS})
    store['active'] = selection
    save_store(path, store)
    print('模型选择已保存；下一次请求使用新连接。')
    if any(name in os.environ for name in FIELDS.values()):
        print('注意：ASTRA_DESIGNER_* 环境变量仍优先于保存的选择；状态栏显示实际生效配置。')
    show_status(path)
    print(f'密钥读取：{credential_environment(path)}；未设置时会在首次请求前隐藏询问。')
    return True
