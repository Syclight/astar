"""Typical business requests, each from confirmed requirements to a checked result.

A case is what a user would have confirmed after the requirements conversation: the goal in their
words, the requirement items quoting it, the per-run input, the deliverables and a one-role team.
`prepare` makes sample input files and returns the run's task_input; `check` looks at the result
beyond the blueprint's own acceptance (the right numbers, the expected content).
"""
import csv
import datetime
import os
from pathlib import Path


def text_field(description):
    return {'type': 'string', 'minLength': 1, 'description': description}


def case(identifier, title, goal, items, fields, deliverables, *, prepare, check, runs=1, needs=(), rules=None):
    """items: (category, statement, quote in the goal); deliverables refer to items by 1-based position."""
    for _, _, quote in items:
        assert quote in goal, (identifier, quote)
    records = [{'id': f'REQ-{number:03d}', 'category': category, 'statement': statement,
                'sources': [{'turn': 1, 'quote': quote}]} for number, (category, statement, quote) in enumerate(items, 1)]
    required = [name for name, (_, needed) in fields.items() if needed]
    task_input = {'type': 'object', 'additionalProperties': False, 'required': required,
                  'properties': {name: spec for name, (spec, _) in fields.items()}}
    for item in deliverables:
        item['requirement_ids'] = [f'REQ-{number:03d}' for number in item.pop('requirements')]
    import re
    from astra_designer.contracts.team import REVIEW_PATTERN
    checked = [record['id'] for record in records if record['category'] != 'goal'
               and (record['category'] == 'acceptance' or re.search(REVIEW_PATTERN, record['statement']))]
    roles = [{'id': 'maker', 'responsibility': '按需求完成交付', 'priority': 'delivery',
              'requirement_ids': [record['id'] for record in records
                                  if record['category'] != 'goal' and record['id'] not in checked]}]
    if checked:  # a request that mentions reviewing keeps a checking duty, as the requirements rules demand
        roles.append({'id': 'checker', 'responsibility': '检查成果', 'priority': 'review', 'requirement_ids': checked})
    document = {'summary': title, 'items': records, 'issues': [], 'questions': [],
                'team_proposal': {'options': [{'id': 'lean', 'label': '精简', 'roles': roles,
                                               'deliverables': [item['name'] for item in deliverables],
                                               'tradeoffs': ['一个角色完成全部步骤']}],
                                  'recommended_id': 'lean', 'reason': '任务明确'},
                'task_input': task_input,
                'interaction': rules or {'on_missing': 'error', 'confirm_before_run': False, 'questions': {}},
                'deliverables': deliverables}
    return {'id': identifier, 'title': title, 'goal': goal, 'document': document, 'prepare': prepare,
            'check': check, 'runs': runs, 'needs': needs}


# ---------- helpers for sample data and checks ----------

def files_in(state, suffix):
    """Paths of the run's output files with this suffix, wherever the data keeps them."""
    found = []
    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and value.lower().endswith(suffix) and len(value) < 1000:
            try:
                if Path(value).is_file():
                    found.append(value)
            except OSError:
                pass
    walk({key: value for key, value in state.get('data', {}).items() if key not in {'run', 'task_input'}})
    return list(dict.fromkeys(found))


def text_of(path):
    path = Path(path)
    if path.suffix == '.docx':
        import re
        import zipfile
        with zipfile.ZipFile(path) as archive:
            return re.sub(r'<[^>]+>', '', archive.read('word/document.xml').decode('utf-8'))
    if path.suffix == '.xlsx':
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=True)
        return '\n'.join(' '.join('' if cell is None else str(cell) for cell in row)
                         for sheet in book.worksheets for row in sheet.iter_rows(values_only=True))
    return path.read_text(encoding='utf-8-sig', errors='replace')


def produced(state, suffixes):
    return [path for suffix in suffixes for path in files_in(state, suffix)]


def contains(state, suffixes, words, label):
    """(label, passed, detail): some output file of these types mentions every word."""
    paths = produced(state, suffixes)
    if not paths:
        return label, False, f"没有 {'/'.join(suffixes)} 文件"
    text = '\n'.join(text_of(path) for path in paths)
    missing = [word for word in words if word not in text]
    return label, not missing, ('缺少：' + '、'.join(missing)) if missing else f'{len(paths)} 个文件'


