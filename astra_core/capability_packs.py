"""Local executable capability packages. Discovery never imports package code."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import ast
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, SchemaError
from astra_core.llm.contracts import check_schema, parse_json

TEXT = {'type': 'string', 'minLength': 1}
PORTS = {'type': 'object', 'additionalProperties': TEXT}
DEPENDENCIES = {'type': 'object', 'additionalProperties': False, 'properties': {
    k: {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string', 'pattern': pattern}}
    for k, pattern in {'environment': r'^[A-Za-z_][A-Za-z0-9_]*$',
                       'executables': r'^[A-Za-z0-9_.-]+$', 'python_modules': r'^[A-Za-z_][A-Za-z0-9_]*$'}.items()}}
MANIFEST = {'type': 'object', 'additionalProperties': False,
    'required': ['format_version', 'name', 'version', 'runtime', 'files', 'permissions', 'schemas', 'capabilities'],
    'properties': {
        'format_version': {'const': 1},
        'name': {'type': 'string', 'pattern': r'^[a-z][a-z0-9_]{0,40}$'},
        'version': {'type': 'string', 'pattern': r'^\d+\.\d+\.\d+$'},
        'runtime': {'const': 'astra-pack@1'},
        'files': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': TEXT},
        'permissions': {'type': 'array', 'uniqueItems': True, 'items': {'enum': ['network', 'process', 'filesystem']}},
        'schemas': {'type': 'object', 'additionalProperties': {'type': 'object'}},
        'capabilities': {'type': 'array', 'minItems': 1, 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['id', 'title', 'description', 'tags', 'entry', 'inputs', 'outputs', 'parameters'],
            'properties': {
                **{key: TEXT for key in ('id', 'title', 'description', 'entry', 'guide')},
                'tags': {'type': 'array', 'items': TEXT}, 'inputs': PORTS, 'outputs': PORTS,
                'parameters': {'type': 'object'}, 'dependencies': DEPENDENCIES,
            }}}}}


def store_root():
    return Path(os.environ.get('ASTRA_CAPABILITY_PACK_HOME', 'workspace/capability-packs')).resolve()


def safe_file(root, name):
    if not isinstance(name, str) or '\\' in name or ':' in name:
        raise ValueError('能力包路径必须是包内相对路径')
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in ('.', '..') for part in relative.parts) or str(relative) != name:
        raise ValueError('能力包路径不能越界或含歧义')
    path = Path(root) / name
    if not path.resolve().is_relative_to(Path(root).resolve()) or path.is_symlink() or not path.is_file():
        raise ValueError(f'能力包文件缺失或越界: {name}')
    if path.stat().st_size > 2_000_000:
        raise ValueError(f'能力包文件过大: {name}')
    return path


def load_pack(root):
    root = Path(root).resolve()
    manifest = parse_json(safe_file(root, 'manifest.json').read_text(encoding='utf-8'))
    try:
        Draft202012Validator(MANIFEST).validate(manifest)
    except ValidationError as exc:
        raise ValueError('能力包清单无效: ' + exc.message) from exc
    prefix = manifest['name'] + '.'
    for name, schema in manifest['schemas'].items():
        if not re.fullmatch(manifest['name'] + r'_[a-z0-9_]+', name):
            raise ValueError('包内 Schema 必须使用 包名_ 前缀')
        try:
            check_schema(schema)
        except SchemaError as exc:
            raise ValueError('能力包 Schema 无效: ' + exc.message) from exc
    ids = set()
    for cap in manifest['capabilities']:
        if not cap['id'].startswith(prefix) or not re.fullmatch(r'[a-z][a-z0-9_.]+@[1-9][0-9]*', cap['id']) or cap['id'] in ids:
            raise ValueError('能力 ID 必须唯一，使用 包名.能力@版本 格式')
        ids.add(cap['id'])
        entry, separator, function = cap['entry'].partition(':')
        if not separator or not entry.endswith('.py') or not re.fullmatch(r'[a-zA-Z_]\w*', function) or entry not in manifest['files']:
            raise ValueError('entry 必须为已声明的 Python文件:函数名')
        try:
            check_schema(cap['parameters'])
        except SchemaError as exc:
            raise ValueError('能力参数 Schema 无效: ' + exc.message) from exc
        if cap['parameters'].get('type') != 'object':
            raise ValueError('能力参数 Schema 必须为 object')
        if not cap['outputs']:
            raise ValueError('能力必须声明输出端口')
    if any(name.casefold() in {'manifest.json', '.installed.json'} for name in manifest['files']):
        raise ValueError('files 不能声明 manifest.json 或安装保留文件')
    names = ['manifest.json', *manifest['files']]
    if len({n.casefold() for n in names}) != len(names):
        raise ValueError('包文件名忽略大小写后重复')
    files = {name: safe_file(root, name).read_text(encoding='utf-8') for name in names}
    for name, content in files.items():
        if name.endswith('.py'):
            try:
                ast.parse(content, filename=name)
            except SyntaxError as exc:
                raise ValueError(f'能力包 Python 语法错误: {name}:{exc.lineno}') from exc
    hashes = {name: hashlib.sha256(content.encode('utf-8')).hexdigest() for name, content in files.items()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {'root': str(root), 'manifest': manifest, 'files': files, 'hashes': hashes, 'sha256': digest}


def readiness(capability):
    missing = []
    for kind, names in capability.get('dependencies', {}).items():
        for name in names:
            available = (bool(os.environ.get(name)) if kind == 'environment' else
                         bool(shutil.which(name)) if kind == 'executables' else importlib.util.find_spec(name) is not None)
            if not available:
                missing.append(f'{kind}:{name}')
    return {'status': 'ready' if not missing else 'needs_configuration', 'missing': missing}


def installed_packs():
    root = store_root()
    index = root / 'active.json'
    active = json.loads(index.read_text(encoding='utf-8')) if index.is_file() else {}
    result = []
    for name, version in sorted(active.items()):
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,40}', name) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('能力包索引无效')
        pack = load_pack(root / name / version)
        receipt = json.loads((root / name / version / '.installed.json').read_text(encoding='utf-8'))
        if receipt.get('sha256') != pack['sha256']:
            raise ValueError('已安装能力包内容被修改，请使用新版本重新安装: ' + name)
        if (pack['manifest']['name'], pack['manifest']['version']) != (name, version):
            raise ValueError('能力包索引与清单不一致')
        result.append(pack)
    return result


def install_pack(source):
    pack = load_pack(source)  # metadata only; installing never imports implementation
    manifest = pack['manifest']
    from importlib.resources import files
    builtin = json.loads(files('astra_designer.resources').joinpath('capabilities.json').read_text(encoding='utf-8'))
    reserved = set(builtin) | {'integration.pending@1'}
    if any(cap['id'] in reserved for cap in manifest['capabilities']):
        raise ValueError('能力包不能覆盖内置能力')
    builtin_schemas = json.loads(files('astra_designer.resources').joinpath('data.schemas.json').read_text(encoding='utf-8'))
    known_schemas = set(builtin_schemas) | set(manifest['schemas']) | {'task_input'}
    if set(manifest['schemas']) & (set(builtin_schemas) | {'task_input'}):
        raise ValueError('能力包不能覆盖内置 Schema')
    for cap in manifest['capabilities']:
        if (set(cap['inputs'].values()) | set(cap['outputs'].values())) - known_schemas:
            raise ValueError('能力包端口必须引用内置或本包定义的 Schema')
    root = store_root()
    target = root / manifest['name'] / manifest['version']
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        receipt = json.loads((target / '.installed.json').read_text(encoding='utf-8'))
        if load_pack(target)['sha256'] != pack['sha256'] or receipt.get('sha256') != pack['sha256']:
            raise ValueError('同版本能力包内容不同，请提高版本号；不会覆盖')
    else:
        with tempfile.TemporaryDirectory(dir=target.parent, prefix='.install-') as temporary:
            staging = Path(temporary) / 'package'
            staging.mkdir()
            for name, content in pack['files'].items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding='utf-8', newline='\n')
            (staging / '.installed.json').write_text(json.dumps({'sha256': pack['sha256']}), encoding='utf-8')
            staging.rename(target)
    index = root / 'active.json'
    active = json.loads(index.read_text(encoding='utf-8')) if index.exists() else {}
    active[manifest['name']] = manifest['version']
    temporary = index.with_suffix('.tmp')
    temporary.write_text(json.dumps(active, indent=2), encoding='utf-8')
    temporary.replace(index)
    return pack_summary(load_pack(target))


def uninstall_pack(name):
    """Remove all installed versions; never touch source or project-vendored copies."""
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,40}', name):
        raise ValueError('请提供能力包名称，不是路径或能力 ID')
    root = store_root()
    target = root / name
    if not target.exists():
        raise ValueError('能力包未安装: ' + name)
    # Check the absolute removal boundary and reject links/junctions before moving anything.
    if target.resolve() != target or not target.is_dir():
        raise ValueError('能力包安装路径异常，拒绝卸载')
    for folder, dirs, files in os.walk(target, followlinks=False):
        for path in [Path(folder), *(Path(folder) / item for item in dirs + files)]:
            if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('能力包目录含符号链接或重解析点，拒绝递归删除')
    index = root / 'active.json'
    active = json.loads(index.read_text(encoding='utf-8')) if index.exists() else {}
    if not isinstance(active, dict):
        raise ValueError('能力包索引无效')
    active.pop(name, None)
    staging = root / ('.uninstall-' + uuid4().hex)
    if not staging.resolve().is_relative_to(root) or not target.resolve().is_relative_to(root):
        raise ValueError('卸载路径越出能力包安装目录')
    target.rename(staging)
    temporary = root / ('.active-' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(active, indent=2), encoding='utf-8')
        temporary.replace(index)
    except OSError:
        staging.rename(target)
        temporary.unlink(missing_ok=True)
        raise
    result = {'name': name, 'status': 'uninstalled', 'scope': 'all_installed_versions',
              'generated_projects': 'unchanged'}
    try:
        shutil.rmtree(staging)
    except OSError as exc:
        result['cleanup_warning'] = f'已从目录取消登记，但部分文件未删除：{staging}；{exc}'
    return result


def pack_summary(pack):
    manifest = pack['manifest']
    return {'name': manifest['name'], 'version': manifest['version'], 'sha256': pack['sha256'],
            'verification': 'metadata_syntax_and_dependencies_only',
            'permissions': manifest['permissions'], 'capabilities': [
                {'id': cap['id'], **readiness(cap)} for cap in manifest['capabilities']]}
