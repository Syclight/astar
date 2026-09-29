import json
import keyword
import re
import urllib.parse
from pathlib import Path, PureWindowsPath
import sys

from jsonschema import Draft202012Validator

from astra_designer.catalog.registry import capability_for, resource_text, schemas
from astra_core.llm.contracts import check_schema
from jsonschema.exceptions import SchemaError
from astra_designer.contracts.validation import Issue, ValidationResult


FILE_FORMATS = ('txt', 'md', 'docx', 'pdf', 'csv', 'xlsx')
WINDOWS_RESERVED = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}


def source_path(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or PureWindowsPath(value).drive or "\\" in value:
        raise ValueError("输入路径必须是蓝图目录内的相对路径，使用 / 分隔")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("输入路径不能越出蓝图目录")
    if not resolved.is_file():
        raise ValueError(f"输入文件不存在: {value}")
    return resolved


def validate(value: dict, root: Path) -> ValidationResult:
    result = ValidationResult()
    def issue(path, code, message):
        result.issues.append(Issue(path, code, message))
    schema = json.loads(resource_text("blueprint.schema.json"))
    for error in Draft202012Validator(schema).iter_errors(value):
        issue("/" + "/".join(map(str, error.absolute_path)), "schema", error.message)
    if not result.valid:
        return result

    from astra_core.task_contract import validate_contract
    try:
        validate_contract(value.get('task_input'), value.get('interaction'))
    except (ValueError, SchemaError) as exc:
        issue('/task_input', 'contract', str(exc))

    name = value["project"]["name"]
    if keyword.iskeyword(name) or name in sys.stdlib_module_names or name.startswith("astra") or name in WINDOWS_RESERVED or name in {'yaml', 'jsonschema'}:
        issue("/project/name", "package", "项目名不能是 Python 关键字、标准库或 Astra 保留名称")
    data_schemas = schemas()
    if 'task_input' in value:
        data_schemas['task_input'] = value['task_input']
    for identifier, custom in value.get('schemas', {}).items():
        if identifier in data_schemas or identifier in WINDOWS_RESERVED:
            issue('/schemas/' + identifier, 'conflict', '自定义 Schema 不能覆盖内置或保留名称')
            continue
        try:
            check_schema(custom)
        except (ValueError, SchemaError) as exc:
            issue('/schemas/' + identifier, 'schema', str(exc))
            continue
        data_schemas[identifier] = custom
    if not result.valid:
        return result
    for key, item in value["inputs"].items():
        if key in WINDOWS_RESERVED:
            issue(f"/inputs/{key}", "path", "输入名不能使用 Windows 保留文件名")
        if item["schema"] not in data_schemas:
            issue(f"/inputs/{key}/schema", "reference", "未知输入 Schema")
            continue
        try:
            data = json.loads(source_path(root, item["path"]).read_text(encoding="utf-8"))
            json.dumps(data, allow_nan=False)
            for error in Draft202012Validator(data_schemas[item["schema"]]).iter_errors(data):
                issue(f"/inputs/{key}", "input", error.message)
        except (OSError, ValueError) as exc:
            issue(f"/inputs/{key}", "input", str(exc))

    stage_ids, agent_ids, available, placed, producer = set(), set(), {}, [], {}
    produced_at, routes, deferred, file_formats = {}, [], [], {}
    if 'task_input' in value:
        available['task_input'] = 'task_input'
    for index, stage in enumerate(value["stages"]):
        stage_path = f"/stages/{index}"
        if stage["id"] in stage_ids or stage["id"] in {"init", "end"}:
            issue(stage_path + "/id", "duplicate", "阶段名重复或使用了保留名称 init/end")
        stage_ids.add(stage["id"])
        for position, agent in enumerate(stage["agents"]):
            path = f"{stage_path}/agents/{position}"
            if agent["id"] in agent_ids or agent["id"] in WINDOWS_RESERVED:
                issue(path + "/id", "duplicate", "Agent 标识必须在项目内唯一")
            agent_ids.add(agent["id"])
            capability = capability_for(agent)
            if capability is None:
                issue(path + "/capability", "reference", f"未知能力 {agent['capability']}；只能使用 capabilities 中列出的标识")
                continue
            parameter_errors = list(Draft202012Validator(capability["parameters"]).iter_errors(capability['effective_parameters']))
            for error in parameter_errors:
                issue(path + "/parameters", "parameters", error.message)
            if parameter_errors:
                continue
            unknown = set()
            for direction in ('inputs', 'outputs'):
                for port, identifier in capability[direction].items():
                    if identifier not in data_schemas:
                        unknown.add(identifier)
                        issue(path + '/' + direction + '/' + port, 'schema',
                              f'引用了未知 Schema {identifier}；请使用 data_schemas 中的标识或 task_input，或在顶层 schemas 中声明它')
            for problem in capability_issues(capability['base'], capability['effective_parameters'], data_schemas,
                                             value.get('task_input'), value.get('interaction')):
                issue(path + "/parameters", "contract", problem)
            if capability["base"] == "feedback.read@1":
                source = capability["effective_parameters"].get("source")
                if not isinstance(source, str) or source not in value["inputs"]:
                    issue(path + "/parameters/source", "reference", "引用了不存在的输入资源")
                elif value["inputs"][source]["schema"] != "feedback_records":
                    issue(path + "/parameters/source", "contract", "读取能力需要 feedback_records 输入")
            for direction in ("inputs", "outputs"):
                if agent[direction].keys() != capability[direction].keys():
                    issue(path + "/" + direction, "ports",
                          f"{direction} 端口必须是 {list(capability[direction])}，当前为 {list(agent[direction])}"
                          + ("（端口由 parameters 中的 input_schemas/output_schemas 决定）" if capability['base'] in {'llm.transform@1', 'llm.map@1', 'integration.pending@1'} else ''))
            optional = set(capability['effective_parameters'].get('optional_inputs', []))
            for port, key in agent["inputs"].items():
                if key not in available and port in optional:
                    deferred.append((path, index, port, key, capability["inputs"].get(port)))  # checked once loops are known
                elif key not in available:
                    issue(path + "/inputs/" + port, "reference", f"数据 {key} 尚未由上游产生")
                elif available[key] != capability["inputs"].get(port) and capability["inputs"].get(port) not in unknown \
                        and available[key] in data_schemas:  # unknown Schemas are reported once, where declared
                    issue(path + "/inputs/" + port, "contract",
                          f"数据 {key} 的 Schema 是 {available[key]}，但端口 {port} 需要 {capability['inputs'].get(port)}；"
                          "两者必须使用同一个 Schema 标识")
            placed.append((path, agent, capability))
            if capability['base'] != 'object.resolve@1' and 'business_object' in capability['outputs'].values():
                issue(path + '/outputs', 'contract', 'business_object 必须由 object.resolve@1 程序创建，不能由模型生成')
            for port, key in agent["outputs"].items():
                if key in {"run", "task", "selection", "task_input"} or key.startswith("hook_"):
                    issue(path + "/outputs/" + port, "conflict",
                          f"数据键 {key} 是运行时保留名（run、task、selection、task_input、hook_*），请改用其他名称")
                elif key in producer:
                    issue(path + "/outputs/" + port, "conflict",
                          f"数据键 {key} 已由 {producer[key]}产出，每个数据键只能由一个 Agent 产出。"
                          f"若 {agent['id']} 只是沿用该值，删除这个输出端口，下游直接读取 {key}；"
                          f"若要输出修改后的值，改用新的数据键")
                producer.setdefault(key, f"{agent['id']}（{path}）")
                produced_at.setdefault(key, index)
                available[key] = capability["outputs"].get(port)
                if capability['base'] in {'document.write@1', 'table.write@1'}:
                    file_formats[key] = set(capability['effective_parameters'].get('formats', []))
                elif capability['base'] in IMAGE_CAPABILITIES:
                    file_formats[key] = {'image'}
            if capability['base'] == 'flow.route@1':
                routes.append((path, index, position == len(stage['agents']) - 1, capability['effective_parameters']))
    for issue_args in flow_issues(value, routes, deferred, produced_at, available, placed):
        issue(*issue_args)
    for index, check in enumerate(value["acceptance"]):
        if check['op'] == 'matches_schema':
            try:
                check_schema(check['value'])
            except (ValueError, SchemaError) as exc:
                issue(f'/acceptance/{index}/value', 'schema', str(exc))
        parts = check["path"].split("/")
        if parts[1] == "data" and (len(parts) < 3 or parts[2] not in available):
            issue(f"/acceptance/{index}/path", "reference",
                  f"验收路径 {check['path']} 中的数据键 {parts[2] if len(parts) > 2 else '（缺失）'} 不是任何 Agent 的输出")
        if check["op"] == "artifact_exists" and not (
                (len(parts) == 3 and available.get(parts[2]) == "report_path")
                or (len(parts) == 4 and available.get(parts[2]) in {"document_files", "document_file_lists"} and parts[3] in FILE_FORMATS
                    and parts[3] in file_formats.get(parts[2], FILE_FORMATS))
                or (len(parts) == 4 and available.get(parts[2]) == "image_files" and parts[3] == "image")):
            written = '、'.join(sorted(file_formats.get(parts[2], ()))) if len(parts) > 2 else ''
            issue(f"/acceptance/{index}", "contract",
                  "artifact_exists 必须指向文件路径输出：/data/<报告路径键>，或 /data/<文件输出键>/<该 Agent 写出的格式>"
                  "（图片为 /data/<图片输出键>/image）"
                  + (f"；{parts[2]} 只写出 {written}" if written else ''))
    if result.valid:
        for issue_args in redundancy_issues(value, placed):
            issue(*issue_args)
    # Checked after the structure so one round reports every problem, not just the first.
    from astra_designer.discovery.validation import validate_trace
    try:
        validate_trace(value)
    except ValueError as exc:
        issue('/traceability', 'requirements', str(exc))
    if result.valid:
        from astra_designer.frameworks.registry import resolve
        try:
            resolve(value)
        except ValueError as exc:
            issue('/execution', 'framework', str(exc))
    return result


EXPRESSION_FUNCTIONS = {'round', 'abs', 'min', 'max', 'int', 'float', 'str', 'len', 'col', 'concat', 'if_empty',
                        'left', 'right', 'replace', 'trim', 'date', 'year', 'month', 'year_month', 'days_between'}


def check_expression(expr):
    """Same rules as the generated TableComputeAgent: arithmetic, comparisons, conditionals and a few functions."""
    import ast
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.USub, ast.UAdd, ast.Constant, ast.Name, ast.Load, ast.Call,
               ast.IfExp, ast.Compare, ast.BoolOp, ast.And, ast.Or, ast.Not, ast.Add, ast.Sub, ast.Mult, ast.Div,
               ast.FloorDiv, ast.Mod, ast.Pow, ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE)
    for node in ast.walk(ast.parse(expr, mode='eval')):
        if not isinstance(node, allowed):
            raise ValueError(f'不支持 {type(node).__name__}')
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in EXPRESSION_FUNCTIONS):
            raise ValueError(f"只能调用 {'、'.join(sorted(EXPRESSION_FUNCTIONS))}")