def write_csv(path, header, rows):
    with Path(path).open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)


# ---------- the cases ----------

def payroll_prepare(folder, run):
    rows = [('张三', '研发', 12000, 2350.5, 300), ('李四', '研发', 15000, 1000, 0), ('王五', '销售', 9000, 4125.25, 150.5)]
    return {'sheet_file': write_csv(folder / '9月工资表.csv', ['姓名', '部门', '基本工资', '绩效', '扣款'], rows)}


def payroll_check(state, folder, states=()):
    # 研发 12000+2350.5-300 + 15000+1000-0 = 30050.5 ; 销售 9000+4125.25-150.5 = 12974.75
    return [contains(state, ['.xlsx'], ['30050.5', '12974.75'], '部门实发合计精确'),
            contains(state, ['.xlsx'], ['14050.5', '16000'], '个人实发工资')]


def sales_prepare(folder, run):
    rows = [('2026-01-05', '甲', 100), ('2026-01-20', '乙', 300), ('2026-02-03', '甲', 110), ('2026-02-28', '甲', 150.5)]
    return {'sales_file': write_csv(folder / '销售明细.csv', ['日期', '产品', '金额'], rows)}


def sales_check(state, folder, states=()):
    # Only the February total of 甲 is 260.5; no raw row or other sum produces it.
    return [contains(state, ['.xlsx', '.csv'], ['260.5'], '按月汇总正确（2 月甲 260.5）')]


def weekly_prepare(folder, run):
    return {'week': '第 39 周', 'notes': '完成登录页改版并上线；支付接口联调延期两天，原因是对方证书过期；'
                                      '下周完成支付联调并开始会员积分需求评审。'}


def weekly_check(state, folder, states=()):
    return [contains(state, ['.docx'], ['支付'], '周报包含本周要点'),
            contains(state, ['.docx'], ['下周'], '包含下周计划')]


def contract_prepare(folder, run):
    text = ('第一条 甲方委托乙方开发软件系统。\n\n第二条 甲方应在验收后三十日内付款。\n\n'
            '第三条 任何一方逾期付款的，按日千分之五支付违约金。\n\n第四条 乙方可随时单方解除合同，无需承担责任。\n\n'
            '第五条 争议由乙方所在地法院管辖。')
    path = folder / '软件开发合同.txt'
    path.write_text(text, encoding='utf-8')
    return {'contract_file': str(path)}


def contract_check(state, folder, states=()):
    return [contains(state, ['.md'], ['违约金'], '指出违约金条款'), contains(state, ['.md'], ['解除'], '指出单方解除条款')]


def meeting_prepare(folder, run):
    path = folder / '周会纪要.txt'
    path.write_text('产品周会纪要（9 月 25 日）\n张三负责在 10 月 8 日前完成登录页改版。\n'
                    '李四负责 9 月 30 日前提交支付联调报告。\n王五跟进客户反馈，本周五前给出分类结果。', encoding='utf-8')
    return {'minutes_file': str(path)}


def meeting_check(state, folder, states=()):
    return [contains(state, ['.xlsx'], ['张三', '李四', '王五'], '每个待办都在表中')]


def feedback_prepare(folder, run):
    rows = [('页面加载很慢',), ('希望增加导出功能',), ('按钮太小不好点',), ('打开 App 要等十几秒',), ('能不能支持夜间模式',), ('客服态度很好',)]
    return {'feedback_file': write_csv(folder / '客户反馈.csv', ['反馈内容'], rows)}


def feedback_check(state, folder, states=()):
    return [contains(state, ['.md'], ['性能', '功能', '易用性'], '报告含各类别')]


def article_prepare(folder, run):
    return {'product': '晴空智能空气净化器', 'points': '除醛率 99%、噪音 30 分贝、App 远程控制、滤网寿命 12 个月'}


def article_check(state, folder, states=()):
    return [contains(state, ['.docx'], ['净化器'], '文章围绕产品'), contains(state, ['.docx'], ['99%'], '写入卖点')]


def translate_prepare(folder, run):
    path = folder / '产品说明.txt'
    path.write_text('晴空净化器采用三层滤网设计。\n\n第一层过滤毛发和大颗粒灰尘。\n\n'
                    '第二层为高效滤网，可去除细颗粒物。\n\n第三层活性炭去除甲醛和异味。', encoding='utf-8')
    return {'doc_file': str(path)}


