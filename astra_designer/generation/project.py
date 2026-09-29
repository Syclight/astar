import hashlib
import json
from pathlib import Path

import yaml

from astra_designer.catalog.registry import resource_text, schemas, capability_for, catalog_lock
from astra_designer.generation.workflow import compile_workflow
from astra_designer.generation.writer import write_project
from astra_designer.validation.static import source_path
from astra_core.llm.project_config import validate_profile


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n'


def yaml_text(value) -> str:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=True)


STUB = """\"\"\"待接入：{title}

{description}

接入步骤：
{steps}

完成后：
1. 在下面的 call 方法中调用实际服务，返回 {{输出端口: 数据}}，数据须符合 OUTPUT_SCHEMAS（运行时会检查）。
2. 把 configs/workflow.yaml 中 role 为 {agent} 的 class 改为 {package}.agents.integrations.{agent}.Integration。
3. 运行 astra resume project.yaml <运行ID>，已暂停的任务从这一步继续，前面的步骤不会重做。
\"\"\"
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from astra import BaseAgent

INPUTS = {inputs}  # 输入端口 -> 数据键
OUTPUTS = {outputs}  # 输出端口 -> 数据键
INPUT_SCHEMAS = {input_schemas}
OUTPUT_SCHEMAS = {output_schemas}


class Integration(BaseAgent):
    def __init__(self, name, **_):
        super().__init__(name)
        self.schemas = Path(__file__).resolve().parents[2] / 'schemas'

    def check(self, identifier, value):
        schema = json.loads((self.schemas / f'{{identifier}}.json').read_text(encoding='utf-8'))
        Draft202012Validator(schema).validate(value)

    def run(self, state):
        values = {{port: state['data'].get(key) for port, key in INPUTS.items()}}
        result = self.call(values)
        for port, identifier in OUTPUT_SCHEMAS.items():
            self.check(identifier, result[port])
        return {{'status': 'success', 'data': {{OUTPUTS[port]: result[port] for port in OUTPUTS}}}}

    def call(self, values):
        \"\"\"values 是 {{输入端口: 数据}}；在这里调用实际服务，返回 {{输出端口: 数据}}。\"\"\"
        raise NotImplementedError('尚未实现：{title}')
"""


def integration_stub(item, package):
    """A file to fill in: ports, data keys and Schemas are already wired; only the service call is missing."""
    literal = lambda value: json.dumps(value, ensure_ascii=False)
    return STUB.format(title=item['title'], description=item['description'], agent=item['agent'], package=package,
                       steps='\n'.join(f'- {step}' for step in item['setup']),
                       inputs=literal(item['inputs']), outputs=literal(item['outputs']),
                       input_schemas=literal(item['input_schemas']), output_schemas=literal(item['output_schemas']))


def with_path_checks(blueprint):
    """Mark fields that name a file or folder, so a wrong path is asked again before the run starts."""
    schema = json.loads(json.dumps(blueprint['task_input']))
    kinds = {'document.read@1': ('path_field', 'file'), 'table.read@1': ('path_field', 'file'),
             'fs.scan@1': ('roots_field', 'dir')}
    for stage in blueprint['stages']:
        for agent in stage['agents']:
            spec = capability_for(agent)
            if spec and spec['base'] in kinds:
                parameter, kind = kinds[spec['base']]
                field = spec['effective_parameters'].get(parameter)
                if field in schema.get('properties', {}):
                    schema['properties'][field]['x-astra-path'] = kind
    return schema