def schema_at(schema, pointer):
    """The sub-schema a JSON pointer reaches, following properties and array items; None if unknown."""
    target = schema
    for token in (pointer or '').split('/')[1:]:
        token = token.replace('~1', '/').replace('~0', '~')
        if isinstance(target, dict) and token in target.get('properties', {}):
            target = target['properties'][token]
        elif isinstance(target, dict) and token.isdigit() and isinstance(target.get('items'), dict):
            target = target['items']
        else:
            return None
    return target if isinstance(target, dict) else None


def has_type(schema, kind):
    value = (schema or {}).get('type')
    return value == kind or (isinstance(value, list) and kind in value)


def installed(module):
    from importlib.util import find_spec
    return find_spec(module) is not None


def required_pointer(schema, pointer):
    """Prove that every segment exists, including parent objects and indices."""
    if pointer and not pointer.startswith('/'):
        return False
    target = schema
    for raw in (pointer or '').split('/')[1:]:
        token = raw.replace('~1', '/').replace('~0', '~')
        if not isinstance(target, dict):
            return False
        if target.get('type') == 'object' and token in target.get('required', []):
            target = target.get('properties', {}).get(token)
        elif target.get('type') == 'array' and token.isdigit() and str(int(token)) == token:
            index = int(token)
            if target.get('minItems', 0) <= index:
                return False
            prefix = target.get('prefixItems', [])
            target = prefix[index] if index < len(prefix) else target.get('items')
        else:
            return False
    return isinstance(target, dict)


def required_path_issues(base, parameters, data_schemas):
    # Only unconditional reads. key_path writes an ID; routing may test absence.
    specs = {
        'state.save@1': [('value_schema', 'append_path')],
        'document.write@1': [('input_schema', 'text_path'), ('input_schema', 'title_path')],
        'table.compute@1': [('input_schema', 'rows_path'), ('lookup_schema', 'lookup_path')],
        'http.request@1': [('body_schema', 'each_path')],
    }.get(base, [])
    if base == 'table.write@1':
        for sheet in parameters.get('sheets') or [parameters]:
            yield from required_path_issues('table.compute@1', {
                'input_schema': parameters.get('input_schema'), 'rows_path': sheet.get('rows_path')}, data_schemas)
    for schema_key, path_key in specs:
        pointer = parameters.get(path_key)
        schema = data_schemas.get(parameters.get(schema_key))
        if pointer and schema is not None and not required_pointer(schema, pointer):
            yield (f'{path_key} {pointer} 是下游执行必需的路径，但 Schema {parameters[schema_key]} '
                   '不能保证它存在；请在上游输出 Schema 中将沿途对象字段逐层加入 required，'
                   '数组下标需有足够的 minItems，或改用保证存在的路径。仅在提示词中要求输出不够')


def filename_issues(parameters, data_schemas, task_input):
    """{field} must be a task_input field and {/pointer} a text or number in the written data."""
    problems, schema = [], data_schemas.get(parameters.get('input_schema'))
    where = f"Schema {parameters.get('input_schema')} 中"
    if parameters.get('per_item') and schema is not None:
        # One file per item: {/field} reads the item, e.g. {/chapter_number} of each chapter.
        items = schema_at(schema, parameters.get('text_path'))
        schema = items.get('items') if isinstance(items, dict) and isinstance(items.get('items'), dict) else None
        where = f"Schema {parameters.get('input_schema')} 的 {parameters.get('text_path') or ''} 数组元素中"
    for token in re.findall(r'\{([^{}]*)\}', parameters.get('filename', '')):
        if token.startswith('/'):
            if parameters.get('accumulate'):
                problems.append(f'accumulate 的文稿每次都写到同一个文件，filename 不能用 {{{token}}} 这类每次不同的内容；'
                                '可用 {task_input 字段}（如书名）')
                continue
            target = schema_at(schema, token) if schema is not None else None
            if schema is not None and not any(has_type(target, kind) for kind in ('string', 'integer', 'number')):
                problems.append(f"filename 中的 {{{token}}} 在 {where}没有指向文字或数字字段；"
                                '文件名里的序号、标题等要放进被写入的数据中')
        elif re.fullmatch(r'[a-z][a-z0-9_]*', token):
            if token not in task_fields(task_input):
                problems.append(f'filename 中的 {{{token}}} 不是 task_input 的字段；来自上游数据时写成 {{/字段路径}}')
            elif token not in (task_input or {}).get('required', []):
                problems.append(f'filename 引用了非必填的 task_input.{token}，某些运行中可能为空；'
                                '对象归档请用 object.resolve@1 和 object_context=true，文件名使用固定名称')
        else:
            problems.append(f'filename 中的 {{{token}}} 无效：只能写 {{task_input 字段名}} 或 {{/被写入数据中的路径}}')
    return problems


def external_key_issues(parameters, task_input):
    field = parameters.get('key_field')
    problems = []
    if parameters.get('key_namespace') and not field:
        problems.append('key_namespace 必须与 key_field 一起使用')
    if field:
        spec = task_fields(task_input).get(field)
        if not any(has_type(spec, kind) for kind in ('string', 'integer')):
            problems.append('key_field 必须指向 task_input 中的字符串或整数编号')
        if field not in (task_input or {}).get('required', []):
            problems.append('key_field 是已有业务编号，必须在 task_input.required 中；新建时没有编号请用 object.resolve 的 mode_field/id_field 模式')
        if parameters.get('object_context'):
            problems.append('key_field 与 object_context 不能同时使用，请只保留一个身份来源')
    return problems


