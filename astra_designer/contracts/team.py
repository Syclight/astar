"""Team proposals: which roles carry out which confirmed requirements.

Token budgeting was retired until it is redesigned. Sessions saved earlier may
still contain its fields; they stay readable but are never requested again.
"""
import re

LEGACY_PROPOSAL_FIELDS = ('budget_mode', 'target_tokens', 'budget_sources')
LEGACY_OPTION_FIELDS = ('tokens', 'reserve_tokens', 'estimate', 'convergence', 'assumptions', 'other_costs')
LEGACY_ROLE_FIELDS = ('model_strategy', 'calls', 'input_tokens', 'output_tokens')
REVIEW_PATTERN = r'审阅|审核|审查|复核|评审|校对|修订|review'


def model_team_schema(team):
    """Return the team_proposal schema the model may fill, without retired fields."""
    for key in LEGACY_PROPOSAL_FIELDS:
        team['properties'].pop(key, None)
    option = team['properties']['options']['items']
    for key in LEGACY_OPTION_FIELDS:
        option['properties'].pop(key, None)
    option['properties']['label']['maxLength'] = 80
    role = option['properties']['roles']['items']
    for key in LEGACY_ROLE_FIELDS:
        role['properties'].pop(key, None)
    return team


def validate_team(document, *, design_rules=True):
    """Check a proposal; design_rules=False only checks integrity of a confirmed one."""
    proposal = document.get('team_proposal')
    if proposal is None:
        return
    options = proposal['options']
    identifiers = [option['id'] for option in options]
    if len(identifiers) != len(set(identifiers)) or proposal['recommended_id'] not in identifiers:
        raise ValueError('团队方案 ID 重复或推荐方案不存在')
    reqs = {item['id'] for item in document['items']}
    for option in options:
        roles = option['roles']
        if len({r['id'] for r in roles}) != len(roles):
            raise ValueError('团队角色 ID 重复')
        if any(not set(r['requirement_ids']) <= reqs for r in roles):
            raise ValueError('角色引用了不存在的需求')
        if not design_rules:
            continue
        validate_roster(option, role_requirements(document))
        if not any(r['priority'] == 'delivery' for r in roles):
            raise ValueError('每个方案必须有交付职责（priority=delivery）')
        if needs_review(document) and not any(r['priority'] == 'review' for r in roles):
            raise ValueError('需求包含验收或审阅要求，方案必须保留检查职责（priority=review），可以由交付 Agent 兼任')


def needs_review(document):
    """A separate check role is required only when the user asked for checking.

    Otherwise quality is enforced by the blueprint's deterministic acceptance,
    and an extra reviewer would be an entity without necessity (Occam's razor).
    """
    return any(item['category'] == 'acceptance' or re.search(REVIEW_PATTERN, item['statement'], re.I)
               for item in document['items'])


def role_requirements(document):
    """Requirements that some role must carry out.

    The overall goal is served by the whole team. Sessions from the retired
    budget feature may contain preferences whose only evidence is a budget
    statement; those were satisfied by the proposal itself, not by a role.
    """
    budget = (document.get('team_proposal') or {}).get('budget_sources', [])
    def budget_only(item):
        return bool(budget) and all(any(source['turn'] == b['turn'] and source['quote'] in b['quote'] for b in budget)
                                    for source in item['sources'])
    return {item['id'] for item in document['items']
            if item['category'] != 'goal' and not (item['category'] == 'preference' and budget_only(item))}


def validate_roster(option, requirement_ids):
    missing = requirement_ids - set().union(*(set(r['requirement_ids']) for r in option['roles']))
    if missing:
        raise ValueError(f"方案 {option['id']} 未覆盖需求 {', '.join(sorted(missing))}；每项需求都要由某个角色负责")
    roles = option['roles']
    for index, role in enumerate(roles):
        for other in roles[index + 1:]:
            if role['priority'] == other['priority'] and set(role['requirement_ids']) == set(other['requirement_ids']):
                raise ValueError(f"角色 {role['id']} 与 {other['id']} 职责依据完全相同，属于冗余角色：请合并为一个角色；"
                                 '若二者确是用户要求的不同步骤，requirement_ids 只列各自真正负责的需求')


def chosen_team(state):
    proposal = (state.get('document') or {}).get('team_proposal')
    if not proposal:
        return None
    choice = state.get('team_choice') or proposal['recommended_id']
    return next((option for option in proposal['options'] if option['id'] == choice), None)


def validate_team_mapping(blueprint):
    snapshot = blueprint.get('requirements', {})
    proposal = snapshot.get('document', {}).get('team_proposal')
    if not proposal:
        return
    from astra_designer.contracts.requirements import digest
    # The user already confirmed this team; later design rules must not void it.
    validate_team(snapshot['document'], design_rules=False)
    selected = snapshot.get('selected_team')
    if selected not in proposal['options'] or snapshot['approval'].get('team_sha256') != digest(selected):
        raise ValueError('团队方案与用户确认不一致')
    mapping = blueprint.get('team_assignment', {})
    roles = {r['id'] for r in selected['roles']}
    if set(mapping) != roles:
        missing, extra = sorted(roles - set(mapping)), sorted(set(mapping) - roles)
        raise ValueError('team_assignment 必须且只能包含已确认团队的角色 ' + '、'.join(sorted(roles))
                         + (f"；缺少 {'、'.join(missing)}" if missing else '')
                         + (f"；不存在的角色 {'、'.join(extra)}（其 Agent 应归入已有角色）" if extra else ''))
    agents = {a['id'] for s in blueprint['stages'] for a in s['agents']}
    assigned = set()
    for values in mapping.values():
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or v not in agents for v in values):
            raise ValueError('团队角色映射必须引用真实 Agent')
        assigned.update(values)
    if assigned != agents:
        raise ValueError('所有执行 Agent 必须归属于已确认团队')
