"""Readable provider catalogs, with legacy configuration migration."""
import copy
import json
import re
from dataclasses import fields
from pathlib import Path
from urllib.parse import urlparse

from astra_core.llm.client import ChatModel, parse_stream

MODEL_FIELDS = {field.name for field in fields(ChatModel)} - {'api_key'}
TUNING_FIELDS = MODEL_FIELDS - {'base_url', 'model'}
META_FIELDS = {'provider', 'api_key_env', 'provider_name', 'model_name'}
DEFAULTS = {field.name: field.default for field in fields(ChatModel) if field.name in TUNING_FIELDS}


def validate_connection(profile):
    if not isinstance(profile, dict) or set(profile) - (MODEL_FIELDS | META_FIELDS):
        raise ValueError('模型配置包含未知字段；密钥只能使用环境变量或交互输入')
    key_env = profile.get('api_key_env', 'ASTRA_DESIGNER_API_KEY')
    if not isinstance(key_env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key_env):
        raise ValueError('api_key_env 必须是环境变量名，不是密钥')
    for key in ('provider', 'provider_name', 'model_name'):
        if key in profile and not isinstance(profile[key], str):
            raise ValueError(f'{key} 必须是字符串')


def tuning(profile):
    return {key: profile.get(key, value) for key, value in DEFAULTS.items()}


def connection(store, selection=None):
    selected = store['active'] if selection is None else selection
    if selected is None:
        return {}
    provider = store['providers'][selected['provider']]
    model = next(item for item in provider['models'] if item['id'] == selected['model'])
    return {**DEFAULTS, **provider.get('defaults', {}),
            **{key: value for key, value in model.items() if key in TUNING_FIELDS},
            'base_url': provider['base_url'], 'model': model['id'],
            'provider': selected['provider'], 'provider_name': provider.get('name', selected['provider']),
            'model_name': model.get('name', model['id']),
            'api_key_env': provider.get('api_key_env', 'ASTRA_DESIGNER_API_KEY')}


def validate_store(store):
    if not isinstance(store, dict) or set(store) != {'active', 'providers'} or not isinstance(store['providers'], dict):
        raise ValueError('模型配置需要 active 和 providers')
    for key, provider in store['providers'].items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]*', key):
            raise ValueError('供应商 key 请使用可读名称，如 ollama-local、vllm-lab')
        if (not isinstance(provider, dict) or set(provider) - {'name', 'base_url', 'api_key_env', 'defaults', 'models'}
                or not {'base_url', 'models'} <= provider.keys()):
            raise ValueError(f'供应商 {key} 字段无效；使用 base_url，不使用 baseUrl 或 apiKey')
        defaults = provider.get('defaults', {})
        if not isinstance(defaults, dict) or set(defaults) - TUNING_FIELDS:
            raise ValueError(f'供应商 {key} 的 defaults 只能包含支持的模型参数')
        validate_connection({'provider_name': provider.get('name', key), 'api_key_env': provider.get('api_key_env', 'ASTRA_DESIGNER_API_KEY')})
        models = provider['models']
        if not isinstance(models, list) or not models:
            raise ValueError(f'供应商 {key} 至少需要一个模型')
        ids = set()
        for model in models:
            if (not isinstance(model, dict) or set(model) - (TUNING_FIELDS | {'id', 'name'})
                    or not isinstance(model.get('id'), str) or not model['id'].strip() or model['id'] != model['id'].strip()):
                raise ValueError(f'供应商 {key} 的模型需要非空 id，只能填写支持的参数')
            if model['id'] in ids:
                raise ValueError(f'供应商 {key} 的模型 id 重复')
            ids.add(model['id'])
            if 'name' in model and not isinstance(model['name'], str):
                raise ValueError('模型 name 必须是字符串')
            values = {**defaults, **{k: v for k, v in model.items() if k in TUNING_FIELDS}}
            # Check provider defaults even when every model overrides them.
            try:
                ChatModel(provider['base_url'], model['id'], **defaults)
                ChatModel(provider['base_url'], model['id'], **values)
            except (TypeError, AttributeError) as exc:
                raise ValueError(f'供应商 {key} 的模型参数类型无效') from exc
    active = store['active']
    if active is None and not store['providers']:
        return
    if not isinstance(active, dict) or set(active) != {'provider', 'model'}:
        raise ValueError('active 必须明确指定 provider 和 model')
    if (not isinstance(active['provider'], str) or not isinstance(active['model'], str)
            or active['provider'] not in store['providers']
            or active['model'] not in {m['id'] for m in store['providers'][active['provider']]['models']}):
        raise ValueError('active 引用了不存在的供应商或模型')