def image_issues(base, parameters, data_schemas, task_input):
    """image.generate@1 / image.edit@1: one prompt source, the pictures to edit, and file names that can be filled."""
    problems, fields = [], task_fields(task_input)
    sources = [key for key in ('prompt', 'prompt_field', 'input_schema') if parameters.get(key)]
    if len(sources) != 1:
        problems.append('提示词来源只能设一个：固定模板 prompt、每次输入 prompt_field，或上游数据 input_schema（配 prompt_path）；'
                        '通常由上游 llm.transform 按需求写好英文提示词')
    if parameters.get('prompt_path') and not parameters.get('input_schema'):
        problems.append('prompt_path 指向 content 端口的数据，需同时设置 input_schema')
    for token in re.findall(r'\{([^{}]*)\}', parameters.get('prompt', '')):
        if not re.fullmatch(r'[a-z][a-z0-9_]*', token):
            problems.append(f'prompt 中的 {{{token}}} 无效：固定模板只能引用 {{task_input 字段}}；由上游生成提示词请用 input_schema')
        elif token not in fields:
            problems.append(f'prompt 中的 {{{token}}} 不是 task_input 的字段')
    for name in ('prompt_field', 'image_field'):
        spec = fields.get(parameters.get(name))
        if parameters.get(name) and not (has_type(spec, 'string') or (has_type(spec, 'array') and has_type((spec or {}).get('items'), 'string'))):
            problems.append(f'{name} {parameters[name]} 必须是 task_input 中的 string 或 string 数组字段')
    item = None  # the Schema of one prompt object, when prompts are a list of objects
    schema = data_schemas.get(parameters.get('input_schema'))
    if schema is not None:
        target = schema_at(schema, parameters.get('prompt_path'))
        items = (target or {}).get('items') if has_type(target, 'array') else None
        if not (has_type(target, 'string') or has_type(target, 'array')):
            problems.append(f"prompt_path {parameters.get('prompt_path') or '（空）'} 在 Schema {parameters['input_schema']} 中"
                            '不是提示词（string、string 数组或对象数组）')
        elif isinstance(items, dict) and has_type(items, 'object'):
            item = items
            for key in ('prompt_key', 'aspect_key'):
                name = parameters.get(key, 'prompt' if key == 'prompt_key' else None)
                if name and name not in (items.get('properties') or {}):
                    problems.append(f"{key} {name} 不是 {parameters['input_schema']}{parameters.get('prompt_path') or ''} 数组元素的字段")
    for key in ('prompt_key', 'aspect_key'):
        if parameters.get(key) and item is None and schema is not None:
            problems.append(f'{key} 只用于 prompt_path 指向对象数组的情况')
    for token in re.findall(r'\{([^{}]*)\}', parameters.get('filename', '')):
        if token.startswith('/'):
            target = schema_at(item, token) if item is not None else None
            if not any(has_type(target, kind) for kind in ('string', 'integer', 'number')):
                problems.append(f'filename 中的 {{{token}}} 要指向提示词对象中的文字或数字字段（提示词须来自上游对象数组，'
                                '如每章一项、带 chapter_number）')
        elif not re.fullmatch(r'[a-z][a-z0-9_]*', token) or token not in fields:
            problems.append(f'filename 中的 {{{token}}} 不是 task_input 的字段')
    if base == 'image.edit@1' and bool(parameters.get('image_field')) == bool(parameters.get('images_schema')):
        problems.append('要编辑的图片只能设一个来源：每次提交的图片用 image_field，上游生成的图片用 images_schema=image_files')
    return problems


def reset_issues(parameters, task_input):
    problems = []
    if parameters.get('reset_field') and parameters['reset_field'] not in task_fields(task_input):
        problems.append(f"reset_field {parameters['reset_field']} 不是 task_input 的字段")
    if bool(parameters.get('reset_field')) != bool(parameters.get('reset_values')):
        problems.append('reset_field 与 reset_values 必须同时设置，例如 mode 为 new 时从头开始')
    if parameters.get('reset_field') and not parameters.get('accumulate'):
        problems.append('document.write@1 的 reset_field 只用于 accumulate')
    return problems


def task_fields(task_input):
    return ((task_input or {}).get('properties') or {})


# Must stay identical to root_problem() in resources/feedback_agents.py.tpl (tests compare them).
WINDOWS_SYSTEM = {'windows', 'program files', 'program files (x86)', 'programdata', '$recycle.bin',
                  'system volume information', 'recovery', 'boot', 'perflogs'}
POSIX_SYSTEM = {'usr', 'etc', 'bin', 'sbin', 'lib', 'lib32', 'lib64', 'boot', 'proc', 'sys', 'dev', 'opt', 'var',
                'system', 'library', 'applications', 'private', 'cores', 'snap', 'srv'}
POSIX_TEMP = {('var', 'tmp'), ('private', 'tmp'), ('private', 'var', 'folders')}


def root_problem(value):
    """Why a folder may not be scanned or cleaned, or None. Kept identical to the designer's design-time check."""
    import ntpath
    import posixpath
    import os
    import re
    raw = str(value).strip().strip('"')
    if raw.startswith('~'):
        raw = os.path.expanduser(raw)
    windows = bool(re.match(r'^[A-Za-z]:[\\/]', raw)) or raw.startswith('\\\\')
    flavour = ntpath if windows else posixpath
    if not flavour.isabs(raw):
        return '必须是绝对路径'
    parts = [part.casefold() for part in re.split(r'[\\/]+', flavour.splitdrive(flavour.normpath(raw))[1]) if part]
    if not parts:
        return '不能是磁盘根目录'
    first = parts[0]
    if windows:
        if first in WINDOWS_SYSTEM:
            return '不能是系统目录'
        if first == 'users' and len(parts) <= 2:
            return '不能是用户目录本身，请指定其中具体的文件夹'
        if first == 'users' and len(parts) >= 3 and parts[2] == 'appdata' and (
                len(parts) <= 4 or parts[3:5] == ['roaming', 'microsoft']):
            return '不能是 AppData 本身或系统配置目录，请指定如 AppData\\Local\\Temp 这样的具体缓存目录'
        return None
    if first in POSIX_SYSTEM and not any(tuple(parts[:len(item)]) == item for item in POSIX_TEMP):
        return '不能是系统目录'
    if first in {'home', 'users'} and len(parts) <= 2 or parts == ['root']:
        return '不能是用户目录本身，请指定其中具体的文件夹'
    return None



def root_problems(value, label):
    """A cleanup or scan root must be a specific folder, never a drive root, user folder or system directory."""
    problem = root_problem(value)
    return [f'{label} {problem}：{value}'] if problem else []


