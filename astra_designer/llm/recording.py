"""Per-call designer traces, persisted before sending and during streaming."""
import time
from pathlib import Path

from astra_core.llm.contracts import complete_json
from astra_designer.contracts.session import model_settings, now, save_json

PARTIAL_LIMIT = 200_000


def recorded_complete_json(client, messages, schema, *, trace_path, on_progress=None):
    """Record actual messages/schema and returned text, without connection credentials.

    A completed trace means the transport returned, not that business validation passed.
    Partial streams are bounded; a successful response is saved in full.
    """
    path = Path(trace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = getattr(client, 'api_key', None)

    def redact(value):
        if isinstance(value, str):
            return value.replace(secret, '[REDACTED]') if isinstance(secret, str) and secret else value
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [redact(item) for item in value]
        return value

    record = {'version': 1, 'status': 'requesting', 'started_at': now(),
              'model': model_settings(client), 'request': {'messages': messages, 'response_schema': schema}}
    for key in ('token_parameter', 'max_response_bytes', 'max_content_bytes'):
        value = getattr(client, key, None)
        if isinstance(value, (str, int)):
            record['model'][key] = value
    started = time.monotonic()
    last_save = 0
    chunks = {'content': [], 'reasoning': []}
    counts = {'content': 0, 'reasoning': 0}

    def persist():
        record['elapsed_seconds'] = round(time.monotonic() - started, 3)
        record['stream'] = {kind: {'text': ''.join(chunks[kind]), 'chars': counts[kind],
                                  'truncated': counts[kind] > PARTIAL_LIMIT} for kind in chunks}
        save_json(path, redact(record))

    def progress(kind, text):
        nonlocal last_save
        if kind in chunks and isinstance(text, str):
            remaining = max(0, PARTIAL_LIMIT - counts[kind])
            if remaining:
                chunks[kind].append(text[:remaining])
            counts[kind] += len(text)
            current = time.monotonic()
            if current - last_save >= 1:
                persist()
                last_save = current
        if on_progress is not None:
            on_progress(kind, text)

    persist()  # A killed process still leaves its exact request and latest partial response.
    try:
        reply = complete_json(client, messages, schema, on_progress=progress)
        record.update(status='completed', response={'content': reply.content, 'usage': reply.usage,
                                                    'chars': len(reply.content), 'truncated': False})
        return reply
    except BaseException as exc:
        record.update(status='failed' if isinstance(exc, Exception) else 'interrupted',
                      error={'type': type(exc).__name__, 'message': str(exc)})
        if getattr(exc, 'details', None):
            record['error']['details'] = exc.details
        raise
    finally:
        record['finished_at'] = now()
        persist()
