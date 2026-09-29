"""Stream the user-facing summary, keeping model bookkeeping out of the CLI."""
import json
import sys
import threading
import time

from astra_core.llm.client import ChatModel


def terminal_text(text):
    return ''.join(c for c in text if c in '\n\t' or
                   (ord(c) >= 32 and not 127 <= ord(c) <= 159 and not 0xD800 <= ord(c) <= 0xDFFF))


def summary_text(text):
    """Keep a summary on one logical line, also while streaming partial text."""
    return ' '.join(terminal_text(text.replace('\r', '\n')).split())


def question_text(text):
    """Let the CLI own spacing; retain line breaks for options and examples."""
    return '\n'.join(line.strip() for line in terminal_text(text.replace('\r', '\n')).splitlines()
                     if line.strip())


class SummaryPreview:
    """Decode only a top-level JSON summary, including split Unicode escapes."""
    def __init__(self):
        self.buffer = ''
        self.position = 0
        self.started = False
        self.value_start = None
        self.text = ''
        self.done = False

    def feed(self, fragment):
        if self.done:
            return ''
        self.buffer += fragment
        decoder = json.JSONDecoder()
        if not self.started:
            start = len(self.buffer) - len(self.buffer.lstrip())
            if start >= len(self.buffer) or self.buffer[start] != '{':
                return ''
            self.position, self.started = start + 1, True
        while self.value_start is None:
            pos = self.position
            while pos < len(self.buffer) and self.buffer[pos] in ' \r\n\t,':
                pos += 1
            try:
                key, end = decoder.raw_decode(self.buffer, pos)
                while end < len(self.buffer) and self.buffer[end].isspace():
                    end += 1
                if end >= len(self.buffer) or self.buffer[end] != ':':
                    return ''
                end += 1
                while end < len(self.buffer) and self.buffer[end].isspace():
                    end += 1
                if key == 'summary':
                    if end >= len(self.buffer) or self.buffer[end] != '"':
                        return ''
                    self.value_start = end
                else:
                    _, self.position = decoder.raw_decode(self.buffer, end)
            except ValueError:
                return ''
        raw = self.buffer[self.value_start:]
        try:
            value, _ = decoder.raw_decode(raw)
            self.done = True
        except ValueError:
            # A trailing JSON escape may span chunks (including Unicode escapes).
            value = None
            for trim in range(min(6, len(raw) - 1) + 1):
                candidate = raw if trim == 0 else raw[:-trim]
                try:
                    value = json.loads(candidate + '"')
                    break
                except ValueError:
                    continue
            if value is None:
                return ''
        if not isinstance(value, str):
            return ''
        value = summary_text(value)[:400]
        addition = value[len(self.text):]
        self.text = value
        if len(value) == 400:
            self.done = True
        return addition


class LiveModel:
    def __init__(self, model):
        self.model_client = model
        self.last_summary = ''

    def __getattr__(self, name):
        return getattr(self.model_client, name)

    def validation_failed(self, *, retrying=True):
        self.last_summary = ''
        print('模型回复未通过检查，正在自动修复…' if retrying else '自动修复未成功，已停止重试。', flush=True)

    def complete_json(self, messages, schema):
        return self.complete(messages, response_schema=schema)

    def complete_json_with_progress(self, messages, schema, on_progress):
        return self.complete(messages, response_schema=schema, on_progress=on_progress)

    def complete(self, messages, *, response_schema=None, on_progress=None):
        output = sys.stdout
        started = time.monotonic()
        stopped = threading.Event()
        lock = threading.Lock()
        preview = SummaryPreview()
        self.last_summary = ''
        shown = False
        summary_closed = False
        status_visible = False
        tty = output.isatty()
        activity = '正在理解需求'
        content_chars = reasoning_chars = 0
        try:
            context = json.loads(messages[-1]['content'])
        except (ValueError, KeyError, IndexError, TypeError):
            context = {}
        if not isinstance(context, dict):
            context = {}
        stage = '蓝图' if 'blueprint_schema' in context else '方案' if context.get('phase') == 'proposal' else '需求'
        section_activity = {'requirements': '正在核对需求（1/3）', 'team': '正在设计团队角色（2/3）',
                            'contract': '正在整理输入与交付规范（3/3）'}.get(context.get('section'))
        if section_activity:
            activity = section_activity
        repair_label = '自动修复' if context.get('attempt_kind') == 'repair' else '生成'

        def waiting():
            nonlocal status_visible
            while not stopped.wait(1):
                with lock:
                    if shown and not summary_closed:
                        continue
                    progress = f'已接收正文 {content_chars:,} 字符、思考 {reasoning_chars:,} 字符' if content_chars or reasoning_chars else '等待模型输出'
                    tag = '｜自动修复' if repair_label == '自动修复' else ''
                    output.write(f'\r{activity}{tag}｜{progress}… {time.monotonic() - started:.0f} 秒（Ctrl+C 退出）   ')
                    output.flush()
                    status_visible = True

        def delta(kind, text):
            nonlocal shown, activity, summary_closed, status_visible, content_chars, reasoning_chars
            if on_progress:
                on_progress(kind, text)
            with lock:
                if kind == 'content':
                    content_chars += len(text)
                elif kind == 'reasoning':
                    reasoning_chars += len(text)
                if kind != 'content':
                    return
                addition = preview.feed(text)
                activity = section_activity or '正在整理需求记录'
                if addition:
                    if not shown:
                        if tty:
                            output.write('\r\x1b[2K')
                            status_visible = False
                        output.write(f'Astra-Designer[{stage}]> ')
                        shown = True
                    output.write(addition)
                    output.flush()
                if shown and preview.done and not summary_closed:
                    output.write('\n')
                    output.flush()
                    summary_closed = True

        print((section_activity or '正在整理你的需求') + ('｜自动修复' if repair_label == '自动修复' else '') + '…', file=output, flush=True)
        thread = threading.Thread(target=waiting, daemon=True) if tty else None
        if thread:
            thread.start()
        try:
            reply = self.model_client.complete(messages, on_delta=delta, response_schema=response_schema)
        except BaseException:
            stopped.set()
            if thread:
                thread.join()
            print('\n模型回复中断，已提交的内容已保存。', file=output, flush=True)
            raise
        else:
            stopped.set()
            if thread:
                thread.join()
            self.last_summary = preview.text
            if status_visible:
                output.write('\r\x1b[2K')
            # The preview already ends its line when the summary is complete.
            # The following display owns spacing before questions and details.
            if shown and not summary_closed:
                print(file=output, flush=True)
            return reply


def live_model(model):
    return LiveModel(model) if isinstance(model, ChatModel) and model.stream else model
