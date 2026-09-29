"""User-visible design editions, separate from immutable storage revisions."""
import copy

from astra_designer.contracts.requirements import ready


def content(state):
    document = state['document']
    proposal = copy.deepcopy(document.get('team_proposal'))
    if proposal:
        proposal.pop('budget_sources', None)
    return {
        'requirements': sorted((item['category'], item['statement']) for item in document['items']),
        'task_input': document.get('task_input'),
        'interaction': document.get('interaction'),
        'deliverables': document.get('deliverables'),
        'team_proposal': proposal,
        'choice': (state.get('team_choice') or proposal['recommended_id']) if proposal else None,
    }


def update_design_version(state, *, migrating=False):
    """Advance the edition only when published content changes; stores digests, not a copy."""
    from astra_designer.contracts.requirements import digest
    state.setdefault('design_version', 0)
    document = state.get('document')
    if (not document or not ready(document) or
            (not migrating and state['status'] not in {'requirements_ready', 'awaiting_confirmation', 'confirmed'})):
        return
    current = {key: digest(value) for key, value in content(state).items()}
    previous = state.get('published_digests')
    if previous is None and state.get('published_content') is not None:
        previous = {key: digest(value) for key, value in state['published_content'].items()}  # older sessions
    state.pop('published_content', None)
    if previous is not None:
        previous = {'deliverables': digest(None), **previous}  # sessions from before deliverables existed
    if previous is None:
        state['design_version'] = 1
    else:
        empty = digest(None)
        initial_proposal = (previous['team_proposal'] == empty and current['team_proposal'] != empty
                            and previous['requirements'] == current['requirements']
                            and all(previous[key] in (empty, current[key]) for key in ('task_input', 'interaction', 'deliverables')))
        if previous != current and not initial_proposal:
            state['design_version'] += 1
    state['published_digests'] = current


def design_label(state):
    number = state.get('design_version', 0)
    return f'设计第 {number} 版' if number else '需求澄清中（尚未形成第 1 版）'
