"""Render saved conversations as plain text only when explicitly exported."""
import json
from pathlib import Path


def conversation_text(state, *, legacy=False):
    lines = ['Astra Designer 对话记录', '']

    def turn(speaker, text, questions=()):
        lines.extend([speaker + '：', text])
        for question in questions:
            lines.append('提问：' + question['question'])
        lines.append('')

    if legacy:
        lines.extend(['说明：旧版会话仅保留目标、历史问答和最近回复。', ''])
        turn('你', state['goal'])
        for entry in state.get('history', []):
            answers = entry.get('answers', {})
            for question in entry.get('questions', []):
                turn('Astra Designer', question['question'])
                if question['id'] in answers:
                    turn('你', answers[question['id']])
        turn('Astra Designer', state.get('summary', ''), state.get('questions', []))
    else:
        lines.extend(['说明：根据已保存的发言、回复摘要和追问整理，不包含完整终端输出。', ''])
        for entry in state['transcript']:
            speaker = '你' if entry['role'] == 'user' else 'Astra Designer'
            if entry['role'] == 'user' and entry['text'] == '我不确定合适的 Token 预算，请根据已知需求提供可比较的团队与预算建议，保留必要交付及检查，并说明推荐理由。':
                speaker = '预算请求（历史记录可能为命令展开，未保存原始输入）'
            turn(speaker,
                 entry['text'], entry.get('questions', []))
        if not state['transcript']:
            lines.append('暂无对话记录。')
        if state.get('status') in {'failed', 'budget_exhausted'}:
            turn('会话状态', '本次回复未完成，已提交的需求已保存。\n' +
                 '\n'.join(state.get('feedback', [])))
    return '\n'.join(lines).rstrip() + '\n'


def export_conversation(session_dir, output):
    if not session_dir:
        raise ValueError('请先建立会话，或指定已有会话目录。')
    if not output or not str(output).strip():
        raise ValueError('请提供导出文件路径，例如 /export "D:\\导出\\对话.txt"')
    target = Path(output).expanduser().resolve()
    if target.suffix.lower() != '.txt':
        raise ValueError('导出路径必须是以 .txt 结尾的文件路径。')
    directory = Path(session_dir).expanduser().resolve()
    source = directory / 'discovery.json'
    legacy = not source.is_file()
    if legacy:
        source = directory / 'session.json'
    if not source.is_file():
        raise ValueError('该目录中没有已保存的对话记录。')
    state = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(state, dict) or (('goal' if legacy else 'transcript') not in state):
        raise ValueError('无效的会话记录。')
    content = conversation_text(state, legacy=legacy)
    history_path = directory / 'cli-history.json'
    if not legacy and history_path.is_file():
        history = json.loads(history_path.read_text(encoding='utf-8'))
        lines = ['Astra Designer CLI 对话记录', '',
                 '说明：记录启用后的实际输入与终端文字；不含密钥、配置向导和动态进度刷新。此前对话见下方历史摘要。', '',
                 history.get('previous_summary', '').rstrip(), '', '以下为已保存的 CLI 实际对话：', '']
        for entry in history['turns']:
            lines.extend(['你> ' + entry['input'], entry['output'], ''])
        content = '\n'.join(lines).rstrip() + '\n'
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation protects existing exports and other user files.
    with target.open('x', encoding='utf-8-sig', newline='\n') as stream:
        stream.write(content)
    return target
