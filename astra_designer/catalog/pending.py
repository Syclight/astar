"""Explicit design-time contracts for integrations that are not implemented."""

IDENTIFIER = 'integration.pending@1'


def specification():
    text = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
    ports = {'type': 'object', 'additionalProperties': text}
    return {'class': 'PendingIntegrationAgent', 'inputs': {}, 'outputs': {},
            'parameters': {'type': 'object', 'additionalProperties': False,
                           'required': ['title', 'description', 'setup', 'input_schemas', 'output_schemas'],
                           'properties': {'title': text, 'description': text,
                                          'setup': {'type': 'array', 'minItems': 1, 'items': text},
                                          'input_schemas': ports,
                                          'output_schemas': {**ports, 'minProperties': 1}}}}


def dependencies(blueprint):
    from astra_designer.catalog.registry import capability_for
    result = []
    for stage in blueprint['stages']:
        for agent in stage['agents']:
            spec = capability_for(agent)
            if spec and spec['base'] == IDENTIFIER:
                result.append({'agent': agent['id'], 'stage': stage['id'],
                               'inputs': agent['inputs'], 'outputs': agent['outputs'],
                               **spec['effective_parameters']})
    return result