def capability_issues(base, parameters, data_schemas, task_input, interaction=None):
    """Parameter checks a JSON Schema cannot express, per capability."""
    import re
    problems = external_key_issues(parameters, task_input) if base in {'object.resolve@1', 'state.load@1', 'state.save@1', 'document.write@1'} else []
    if base in {'llm.transform@1', 'llm.map@1'}:
        extra = set(parameters.get('optional_inputs', [])) - set(parameters.get('input_schemas', {}))
        if extra:
            problems.append(f"optional_inputs 中的 {'、'.join(sorted(extra))} 不是 input_schemas 的端口")
        if base == 'llm.map@1' and parameters.get('items_port') in parameters.get('optional_inputs', []):
            problems.append('items_port 是逐项处理的数组，第一次运行就必须存在，不能列入 optional_inputs')
    if base in {'document.write@1', 'table.write@1'}:
        problems += filename_issues(parameters, data_schemas, task_input)
    if base == 'document.write@1':
        schema = data_schemas.get(parameters.get('input_schema'))
        if schema is not None:
            target = schema_at(schema, parameters.get('text_path'))
            if has_type(target, 'array'):
                items = target.get('items') if isinstance(target.get('items'), dict) else {}
                if has_type(items, 'object'):
                    key = parameters.get('section_text_key')
                    if not key or not has_type(schema_at(items, '/' + key), 'string'):
                        problems.append('text_path 指向对象数组时，section_text_key 必须是每项中的正文 string 字段；'
                                        '可选 section_title_key 作为每节标题')
                elif not has_type(items, 'string'):
                    problems.append('text_path 指向的数组元素必须是 string，或带正文字段的对象')
            elif not has_type(target, 'string'):
                problems.append(f"text_path {parameters.get('text_path') or '（空）'} 在 Schema {parameters['input_schema']} 中"
                                "没有指向 string 字段；请指向正文文本字段，例如 /full_text")
            if parameters.get('title_path') and not has_type(schema_at(schema, parameters['title_path']), 'string'):
                problems.append(f"title_path {parameters['title_path']} 在 Schema {parameters['input_schema']} 中没有指向 string 字段；"
                                '没有标题字段时省略 title_path')
        field = parameters.get('format_field')
        if field and field not in task_fields(task_input):
            problems.append(f'format_field {field} 不是 task_input 的字段；没有每次选择格式的字段时省略 format_field，只用 formats')
        if parameters.get('per_item'):
            if parameters.get('accumulate'):
                problems.append('per_item（每项一个文件）与 accumulate（累积成一份文稿）不能同时使用；需要两者时用两个 document.write@1')
            if schema is not None and not has_type(schema_at(schema, parameters.get('text_path')), 'array'):
                problems.append('per_item 需要 text_path 指向数组（如各章）')
            if parameters.get('title_path'):
                problems.append('per_item 时每个文件的标题取 section_title_key，不用 title_path')
        problems += reset_issues(parameters, task_input)
        if 'pdf' in parameters.get('formats', []) and not installed('reportlab'):
            problems.append('导出 PDF 需要 reportlab，请先安装（pip install reportlab）')
    elif base == 'table.write@1':
        schema = data_schemas.get(parameters.get('input_schema'))
        sheets = parameters.get('sheets') or [{'name': '', 'rows_path': parameters.get('rows_path')}]
        if parameters.get('sheets') and parameters.get('formats') != ['xlsx']:
            problems.append('sheets（多个工作表）只能写成 xlsx：formats 设为 ["xlsx"]')
        for sheet in sheets if schema is not None else []:
            target = schema_at(schema, sheet.get('rows_path'))
            if not (has_type(target, 'array') and has_type(target.get('items') if isinstance(target.get('items'), dict) else {}, 'object')):
                problems.append(f"{('工作表 ' + sheet['name'] + ' 的 ') if sheet['name'] else ''}rows_path "
                                f"{sheet.get('rows_path') or '（空）'} 在 Schema {parameters['input_schema']} 中"
                                '没有指向对象数组；表格的每一行必须是一个对象')
        if 'xlsx' in parameters.get('formats', []) and not installed('openpyxl'):
            problems.append('导出 Excel 需要 openpyxl，请先安装（pip install openpyxl）')
    elif base == 'table.read@1':
        field = parameters.get('path_field')
        spec = task_fields(task_input).get(field)
        if not (has_type(spec, 'string') or (has_type(spec, 'array') and has_type((spec or {}).get('items'), 'string'))):
            problems.append(f'path_field {field} 必须是 task_input 中的 string 字段（一个表格）或 string 数组字段（多个同样结构的表格）')
    elif base == 'http.request@1':
        missing = [name for name in re.findall(r'\{([a-z][a-z0-9_]*)\}', parameters.get('url', ''))
                   if name not in task_fields(task_input)]
        if missing:
            problems.append(f"url 占位符 {'、'.join(missing)} 不是 task_input 的字段")
        secret_headers = [key for key, value in parameters.get('headers', {}).items()
                          if key.lower() in {'authorization', 'x-api-key', 'api-key', 'cookie', 'x-auth-token', 'token'}
                          and not re.fullmatch(r'(?:[A-Za-z-]+ )?\{env:[A-Z][A-Z0-9_]*\}', value)]
        if secret_headers:
            problems.append(f"请求头 {'、'.join(secret_headers)} 的值不能直接写密钥；写成 {{env:环境变量名}}，"
                            '或用 auth_env 设置 Authorization')
        query = urllib.parse.parse_qsl(urllib.parse.urlsplit(parameters.get('url', '')).query, keep_blank_values=True)
        leaked = [name for name, value in query
                  if re.fullmatch(r'(?i)(access_?)?(key|token|secret|sign(ature)?|api_?key|password)', name)
                  and not re.fullmatch(r'\{env:[A-Z][A-Z0-9_]*\}', value)]
        if leaked:
            problems.append(f"url 中的 {'、'.join(leaked)} 是密钥，不能写进项目文件；写成 {{env:环境变量名}}，"
                            '例如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={env:WECOM_BOT_KEY}')
        if parameters.get('auth_scheme') and not parameters.get('auth_env'):
            problems.append('设置 auth_scheme 时必须同时设置 auth_env')
        item_tokens = re.findall(r'\{(/[^{}]*)\}', parameters.get('url', ''))
        body = data_schemas.get(parameters.get('body_schema'))
        if 'each_path' in parameters:
            items = schema_at(body, parameters['each_path']) if body is not None else None
            if not parameters.get('body_schema'):
                problems.append('each_path 需要 body_schema：要逐条发送的列表来自 body 端口')
            elif body is not None and not has_type(items, 'array'):
                problems.append(f"each_path {parameters['each_path'] or '（空）'} 在 Schema {parameters['body_schema']} 中不是数组")
            else:
                element = (items or {}).get('items') if isinstance(items, dict) else None
                for token in item_tokens:
                    if isinstance(element, dict) and schema_at(element, token) is None:
                        problems.append(f'url 中的 {{{token}}} 在每个条目中不存在')
        elif item_tokens:
            problems.append(f"url 中的 {{{item_tokens[0]}}} 只能与 each_path 一起使用（取当前条目的字段）")
        if parameters.get('page_param'):
            if parameters.get('method') != 'GET' or parameters.get('each_path') is not None:
                problems.append('page_param 分页只用于单个 GET 请求')
            if 'items_path' not in parameters:
                problems.append('page_param 需要 items_path：每页响应中条目数组的位置（整个响应就是数组时写空字符串）')
        if parameters.get('body_format') == 'form' and not parameters.get('body_schema'):
            problems.append('body_format=form 需要 body_schema 声明表单内容')
    elif base == 'fs.scan@1':
        if bool(parameters.get('roots')) == bool(parameters.get('roots_field')):
            problems.append('roots 与 roots_field 必须且只能设置一个：固定目录用 roots，每次运行指定的目录用 roots_field')
        elif parameters.get('roots_field') and not has_type(task_fields(task_input).get(parameters['roots_field']), 'string') \
                and not has_type(task_fields(task_input).get(parameters['roots_field']), 'array'):
            problems.append(f"roots_field {parameters['roots_field']} 必须是 task_input 中的 string 或 string 数组字段")
        problems += [message for root in parameters.get('roots', []) for message in root_problems(root, '扫描目录')]
    elif base == 'fs.cleanup@1':
        problems += [message for root in parameters.get('allowed_roots', []) for message in root_problems(root, 'allowed_roots')]
        if not parameters.get('input_schema') and parameters.get('mode') != 'restore':
            problems.append('input_schema 必填：要处理的文件清单所在 files 端口的 Schema（只有 mode=restore 可以省略）')
        if parameters.get('purge_after_days') and parameters.get('mode') not in ('quarantine', 'delete'):
            problems.append('purge_after_days 只用于 quarantine 或 delete 模式')
        if parameters.get('execute_field') and parameters['execute_field'] not in task_fields(task_input):
            problems.append(f"execute_field {parameters['execute_field']} 不是 task_input 的字段")
        if parameters.get('execute_field') and not parameters.get('execute_values'):
            problems.append('设置 execute_field 时必须给出 execute_values，列出表示“执行”的取值')
        schema = data_schemas.get(parameters.get('input_schema'))
        if schema is not None and not has_type(schema_at(schema, parameters.get('rows_path')), 'array'):
            problems.append(f"rows_path {parameters.get('rows_path') or '（空）'} 在 Schema {parameters['input_schema']} 中不是数组")
    elif base == 'object.resolve@1':
        fields = task_fields(task_input)
        if parameters.get('key_field') and any(k in parameters for k in ('mode_field', 'id_field', 'new_value', 'continue_value')):
            problems.append('外部编号 key_field 模式与新建/继续模式不能混用')
        for name in (() if parameters.get('key_field') else ('mode_field', 'id_field')):
            if not has_type(fields.get(parameters.get(name)), 'string'):
                problems.append(f'{name} 必须指向 task_input 中的字符串字段')
        new, continuing = parameters.get('new_value', 'new'), parameters.get('continue_value', 'continue')
        if new == continuing:
            problems.append('新建和继续的模式值必须不同')
        modes = (fields.get(parameters.get('mode_field')) or {}).get('enum', [])
        if modes and (new not in modes or continuing not in modes):
            problems.append('新建和继续的模式值必须包含在 mode_field 的 enum 中')
    elif base in {'state.load@1', 'state.save@1'}:
        fields = task_fields(task_input)
        if parameters.get('key_path') and not parameters.get('object_context'):
            problems.append('key_path 只能在 object_context=true 时回填程序 ID，不能从模型输出生成存储身份')
        if parameters.get('object_context') and any(parameters.get(k) for k in ('key', 'key_field', 'reset_field', 'reset_values')):
            problems.append('object_context 使用 object 输入决定身份和新建状态，不能同时配置 key/key_field/reset_field/reset_values')
        if parameters.get('key') and parameters.get('key_field'):
            problems.append('key 与 key_field 只能二选一：固定键用 key，按本次输入区分（如书名）用 key_field')
        for name in ('key_field', 'reset_field'):
            if parameters.get(name) and parameters[name] not in fields:
                problems.append(f'{name} {parameters[name]} 不是 task_input 的字段')
        if bool(parameters.get('reset_field')) != bool(parameters.get('reset_values')):
            problems.append('reset_field 与 reset_values 必须同时设置，例如 mode 为 new 时从头开始')
        if parameters.get('value_schema') and parameters['value_schema'] not in data_schemas:
            problems.append(f"value_schema {parameters['value_schema']} 不是已知 Schema")
        elif parameters.get('append_path'):
            target = schema_at(data_schemas[parameters['value_schema']], parameters['append_path'])
            if not has_type(target, 'array'):
                problems.append(f"append_path {parameters['append_path']} 在 Schema {parameters['value_schema']} 中不是数组；"
                                '它应指向要逐次累积的列表，如各章摘要')
            elif target.get('type') not in ('array', ['array']):
                problems.append(f"append_path {parameters['append_path']} 必须始终是数组，不能允许 null 或其他类型")
        if parameters.get('max_items') and not parameters.get('append_path'):
            problems.append('max_items 只用于 append_path 的列表')
        if parameters.get('key_path'):
            if parameters.get('key'):
                problems.append('key_path（团队生成的编号）与固定 key 不能同时使用')
            target = schema_at(data_schemas.get(parameters.get('value_schema')) or {}, parameters['key_path'])
            if parameters.get('object_context') and not has_type(target, 'string'):
                problems.append('对象 ID 是字符串，key_path 必须指向字符串字段')
            if parameters.get('value_schema') in data_schemas and not any(has_type(target, kind) for kind in ('string', 'integer')):
                problems.append(f"key_path {parameters['key_path']} 在 Schema {parameters['value_schema']} 中不是编号（string 或 integer）字段")
        if parameters.get('require_found') and not parameters.get('reset_field') and not parameters.get('object_context') and not parameters.get('key_field'):
            problems.append('require_found=true 时要有 reset_field/reset_values 表示“新建”，否则第一次运行就会因为找不到记录而失败')
    elif base == 'integration.pending@1':
        # Pending nodes are for operations the built-in capabilities cannot perform, not for work they can.
        text = ' '.join([str(parameters.get('title', '')), str(parameters.get('description', '')),
                         *map(str, parameters.get('setup', []))])
        if re.search(r'AI|人工智能|大模型|模型|LLM|语义', text) and not re.search(r'图|视频|音频|语音|OCR', text):
            problems.append(f"待接入节点“{parameters.get('title')}”描述的是模型判断或生成，这由 llm.transform@1"
                            '（逐项处理用 llm.map@1）直接实现；待接入节点只用于模型和内置能力做不到的外部操作')
        if re.search(r'本地.{0,6}(JSON|json|文件|存储)|状态.{0,4}(读写|保存|读取|持久|存储)|跨运行', text):
            problems.append(f"待接入节点“{parameters.get('title')}”描述的是跨运行保存状态，这由 state.load@1（开头读取）"
                            '和 state.save@1（末尾保存）直接实现')
        if re.search(r'(导出|生成|写入|输出|保存)[^，。；]{0,8}(txt|TXT|Markdown|Word|docx|PDF|pdf|CSV|csv|Excel|xlsx)', text):
            problems.append(f"待接入节点“{parameters.get('title')}”描述的是写文件，这由 document.write@1"
                            '（txt/md/docx/pdf）或 table.write@1（csv/xlsx）直接实现')
        built_in = [(r'搜索引擎|网络搜索|联网搜索|web ?search', 'web.search@1（网络搜索）'),
                    (r'(发送|发出|寄出|群发)[^，。；]{0,4}邮件|SMTP', 'email.send@1（发送邮件）'),
                    (r'人工审阅|人工审核|审稿|人工确认', 'human.review@1（人工审阅）'),
                    (r'OCR|文字识别|扫描件识别', 'document.read@1（扫描件与图片自动文字识别）'),
                    (r'(生成|绘制|画|制作)[^，。；]{0,6}(图片|图像|插图|配图|海报|主图|头像|图标|logo)|文生图|图生图|'
                     r'AI ?(绘图|绘画|作图|画图)|绘图模型|图像生成|改图|修图|换背景|qwen-?image', 'image.generate@1 或 image.edit@1（绘图、改图）')]
        external = re.search(r'OA|审批系统|审批流|钉钉|飞书|企业微信|收件箱|IMAP|POP3|读取邮件', text, re.I)
        # Beyond the image model: layered source files, vector artwork, video, screenshots and plain image
        # processing, delivering pictures elsewhere, or a third-party drawing service the user named.
        beyond = re.search(r'PSD|分层|源文件|矢量|SVG|视频|动画|3D|排版|截图|压缩|缩略|转格式|格式转换|裁剪|'
                           r'(发送|推送|发布|上传|同步)(到|至|给)|Midjourney|DALL|Stable ?Diffusion|即梦|通义万相|可灵|文心一格|'
                           r'Ideogram|Flux|豆包', text, re.I)
        for pattern, capability in built_in:
            # Reading a mailbox or driving an OA approval is a real external system, not these built-ins.
            if capability.startswith('image') and (beyond or external):
                continue
            if re.search(pattern, text, re.I) and not (external and capability.startswith(('human', 'email'))):
                problems.append(f"待接入节点“{parameters.get('title')}”描述的能力已内置，改用 {capability}")
    elif base == 'document.read@1':
        spec = task_fields(task_input).get(parameters.get('path_field'))
        if not (has_type(spec, 'string') or (has_type(spec, 'array') and has_type((spec or {}).get('items'), 'string'))):
            problems.append(f"path_field {parameters.get('path_field')} 必须是 task_input 中的 string 字段（一个文档路径）"
                            '或 string 数组字段（多个文档）')
    elif base == 'web.fetch@1':
        sources = [key for key in ('url', 'urls', 'url_field') if parameters.get(key)]
        spec = task_fields(task_input).get(parameters.get('url_field'))
        if len(sources) != 1:
            problems.append('url、urls、url_field 必须且只能设置一个：固定网页用 url（多个用 urls），每次运行提供的网址用 url_field')
        elif parameters.get('urls') and not parameters.get('many'):
            problems.append('urls 是多个网页，需同时设 many=true')
        elif parameters.get('url_field') and parameters.get('many'):
            if not (has_type(spec, 'array') and has_type((spec or {}).get('items'), 'string')) and not has_type(spec, 'string'):
                problems.append(f"url_field {parameters['url_field']} 必须是 task_input 中的 string 数组字段（多个网址）")
        elif parameters.get('url_field') and not has_type(spec, 'string'):
            problems.append(f"url_field {parameters['url_field']} 必须是 task_input 中的 string 字段；多个网址时设 many=true 并用 string 数组字段")
    elif base == 'table.compute@1':
        schema = data_schemas.get(parameters.get('input_schema'))
        if schema is not None and not has_type(schema_at(schema, parameters.get('rows_path')), 'array'):
            problems.append(f"rows_path {parameters.get('rows_path') or '（空）'} 在 Schema {parameters['input_schema']} 中不是数组")
        steps = parameters.get('steps', [])
        if any(step.get('op') in ('join', 'append') for step in steps) and not parameters.get('lookup_schema'):
            problems.append('join、append 步骤需要 lookup_schema（另一张表所在 lookup 端口的 Schema），可选 lookup_path')
        for number, step in enumerate(steps, 1):
            if step.get('op') == 'derive':
                try:
                    check_expression(step['expr'])
                except (SyntaxError, ValueError) as exc:
                    problems.append(f'第 {number} 步 derive 的表达式无效：{exc}')
            if step.get('op') == 'group':
                for aggregate in step.get('aggregates', []):
                    if aggregate.get('fn') != 'count' and not aggregate.get('column'):
                        problems.append(f"第 {number} 步 group 的 {aggregate.get('fn')} 需要 column")
            if step.get('op') == 'window' and step.get('fn') != 'row_number' and not step.get('column'):
                problems.append(f"第 {number} 步 window 的 {step.get('fn')} 需要 column")
    elif base == 'web.search@1':
        sources = [key for key in ('query', 'query_field', 'queries_schema') if parameters.get(key)]
        if len(sources) != 1:
            problems.append('搜索词来源只能设一个：固定模板 query、每次输入 query_field，或上游数据 queries_schema（配 queries_path）')
        spec = task_fields(task_input).get(parameters.get('query_field'))
        if parameters.get('query_field') and not (has_type(spec, 'string') or has_type(spec, 'array')):
            problems.append(f"query_field {parameters['query_field']} 必须是 task_input 中的 string 或 string 数组字段")
        for token in re.findall(r'\{([^{}]*)\}', parameters.get('query', '')):
            if not re.fullmatch(r'[a-z][a-z0-9_]*', token):
                problems.append(f'query 中的 {{{token}}} 无效：固定模板只能引用 {{task_input 字段}}；由上游生成搜索词请用 queries_schema')
            elif token not in task_fields(task_input):
                problems.append(f'query 中的 {{{token}}} 不是 task_input 的字段')
        schema = data_schemas.get(parameters.get('queries_schema'))
        if schema is not None:
            target = schema_at(schema, parameters.get('queries_path'))
            if not (has_type(target, 'string') or has_type(target, 'array')):
                problems.append(f"queries_path {parameters.get('queries_path') or '（空）'} 在 Schema {parameters['queries_schema']} 中"
                                '不是搜索词（string 或 string 数组）')
    elif base == 'email.send@1':
        fields = task_fields(task_input)
        if not parameters.get('to') and not parameters.get('to_field'):
            problems.append('需要收件人：固定地址写 to，每次运行填写的写 to_field')
        for name in ('to_field', 'cc_field'):
            if parameters.get(name) and not (has_type(fields.get(parameters[name]), 'string') or has_type(fields.get(parameters[name]), 'array')):
                problems.append(f'{name} {parameters[name]} 必须是 task_input 中的 string（可用逗号分隔多个地址）或 string 数组字段')
        for token in re.findall(r'\{([a-z][a-z0-9_]*)\}', parameters.get('subject', '')):
            if token not in fields:
                problems.append(f'subject 中的 {{{token}}} 不是 task_input 的字段')
        schema = data_schemas.get(parameters.get('input_schema'))
        if schema is not None and not has_type(schema_at(schema, parameters.get('text_path')), 'string'):
            problems.append(f"text_path {parameters.get('text_path') or '（空）'} 在 Schema {parameters['input_schema']} 中没有指向正文文字")
    elif base == 'human.review@1':
        schema = data_schemas.get(parameters.get('input_schema'))
        for name in ('text_path', 'images_path'):
            if schema is not None and parameters.get(name) and schema_at(schema, parameters[name]) is None:
                problems.append(f"{name} {parameters[name]} 在 Schema {parameters['input_schema']} 中不存在")
    elif base in {'image.generate@1', 'image.edit@1'}:
        problems += image_issues(base, parameters, data_schemas, task_input)
    elif base == 'flow.route@1':
        schema = data_schemas.get(parameters.get('subject_schema'))
        if schema is not None and parameters.get('field') and schema_at(schema, parameters['field']) is None:
            problems.append(f"field {parameters['field']} 在 Schema {parameters['subject_schema']} 中不存在")
        for case in parameters.get('cases', []):
            for condition in [case['when']] if 'when' in case else case.get('all', []) + case.get('any', []):
                given = [key for key in ('value', 'value_path', 'value_field') if key in condition]
                if condition['op'] not in {'is_true', 'is_false', 'empty', 'not_empty'} and len(given) != 1:
                    problems.append(f"条件 {condition['op']} 需要 value、value_path（结果中的另一字段）或 value_field"
                                    '（task_input 字段）中的一个')
                for key in ('field', 'value_path'):
                    if schema is not None and condition.get(key) and schema_at(schema, condition[key]) is None:
                        problems.append(f"{key} {condition[key]} 在 Schema {parameters['subject_schema']} 中不存在")
                if condition.get('value_field') and condition['value_field'] not in task_fields(task_input):
                    problems.append(f"value_field {condition['value_field']} 不是 task_input 的字段")
    elif base == 'llm.map@1':
        inputs, outputs = parameters.get('input_schemas', {}), parameters.get('output_schemas', {})
        port = parameters.get('items_port')
        if port not in inputs:
            problems.append(f'items_port {port} 必须是 input_schemas 中的端口')
        elif data_schemas.get(inputs[port]) is not None and not has_type(
                schema_at(data_schemas[inputs[port]], parameters.get('items_path')), 'array'):
            problems.append(f"items_port {port}{parameters.get('items_path') or ''} 在 Schema {inputs[port]} 中不是数组")
        reserved = {'item', 'position', 'previous_results'} & set(inputs)
        if reserved:
            problems.append(f"输入端口不能命名为 {'、'.join(sorted(reserved))}（逐项处理时由程序使用）")
        for identifier in outputs.values():
            schema = data_schemas.get(identifier)
            if schema is not None and not (has_type(schema, 'array') and isinstance(schema.get('items'), dict)):
                problems.append(f'输出 Schema {identifier} 必须是带 items 的数组，items 是单项结果的结构')
    problems.extend(required_path_issues(base, parameters, data_schemas))
    return problems


