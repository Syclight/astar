"""Complete legacy input contracts without rewriting confirmed requirements."""
import copy
import json

from astra_core.llm.contracts import parse_json
from astra_designer.llm.recording import recorded_complete_json
from astra_designer.contracts.requirements import response_schema, validate_record
from astra_designer.contracts.session import save_json
from astra_designer.prompts import with_product_context


def complete_contract(state, directory, client, fields=('task_input', 'interaction', 'deliverables')):
    from astra_designer.discovery.pipeline import persist
    if client is None:
        from astra_designer.llm.config import configured_model
        client = configured_model()
    # Only what the confirmed record lacks; existing contracts are kept as confirmed.
    fields = tuple(key for key in fields if not state['document'].get(key))
    # Every contract field may appear, but only missing ones are required and taken: confirmed ones stay as they are.
    schema = {'type': 'object', 'additionalProperties': False,
              'required': list(fields),
              'properties': {key: response_schema()['properties'][key] for key in ('task_input', 'interaction', 'deliverables')}}
    if 'interaction' in fields:
        schema['properties']['interaction']['required'] = ['on_missing', 'confirm_before_run', 'questions']
    if 'task_input' in fields:
        task = schema['properties']['task_input']
        task['required'] = ['type', 'properties', 'required', 'additionalProperties']
        task['properties']['properties']['propertyNames'] = {'pattern': '^[a-z][a-z0-9_]{0,63}$'}
        task['properties']['properties']['additionalProperties'] = {
            'type': 'object', 'required': ['type', 'description'],
            'properties': {'type': {'enum': ['string', 'number', 'integer', 'boolean', 'array', 'object']},
                           'description': {'type': 'string', 'minLength': 1}}}
    from astra_designer.contracts.settings import limit_message, load_settings
    limit = load_settings()['max_plan_calls']
    if limit is not None and state.get('contract_calls', 0) >= limit:
        raise ValueError(limit_message('输入规范补全', limit))
    state['contract_calls'] = state.get('contract_calls', 0) + 1
    # File numbers keep increasing even when a failed call is not counted against the limit.
    call_number = 1 + len(list((directory / 'diagnostics').glob('input-contract-*.json')))
    persist(directory, state)
    path = directory / 'diagnostics' / f"input-contract-{call_number:04d}.json"
    path.parent.mkdir(exist_ok=True)
    diagnostic = {'status': 'requesting'}
    save_json(path, diagnostic)
    try:
        diagnostic['model_trace'] = f'model-calls/input-contract-{call_number:04d}.json'
        reply = recorded_complete_json(client, [
            {'role': 'system', 'content': with_product_context(
             f"为已确认的可复用 Astra 业务项目补全 {'、'.join(fields)}。"
             '仅返回这些字段的 JSON，不重写需求或团队方案。根据已有需求定义，不索要实际素材。'
             'task_input：字段名为小写英文字母开头的 snake_case，每个字段声明 type 和中文 description；'
             'required 至少一项，列出任何时候都必填的资料；只在某个选项下必填的字段写进 x-astra-required-when'
             '（如 [{"field": "mode", "values": ["new"], "required": ["genre"]}]），真正可不填的字段在 description 中写明“可选”；'
             'additionalProperties=false；不把产品例子写为默认值。'
             'interaction：明确 on_missing、confirm_before_run、questions；它只管执行前收集资料与确认，运行中审阅由团队中的审阅步骤实现。'
             'deliverables：每项交付物写明名称、包含的内容和形式（file 须给出 txt/md/docx/pdf/csv/xlsx/image 格式；data 为运行记录中的结构化结果；'
             'action 为对外部状态的改变，如清理文件、发送通知，须写 evidence 说明如何验证它确实做了），'
             '引用对应的需求 ID，覆盖全部 output 需求；不写“待定”或“运行时决定”。未指定的低影响细节按公共默认规则直接决定，不单独询问或标为待确认。')},
            {'role': 'user', 'content': json.dumps({'requirements': state['document'],
                                                   'transcript': state['transcript']}, ensure_ascii=False)}], schema,
                                       trace_path=path.parent / diagnostic['model_trace'])
        diagnostic.update(response=reply.content[:200000], usage=reply.usage)
        contract = parse_json(reply.content)
        from jsonschema import Draft202012Validator
        errors = list(Draft202012Validator(schema).iter_errors(contract))
        if errors:
            raise ValueError('输入规范响应无效：' + errors[0].message)
        document = copy.deepcopy(state['document'])
        document.update({key: contract[key] for key in fields})
        validate_record(document, state['transcript'])
        state.update(document=document, revision=state['revision'] + 1,
                     approval=None, plan=None, status='awaiting_confirmation')
        history = directory / 'requirements-history'
        history.mkdir(exist_ok=True)
        save_json(history / f"{state['revision']:04d}.json", document)
        persist(directory, state)
        diagnostic['status'] = 'accepted'
        return {'status': 'needs_confirmation', 'summary':
                '已根据已有需求补齐接单规范和交付物清单。请用 /requirements 核对输入资料和交付成果，确认后再 /plan。'}
    except Exception as exc:
        from astra_designer.llm.protocol import ModelError
        if isinstance(exc, ModelError):
            # Model-side failures (truncation, connection) do not use up the session's limit.
            state['contract_calls'] -= 1
            persist(directory, state)
            diagnostic['counted'] = False
        diagnostic.update(status='failed', error={'type': type(exc).__name__, 'message': str(exc)[:2000]})
        if getattr(exc, 'details', None):
            diagnostic['error']['details'] = exc.details
        raise
    finally:
        save_json(path, diagnostic)
