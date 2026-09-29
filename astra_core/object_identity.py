"""Program-owned, project-local business identities, independent of model output."""
import datetime
import json
import re
import hashlib
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5


def output_root(state):
    return Path(state['data']['run']['output_dir']).resolve().parents[1]


def alias_path(root, key):
    return Path(root) / 'objects' / 'aliases' / (hashlib.sha256(key.encode('utf-8')).hexdigest() + '.json')


def resolve_identifier(root, key):
    if not isinstance(key, str) or not key.strip():
        raise ValueError('请提供要继续的业务对象编号')
    key = key.strip()
    # Registered canonical IDs take precedence over legacy aliases.
    if re.fullmatch(r'obj_[0-9a-f]{32}', key) and (Path(root) / 'objects' / key / 'object.json').is_file():
        return key
    alias = alias_path(root, key)
    if alias.is_file():
        record = json.loads(alias.read_text(encoding='utf-8'))
        if record.get('legacy_key') != key:
            raise ValueError('旧编号映射不一致')
        return record['id']
    raise ValueError(f'找不到业务对象 {key}；旧状态请先执行 astra migrate-state 迁移')


def object_directory(context, state):
    identifier = context.get('id') if isinstance(context, dict) else None
    if not isinstance(identifier, str) or not re.fullmatch(r'obj_[0-9a-f]{32}', identifier):
        raise ValueError('业务对象 ID 无效，请使用程序创建的编号')
    path = output_root(state) / 'objects' / identifier
    manifest = path / 'object.json'
    if not manifest.is_file():
        raise ValueError(f'找不到业务对象 {identifier}，请检查编号')
    record = json.loads(manifest.read_text(encoding='utf-8'))
    if record.get('id') != identifier:
        raise ValueError('业务对象登记信息不一致')
    expected_new = record['creation_run'] == state['data']['run']['id']
    if context.get('is_new') is not expected_new:
        raise ValueError('业务对象的新建状态与当前运行不一致')
    return path


