"""Deterministic compiler adapters; only registered implementations may be emitted."""


def compile_agent(agent, capability, package, framework):
    if capability.get('source') == 'pack':
        pack = capability['pack']
        return {'role': agent['id'], 'class': 'astra_core.agents.pack.PackAgent', 'max_retries': 1,
                'kwargs': {'name': agent['id'], 'capability': agent['capability'],
                    'bundle': f"capability_packs/{pack['name']}/{pack['version']}", 'sha256': pack['sha256'],
                    'inputs': agent['inputs'], 'outputs': agent['outputs'], 'parameters': capability['effective_parameters']}}
    if capability['base'] == 'integration.pending@1':
        return {'role': agent['id'], 'class': 'astra_core.agents.pending.PendingIntegrationAgent',
                'max_retries': 1, 'kwargs': {'name': agent['id'], **capability['effective_parameters'],
                                             'stub': f"agents/integrations/{agent['id']}.py"}}
    if framework == 'astra.configured@1':
        # A model call can time out or be cut off: one retry, and llm.map keeps finished items.
        return {'role': agent['id'], 'class': 'astra_core.agents.configured_llm.ConfiguredLLMAgent',
                'max_retries': 2, 'kwargs': {'name': agent['id'], 'config_file': 'configs/agents.yaml'}}
    if framework == 'astra.native@1':
        return {'role': agent['id'], 'class': f"{package}.agents.implementation.{capability['class']}",
                'max_retries': 1, 'kwargs': {'name': agent['id'], 'inputs': agent['inputs'], 'outputs': agent['outputs'],
                'parameters': capability['effective_parameters'], 'input_schemas': capability['inputs'],
                'output_schemas': capability['outputs']}}
    raise ValueError('框架没有已注册的生成适配器: ' + framework)
