"""Read-only discovery of saved sessions; never descend into generated projects."""
import json
import os
from datetime import datetime
from pathlib import Path


def list_sessions(root='workspace/designs'):
    root = Path(root).resolve()
    rows, warnings = [], []
    if not root.exists():
        return rows, warnings
    if not root.is_dir():
        raise ValueError('会话搜索路径必须是目录')

    def error(exc):
        warnings.append(f'无法读取目录：{exc.filename}')

    for directory, children, files in os.walk(root, followlinks=False, onerror=error):
        children[:] = sorted(name for name in children if name not in {
            'candidate', 'output', 'revisions', 'requirements-history', 'plans', '__pycache__', '.git', '.venv'
        } and not (Path(directory) / name).is_symlink())
        filename = 'discovery.json' if 'discovery.json' in files else 'session.json' if 'session.json' in files else None
        if filename is None:
            continue
        children[:] = []
        path = Path(directory) / filename
        try:
            if path.is_symlink() or path.stat().st_size > 20_000_000:
                raise ValueError('会话文件过大或为链接')
            record = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(record, dict) or not isinstance(record.get('status'), str):
                raise ValueError('无效的会话结构')
            if filename == 'discovery.json':
                transcript = record.get('transcript', [])
                goal = next((t.get('text', '') for t in transcript if isinstance(t, dict) and t.get('role') == 'user'), '')
                summary = (record.get('document') or {}).get('summary', '')
            else:
                goal, summary = record.get('goal', ''), record.get('summary', '')
            stamp = path.stat().st_mtime
            rows.append({'name': Path(directory).name, 'path': str(Path(directory).resolve()),
                         'kind': '需求探索' if filename == 'discovery.json' else '蓝图设计',
                         'status': record['status'], 'summary': summary or goal,
                         'updated_at': datetime.fromtimestamp(stamp).astimezone().isoformat(timespec='seconds'),
                         '_mtime': stamp})
        except (OSError, ValueError, TypeError, AttributeError):
            warnings.append(f'跳过无法解析的会话：{path}')
    rows.sort(key=lambda row: (-row['_mtime'], row['path']))
    for row in rows:
        del row['_mtime']
    return rows, warnings


def print_sessions(root='workspace/designs'):
    rows, warnings = list_sessions(root)
    print(f'会话目录：{Path(root).resolve()}')
    print('按最近保存时间排序（本机时区；旧记录使用会话文件修改时间）。')
    for index, row in enumerate(rows, 1):
        summary = ' '.join(str(row['summary']).split())[:100]
        print(f"\n{index}. {row['name']} | {row['kind']} | {row['status']}")
        print(f"   保存时间：{row['updated_at']}\n   摘要：{summary}\n   路径：{row['path']}")
        print(f'   恢复：python -m astra_designer chat --session "{row["path"]}"')
    if not rows:
        print('未找到已保存会话。自定义位置请通过 --session 指定搜索目录。')
    for warning in warnings:
        print('提示：' + warning)
    return 0
