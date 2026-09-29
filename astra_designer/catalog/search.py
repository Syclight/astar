"""Deterministic local search with visible match reasons; no model required."""
import re
from astra_designer.catalog.registry import capabilities


def search_capabilities(query='', limit=10):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit 必须在 1–100 之间')
    terms = set(re.findall(r'[a-z0-9_.@-]+|[\u4e00-\u9fff]', query.casefold()))
    result = []
    for identifier, spec in capabilities().items():
        text = ' '.join([identifier, spec['title'], spec['description'], *spec['tags']]).casefold()
        matched = sorted(term for term in terms if term in text)
        if terms and not matched:
            continue
        result.append({'id': identifier, 'title': spec['title'], 'description': spec['description'],
                       'availability': spec.get('availability', {'status': 'builtin', 'missing': []}),
                       'tags': spec['tags'], 'base': spec['base'], 'source': spec['source'],
                       'sha256': spec['sha256'], 'score': len(matched), 'matched_terms': matched})
    return sorted(result, key=lambda x: (-x['score'], x['id']))[:limit]