def generate(blueprint: dict, root: Path, destination: Path, *, model_config=None) -> Path:
    from astra_designer.frameworks.registry import resolve
    framework_lock = resolve(blueprint)
    name = blueprint['project']['name']
    if destination.name != name:
        raise ValueError(f'目标目录名必须与项目名一致: {name}')
    project = {
        'name': name, 'package': name, 'workflow_config': 'configs/workflow.yaml',
        'tools_dir': 'tools', 'hooks_dir': 'hooks',
        'model_config': 'configs/model.json',
        'extensions': ['astra_core.extensions.project_tools', 'astra_core.extensions.project_hooks',
                       'astra_core.extensions.workflow_artifacts'],
        'paths': {'data': 'data', 'prompts': 'prompts', 'output': 'output'},
        'initial_task': blueprint['project']['goal'],
        'distribution': {'version': '0.1.0', 'runtime': '>=0.1.0',
                         'include': ['agents', 'configs', 'tools', 'hooks', 'prompts', 'schemas', 'data', 'tests', 'capability_packs']},
    }
    # Rewrite resource paths so the saved blueprint can be recompiled after relocation.
    for key in ('task_input', 'interaction'):
        if key in blueprint:
            project[key] = blueprint[key]
    if 'task_input' in blueprint:
        project['task_input'] = with_path_checks(blueprint)
    portable = json.loads(json.dumps(blueprint))
    portable['execution'] = {'default_framework': 'auto', 'agents': {key: value['framework'] for key, value in framework_lock['agents'].items()}}
    for key in portable['inputs']:
        portable['inputs'][key]['path'] = f'data/{key}.json'
    contents = {
        'configs/model.json': json_text(validate_profile({} if model_config is None else model_config)),
        'frameworks.lock.json': json_text(framework_lock),
        'project.yaml': yaml_text(project),
        'blueprint.yaml': yaml_text(portable),
        'configs/workflow.yaml': yaml_text(compile_workflow(blueprint)),
        'tests/acceptance.yaml': yaml_text(blueprint['acceptance']),
        'agents/implementation.py': resource_text('feedback_agents.py.tpl'),
        '__init__.py': '', 'agents/__init__.py': '', 'configs/__init__.py': '',
        'tools/__init__.py': '', 'hooks/__init__.py': '',
        'data/README.md': '# 固定资源\n\n每次任务资料保存在各自运行记录中，不写入此目录。\n',
        'prompts/README.md': '# 提示词\n\n本项目没有配置型 LLM Agent，无模型提示词。\n',
        'README.md': f'# {name}\n\n{blueprint["project"]["goal"]}\n\n'
                     '由 Astra Designer 生成，全部由确定性 Agent 组成，不调用模型。\n\n'
                     '使用 `astra run project.yaml` 运行，加 `--verbose` 查看详细日志；验收规则位于 tests/acceptance.yaml。\n'
                     '修改蓝图后应生成到新的同名项目目录，生成器不覆盖已有文件。\n',
        'pyproject.toml': f'''[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "astra-business-{name.replace('_', '-')}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["astra-runtime>=0.1.0", "jsonschema>=4.18,<5"]

[tool.setuptools]
packages = ["{name}", "{name}.agents", "{name}.configs", "{name}.tools", "{name}.hooks"]

[tool.setuptools.package-dir]
"{name}" = "."

[tool.setuptools.package-data]
"{name}" = ["*.yaml", "*.json", "*.md", "configs/*.yaml", "configs/*.json", "schemas/*.json", "data/*.json", "tests/*.yaml", "prompts/*.md"]
''',
    }
    for key, resource in blueprint['inputs'].items():
        contents[f'data/{key}.json'] = json_text(json.loads(source_path(root, resource['path']).read_text(encoding='utf-8')))
    llm_agents = {}
    for stage in blueprint['stages']:
        for agent in stage['agents']:
            spec = capability_for(agent)
            if spec['base'] not in {'llm.transform@1', 'llm.map@1'}:
                continue
            params = spec['effective_parameters']
            prompt_file = f"prompts/{agent['id']}.md"
            contents[prompt_file] = params['prompt'] + '\n'
            llm_agents[agent['id']] = {'prompt': prompt_file, 'inputs': agent['inputs'], 'outputs': agent['outputs'],
                                      'input_schemas': params['input_schemas'], 'output_schemas': params['output_schemas'],
                                      'max_input_chars': params.get('max_input_chars', 50000)}
            if params.get('optional_inputs'):
                llm_agents[agent['id']]['optional_inputs'] = params['optional_inputs']
            if params.get('max_output_tokens'):
                llm_agents[agent['id']]['max_output_tokens'] = params['max_output_tokens']
            if spec['base'] == 'llm.map@1':
                llm_agents[agent['id']].update(mode='map', items_port=params['items_port'],
                                               items_path=params.get('items_path', ''),
                                               previous_results=params.get('previous_results', 0))
    if llm_agents:
        contents['configs/agents.yaml'] = yaml_text(llm_agents)
        contents['prompts/README.md'] = '# 提示词\n\n每个配置型 LLM Agent 使用独立提示词文件；配置见 configs/agents.yaml。\n'
        contents['README.md'] = contents['README.md'].replace('由 Astra Designer 生成，全部由确定性 Agent 组成，不调用模型。',
            '由 Astra Designer 生成，包含配置型 LLM Agent。运行会将声明的输入发送到所配置的模型服务；请先配置模型。')
    for identifier, schema in {**schemas(), **blueprint.get('schemas', {})}.items():
        contents[f'schemas/{identifier}.json'] = json_text(schema)
    if 'task_input' in blueprint:
        contents['tools/workflow_interaction.py'] = 'from astra_core.tools.workflow_interaction import register_tools\nregister_tools()\n'
        contents['schemas/task_input.json'] = json_text(blueprint['task_input'])
        contents['README.md'] += ('\n本项目每次运行独立收集任务资料，不会改写项目配置。\n'
            '`astra run project.yaml` 逐项收集并确认；也可使用 '
            '`astra run project.yaml --input task.json`。\n'
            '中断后使用 `astra resume project.yaml <运行ID>` 继续。\n')
    contents['capabilities.lock.json'] = json_text(catalog_lock(blueprint))
    from astra_core.capability_packs import load_pack
    pack_lock = {}
    for stage in blueprint['stages']:
        for agent in stage['agents']:
            spec = capability_for(agent)
            if spec.get('source') != 'pack':
                continue
            info = spec['pack']
            pack = load_pack(info['root'])
            if pack['sha256'] != info['sha256']:
                raise ValueError('能力包在设计过程中发生变更，请重新生成')
            folder = f"capability_packs/{info['name']}/{info['version']}"
            for name, content in pack['files'].items():
                contents[f'{folder}/{name}'] = content
            pack_lock[info['name']] = {k: info[k] for k in ('version', 'sha256', 'permissions')}
    if pack_lock:
        contents['capability-packs.lock.json'] = json_text({'format_version': 1, 'packs': pack_lock})
        contents['README.md'] += '\n本项目包含已锁定版本与哈希的能力包副本；包是受信任的 Python 扩展，权限声明不构成操作系统沙箱。\n'
        for stage in blueprint['stages']:
            for agent in stage['agents']:
                spec = capability_for(agent)
                if spec.get('source') == 'pack':
                    missing = spec['availability']['missing']
                    contents['README.md'] += f"- {agent['capability']}：" + ('待配置：' + '、'.join(missing) if missing else '声明依赖齐备，仍需实际运行验证') + '\n'
    from astra_designer.catalog.pending import dependencies
    pending = dependencies(blueprint)
    if pending:
        contents['integrations.json'] = json_text({'status': 'pending_integration', 'dependencies': pending})
        contents['agents/integrations/__init__.py'] = ''
        for item in pending:
            contents[f"agents/integrations/{item['agent']}.py"] = integration_stub(item, name)
        contents['README.md'] = (f'# {name}\n\n{blueprint["project"]["goal"]}\n\n'
                                '项目已生成，可先接单；制作能力待接入，清单见 integrations.json。\n'
                                '使用 astra run project.yaml 运行：待接入步骤之前的工作照常完成，到该步骤时保存任务并暂停，接入后用 astra resume 从这一步继续。\n'
                                '每个待接入步骤在 agents/integrations/ 下有一个实现模板（端口和数据已接好，只需填写服务调用），'
                                '填写后替换 configs/workflow.yaml 中对应节点的 class。\n'
                                '不要仅删除接入清单或占位节点，否则工作流不能完成声明的交付。\n\n') + '\n'.join(
            f"- {item['agent']}：{item['title']}。{'；'.join(item['setup'])}" for item in pending)
    deliverables = blueprint.get('requirements', {}).get('document', {}).get('deliverables', [])
    if deliverables:
        labels = {'txt': 'TXT', 'md': 'Markdown', 'docx': 'Word（docx）', 'pdf': 'PDF', 'csv': 'CSV', 'xlsx': 'Excel（xlsx）', 'image': '图片'}
        contents['README.md'] += '\n## 交付成果\n\n' + '\n'.join(
            f"- {item['name']}（" + {'file': '文件：' + '、'.join(labels.get(f, f) for f in item.get('formats', [])),
                                      'action': '实际执行的动作，验证：' + item.get('evidence', ''),
                                      'data': '运行记录 state.json 中的数据'}[item['form']]
            + f"）：{item['description']}" for item in deliverables) + \
            '\n\n文件保存在 `output/runs/<运行ID>/`，运行结束时会列出路径。\n'
    if blueprint.get('requirements', {}).get('selected_team'):
        contents['team_plan.json'] = json_text({
            'purpose': 'confirmed_team_and_agent_assignment',
            'requirements_revision': blueprint['requirements']['revision'],
            'selected_team': blueprint['requirements']['selected_team'],
            'agent_assignment': blueprint['team_assignment'],
        })
    contents['README.md'] += ('\n## 模型配置\n\n项目使用 `configs/model.json`，由 `project.yaml` 的 `model_config` 引用。'
        'base_url 和 model 可留空，运行到 LLM 节点前请填写。api_key_env 是密钥环境变量名，不保存密钥。'
        '此文件允许在生成后自行修改，修改后重新加载项目；试运行会记录本次配置摘要。'
        'ASTRA_RUNTIME_BASE_URL 与 ASTRA_RUNTIME_MODEL 同时设置时覆盖项目配置。\n')
    contents['generation.json'] = json_text({
        'editable_model_config': 'configs/model.json',
        'generator': 'astra_designer', 'version': '0.1.0', 'blueprint_version': blueprint['version'],
        'blueprint_sha256': hashlib.sha256(contents['blueprint.yaml'].encode('utf-8')).hexdigest(),
        'capabilities': sorted({a['capability'] for s in blueprint['stages'] for a in s['agents']}),
        'files': {path: hashlib.sha256(content.encode('utf-8')).hexdigest() for path, content in sorted(contents.items())},
    })
    return write_project(destination, contents)