def flow_issues(value, routes, deferred, produced_at, available, placed):
    """Jumps between stages: redo loops go back, skips go forward, and both must keep the data flow valid."""
    stages = [stage['id'] for stage in value['stages']]
    loops = []
    for path, index, last, parameters in routes:
        if not last:
            yield (path, 'flow', 'flow.route@1 必须是所在阶段的最后一个 Agent，跳转在阶段结束时生效')
        for case in parameters.get('cases', []):
            target = case.get('goto')
            if target not in stages:
                yield (path + '/parameters', 'flow', f"跳转目标 {target} 不是已有阶段；可用：{'、'.join(stages)}")
                continue
            goal = stages.index(target)
            if goal == index:
                yield (path + '/parameters', 'flow', f'不能跳转到自身所在阶段 {target}；重做时跳到产出被检查结果的阶段')
            elif goal < index:
                loops.append((goal, index))
            else:
                skipped = {key for key, at in produced_at.items() if index < at < goal}
                later = {key for _, agent, capability in placed if stages.index(agent_stage(value, agent)) >= goal
                         for port, key in agent['inputs'].items()
                         if port not in capability['effective_parameters'].get('optional_inputs', [])}
                accepted = {check['path'].split('/')[2] for check in value['acceptance']
                            if check['path'].startswith('/data/') and len(check['path'].split('/')) > 2}
                broken = sorted(skipped & (later | accepted))
                if broken:
                    yield (path + '/parameters', 'flow', f"跳到 {target} 会跳过产出 {'、'.join(broken)} 的阶段，"
                                                         '而后续步骤或验收仍需要它们')
    stage_of = {id(agent): position for position, stage in enumerate(value['stages']) for agent in stage['agents']}
    from astra_designer.discovery.validation import acts
    for start, end in loops:
        for path, agent, capability in placed:
            base = capability['base']
            inside = start <= stage_of.get(id(agent), -1) <= end
            if inside and (base in {'state.save@1', 'email.send@1'} or (base in {'fs.cleanup@1', 'http.request@1'}
                                                     and acts(base, capability['effective_parameters']))):
                yield (path, 'flow', f"{agent['id']}（{base}）位于重做循环 {value['stages'][start]['id']}–"
                                     f"{value['stages'][end]['id']} 内，每次返工都会重复保存或执行外部动作；"
                                     '把它移到循环之后，等检查通过再执行')
    for path, index, port, key, expected in deferred:
        at = produced_at.get(key)
        if at is None:
            yield (path + '/inputs/' + port, 'reference', f'数据 {key} 没有任何 Agent 产出')
        elif not any(start <= index and end >= at for start, end in loops):
            yield (path + '/inputs/' + port, 'flow', f'optional_inputs 的 {port} 读取后面产出的 {key}，'
                                                     '只有在跳回本阶段或更早阶段的重做循环（flow.route@1）中才有意义')
        elif expected != available.get(key):
            yield (path + '/inputs/' + port, 'contract', f'数据 {key} 的 Schema 是 {available.get(key)}，但端口 {port} 需要 {expected}')
    routed = {agent['inputs'].get('subject') for _, agent, capability in placed if capability['base'] == 'flow.route@1'}
    for path, agent, capability in placed:
        if capability['base'] == 'human.review@1' and not set(agent['outputs'].values()) & routed:
            yield (path, 'flow', f"{agent['id']}（human.review@1）的结果要交给 flow.route@1：field=/approved 为 false 时"
                                 '跳回返工（或 on_exhausted=fail 停止），否则审阅不通过也会照常往下执行')
    loads = [(agent, capability) for _, agent, capability in placed if capability['base'] == 'state.load@1']
    saves = [(agent, capability) for _, agent, capability in placed if capability['base'] == 'state.save@1']
    yield from object_lifecycle_issues(placed)
    def key_of(agent, capability):
        parameters = capability['effective_parameters']  # recipe defaults count
        if parameters.get('object_context'):
            return ('object', agent['inputs'].get('object'))
        if parameters.get('key_field'):
            return ('external', parameters.get('key_namespace') or parameters['key_field'], parameters['key_field'])
        return (parameters.get('key') or ('default' if not parameters.get('key_field') and not parameters.get('key_path') else None),
                parameters.get('key_field'))
    for agent, capability in loads:
        if not any(key_of(save_agent, save) == key_of(agent, capability) for save_agent, save in saves):
            yield (f"/stages/{stages.index(agent_stage(value, agent))}", 'flow',
                   f"{agent['id']} 读取了状态，但没有使用相同 key/key_field 的 state.save@1 在末尾保存更新后的状态，"
                   '下次运行将接不上')


