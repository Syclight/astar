"""Execute trusted, project-vendored capability packages behind validated ports."""
import importlib.util
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from astra_core.core.base_agent import BaseAgent
from astra_core.runtime.context import project_directory
from astra_core.runtime.pause import WorkflowPause
from astra_core.capability_packs import load_pack, readiness, safe_file


def checked_pack(root, bundle, sha256, capability):
    path = (Path(root) / bundle).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError('能力包不能越出项目目录')
    pack = load_pack(path)
    if pack['sha256'] != sha256:
        raise ValueError('能力包内容与项目锁定哈希不一致，拒绝执行')
    cap = next((c for c in pack['manifest']['capabilities'] if c['id'] == capability), None)
    if cap is None:
        raise ValueError('锁定的能力包中没有该能力')
    status = readiness(cap)
    if status['missing']:
        raise WorkflowPause('waiting_dependencies', dependency_message=f"能力包 {capability} 待配置：" + '、'.join(status['missing']))
    return pack, cap


def check_project_packs(workflow_path):
    import yaml
    root = project_directory.get()
    workflow = yaml.safe_load(Path(workflow_path).read_text(encoding='utf-8'))
    for agent in workflow.get('agents', []):
        if agent.get('class') == 'astra_core.agents.pack.PackAgent':
            args = agent['kwargs']
            checked_pack(root, args['bundle'], args['sha256'], args['capability'])


class PackAgent(BaseAgent):
    def __init__(self, name, capability, bundle, sha256, inputs, outputs, parameters):
        super().__init__(name)
        self.root = Path(project_directory.get())
        self.capability, self.bundle, self.sha256 = capability, bundle, sha256
        self.inputs, self.outputs, self.parameters = inputs, outputs, parameters

    def run(self, state):
        pack, cap = checked_pack(self.root, self.bundle, self.sha256, self.capability)
        if self.inputs.keys() != cap['inputs'].keys() or self.outputs.keys() != cap['outputs'].keys():
            raise ValueError('能力包端口与锁定契约不一致')
        Draft202012Validator(cap['parameters']).validate(self.parameters)
        def validate(identifier, value):
            schema = json.loads(safe_file(self.root, f'schemas/{identifier}.json').read_text(encoding='utf-8'))
            Draft202012Validator(schema).validate(value)
        values = {port: state['data'][key] for port, key in self.inputs.items()}
        for port, value in values.items():
            validate(cap['inputs'][port], value)
        filename, function = cap['entry'].split(':')
        module_spec = importlib.util.spec_from_file_location('astra_pack_' + pack['sha256'], safe_file(pack['root'], filename))
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        result = getattr(module, function)(values, dict(self.parameters), {
            'package_dir': pack['root'], 'output_dir': state['data']['run']['output_dir'],
            'run_id': state['data']['run']['id']})
        if not isinstance(result, dict) or result.keys() != cap['outputs'].keys():
            raise ValueError('能力包返回值必须恰好包含声明的输出端口')
        for port, value in result.items():
            validate(cap['outputs'][port], value)
        return {'status': 'success', 'data': {self.outputs[port]: value for port, value in result.items()}}