def translate_check(state, folder, states=()):
    return [contains(state, ['.md'], ['filter'], '译文为英文（出现 filter）')]


def cleanup_prepare(folder, run):
    target = folder / '下载'
    (target / '旧').mkdir(parents=True, exist_ok=True)
    old = datetime.datetime.now().timestamp() - 60 * 86400
    for name in ('旧/a.log', '旧/b.tmp', 'c.log'):
        path = target / name
        path.write_text('x' * 100, encoding='utf-8')
        if name != 'c.log':
            os.utime(path, (old, old))
    (target / '合同.pdf').write_text('keep', encoding='utf-8')
    os.utime(target / '合同.pdf', (old, old))
    return {'folder': str(target)}


def cleanup_check(state, folder, states=()):
    target = folder / '下载'
    moved = not (target / '旧' / 'a.log').exists() and not (target / '旧' / 'b.tmp').exists()
    kept = (target / 'c.log').exists() and (target / '合同.pdf').exists()
    return [('超过 30 天的 .log/.tmp 已移入隔离区', moved, ''), ('新文件和其他类型未动', kept, ''),
            ('隔离区可还原', any((target / '.astra_quarantine').rglob('manifest.csv')), '')]


def novel_prepare(folder, run):
    return {'title': '长夜', 'mode': 'new' if run == 1 else 'continue'}


def novel_check(state, folder, states=()):
    first, second = (produced(item, ['.txt']) for item in (states[0], states[-1])) if len(states) > 1 else ([], [])
    names = [Path(path).name for path in first + second]
    different = bool(first and second) and {text_of(path) for path in first}.isdisjoint(text_of(path) for path in second)
    return [('两次运行各写出一章，内容不同', different, f'{len(first)} + {len(second)} 个 txt'),
            ('文件名含书名', bool(names) and all('长夜' in name for name in names), '、'.join(names)),
            ('两章文件名不同（含章节号）', len(set(names)) == len(names) > 1, '')]


def research_prepare(folder, run):
    return {'topic': '2026 年固态电池产业化进展'}


def research_check(state, folder, states=()):
    return [contains(state, ['.md'], ['http'], '简报附出处链接')]


IMAGE_SUFFIXES = ['.png', '.jpg', '.jpeg', '.webp']