def resolve_object(parameters, brief, state, agent_name):
    if parameters.get('key_field'):
        return resolve_external_object(parameters, brief, state, create=not parameters.get('require_found', False))
    mode = brief.get(parameters['mode_field'])
    run_id = state['data']['run']['id']
    if mode == parameters.get('new_value', 'new'):
        # A retry of the same creation step must reuse its identity, even after a crash.
        seed = json.dumps([str(output_root(state)), run_id, agent_name], ensure_ascii=False)
        identifier = 'obj_' + uuid5(NAMESPACE_URL, seed).hex
        path = output_root(state) / 'objects' / identifier
        path.mkdir(parents=True, exist_ok=True)
        record = {'id': identifier, 'creation_run': run_id, 'creator': agent_name,
                  'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}
        try:
            with (path / 'object.json').open('x', encoding='utf-8') as file:
                json.dump(record, file, ensure_ascii=False, indent=2)
        except FileExistsError:
            record = json.loads((path / 'object.json').read_text(encoding='utf-8'))
            if record.get('creation_run') != run_id or record.get('creator') != agent_name:
                raise ValueError('业务对象编号冲突，已停止创建')
        context = {'id': identifier, 'is_new': True}
    elif mode == parameters.get('continue_value', 'continue'):
        context = {'id': resolve_identifier(output_root(state), brief.get(parameters['id_field'])), 'is_new': False}
    else:
        raise ValueError('业务对象模式无效，必须选择新建或继续')
    object_directory(context, state)
    return context


def resolve_external_object(parameters, brief, state, *, create=True):
    """Stable, typed external keys scoped by business namespace, never model output."""
    field = parameters['key_field']
    value = brief.get(field)
    if isinstance(value, str):
        value = value.strip()
    if type(value) not in (str, int) or value == '':
        raise ValueError(f'task_input.{field} 必须提供非空字符串或整数编号；缺失时不能由模型补造')
    namespace = parameters.get('key_namespace') or field
    root = output_root(state)
    seed = json.dumps(['external', namespace, value], ensure_ascii=False)
    identifier = 'obj_' + uuid5(NAMESPACE_URL, seed).hex
    path = root / 'objects' / identifier
    manifest = path / 'object.json'
    if not manifest.is_file():
        # Never silently start a second history beside a legacy record.
        for old_path in (root / 'state').glob('*.json'):
            if old_path.name.endswith('.previous.json'):
                continue
            old = json.loads(old_path.read_text(encoding='utf-8'))
            if isinstance(old, dict) and str(old.get('key')) == str(value):
                raise ValueError('发现同编号的旧状态；请先用 astra migrate-state 迁移，并通过 object.resolve 的继续模式读取旧编号')
        if not create:
            raise ValueError(f'找不到业务对象 {namespace}:{value}')
        path.mkdir(parents=True, exist_ok=True)
        record = {'id': identifier, 'creation_run': state['data']['run']['id'],
                  'external_namespace': namespace, 'external_key': value,
                  'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}
        try:
            with manifest.open('x', encoding='utf-8') as file:
                json.dump(record, file, ensure_ascii=False, indent=2)
        except FileExistsError:
            pass
    record = json.loads(manifest.read_text(encoding='utf-8'))
    if record.get('id') != identifier or record.get('external_namespace') != namespace or type(record.get('external_key')) is not type(value) or record.get('external_key') != value:
        raise ValueError('外部业务编号登记信息不一致')
    return {'id': identifier, 'is_new': record['creation_run'] == state['data']['run']['id']}


def migrate_legacy_state(root, legacy_key, *, key_path=None, documents=None):
    """Copy a legacy record and explicitly selected documents; keep the originals.

    documents maps destination file names to existing files inside this output root.
    A matching accumulation journal is copied under the new document stem too.
    Publish a complete object before its alias. Repeating the same import is safe.
    """
    root = Path(root).resolve()
    legacy_key = str(legacy_key).strip()
    if not legacy_key:
        raise ValueError('旧编号不能为空')
    matches = []
    for path in (root / 'state').glob('*.json'):
        if path.name.endswith('.previous.json'):
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError('旧状态文件不能指向项目输出目录之外')
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            continue
        if isinstance(record, dict) and 'key' in record and str(record['key']) == legacy_key and 'value' in record:
            matches.append((path, record))
    if len(matches) != 1:
        raise ValueError(f'旧编号 {legacy_key} 必须对应恰好一份状态，实际找到 {len(matches)} 份；未迁移')
    source, record = matches[0]
    identifier = 'obj_' + uuid5(NAMESPACE_URL, json.dumps([str(root), 'legacy', legacy_key], ensure_ascii=False)).hex
    files = {'state.json': source.read_bytes()}
    previous = source.with_name(source.stem + '.previous.json')
    if previous.is_file():
        if not previous.resolve().is_relative_to(root):
            raise ValueError('上一版状态不能指向项目输出目录之外')
        files['state.previous.json'] = previous.read_bytes()
    used_names = set()
    for name, path in (documents or {}).items():
        if not re.fullmatch(r'[^/\\:]+\.(?:txt|md|docx|pdf)', name, re.I) or name.startswith('.'):
            raise ValueError('迁移文档目标必须是普通文件名（txt/md/docx/pdf），不能含路径')
        if name.casefold() in used_names:
            raise ValueError('迁移文档文件名忽略大小写后重复')
        used_names.add(name.casefold())
        path = Path(path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('迁移文档必须是该项目输出目录内的文件')
        files['documents/' + name] = path.read_bytes()
        journal = path.with_name('.' + path.stem + '.parts.json')
        if journal.is_file():
            if not journal.resolve().is_relative_to(root):
                raise ValueError('累积记录不能指向项目输出目录之外')
            destination = 'documents/.' + Path(name).stem + '.parts.json'
            content = json.loads(journal.read_text(encoding='utf-8'))
            content['name'] = Path(name).stem
            raw = json.dumps(content, ensure_ascii=False, indent=2).encode('utf-8')
            if destination in files and files[destination] != raw:
                raise ValueError('同名文档的累积记录不一致')
            files[destination] = raw
    fingerprints = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    for name in ('state.json', 'state.previous.json'):
        if name not in files:
            continue
        saved = json.loads(files[name])
        saved['key'] = identifier
        if key_path:
            if not key_path.startswith('/') or key_path == '/':
                raise ValueError('key_path 必须是业务状态中的 JSON Pointer')
            tokens = [t.replace('~1', '/').replace('~0', '~') for t in key_path[1:].split('/')]
            parent = saved['value']
            try:
                for token in tokens[:-1]:
                    parent = parent[int(token)] if isinstance(parent, list) else parent[token]
                if isinstance(parent, list):
                    parent[int(tokens[-1])] = identifier
                else:
                    parent[tokens[-1]] = identifier
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError('key_path 不能写入旧状态；未迁移') from exc
        files[name] = json.dumps(saved, ensure_ascii=False, indent=2).encode('utf-8')
    objects = root / 'objects'
    objects.mkdir(parents=True, exist_ok=True)
    destination = objects / identifier
    manifest = {'id': identifier, 'creation_run': 'legacy-import:' + identifier,
                'legacy_key': legacy_key, 'source_sha256': fingerprints, 'key_path': key_path,
                'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}
    alias = alias_path(root, legacy_key)
    if alias.exists():
        existing_alias = json.loads(alias.read_text(encoding='utf-8'))
        if existing_alias.get('id') != identifier or existing_alias.get('legacy_key') != legacy_key:
            raise ValueError('旧编号已指向其他对象；未迁移')
    if destination.exists():
        existing = json.loads((destination / 'object.json').read_text(encoding='utf-8'))
        if any(existing.get(k) != manifest[k] for k in ('id', 'legacy_key', 'source_sha256', 'key_path')):
            raise ValueError('已有迁移与本次来源或选项不同；为避免覆盖对象，未迁移')
    else:
        with tempfile.TemporaryDirectory(prefix='.migration-', dir=objects) as temporary:
            staging = Path(temporary) / identifier
            staging.mkdir()
            for name, content in files.items():
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            (staging / 'object.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            staging.rename(destination)
    alias.parent.mkdir(parents=True, exist_ok=True)
    if not alias.exists():
        with alias.open('x', encoding='utf-8') as file:
            json.dump({'legacy_key': legacy_key, 'id': identifier}, file, ensure_ascii=False)
    return {'id': identifier, 'legacy_key': legacy_key, 'path': str(destination)}
