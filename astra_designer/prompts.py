"""Shared product definitions for every designer model entry point."""
import json
import re

from astra_designer.catalog.registry import resource_text


def with_product_context(stage_prompt):
    return resource_text('product_context.md').strip() + '\n\n' + stage_prompt


def planning_examples():
    return [json.loads(resource_text('examples/text_summary.json'))]


def discovery_prompt(phase, *, section=None, include_contract=False):
    """Compose only the instructions for the response being requested."""
    if section == 'team':
        parts = [resource_text('proposal_prompt.md')]
    elif section == 'contract':
        parts = [resource_text('contract_prompt.md')]
    else:
        parts = [resource_text('discovery_prompt.md')]
        if section is None and phase == 'proposal':
            parts += [resource_text('proposal_prompt.md'), resource_text('contract_prompt.md')]
        elif section is None and include_contract:
            parts.append(resource_text('contract_prompt.md'))
    return with_product_context('\n\n'.join(parts))


# Select explanatory guides, never remove capabilities from the executable catalog.
# Matching is deliberately broad; the catalog remains the authority on availability.
GUIDE_TOPICS = {
    'object': r'对象|编号|身份|唯一|新建|继续|续写|多部|多个|跨运行|object|identity|state',
    'llm.map': r'逐|批量|多个|长文|整本|全书|batch|each|map',
    'document': r'文档|文件|正文|章节|小说|报告|pdf|docx|markdown|txt|\bmd\b|document|report',
    'table': r'表格|统计|汇总|金额|计算|账|csv|xlsx|excel|table|spreadsheet',
    'state': r'状态|记忆|续写|继续|连载|累计|存档|历史|小说|state|memory|persist',
    'flow': r'返工|循环|重做|分支|跳转|不通过|重画|retry|loop|route',
    'human': r'人工|审稿|审阅|审批|挑选|用户确认|human|review|approval',
    'image': r'图|海报|logo|视觉|image|poster|photo',
    'web': r'网页|网址|搜索|检索|联网|互联网|web|search|https?://',
    'http': r'接口|通知|系统|机器人|api|http|webhook',
    'email': r'邮件|邮箱|发信|email|e-mail|smtp',
    'fs': r'清理|删除|隔离|扫描目录|磁盘|回收|cleanup|quarantine|disk',
}


def capability_guides(context, catalog, blueprint=None):
    """Keep task-relevant explanations and every capability already in a candidate."""
    guides = json.loads(resource_text('capability_guides.json'))
    text = json.dumps(context, ensure_ascii=False).casefold()
    selected = {'llm.transform@1', 'integration.pending@1'}
    for identifier in guides:
        if identifier.casefold() in text:
            selected.add(identifier)
        for topic, pattern in GUIDE_TOPICS.items():
            if identifier.startswith(topic + '.') or identifier.startswith(topic + '@'):
                if re.search(pattern, text):
                    selected.add(identifier)
    for identifier, spec in catalog.items():
        if identifier.casefold() in text:
            selected.add(spec.get('base', identifier))
    stages = (blueprint or {}).get('stages', [])
    for stage in stages if isinstance(stages, list) else []:
        if not isinstance(stage, dict):
            continue
        agents = stage.get('agents', [])
        for agent in agents if isinstance(agents, list) else []:
            if isinstance(agent, dict):
                identifier = agent.get('capability')
                if isinstance(identifier, str):
                    selected.add((catalog.get(identifier) or {}).get('base', identifier))
    return list(dict.fromkeys(text for key, text in guides.items() if key in selected))
