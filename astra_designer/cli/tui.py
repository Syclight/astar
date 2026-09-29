"""Small terminal widgets with a plain-input fallback for pipes and tests."""
import os
import sys

from astra_designer.cli.streaming import summary_text


def enabled():
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'


def picker_application(title, options, *, input=None, output=None):
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.widgets import TextArea
    from prompt_toolkit.styles import Style

    position = [0]
    search = TextArea(height=1, prompt='搜索 › ', multiline=False)

    def matches():
        terms = search.text.casefold().split()
        return [(key, summary_text(label)) for key, label in options
                if all(term in label.casefold() for term in terms)]

    def rows():
        items = matches()
        if not items:
            return [('class:muted', '没有匹配项；Esc 返回')]
        position[0] = min(position[0], len(items)-1)
        start = max(0, position[0]-7)
        result = []
        for index in range(start, min(start+9, len(items))):
            selected = index == position[0]
            result.append(('class:selected' if selected else '',
                           ('› ' if selected else '  ') + items[index][1] + '\n'))
        return result

    search.buffer.on_text_changed += lambda _: position.__setitem__(0, 0)
    keys = KeyBindings()

    @keys.add('up')
    def up(event):
        position[0] = max(0, position[0]-1)

    @keys.add('down')
    def down(event):
        position[0] = min(max(0, len(matches())-1), position[0]+1)

    @keys.add('enter')
    def accept(event):
        items = matches()
        if items:
            event.app.exit(result=items[min(position[0], len(items)-1)][0])

    @keys.add('escape')
    @keys.add('c-c')
    @keys.add('c-d')
    def cancel(event):
        event.app.exit(result=None)

    layout = HSplit([
        Window(FormattedTextControl([('class:title', summary_text(title))]), height=1),
        search,
        Window(FormattedTextControl(rows), height=min(10, len(options)+1), wrap_lines=False),
        Window(FormattedTextControl('↑↓ 选择 · 输入筛选 · Enter 确定 · Esc 取消'), height=1),
    ])
    return Application(layout=Layout(layout, focused_element=search), key_bindings=keys,
                       style=Style.from_dict({'title': 'bold ansicyan', 'selected': 'reverse', 'muted': 'ansibrightblack'}),
                       full_screen=False, erase_when_done=True, input=input, output=output)


def choose(title, options):
    if enabled():
        return picker_application(title, options).run()
    print(title)
    for index, (_, label) in enumerate(options, 1):
        print(f'  {index}. {summary_text(label)}')
    while True:
        answer = input('编号（回车取消）> ').strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer)-1][0]
        print('请输入有效编号。')


class ChatInput:
    def __init__(self, status, commands):
        self.status = status
        self.session = None
        self.draft = ''
        if enabled():
            from prompt_toolkit import PromptSession
            from prompt_toolkit.completion import WordCompleter
            from prompt_toolkit.key_binding import KeyBindings
            keys = KeyBindings()

            @keys.add('c-l')
            def model_picker(event):
                self.draft = event.current_buffer.text
                event.app.exit(result='/model')

            self.session = PromptSession(completer=WordCompleter(commands), key_bindings=keys,
                                         bottom_toolbar=lambda: [('reverse', ' ' + summary_text(status()) + ' ')])

    def read(self, prompt='你> '):
        if not self.session:
            return input(prompt)
        draft, self.draft = self.draft, ''
        return self.session.prompt(prompt, default=draft)