def readable_key(profile):
    candidate = profile.get('provider')
    if candidate and re.fullmatch(r'[a-z][a-z0-9_-]*', candidate):
        return candidate
    parsed = urlparse(profile['base_url'])
    if parsed.hostname in {'localhost', '127.0.0.1', '::1'}:
        if parsed.port == 11434:
            return 'ollama'
        if parsed.port == 1234:
            return 'lmstudio'
    for host, name in [('api.openai.com', 'openai'), ('api.deepseek.com', 'deepseek'), ('openrouter.ai', 'openrouter')]:
        if parsed.hostname == host:
            return name
    return 'custom'


def add_connection(store, profile, *, provider_key=None, preserve_conflicts=False):
    """Reuse a server and its existing model; migration splits conflicting duplicates."""
    validate_connection(profile)
    ChatModel(**{key: value for key, value in profile.items() if key in MODEL_FIELDS})
    base = profile['base_url'].rstrip('/')
    env = profile.get('api_key_env', 'ASTRA_DESIGNER_API_KEY')
    settings = tuning(profile)
    keys = [provider_key] if provider_key else list(store['providers'])
    for key in keys:
        provider = store['providers'][key]
        if provider['base_url'].rstrip('/') != base or provider.get('api_key_env', 'ASTRA_DESIGNER_API_KEY') != env:
            continue
        selected = {'provider': key, 'model': profile['model']}
        existing = next((m for m in provider['models'] if m['id'] == profile['model']), None)
        if existing:
            if preserve_conflicts and tuning(connection(store, selected)) != settings:
                continue
            return selected
        item = {'id': profile['model'], **settings}
        if profile.get('model_name'):
            item['name'] = profile['model_name']
        provider['models'].append(item)
        return selected
    seed = readable_key(profile)
    key, suffix = seed, 2
    while key in store['providers']:
        key, suffix = f'{seed}-{suffix}', suffix + 1
    store['providers'][key] = {
        'name': profile.get('provider_name', seed), 'base_url': base, 'api_key_env': env,
        'models': [{'id': profile['model'], **({'name': profile['model_name']} if profile.get('model_name') else {}), **settings}],
    }
    return {'provider': key, 'model': profile['model']}


def migrate(stored):
    if not isinstance(stored, dict):
        raise ValueError('模型配置必须是 JSON object')
    if 'providers' in stored:
        validate_store(stored)
        result = copy.deepcopy(stored)
        for provider in result['providers'].values():
            inherited = provider.pop('defaults', {})
            provider['models'] = [
                {'id': model['id'], **DEFAULTS, **inherited, **model}
                for model in provider['models']]
        return result
    if 'endpoint' in stored:
        raise ValueError('endpoint 已移除，请改用 base_url')
    if 'version' in stored:
        if (set(stored) != {'version', 'active', 'profiles'} or stored['version'] != 2
                or not isinstance(stored['profiles'], dict) or not isinstance(stored['active'], str)
                or stored['active'] not in stored['profiles']):
            raise ValueError('旧版模型配置结构无效')
        profiles, active = stored['profiles'], stored['active']
    else:
        profiles, active = {'default': stored}, 'default'
    result = {'active': None, 'providers': {}}
    for key, original in profiles.items():
        validate_connection(original)
        if not original:
            continue
        profile = dict(original)
        # Legacy files allowed numeric/boolean strings through preferences().
        for field in ('timeout', 'max_output_tokens', 'max_response_bytes', 'max_content_bytes'):
            if isinstance(profile.get(field), str):
                profile[field] = float(profile[field]) if field == 'timeout' else int(profile[field])
        if 'stream' in profile:
            profile['stream'] = parse_stream(profile['stream'])
        selected = add_connection(result, profile, preserve_conflicts=True)
        if key == active:
            result['active'] = selected
    validate_store(result)
    return result


def read_store(path):
    path = Path(path)
    return migrate(json.loads(path.read_text(encoding='utf-8')) if path.exists() else {})


def active_connection(path):
    path = Path(path)
    stored = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    # Preserve partial legacy files whose address/model are supplied by environment.
    if isinstance(stored, dict) and 'version' not in stored and 'providers' not in stored:
        if 'endpoint' in stored:
            raise ValueError('endpoint 已移除，请改用 base_url')
        validate_connection(stored)
        return dict(stored)
    return connection(migrate(stored))


def save_store(path, store):
    from astra_designer.contracts.session import save_json
    store = migrate(store)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, store)


def save_connection(path, values):
    """Edit explicit active-model parameters without changing sibling models."""
    store = read_store(path)
    previous = connection(store)
    profile = {**previous, **values}
    if profile.get('model') != previous.get('model') and 'model_name' not in values:
        profile.pop('model_name', None)
    validate_connection(profile)
    ChatModel(**{k: v for k, v in profile.items() if k in MODEL_FIELDS})
    if any(previous.get(k) != profile.get(k) for k in ('base_url', 'api_key_env', 'model')):
        store['active'] = add_connection(store, profile)
    else:
        active = store['active']
        provider = store['providers'][active['provider']]
        item = next(m for m in provider['models'] if m['id'] == active['model'])
        item.update(tuning(profile))
    save_store(path, store)