def object_lifecycle_issues(placed):
    """Check producer/consumer lifecycle contracts, including recipe defaults.

    External identifiers can register objects without a saved state. Requiring
    state downstream then makes that creation path unusable. Program-owned
    new/continue identities differ: StateLoad explicitly permits a new object.
    """
    resolvers = {key: (agent, capability['effective_parameters'])
                 for _, agent, capability in placed if capability['base'] == 'object.resolve@1'
                 for key in agent['outputs'].values()}
    for path, agent, capability in placed:
        parameters = capability['effective_parameters']
        if capability['base'] != 'state.load@1' or not parameters.get('object_context') \
                or not parameters.get('require_found'):
            continue
        resolved = resolvers.get(agent['inputs'].get('object'))
        if resolved is None:
            continue  # Port/schema validation reports missing or incompatible producers.
        producer, identity = resolved
        if identity.get('key_field') and not identity.get('require_found'):
            yield (path + '/parameters', 'flow',
                   f"{producer['id']} 按外部编号自动登记新对象，但 {agent['id']} 要求已有存档（require_found=true），"
                   '首次使用该编号会失败。需要程序新建/继续对象时，使用 object.resolve 的 mode_field/id_field；'
                   '需要按已有外部编号首次初始化状态时，设 state.load 的 require_found=false；'
                   '仅允许已有对象时，给 object.resolve 也设置 require_found=true。不要仅为通过校验改变业务语义')


