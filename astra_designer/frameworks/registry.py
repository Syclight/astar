"""Closed, versioned framework contracts. Metadata never imports third-party code."""
import json
from importlib.metadata import PackageNotFoundError, version

from astra_designer.catalog.registry import capability_for, resource_text
from astra_designer.contracts.requirements import digest


def contracts():
    return json.loads(resource_text('frameworks.json'))


def describe():
    result = {}
    for identifier, contract in contracts().items():
        dependencies = {}
        for package in contract['dependencies']:
            try:
                dependencies[package] = version(package)
            except PackageNotFoundError:
                dependencies[package] = None
        result[identifier] = {**contract, 'installed_dependencies': dependencies,
                              'sha256': digest(contract)}
    return result


def check_choice(identifier):
    if identifier == 'auto':
        return
    entry = contracts().get(identifier)
    if entry is None:
        raise ValueError('未知框架或版本: ' + str(identifier))
    if entry['status'] != 'generatable':
        raise ValueError(f"{identifier} 当前不能自动生成：{'；'.join(entry['limitations'])}")


def resolve(blueprint):
    config = blueprint.get('execution', {})
    default = config.get('default_framework', 'auto')
    overrides = config.get('agents', {})
    check_choice(default)
    agents = [a for stage in blueprint['stages'] for a in stage['agents']]
    if set(overrides) - {a['id'] for a in agents}:
        raise ValueError('框架覆盖引用了未知 Agent')
    entries, assignments = contracts(), {}
    for agent in agents:
        capability = capability_for(agent)
        if capability is None:
            raise ValueError('无法为未知能力选择框架')
        llm = capability['base'] in {'llm.transform@1', 'llm.map@1'}
        requested = overrides.get(agent['id'], default if llm else 'auto')
        check_choice(requested)
        chosen = ('astra.configured@1' if llm else 'astra.native@1') if requested == 'auto' else requested
        needed = 'structured_output' if llm else 'deterministic_transform'
        entry = entries[chosen]
        if needed not in entry['supports']:
            raise ValueError(f"Agent {agent['id']} 需要 {needed}，框架 {chosen} 不支持")
        assignments[agent['id']] = {'framework': chosen, 'contract_sha256': digest(entry),
                                    'reason': '自动匹配能力与已实现生成器' if requested == 'auto' else '用户显式指定'}
    return {'version': 1, 'agents': assignments,
            'contracts': {identifier: entries[identifier] for identifier in sorted({a['framework'] for a in assignments.values()})}}
