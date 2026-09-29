"""Keep CLI presentation separate from the model's requirements transcript."""
from contextlib import contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import sys

from astra_designer.contracts.session import save_json, session_lock


class ConversationOutput(io.TextIOBase):
    def __init__(self, output):
        self.output = output
        self.parts = []

    def write(self, text):
        self.parts.append(text)
        return self.output.write(text)

    def flush(self):
        self.output.flush()

    def isatty(self):
        return self.output.isatty()

    @property
    def encoding(self):
        return self.output.encoding

    def text(self):
        # Carriage-return updates replace a terminal line, rather than append it.
        return '\n'.join(line.split('\r')[-1].replace('\x1b[2K', '').rstrip()
                         for line in ''.join(self.parts).split('\n')).strip()


@contextmanager
def record_turn(directory, user_text):
    """Resolve the directory after execution, including a newly created session."""
    initial = directory()
    previous = None
    if initial is not None and (Path(initial) / 'discovery.json').is_file():
        previous = json.loads((Path(initial) / 'discovery.json').read_text(encoding='utf-8'))
    output = ConversationOutput(sys.stdout)
    with redirect_stdout(output):
        try:
            yield
        finally:
            target = directory()
            if target is not None and (Path(target) / 'discovery.json').is_file():
                with session_lock(Path(target)):
                    path = Path(target) / 'cli-history.json'
                    if path.is_file():
                        history = json.loads(path.read_text(encoding='utf-8'))
                    else:
                        from astra_designer.cli.export import conversation_text
                        if user_text.startswith('/resume ') and initial != target:
                            previous = json.loads((Path(target) / 'discovery.json').read_text(encoding='utf-8'))
                        history = {'version': 1, 'turns': [],
                                   'previous_summary': conversation_text(previous) if previous else ''}
                    history['turns'].append({'input': user_text, 'output': output.text()})
                    save_json(path, history)