def agent_stage(value, agent):
    return next(stage['id'] for stage in value['stages'] if any(item is agent for item in stage['agents']))


# Ports whose Schema is only a parameter describing the data they receive: the producer's Schema is the truth.
TYPED_BY_PARAMETER = {'document.write@1': ('content', 'input_schema'), 'table.write@1': ('rows', 'input_schema'),
                      'table.compute@1': ('rows', 'input_schema'), 'fs.cleanup@1': ('files', 'input_schema'),
                      'flow.route@1': ('subject', 'subject_schema'), 'state.save@1': ('value', 'value_schema'),
                      'human.review@1': ('content', 'input_schema'), 'email.send@1': ('content', 'input_schema'),
                      'web.search@1': ('topic', 'queries_schema'), 'image.generate@1': ('content', 'input_schema'),
                      'image.edit@1': ('content', 'input_schema')}
IMAGE_CAPABILITIES = {'image.generate@1', 'image.edit@1'}
TEXT_FIELDS = ('full_text', 'text', 'body', 'content', 'markdown')
DYNAMIC_PORTS = {'llm.transform@1', 'llm.map@1', 'integration.pending@1'}


def normalize_wiring(value):
    """Mechanical wiring the program can derive is fixed here instead of costing a repair round.

    Only unambiguous cases: the brief port that *_field parameters imply, a Schema parameter that must equal
    its producer's Schema, readers of state.load@1 (always stored_state), optional_inputs that name a data key
    instead of a port, a misspelled single output port, and the reserved stage names init/end.
    """
    stages = value.get('stages')
    if not isinstance(stages, list) or not all(isinstance(s, dict) and isinstance(s.get('agents'), list) for s in stages):
        return []
    agents = [agent for stage in stages for agent in stage['agents']
              if isinstance(agent, dict) and isinstance(agent.get('inputs'), dict) and isinstance(agent.get('outputs'), dict)
              and isinstance(agent.get('parameters', {}), dict)]
    changes = []
    taken = {stage.get('id') for stage in stages}
    renamed = {}
    for stage in stages:
        if stage.get('id') in {'init', 'end'}:
            new = f"{stage['id']}_stage"
            while new in taken:
                new += '_'
            renamed[stage['id']], stage['id'] = new, new
            taken.add(new)
            changes.append(f"阶段 {next(k for k, v in renamed.items() if v == new)} → {new}（init/end 是保留名）")
    for agent in agents:
        for case in agent.get('parameters', {}).get('cases', []) if isinstance(agent.get('parameters', {}).get('cases'), list) else []:
            if isinstance(case, dict) and case.get('goto') in renamed:
                case['goto'] = renamed[case['goto']]
    for targets in (value.get('traceability') or {}).values() if isinstance(value.get('traceability'), dict) else []:
        for index, target in enumerate(targets if isinstance(targets, list) else []):
            for old, new in renamed.items():
                if isinstance(target, str) and (target == f'/stages/{old}' or target.startswith(f'/stages/{old}/')):
                    targets[index] = f'/stages/{new}' + target[len(f'/stages/{old}'):]

    def spec(agent):
        try:
            return capability_for(agent)
        except (AttributeError, TypeError, KeyError, ValueError):
            return None

    def rename_single(ports, expected, label):
        if len(expected) == 1 and len(ports) == 1 and set(ports) != set(expected):
            (old, key), = ports.items()
            ports.clear()
            ports[next(iter(expected))] = key
            changes.append(f"{label}: 端口 {old} → {next(iter(expected))}")

    schema_of, base_of = ({'task_input': 'task_input'} if 'task_input' in value else {}), {}
    for agent in agents:
        found = spec(agent)
        if not found:
            continue
        if found['base'] not in DYNAMIC_PORTS:
            rename_single(agent['outputs'], found['outputs'], f"{agent.get('id')} outputs")
        for port, key in agent['outputs'].items():
            if isinstance(key, str) and isinstance(found['outputs'].get(port), str):
                schema_of.setdefault(key, found['outputs'][port])
                base_of.setdefault(key, found['base'])
    for agent in agents:
        found = spec(agent)
        if not found:
            continue
        base, parameters, inputs, name = found['base'], agent.setdefault('parameters', {}), agent['inputs'], agent.get('id')
        if base in DYNAMIC_PORTS and isinstance(parameters.get('input_schemas'), dict):
            declared = parameters['input_schemas']
            for port, key in inputs.items():
                wanted = 'stored_state' if base_of.get(key) == 'state.load@1' else None
                if wanted and declared.get(port) != wanted:
                    changes.append(f"{name}.{port}: 读取 state.load@1 的输出，Schema {declared.get(port)} → stored_state")
                    declared[port] = wanted
                elif port not in declared and schema_of.get(key):
                    declared[port] = schema_of[key]
                    changes.append(f"{name}.input_schemas 补充 {port}: {schema_of[key]}")
            optional = parameters.get('optional_inputs')
            for index, entry in enumerate(optional if isinstance(optional, list) else []):
                if entry in declared:
                    continue
                bound = [port for port, key in inputs.items() if key == entry]
                if bound:
                    optional[index] = bound[0]
                    changes.append(f"{name}.optional_inputs: {entry} 是数据键，改为读取它的端口 {bound[0]}")
                elif schema_of.get(entry) and entry not in inputs:
                    inputs[entry], declared[entry] = entry, schema_of[entry]
                    changes.append(f"{name}: 为 optional_inputs 的 {entry} 补充同名端口（Schema {schema_of[entry]}）")
        if base in TYPED_BY_PARAMETER:
            port, parameter = TYPED_BY_PARAMETER[base]
            data_ports = {p: k for p, k in inputs.items() if p != 'brief'}
            if port not in inputs and len(data_ports) == 1:
                (old, key), = data_ports.items()
                inputs[port] = inputs.pop(old)
                changes.append(f"{name}.inputs: 端口 {old} → {port}")
            producer = schema_of.get(inputs.get(port))
            if producer and parameters.get(parameter) != producer:
                changes.append(f"{name}.{parameter}: {parameters.get(parameter)} → {producer}（与上游数据一致）")
                parameters[parameter] = producer
        expected = (spec(agent) or {}).get('inputs', {})
        if 'brief' in expected and 'brief' not in inputs:
            inputs['brief'] = 'task_input'
            changes.append(f"{name}.inputs 补充 brief: task_input（参数引用了 task_input 字段）")
        elif 'brief' in inputs and 'brief' not in expected and base not in DYNAMIC_PORTS:
            del inputs['brief']
            changes.append(f"{name}.inputs 删除多余的 brief")
    known = {**schemas(), **(value.get('schemas') if isinstance(value.get('schemas'), dict) else {})}
    writers = {}
    for agent in agents:
        found = spec(agent)
        if not found:
            continue
        parameters = agent['parameters']
        if found['base'] == 'document.write@1' and isinstance(known.get(parameters.get('input_schema')), dict):
            schema = known[parameters['input_schema']]
            target = schema_at(schema, parameters.get('text_path'))
            if not (has_type(target, 'string') or has_type(target, 'array')):
                strings = [key for key, field in (schema.get('properties') or {}).items()
                           if isinstance(field, dict) and has_type(field, 'string')]
                preferred = [key for key in strings if key in TEXT_FIELDS]
                choice = strings if len(strings) == 1 else preferred
                if len(choice) == 1:
                    parameters['text_path'] = '/' + choice[0]
                    changes.append(f"{agent.get('id')}.text_path → /{choice[0]}（{parameters['input_schema']} 中的正文字段）")
        if found['base'] in {'document.write@1', 'table.write@1'} and isinstance(parameters.get('formats'), list) \
                and not parameters.get('format_field'):
            for key in agent['outputs'].values():
                writers[key] = [item for item in parameters['formats'] if isinstance(item, str)]
        if found['base'] in IMAGE_CAPABILITIES:
            for key in agent['outputs'].values():
                writers[key] = ['image']  # artifact_exists /data/<key> → /data/<key>/image
    # state.load must expect what state.save writes under the same key.
    saves = [agent for agent in agents if (spec(agent) or {}).get('base') == 'state.save@1']
    for load in [agent for agent in agents if (spec(agent) or {}).get('base') == 'state.load@1']:
        def state_binding(agent):
            params = spec(agent)['effective_parameters']
            if params.get('object_context'):
                return ('object', agent['inputs'].get('object'))
            if params.get('key_field'):
                return ('external', params.get('key_namespace') or params['key_field'], params['key_field'])
            return ('legacy', params.get('key'), params.get('key_field'))
        same = [save for save in saves if state_binding(save) == state_binding(load)]
        if len(same) == 1 and same[0]['parameters'].get('value_schema') != load['parameters'].get('value_schema'):
            load['parameters']['value_schema'] = same[0]['parameters'].get('value_schema')
            changes.append(f"{load.get('id')}.value_schema → {load['parameters']['value_schema']}（与 state.save@1 一致）")
        # “Start a new book” must also start the appended history and the whole-book manuscript afresh.
        reset = {key: load['parameters'][key] for key in ('reset_field', 'reset_values') if key in load['parameters']}
        if len(reset) == 2:
            followers = [save for save in same if save['parameters'].get('append_path')]
            followers += [agent for agent in agents if (spec(agent) or {}).get('base') == 'document.write@1'
                          and agent['parameters'].get('accumulate')]
            for agent in followers:
                if not agent['parameters'].get('reset_field'):
                    agent['parameters'].update(json.loads(json.dumps(reset)))
                    agent['inputs'].setdefault('brief', 'task_input')
                    changes.append(f"{agent.get('id')} 沿用 {load.get('id')} 的 reset_field（重新开始时一并清空）")
    acceptance = value.get('acceptance') if isinstance(value.get('acceptance'), list) else []
    for check in list(acceptance):
        path = check.get('path') if isinstance(check, dict) else None
        key = path[len('/data/'):] if isinstance(path, str) and path.startswith('/data/') else None
        if check.get('op') == 'artifact_exists' and key in writers and writers[key]:
            first, *rest = writers[key]
            check['path'] = f'/data/{key}/{first}'
            acceptance.extend({'path': f'/data/{key}/{item}', 'op': 'artifact_exists'} for item in rest)
            changes.append(f"acceptance: {path} → 按写出的格式逐一检查 {'、'.join(writers[key])}")
    return changes


