"""Atomic session persistence, single-writer protection and call records."""
import datetime
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


def save_json(path: Path, value):
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as file:
            json.dump(value, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def session_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.lock').open('a+b') as file:
        if file.tell() == 0:
            file.write(b'0')
            file.flush()
        file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('当前设计会话正在由另一执行者使用') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def model_settings(client):
    """The settings that decide how long a reply may be; never includes secrets."""
    keys = ('model', 'max_output_tokens', 'reasoning_effort', 'stream', 'response_format', 'timeout')
    return {key: getattr(client, key) for key in keys if isinstance(getattr(client, key, None), (str, int, float, bool))}


class CallProgress:
    """Count streamed answer and thinking text; keep a bounded copy of the answer."""
    LIMIT = 200000

    def __init__(self):
        self.content_chars = self.reasoning_chars = 0
        self._content = []

    def __call__(self, kind, text):
        if kind == 'reasoning':
            self.reasoning_chars += len(text)
        elif kind == 'content':
            if self.content_chars < self.LIMIT:
                self._content.append(text)
            self.content_chars += len(text)

    @property
    def content(self):
        return ''.join(self._content)[:self.LIMIT]

    def summary(self):
        return {'content_chars': self.content_chars, 'reasoning_chars': self.reasoning_chars}


def failure_details(exc, client, progress=None):
    """What a user needs to act on a failed model call, e.g. an output limit hit by thinking."""
    details = {key: exc.details[key] for key in ('finish_reason', 'stage') if key in getattr(exc, 'details', {})}
    details.update(model_settings(client))
    if progress is not None:
        details.update(progress.summary())
    return details
