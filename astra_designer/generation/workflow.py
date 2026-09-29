from astra_designer.catalog.registry import capability_for


def compile_workflow(blueprint: dict) -> dict:
    from astra_designer.frameworks.registry import resolve
    from astra_designer.frameworks.adapters import compile_agent
    selection = resolve(blueprint)
    package = blueprint['project']['name']
    agents, stages = [], []
    for index, stage in enumerate(blueprint['stages']):
        next_stage = blueprint['stages'][index + 1]['id'] if index + 1 < len(blueprint['stages']) else 'end'
        stages.append({'stage_id': stage['id'], 'agents': [a['id'] for a in stage['agents']],
                       'next_stage': next_stage, 'flow': {'type': 'serial'}})
        for agent in stage['agents']:
            spec = capability_for(agent)
            compiled = compile_agent(agent, spec, package, selection['agents'][agent['id']]['framework'])
            if spec['base'] == 'flow.route@1':
                # The router must know which targets go back (redo, limited) and which skip ahead.
                compiled['kwargs']['parameters'] = {**compiled['kwargs']['parameters'],
                                                    'backward_targets': [s['id'] for s in blueprint['stages'][:index + 1]]}
            agents.append(compiled)
    # Compile task contracts into ordinary Stage / Agent / Tool nodes.
    if 'task_input' in blueprint:
        used = {s['stage_id'] for s in stages} | {a['role'] for a in agents}
        prefix = 'task_intake'
        while prefix in used or prefix + '_check' in used:
            prefix = '_' + prefix
        entry = stages[0]['stage_id']
        stages[:0] = [
            {'stage_id': prefix, 'agents': [prefix], 'next_stage': prefix + '_check', 'flow': {'type': 'serial'},
             'message': '接收任务资料'},
            {'stage_id': prefix + '_check', 'agents': [prefix + '_check'], 'next_stage': entry, 'flow': {'type': 'serial'},
             'message': '检查所需能力'},
        ]
        agents[:0] = [
            {'role': prefix, 'class': 'astra_core.agents.intake.TaskIntakeAgent', 'max_retries': 1, 'kwargs': {'name': prefix}},
            {'role': prefix + '_check', 'class': 'astra_core.agents.intake.CapabilityCheckAgent', 'max_retries': 1, 'kwargs': {'name': prefix + '_check'}},
        ]
    return {
        'runtime': {'agent_packages': [f'{package}.agents']},
        'orchestrator': {'max_retries': 1, 'hook_profile': 'standard', 'flow': {'type': 'serial'},
                         'stage_routing': {'init': stages[0]['stage_id']}},
        'outputs': {'output_dir': 'output', 'structure_figure_path': 'output/orchestrator_structure.png'},
        'agents': agents, 'stages': stages,
    }