def normalize_references(value):
    """Fix traceability paths whose correct form is unambiguous.

    '/stages/writing' names exactly one stage, so converting it to an index is
    safer than spending a model request on a repair. Returns what was changed.
    """
    import re
    wiring = normalize_wiring(value)
    stages, trace = value.get('stages'), value.get('traceability')
    if not isinstance(stages, list):
        return wiring
    trace = trace if isinstance(trace, dict) else {}
    stage_index = {stage.get('id'): index for index, stage in enumerate(stages) if isinstance(stage, dict)}
    changes = wiring
    for requirement, targets in trace.items():
        if not isinstance(targets, list):
            continue
        for position, target in enumerate(targets):
            match = re.fullmatch(r'/stages/([^/]+)(?:/agents/([^/]+))?(/.*)?', target) if isinstance(target, str) else None
            if not match:
                continue
            stage, agent, rest = match.groups()
            index = int(stage) if stage.isdigit() else stage_index.get(stage)
            if index is None or index >= len(stages):
                continue
            new = f'/stages/{index}'
            if agent is not None:
                agents = [a.get('id') for a in stages[index].get('agents', []) if isinstance(a, dict)]
                position_in_stage = int(agent) if agent.isdigit() else (agents.index(agent) if agent in agents else None)
                if position_in_stage is None:
                    continue
                new += f'/agents/{position_in_stage}'
            new += rest or ''
            if new != target:
                targets[position] = new
                changes.append(f'{requirement}: {target} → {new}')
    # Acceptance must name the data key (right side of outputs), not the port
    # (left side). A port name that maps to exactly one data key is unambiguous.
    ports, keys = {}, set()
    for stage in stages:
        for agent in (stage.get('agents', []) if isinstance(stage, dict) else []):
            if isinstance(agent, dict) and isinstance(agent.get('outputs'), dict):
                for port, key in agent['outputs'].items():
                    ports.setdefault(port, set()).add(key)
                    keys.add(key)
    for index, check in enumerate(value.get('acceptance') or []):
        path = check.get('path') if isinstance(check, dict) else None
        match = re.fullmatch(r'/data/([^/]+)(/.*)?', path) if isinstance(path, str) else None
        if match and match.group(1) not in keys and len(ports.get(match.group(1), ())) == 1:
            new = f"/data/{next(iter(ports[match.group(1)]))}{match.group(2) or ''}"
            check['path'] = new
            changes.append(f'acceptance[{index}]: {path} → {new}（端口名改为数据键）')
    # With no fixed input resources, every confirmed input requirement is by
    # construction part of the injected task_input contract.
    items = ((value.get('requirements') or {}).get('document') or {}).get('items', [])
    if 'task_input' in value and not value.get('inputs'):
        for item in items:
            targets = trace.get(item.get('id'))
            if (item.get('category') == 'input' and isinstance(targets, list)
                    and not any(isinstance(t, str) and t.startswith(('/task_input', '/inputs/')) for t in targets)):
                targets.append('/task_input')
                changes.append(f"{item['id']}: 补充 /task_input")
    return changes


def redundancy_issues(value, placed):
    """Occam's razor: every declared entity must be necessary for the result.

    A serial workflow has no loops, so data that no later Agent reads and no
    acceptance check inspects is never used; the Agent producing only such data
    (e.g. a reviewer whose comments nobody applies) adds cost without effect.
    """
    consumed = {key for _, agent, _ in placed for key in agent['inputs'].values()}
    every = placed
    # A route's effect is the jump and state.save's is the file for the next run, not data for this run.
    placed = [item for item in placed if item[2]['base'] not in {'flow.route@1', 'state.save@1'}]
    accepted = {check['path'].split('/')[2] for check in value['acceptance']
                if check['path'].startswith('/data/') and len(check['path'].split('/')) > 2}
    used = consumed | accepted
    seen = {}
    for path, agent, capability in placed:
        outputs = agent['outputs']
        if outputs and not set(outputs.values()) & used:
            yield (path, 'redundant', f"Agent {agent['id']} 的输出 {sorted(outputs.values())} 既未被后续 Agent 使用，也未被验收检查；"
                                      '请删除该 Agent，或让其成果进入下游/验收')
        signature = json.dumps([agent['capability'], agent['inputs'], capability['effective_parameters']],
                               ensure_ascii=False, sort_keys=True)
        if signature in seen:
            yield (path, 'redundant', f"Agent {agent['id']} 与 {seen[signature]} 的能力、输入和参数完全相同，属于重复计算")
        seen.setdefault(signature, agent['id'])
    referenced = {identifier for _, _, capability in every
                  for direction in ('inputs', 'outputs') for identifier in capability[direction].values()}
    referenced |= {item['schema'] for item in value['inputs'].values()}
    for identifier in value.get('schemas', {}):
        if identifier not in referenced:
            yield (f'/schemas/{identifier}', 'redundant', '自定义 Schema 未被任何端口或输入引用，请删除')
    if 'task_input' in value and placed and 'task_input' not in consumed:
        yield ('/task_input', 'unused', '已确认的任务输入 task_input 未被任何 Agent 读取；请把它绑定到需要这些资料的 Agent 输入')