def png_size(path):
    """(width, height) of a PNG, or None for other formats."""
    head = Path(path).read_bytes()[:24]
    if head[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    return int.from_bytes(head[16:20], 'big'), int.from_bytes(head[20:24], 'big')


def sample_png(path, size=64):
    """A small solid-colour PNG, written without any imaging library."""
    import struct
    import zlib
    rows = b''.join(b'\x00' + bytes((200, 60, 40)) * size for _ in range(size))
    chunk = lambda kind, data: struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
                           + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
    return str(path)


def poster_prepare(folder, run):
    return {'theme': '周末城市咖啡市集', 'slogan': '一杯咖啡，一座城', 'date': '10 月 18 日 10:00–18:00'}


def poster_check(state, folder, states=()):
    images = produced(state, IMAGE_SUFFIXES)
    sizes = [png_size(path) for path in images]
    portrait = [size for size in sizes if size and size[1] > size[0]]
    return [('生成 2 张候选海报', len(images) >= 2, f'{len(images)} 张'),
            ('竖版 3:4', bool(portrait) and len(portrait) == len([size for size in sizes if size]),
             '、'.join(f'{w}×{h}' for w, h in (size for size in sizes if size)) or '非 PNG，未检查尺寸')]


def product_prepare(folder, run):
    return {'photos': [sample_png(folder / 'mug_1.png'), sample_png(folder / 'mug_2.png')]}


def product_check(state, folder, states=()):
    images = [path for path in produced(state, IMAGE_SUFFIXES) if Path(path).parent != Path(folder)]
    return [('每张商品照片各出一张白底图', len(images) >= 2, f'{len(images)} 张'),
            ('文件名保留原图名', all(any(name in Path(path).name for name in ('mug_1', 'mug_2')) for path in images),
             '、'.join(Path(path).name for path in images))]


CASES = [
    case('payroll', '月度工资计算',
         '每月根据工资表计算每个人的实发工资：基本工资加绩效减扣款，四舍五入到分，再按部门汇总实发合计，导出 Excel。',
         [('goal', '计算实发工资并按部门汇总', '计算每个人的实发工资'),
          ('input', '每月提供工资表文件', '根据工资表'),
          ('rule', '实发工资 = 基本工资 + 绩效 - 扣款，四舍五入到分', '基本工资加绩效减扣款，四舍五入到分'),
          ('rule', '按部门汇总实发合计', '按部门汇总实发合计'),
          ('output', '导出 Excel', '导出 Excel'),
          ('scope', '每月一次，处理一份工资表', '每月根据工资表')],
         {'sheet_file': (text_field('工资表文件路径（CSV 或 xlsx），列：姓名、部门、基本工资、绩效、扣款'), True)},
         [{'id': 'DEL-001', 'name': '工资计算表', 'description': '每个人的实发工资明细和各部门实发合计',
           'form': 'file', 'formats': ['xlsx'], 'requirements': [5]}],
         prepare=payroll_prepare, check=payroll_check),
    case('sales_monthly', '销售按月汇总',
         '把销售明细按月份和产品汇总销售额，算出每月各产品的销售额占比，导出 Excel 和 CSV。',
         [('goal', '按月份和产品汇总销售额', '按月份和产品汇总销售额'), ('input', '销售明细文件', '销售明细'),
          ('rule', '计算每月各产品的销售额占比', '算出每月各产品的销售额占比'), ('output', '导出 Excel 和 CSV', '导出 Excel 和 CSV'),
          ('scope', '处理提供的销售明细', '把销售明细')],
         {'sales_file': (text_field('销售明细文件路径，列：日期、产品、金额'), True)},
         [{'id': 'DEL-001', 'name': '月度销售汇总表', 'description': '每月每个产品的销售额与占比', 'form': 'file',
           'formats': ['xlsx', 'csv'], 'requirements': [4]}],
         prepare=sales_prepare, check=sales_check),
    case('weekly_report', '周报撰写',
         '根据我给的本周工作要点写一份周报，包括本周完成、问题与风险、下周计划三部分，输出 Word 文档。',
         [('goal', '根据工作要点写周报', '写一份周报'), ('input', '本周工作要点和周次', '本周工作要点'),
          ('rule', '包括本周完成、问题与风险、下周计划三部分', '包括本周完成、问题与风险、下周计划三部分'),
          ('output', '输出 Word 文档', '输出 Word 文档'), ('scope', '每周一份', '一份周报')],
         {'notes': (text_field('本周工作要点'), True), 'week': (text_field('周次，如第 39 周'), True)},
         [{'id': 'DEL-001', 'name': '周报', 'description': '含本周完成、问题与风险、下周计划三部分的周报', 'form': 'file',
           'formats': ['docx'], 'requirements': [4]}],
         prepare=weekly_prepare, check=weekly_check),
    case('contract_review', '合同审阅',
         '审阅我提供的合同文本，逐条指出对我方（甲方）不利的条款并给出修改建议，输出 Markdown 审阅意见。',
         [('goal', '审阅合同并指出不利条款', '指出对我方（甲方）不利的条款'), ('input', '合同文本文件', '我提供的合同文本'),
          ('rule', '逐条审阅并给出修改建议', '逐条指出对我方（甲方）不利的条款并给出修改建议'),
          ('output', '输出 Markdown 审阅意见', '输出 Markdown 审阅意见'), ('scope', '每次审阅一份合同', '审阅我提供的合同文本')],
         {'contract_file': (text_field('合同文件路径（txt、docx 或 pdf）'), True)},
         [{'id': 'DEL-001', 'name': '合同审阅意见', 'description': '逐条列出不利条款、风险说明和修改建议', 'form': 'file',
           'formats': ['md'], 'requirements': [4]}],
         prepare=contract_prepare, check=contract_check),
    case('meeting_actions', '会议待办提取',
         '从会议纪要中提取所有待办事项，包括事项、负责人和截止日期，导出 Excel 表格。',
         [('goal', '提取会议待办事项', '提取所有待办事项'), ('input', '会议纪要文件', '从会议纪要中'),
          ('rule', '每条待办包括事项、负责人和截止日期', '包括事项、负责人和截止日期'),
          ('output', '导出 Excel 表格', '导出 Excel 表格'), ('scope', '处理一份会议纪要', '从会议纪要中')],
         {'minutes_file': (text_field('会议纪要文件路径'), True)},
         [{'id': 'DEL-001', 'name': '待办事项表', 'description': '每行一个待办：事项、负责人、截止日期', 'form': 'file',
           'formats': ['xlsx'], 'requirements': [4]}],
         prepare=meeting_prepare, check=meeting_check),
    case('feedback_classify', '客户反馈分类统计',
         '把客户反馈逐条分为性能、功能、易用性、其他四类，统计各类数量，输出 Markdown 报告。',
         [('goal', '客户反馈分类统计', '把客户反馈逐条分为性能、功能、易用性、其他四类'), ('input', '客户反馈表格文件', '客户反馈'),
          ('rule', '统计各类数量', '统计各类数量'), ('output', '输出 Markdown 报告', '输出 Markdown 报告'),
          ('scope', '逐条处理全部反馈', '逐条')],
         {'feedback_file': (text_field('客户反馈 CSV 路径，列：反馈内容'), True)},
         [{'id': 'DEL-001', 'name': '反馈分类报告', 'description': '每类反馈的数量和典型例子', 'form': 'file', 'formats': ['md'],
           'requirements': [4]}],
         prepare=feedback_prepare, check=feedback_check),
    case('article_review_loop', '文章撰写与返工',
         '写一篇产品介绍文章，写完由 AI 审稿，不合格就按审稿意见修改，最多修改两次，最后输出 Word。',
         [('goal', '写产品介绍文章', '写一篇产品介绍文章'), ('input', '产品名称和卖点', '产品介绍'),
          ('rule', 'AI 审稿，不合格按意见修改，最多两次', '不合格就按审稿意见修改，最多修改两次'),
          ('output', '输出 Word', '输出 Word'), ('scope', '每次一篇文章', '一篇产品介绍文章')],
         {'product': (text_field('产品名称'), True), 'points': (text_field('主要卖点'), True)},
         [{'id': 'DEL-001', 'name': '产品介绍文章', 'description': '经审稿通过（或达到修改上限）的产品介绍文章', 'form': 'file',
           'formats': ['docx'], 'requirements': [4]}],
         prepare=article_prepare, check=article_check),
    case('translate_document', '长文档翻译',
         '把中文文档逐段翻译成英文，保持原有段落顺序，输出 Markdown。',
         [('goal', '中文文档译成英文', '把中文文档逐段翻译成英文'), ('input', '中文文档文件', '中文文档'),
          ('rule', '逐段翻译，保持段落顺序', '保持原有段落顺序'), ('output', '输出 Markdown', '输出 Markdown'),
          ('scope', '每次一份文档', '把中文文档')],
         {'doc_file': (text_field('中文文档路径（txt、docx 或 pdf）'), True)},
         [{'id': 'DEL-001', 'name': '英文译文', 'description': '按原段落顺序排列的英文译文', 'form': 'file', 'formats': ['md'],
           'requirements': [4]}],
         prepare=translate_prepare, check=translate_check),
    case('file_cleanup', '清理过期文件',
         '清理我指定文件夹中超过 30 天没修改过的 .log 和 .tmp 文件，先移入隔离区方便找回，并保留处理清单。',
         [('goal', '清理过期的日志和临时文件', '清理我指定文件夹中超过 30 天没修改过的 .log 和 .tmp 文件'),
          ('input', '要清理的文件夹', '我指定文件夹'),
          ('rule', '只处理超过 30 天未修改的 .log 和 .tmp 文件', '超过 30 天没修改过的 .log 和 .tmp 文件'),
          ('rule', '移入隔离区，可以找回', '先移入隔离区方便找回'),
          ('output', '文件被移入隔离区并保留处理清单', '先移入隔离区方便找回，并保留处理清单'),
          ('scope', '只在指定文件夹内', '我指定文件夹')],
         {'folder': (text_field('要清理的文件夹路径'), True)},
         [{'id': 'DEL-001', 'name': '已隔离的过期文件', 'description': '超过 30 天未修改的 .log 和 .tmp 文件移入隔离区',
           'form': 'action', 'evidence': '已处理文件清单、隔离位置和涉及空间', 'requirements': [5]}],
         prepare=cleanup_prepare, check=cleanup_check),
    case('novel_serial', '连载小说',
         '连载小说团队：每次运行写下一章，约 800 字，记住前面的人物和情节摘要，每章单独导出 txt 文件，文件名包含书名和章节号。',
         [('goal', '连载写小说，每次一章', '每次运行写下一章'), ('input', '书名和本次是新书还是续写', '连载小说团队'),
          ('rule', '每章约 800 字', '约 800 字'), ('rule', '记住前面的人物和情节摘要', '记住前面的人物和情节摘要'),
          ('output', '每章单独导出 txt 文件，文件名包含书名和章节号', '每章单独导出 txt 文件，文件名包含书名和章节号'),
          ('scope', '每次运行写一章', '每次运行写下一章')],
         {'title': (text_field('书名'), True),
          'mode': ({'type': 'string', 'enum': ['new', 'continue'], 'description': 'new 开新书，continue 续写'}, True)},
         [{'id': 'DEL-001', 'name': '章节 txt 文件', 'description': '本次写出的一章，文件名含书名和章节号', 'form': 'file',
           'formats': ['txt'], 'requirements': [5]}],
         prepare=novel_prepare, check=novel_check, runs=2),
    case('research_brief', '联网调研简报',
         '围绕我给的主题联网搜索最新资料，整理成一份带出处链接的简报，输出 Markdown。',
         [('goal', '联网调研并写简报', '联网搜索最新资料'), ('input', '调研主题', '我给的主题'),
          ('rule', '简报中注明出处链接', '带出处链接'), ('output', '输出 Markdown 简报', '输出 Markdown'),
          ('scope', '每次一个主题', '围绕我给的主题')],
         {'topic': (text_field('调研主题'), True)},
         [{'id': 'DEL-001', 'name': '调研简报', 'description': '要点摘要和每条要点的出处链接', 'form': 'file', 'formats': ['md'],
           'requirements': [4]}],
         prepare=research_prepare, check=research_check, needs=('ASTRA_SEARCH_PROVIDER',)),
    case('event_poster', '活动海报',
         '根据我给的活动主题、标语和时间生成竖版 3:4 的活动海报，标语要出现在画面上，每次出 2 张候选图片。',
         [('goal', '生成活动海报', '生成竖版 3:4 的活动海报'), ('input', '活动主题、标语和时间', '活动主题、标语和时间'),
          ('rule', '竖版 3:4，标语出现在画面上', '标语要出现在画面上'), ('output', '每次 2 张候选海报图片', '每次出 2 张候选图片'),
          ('scope', '每次一个活动', '根据我给的活动主题')],
         {'theme': (text_field('活动主题'), True), 'slogan': (text_field('海报标语'), True), 'date': (text_field('活动时间'), True)},
         [{'id': 'DEL-001', 'name': '海报候选图', 'description': '2 张竖版 3:4 海报，画面含标语和活动时间', 'form': 'file',
           'formats': ['image'], 'requirements': [4]}],
         prepare=poster_prepare, check=poster_check, needs=('ASTRA_IMAGE_URL',)),
    case('product_white_background', '商品图换白底',
         '把我提供的每张商品照片的背景换成纯白色，商品本身保持不变，每张照片各出一张图。',
         [('goal', '商品照片换白底', '背景换成纯白色'), ('input', '商品照片', '我提供的每张商品照片'),
          ('rule', '商品本身保持不变', '商品本身保持不变'), ('output', '每张照片各出一张白底图', '每张照片各出一张图'),
          ('scope', '处理本次提供的全部照片', '我提供的每张商品照片')],
         {'photos': ({'type': 'array', 'minItems': 1, 'items': {'type': 'string', 'minLength': 1},
                      'description': '商品照片路径（png、jpg）'}, True)},
         [{'id': 'DEL-001', 'name': '白底商品图', 'description': '每张商品照片对应一张纯白背景的图片', 'form': 'file',
           'formats': ['image'], 'requirements': [4]}],
         prepare=product_prepare, check=product_check, needs=('ASTRA_IMAGE_URL',)),
]
