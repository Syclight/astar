"""Trusted deterministic agents emitted by Astra Designer.

Feedback classification is first-match keyword matching, not model inference.
DocumentWriterAgent writes upstream text to txt / md / docx (no extra dependency) and pdf (reportlab).
TableReadAgent / TableWriteAgent handle CSV and xlsx (openpyxl); HttpRequestAgent calls a fixed HTTP/JSON endpoint.
StateLoadAgent / StateSaveAgent keep a team's memory between runs in <output>/state/.
DocumentReadAgent / WebFetchAgent turn files and pages into text; TableComputeAgent calculates exactly;
RouteAgent jumps to another stage (redo or skip) when a condition holds.
FileScanAgent only reads; FileCleanupAgent defaults to a dry run and quarantines instead of deleting.
"""
import ast
import fnmatch
import shutil
import time
import html.parser
import math
import csv
import datetime
import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

from jsonschema import Draft202012Validator

from astra import BaseAgent
from astra_core.object_identity import resolve_object, object_directory, resolve_external_object


def bound_object(parameters, values, state, *, create=True):
    if parameters.get('object_context'):
        return values['object']
    if parameters.get('key_field'):
        return resolve_external_object(parameters, values.get('brief') or {}, state, create=create)
    return None


NEXT_STAGE = '__next_stage__'


class ContractAgent(BaseAgent):
    def __init__(self, name, inputs, outputs, parameters, input_schemas, output_schemas):
        super().__init__(name)
        self.inputs = inputs
        self.outputs = outputs
        self.parameters = parameters
        self.input_schemas = input_schemas
        self.output_schemas = output_schemas
        self.project_dir = Path(__file__).resolve().parents[1]

    def validate(self, schema_name, value):
        schema = json.loads((self.project_dir / 'schemas' / f'{schema_name}.json').read_text(encoding='utf-8'))
        Draft202012Validator(schema).validate(value)

    def run(self, state):
        values = {port: state['data'][key] for port, key in self.inputs.items()}
        for port, value in values.items():
            self.validate(self.input_schemas[port], value)
        result = self.execute(values, state)
        next_stage = result.pop(NEXT_STAGE, None)
        for port, value in result.items():
            self.validate(self.output_schemas[port], value)
        response = {'status': 'success', 'data': {self.outputs[port]: value for port, value in result.items()}}
        if next_stage:
            response['next_stage'] = next_stage  # the orchestrator continues at this stage
        return response


class ReadFeedbackAgent(ContractAgent):
    def execute(self, values, state):
        root = (self.project_dir / 'data').resolve()
        path = (root / f"{self.parameters['source']}.json").resolve()
        if not path.is_relative_to(root):
            raise ValueError('Input file is outside project data directory')
        return {'records': json.loads(path.read_text(encoding='utf-8'))}


class ClassifyFeedbackAgent(ContractAgent):
    def execute(self, values, state):
        classified = []
        for record in values['records']:
            text = record['text'].casefold()
            category = next((rule['category'] for rule in self.parameters['rules']
                             if any(word.casefold() in text for word in rule['keywords'])),
                            self.parameters['fallback'])
            classified.append({**record, 'category': category})
        return {'classified': classified}


class CountFeedbackAgent(ContractAgent):
    def execute(self, values, state):
        return {'counts': dict(sorted(Counter(row['category'] for row in values['classified']).items()))}


class ReportFeedbackAgent(ContractAgent):
    def execute(self, values, state):
        counts = values['counts']
        lines = [f"# {self.parameters['title']}", '', f'反馈总数：{sum(counts.values())}', '',
                 '| 问题类型 | 数量 | 改进建议 |', '|---|---:|---|']
        for category, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            recommendation = self.parameters['recommendations'].get(category, '人工复核并补充处理建议。')
            safe_category = category.replace('|', '\\|').replace('\n', ' ')
            safe_recommendation = recommendation.replace('|', '\\|').replace('\n', ' ')
            lines.append(f'| {safe_category} | {count} | {safe_recommendation} |')
        lines.extend(['', '说明：分类使用有序关键词规则，建议来自预设模板，需结合实际业务复核。', ''])
        path = Path(state['data']['run']['output_dir']) / 'feedback_report.md'
        path.write_text('\n'.join(lines), encoding='utf-8')
        return {'report': str(path)}


def resolve_pointer(value, pointer):
    for token in (pointer or '').split('/')[1:]:
        token = token.replace('~1', '/').replace('~0', '~')
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


FILENAME_PLACEHOLDER = re.compile(r'\{(/[^{}]*|[a-z][a-z0-9_]*)\}')
UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
RESERVED_NAMES = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(10)), *(f'lpt{i}' for i in range(10))}


def filled(template, content, brief, purpose='文件名'):
    """The template with {task_input field} and {/pointer into the data} filled in."""
    def fill(match):
        token = match.group(1)
        try:
            value = resolve_pointer(content, token) if token.startswith('/') else (brief or {}).get(token)
        except (KeyError, IndexError, TypeError, ValueError):
            value = None
        where = f'数据 {token}' if token.startswith('/') else f'task_input.{token}'
        if isinstance(value, bool) or isinstance(value, (dict, list)) or value is None or str(value).strip() == '':
            raise ValueError(f'{purpose}要用到 {where}，但它为空或不是文字、数字')
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value).strip()
    return FILENAME_PLACEHOLDER.sub(fill, template)


def output_name(template, content, brief):
    """The file name with {task_input field} and {/pointer into the written data} filled in, safe on Windows."""
    name = UNSAFE_NAME.sub('_', filled(template, content, brief)).strip(' .')[:100].rstrip(' .') or 'output'
    return '_' + name if name.split('.')[0].casefold() in RESERVED_NAMES else name


FORMAT_WORDS = (('docx', ('docx', 'word', 'doc')), ('pdf', ('pdf',)), ('md', ('markdown', 'md')),
                ('txt', ('txt', '文本', 'text')))


def chosen_formats(value, allowed):
    """Map a run's answer such as "Word 文档" or ["txt", "docx"] to known formats."""
    answers = value if isinstance(value, list) else [value]
    result = []
    for answer in answers:
        text = str(answer).casefold()
        for fmt, words in FORMAT_WORDS:
            if any(word in text for word in words) and fmt in allowed and fmt not in result:
                result.append(fmt)
                break
    return result


HEADING = re.compile(r'^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$')
LIST_ITEM = re.compile(r'^\s*([-*+•]|\d{1,3}[.、)）])\s+(.*)$')
TABLE_RULE = re.compile(r'^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$')


def markdown_blocks(text):
    """The Markdown a model usually writes: headings, list items, pipe tables and paragraphs."""
    lines, index = text.splitlines(), 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith('|') and index + 1 < len(lines) and TABLE_RULE.match(lines[index + 1]):
            rows = []
            while index < len(lines) and lines[index].strip().startswith('|'):
                if not TABLE_RULE.match(lines[index]):
                    rows.append([cell.strip() for cell in lines[index].strip().strip('|').split('|')])
                index += 1
            yield ('table', rows)
            continue
        heading, item = HEADING.match(line), LIST_ITEM.match(line)
        if heading:
            yield ('heading', min(len(heading.group(1)), 3), heading.group(2))
        elif item:
            marker = '•' if item.group(1) in '-*+•' else item.group(1)
            yield ('item', marker, item.group(2))
        elif line.strip():
            yield ('para', line.strip())
        index += 1


def plain_lines(text):
    """Markdown headings and emphasis become plain text for .txt files."""
    for line in text.splitlines():
        line = re.sub(r'^\s{0,3}#{1,6}\s+', '', line)
        yield line.replace('**', '').replace('__', '')


def needs_title(text, title):
    """Add the title unless the text already opens with it."""
    first = next((line for line in plain_lines(text) if line.strip()), '')
    return bool(title and title.strip() and first.strip() != title.strip())


def write_txt(path, text, title):
    lines = list(plain_lines(text))
    if needs_title(text, title):
        lines[:0] = [title.strip(), '']
    path.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8')


def write_md(path, text, title):
    if needs_title(text, title):
        text = f'# {title.strip()}\n\n{text}'
    path.write_text(text.rstrip() + '\n', encoding='utf-8')


def _xml_text(value):
    value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', value)
    return escape(value.replace('**', '').replace('__', ''))


def _paragraph(text, style=None):
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ''
    return f'<w:p>{props}<w:r><w:t xml:space="preserve">{_xml_text(text)}</w:t></w:r></w:p>'


def _table(rows):
    width = max(len(row) for row in rows)
    border = ''.join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
                     for side in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'))
    cells = lambda row, header: ''.join(
        f'<w:tc><w:p><w:pPr><w:ind w:firstLineChars="0" w:firstLine="0"/></w:pPr><w:r>'
        + ('<w:rPr><w:b/></w:rPr>' if header else '')
        + f'<w:t xml:space="preserve">{_xml_text(cell)}</w:t></w:r></w:p></w:tc>' for cell in row + [''] * (width - len(row)))
    return (f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:tblBorders>{border}</w:tblBorders></w:tblPr>'
            + ''.join(f'<w:tr>{cells(row, number == 0)}</w:tr>' for number, row in enumerate(rows)) + '</w:tbl>')


DOCX_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="SimSun"/>
<w:sz w:val="24"/><w:szCs w:val="24"/><w:lang w:val="en-US" w:eastAsia="zh-CN"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="360" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/>
<w:pPr><w:ind w:firstLineChars="200" w:firstLine="480"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:jc w:val="center"/><w:ind w:firstLineChars="0" w:firstLine="0"/><w:spacing w:before="240" w:after="360"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="44"/><w:szCs w:val="44"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:keepNext/><w:jc w:val="center"/><w:ind w:firstLineChars="0" w:firstLine="0"/><w:spacing w:before="360" w:after="240"/><w:outlineLvl w:val="0"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:keepNext/><w:ind w:firstLineChars="0" w:firstLine="0"/><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="1"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:qFormat/>
<w:pPr><w:keepNext/><w:ind w:firstLineChars="0" w:firstLine="0"/><w:outlineLvl w:val="2"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="26"/><w:szCs w:val="26"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListItem"><w:name w:val="List Item"/><w:basedOn w:val="Normal"/>
<w:pPr><w:spacing w:after="60"/><w:ind w:left="420" w:hanging="300" w:firstLineChars="0"/></w:pPr></w:style>
</w:styles>"""


def write_docx(path, text, title):
    body = []
    if needs_title(text, title):
        body.append(_paragraph(title.strip(), 'Title'))
    for block in markdown_blocks(text):
        if block[0] == 'heading':
            body.append(_paragraph(block[2], f'Heading{block[1]}'))
        elif block[0] == 'item':
            body.append(_paragraph(f'{block[1]} {block[2]}', 'ListItem'))
        elif block[0] == 'table':
            body.append(_table(block[1]))
        else:
            body.append(_paragraph(block[1]))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                + ''.join(body) +
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800" w:header="851" w:footer="992" w:gutter="0"/>'
                '</w:sectPr></w:body></w:document>')
    parts = {
        '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            '</Types>',
        '_rels/.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>',
        'word/_rels/document.xml.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        'word/document.xml': document,
        'word/styles.xml': DOCX_STYLES,
    }
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


PDF_FONT_CANDIDATES = (
    'C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/simsun.ttc', 'C:/Windows/Fonts/simhei.ttf', 'C:/Windows/Fonts/Deng.ttf',
    '/Library/Fonts/Arial Unicode.ttf', '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc', '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf', '/usr/share/fonts/truetype/arphic/uming.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc')
_PDF_FONT = []


def pdf_font():
    """Embed a local CJK TrueType font when one exists; otherwise use the viewer-provided STSong-Light."""
    if _PDF_FONT:
        return _PDF_FONT[0]
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for path in (os.environ.get('ASTRA_PDF_FONT'), *PDF_FONT_CANDIDATES):
        if path and Path(path).is_file():
            try:
                pdfmetrics.registerFont(TTFont('AstraCJK', path, subfontIndex=0))
                _PDF_FONT.append('AstraCJK')
                return 'AstraCJK'
            except Exception:
                continue  # e.g. CFF-based OpenType, which reportlab cannot embed
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    _PDF_FONT.append('STSong-Light')
    return 'STSong-Light'


def write_pdf(path, text, title):
    try:
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise ValueError('导出 PDF 需要 reportlab：pip install reportlab') from exc
    font = pdf_font()
    body = ParagraphStyle('body', fontName=font, fontSize=11, leading=19, firstLineIndent=22, spaceAfter=6, wordWrap='CJK')
    styles = {'Title': ParagraphStyle('title', parent=body, fontSize=20, leading=28, alignment=TA_CENTER,
                                      firstLineIndent=0, spaceAfter=18),
              'Heading1': ParagraphStyle('h1', parent=body, fontSize=16, leading=24, alignment=TA_CENTER,
                                         firstLineIndent=0, spaceBefore=12, spaceAfter=12),
              'Heading2': ParagraphStyle('h2', parent=body, fontSize=13, leading=20, firstLineIndent=0, spaceBefore=8),
              'Heading3': ParagraphStyle('h3', parent=body, fontSize=12, leading=19, firstLineIndent=0),
              'Item': ParagraphStyle('item', parent=body, firstLineIndent=0, leftIndent=18, bulletIndent=4, spaceAfter=3),
              'Cell': ParagraphStyle('cell', parent=body, firstLineIndent=0, fontSize=10, leading=15, spaceAfter=0)}
    story = []
    def clean(value):
        return escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', value).replace('**', '').replace('__', ''))
    def add(value, style, **extra):
        story.append(Paragraph(clean(value), style, **extra))
    if needs_title(text, title):
        add(title.strip(), styles['Title'])
    for block in markdown_blocks(text):
        if block[0] == 'heading':
            add(block[2], styles[f'Heading{block[1]}'])
        elif block[0] == 'item':
            add(block[2], styles['Item'], bulletText=block[1])
        elif block[0] == 'table':
            width = max(len(row) for row in block[1])
            data = [[Paragraph(clean(cell), styles['Cell']) for cell in row + [''] * (width - len(row))] for row in block[1]]
            table = Table(data, repeatRows=1, hAlign='LEFT', colWidths=[160 * mm / width] * width)
            table.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.grey), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                       ('BACKGROUND', (0, 0), (-1, 0), colors.whitesmoke)]))
            story += [table, Spacer(1, 6)]
        else:
            add(block[1], body)
    if not story:
        story.append(Spacer(1, 1))
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=25 * mm, rightMargin=25 * mm,
                      topMargin=22 * mm, bottomMargin=22 * mm, title=title or path.stem).build(story)


WRITERS = {'txt': write_txt, 'md': write_md, 'docx': write_docx, 'pdf': write_pdf}


def section(item, index, parameters):
    """(text, heading) of one section: a string, or an object with section_text_key / section_title_key."""
    if isinstance(item, str):
        return item, None
    text_key, title_key = parameters.get('section_text_key'), parameters.get('section_title_key')
    text = item.get(text_key) if isinstance(item, dict) and text_key else None
    if not isinstance(text, str):
        raise ValueError(f'第 {index} 节缺少文本字段 {text_key or "（未设置 section_text_key）"}')
    heading = item.get(title_key) if title_key else None
    return text, heading if isinstance(heading, str) and heading.strip() else None


def document_text(content, parameters):
    """Text at text_path; a list of sections (e.g. chapters) is joined with each title as a heading."""
    value = resolve_pointer(content, parameters.get('text_path', ''))
    if not isinstance(value, list):
        return value
    parts = []
    for index, item in enumerate(value, 1):
        text, heading = section(item, index, parameters)
        parts.append((f'# {heading}\n\n' if heading else '') + text)
    return '\n\n'.join(parts)


class DocumentWriterAgent(ContractAgent):
    def execute(self, values, state):
        formats = self.formats(values)
        root = Path(state['data']['run']['output_dir'])
        library = None
        context = bound_object(self.parameters, values, state)
        if context is not None:
            library = object_directory(context, state) / 'documents'
            library.mkdir(parents=True, exist_ok=True)
            root = root / context['id']
        root.mkdir(parents=True, exist_ok=True)
        if self.parameters.get('per_item'):
            files = self.each(values, formats, root)
            if library is not None:
                for paths in files.values():
                    for path in paths:
                        shutil.copy2(path, library / Path(path).name)
            return {'files': files}
        text = document_text(values['content'], self.parameters)
        if not isinstance(text, str) or not text.strip():
            raise ValueError('要写入文档的文本为空或不是字符串，请检查 text_path')
        try:
            title = resolve_pointer(values['content'], self.parameters['title_path']) if self.parameters.get('title_path') else None
        except (KeyError, IndexError, TypeError, ValueError):
            title = None  # an optional title that this run did not produce
        name = output_name(self.parameters['filename'], values['content'], values.get('brief'))
        if self.parameters.get('accumulate'):
            text, library = self.accumulated(name, text, values, state)
        files = {}
        for fmt in formats:
            path = root / f"{name}.{fmt}"
            WRITERS[fmt](path, text, title if isinstance(title, str) else None)
            files[fmt] = str(path)
            if library is not None:
                shutil.copy2(path, library / path.name)  # the whole manuscript, always in the same place
        return {'files': files}

    def each(self, values, formats, root):
        """One file per item of the array at text_path, e.g. one per chapter; {/field} in the name reads the item."""
        items = resolve_pointer(values['content'], self.parameters.get('text_path', ''))
        if not isinstance(items, list) or not items:
            raise ValueError('per_item 需要 text_path 指向非空数组（如各章）')
        files, used = {fmt: [] for fmt in formats}, set()
        for index, item in enumerate(items, 1):
            text, heading = section(item, index, self.parameters)
            if not text.strip():
                raise ValueError(f'第 {index} 项的正文为空')
            name = output_name(self.parameters['filename'], item if isinstance(item, dict) else {}, values.get('brief'))
            name = name if name.casefold() not in used else f'{name}_{index}'
            used.add(name.casefold())
            for fmt in formats:
                path = root / f'{name}.{fmt}'
                WRITERS[fmt](path, text, heading)
                files[fmt].append(str(path))
        return files

    def accumulated(self, name, text, values, state):
        """This run's text appended to everything written before under the same name (e.g. the whole novel)."""
        library = project_root(state) / 'output' / 'documents'
        fresh = starts_over(self.parameters, values)
        context = bound_object(self.parameters, values, state)
        if context is not None:
            library = object_directory(context, state) / 'documents'
            if self.parameters.get('object_context'):
                fresh = context['is_new']
        library.mkdir(parents=True, exist_ok=True)
        store = library / f'.{name}.parts.json'
        entries = []
        if store.is_file() and not fresh:
            entries = json.loads(store.read_text(encoding='utf-8')).get('entries', [])
        run_id = state['data']['run'].get('id')
        # A retry of the same run replaces its own part instead of adding it twice.
        entries = [entry for entry in entries if entry.get('run_id') != run_id]
        entries.append({'run_id': run_id, 'text': text,
                        'saved_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')})
        temporary = store.with_name(store.name + '.tmp')
        temporary.write_text(json.dumps({'name': name, 'entries': entries}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(store)
        return '\n\n'.join(entry['text'] for entry in entries), library

    def formats(self, values):
        formats = list(self.parameters['formats'])
        field = self.parameters.get('format_field')
        if field and values.get('brief', {}).get(field) not in (None, '', []):
            formats = chosen_formats(values['brief'][field], formats)
            if not formats:
                raise ValueError(f"task_input.{field} 必须是 {'、'.join(self.parameters['formats'])} 之一")
        return formats


def _cell(value):
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _records(header_row, data_rows, limit):
    names, seen = [], {}
    for index, value in enumerate(header_row, 1):
        name = str(value).strip() if value not in (None, '') else f'列{index}'
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f'{name}_{seen[name]}')
    records = []
    for row in data_rows:
        row = list(row)
        if all(cell in (None, '') for cell in row):
            continue
        if len(records) >= limit:
            raise ValueError(f'表格超过 {limit} 行，请拆分文件或提高 max_rows')
        row += [None] * (len(names) - len(row))
        records.append({name: _cell(cell) for name, cell in zip(names, row)})
    return records


def table_records(rows, limit, columns=(), header_row=None):
    """Find the header: the given row number, else the first row holding every required column
    (a merged title row such as “2026年9月工资表” above it is skipped), else the first non-empty row."""
    rows, skipped, first = iter(rows), 0, None
    for number, row in enumerate(rows, 1):
        row = list(row)
        if header_row:
            if number == header_row:
                return _records(row, rows, limit)
            continue
        if not any(cell not in (None, '') for cell in row):
            continue
        names = {str(cell).strip() for cell in row if cell not in (None, '')}
        if not columns or set(columns) <= names:
            return _records(row, rows, limit)
        first = names if first is None else first
        skipped += 1
        if skipped >= 30:
            break
    if columns and skipped:
        missing = [name for name in columns if name not in first]
        raise ValueError(f"表格缺少必需列：{'、'.join(missing or columns)}（已查看前 {skipped} 个非空行；"
                         '表头不在这些行时可用 header_row 指定）')
    return []


def read_csv(path, limit, columns=(), header_row=None):
    text = decode_text(path.read_bytes())  # UTF-8, UTF-16 with BOM, or GB18030 as Excel on Chinese Windows saves it
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    return table_records(csv.reader(io.StringIO(text, newline=''), dialect), limit, columns, header_row)


def read_xlsx(path, sheet, limit, columns=(), header_row=None):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError('读取 Excel 需要 openpyxl：pip install openpyxl') from exc
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet and sheet not in book.sheetnames:
            raise ValueError(f"工作表 {sheet} 不存在，可用：{'、'.join(book.sheetnames)}")
        rows = book[sheet].iter_rows(values_only=True) if sheet else book.active.iter_rows(values_only=True)
        return table_records(rows, limit, columns, header_row)
    finally:
        book.close()


class TableReadAgent(ContractAgent):
    def execute(self, values, state):
        field = self.parameters['path_field']
        raw = values['brief'].get(field)
        if isinstance(raw, list):
            # Several files of the same layout (e.g. twelve monthly sheets) become one table.
            if not raw or not all(isinstance(item, str) and item.strip() for item in raw):
                raise ValueError(f'task_input.{field} 应填写表格文件路径')
            column, rows = self.parameters.get('source_column', '来源文件'), []
            for item in raw:
                part = self.read(item)
                rows += [{**row, column: Path(item.strip().strip('"')).name} for row in part]
                if len(rows) > self.parameters.get('max_rows', 10000):
                    raise ValueError(f"表格合计超过 {self.parameters.get('max_rows', 10000)} 行，请拆分或提高 max_rows")
            return {'rows': rows}
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f'task_input.{field} 应填写表格文件路径')
        return {'rows': self.read(raw)}

    def read(self, raw):
        path = Path(raw.strip().strip('"')).expanduser()
        if not path.is_file():
            raise ValueError(f'找不到表格文件：{path}')
        limit = self.parameters.get('max_rows', 10000)
        suffix = path.suffix.lower()
        columns, header_row = self.parameters.get('columns', []), self.parameters.get('header_row')
        if suffix == '.csv':
            rows = read_csv(path, limit, columns, header_row)
        elif suffix in ('.xlsx', '.xlsm'):
            rows = read_xlsx(path, self.parameters.get('sheet'), limit, columns, header_row)
        else:
            raise ValueError('只支持 .csv、.xlsx 文件；.xls 请先另存为 .xlsx')
        missing = [name for name in self.parameters.get('columns', []) if rows and name not in rows[0]]
        if missing or (self.parameters.get('columns') and not rows):
            raise ValueError(f"表格缺少必需列：{'、'.join(missing or self.parameters['columns'])}（{path.name}）")
        return rows


def table_of(rows, columns):
    """(header, lines) for a list of row objects; nested values are written as JSON text."""
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError('要写入表格的数据必须是对象数组，请检查 rows_path')
    columns = columns or [{'key': key} for key in dict.fromkeys(key for row in rows for key in row)]
    def cell(row, key):
        value = row.get(key)
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
    return [column.get('title', column['key']) for column in columns], \
        [[cell(row, column['key']) for column in columns] for row in rows]


def fill_sheet(sheet, header, table):
    from openpyxl.styles import Font
    sheet.append(header)
    for item in sheet[1]:
        item.font = Font(bold=True)
    for line in table:
        # 18-digit IDs keep every digit; Excel only holds 15 significant digits in a number.
        sheet.append([str(v) if isinstance(v, int) and not isinstance(v, bool) and abs(v) >= 10 ** 15 else v for v in line])
    for row in sheet.iter_rows(min_row=1):
        for item in row:
            if isinstance(item.value, str) and item.value.startswith('='):
                item.data_type = 's'  # text such as “=== 小结 ===” is not a formula
    for index, title in enumerate(header, 1):
        width = max([len(str(title))] + [len(str(line[index - 1] or '')) for line in table[:200]])
        sheet.column_dimensions[sheet.cell(1, index).column_letter].width = min(60, max(8, width * 1.6))
    sheet.freeze_panes = 'A2'


class TableWriteAgent(ContractAgent):
    def execute(self, values, state):
        if self.parameters.get('sheets'):
            return {'files': self.workbook(values, state)}
        rows = resolve_pointer(values['rows'], self.parameters.get('rows_path', ''))
        header, table = table_of(rows, self.parameters.get('columns'))
        root = Path(state['data']['run']['output_dir'])
        root.mkdir(parents=True, exist_ok=True)
        files = {}
        name = output_name(self.parameters['filename'], values['rows'], values.get('brief'))
        for fmt in self.parameters['formats']:
            path = root / f"{name}.{fmt}"
            if fmt == 'csv':
                with path.open('w', encoding='utf-8-sig', newline='') as handle:  # BOM lets Excel detect UTF-8
                    writer = csv.writer(handle)
                    writer.writerow(header)
                    writer.writerows(['' if value is None else value for value in line] for line in table)
            else:
                try:
                    from openpyxl import Workbook
                except ImportError as exc:
                    raise ValueError('导出 Excel 需要 openpyxl：pip install openpyxl') from exc
                book = Workbook()
                book.active.title = self.parameters.get('sheet', 'Sheet1')
                fill_sheet(book.active, header, table)
                book.save(path)
            files[fmt] = str(path)
        return {'files': files}

    def workbook(self, values, state):
        """One xlsx with several sheets, e.g. a payroll summary and the detail rows."""
        try:
            from openpyxl import Workbook
        except ImportError as exc:
            raise ValueError('导出 Excel 需要 openpyxl：pip install openpyxl') from exc
        book = Workbook()
        book.remove(book.active)
        for spec in self.parameters['sheets']:
            header, table = table_of(resolve_pointer(values['rows'], spec.get('rows_path', '')), spec.get('columns'))
            fill_sheet(book.create_sheet(spec['name']), header, table)
        root = Path(state['data']['run']['output_dir'])
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{output_name(self.parameters['filename'], values['rows'], values.get('brief'))}.xlsx"
        book.save(path)
        return {'xlsx': str(path)}


class KeepSecretRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never send the key to a different host or over a different scheme."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        before, after = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new is not None and (before.scheme, before.netloc) != (after.scheme, after.netloc):
            for name in [name for name in new.headers if name.lower() == 'authorization']:
                del new.headers[name]
            new.unredirected_hdrs.pop('Authorization', None)
        return new


class HttpRequestAgent(ContractAgent):
    MAX_BYTES = 5_000_000
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def execute(self, values, state):
        brief = values.get('brief') or {}
        if self.parameters.get('each_path'):
            if self.parameters['method'] != 'GET':
                stop_if_rejected(state, '接口写入')
            return {'response': self.each(values, brief)}
        if self.parameters.get('page_param'):
            return {'response': self.pages(brief)}
        if self.parameters['method'] != 'GET':
            stop_if_rejected(state, '接口写入')
        return {'response': self.send(self.address(brief), values.get('body') if 'body' in values else None)}

    def address(self, brief, item=None):
        """The URL with {task_input field}, {/field of the current item} and {env:SECRET} filled in."""
        def fill(match):
            token = match.group(1)
            value = pointer_value(item, token) if token.startswith('/') else brief.get(token)
            if value in (None, '') or isinstance(value, (dict, list)):
                where = f'当前条目的 {token}' if token.startswith('/') else f'task_input.{token}'
                raise ValueError(f'接口地址需要 {where}')
            return urllib.parse.quote(str(value), safe='')
        url = re.sub(r'\{(/[^{}]*|[a-z][a-z0-9_]*)\}', fill, self.parameters['url'])
        # Group-bot webhooks carry their key in the URL; API keys go in headers: both come from the environment.
        return re.sub(r'\{env:([A-Z][A-Z0-9_]*)\}', lambda m: urllib.parse.quote(secret_value(m.group(1)), safe=''), url)

    def headers(self):
        headers = {'Accept': 'application/json',
                   **{name: re.sub(r'\{env:([A-Z][A-Z0-9_]*)\}', lambda m: secret_value(m.group(1)), value)
                      for name, value in self.parameters.get('headers', {}).items()}}
        if self.parameters.get('auth_env'):
            headers['Authorization'] = f"{self.parameters.get('auth_scheme', 'Bearer')} {secret_value(self.parameters['auth_env'])}"
        return headers

    def send(self, url, body=None):
        headers, data = self.headers(), None
        if body is not None:
            if self.parameters.get('body_format') == 'form':
                if not isinstance(body, dict):
                    raise ValueError('表单提交的请求体必须是对象')
                data = urllib.parse.urlencode({key: '' if value is None else value if isinstance(value, (str, int, float))
                                               else json.dumps(value, ensure_ascii=False) for key, value in body.items()}).encode()
                headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=utf-8'
            else:
                data = json.dumps(body, ensure_ascii=False).encode('utf-8')
                headers['Content-Type'] = 'application/json; charset=utf-8'
        method = self.parameters['method']
        # Reading, replacing and deleting are safe to repeat; creating (POST/PATCH) is not, so it is sent once.
        attempts = 3 if method in ('GET', 'PUT', 'DELETE') else 1
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.build_opener(KeepSecretRedirect).open(request, timeout=self.parameters.get('timeout', 30)) as response:
                    status, raw = response.status, response.read(self.MAX_BYTES + 1)
                    kind = response.headers.get('Content-Type', '')
                break
            except urllib.error.HTTPError as exc:
                if exc.code in self.parameters.get('ok_statuses', []):
                    # An expected answer such as 404 “no such customer”: a result for the next step, not a failure.
                    return {'status': exc.code, 'body': parse_body(exc.read(self.MAX_BYTES + 1), exc.headers.get('Content-Type', ''))}
                if exc.code in self.RETRY_STATUSES and attempt < attempts:
                    wait = exc.headers.get('Retry-After', '')
                    time.sleep(min(float(wait), 10) if wait.isdigit() else attempt * 1.5)
                    continue
                detail = exc.read(500).decode('utf-8', 'replace')
                raise ValueError(f'接口返回 {exc.code}：{detail}') from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < attempts:
                    time.sleep(attempt * 1.5)
                    continue
                raise ValueError(f'无法连接接口 {urllib.parse.urlsplit(url).netloc}：{exc}') from exc
        if len(raw) > self.MAX_BYTES:
            raise ValueError('接口响应超过 5 MB')
        return {'status': status, 'body': parse_body(raw, kind)}

    def each(self, values, brief):
        """One request per item of a list (e.g. notify each overdue customer); one failure does not stop the rest."""
        items = resolve_pointer(values['body'], self.parameters['each_path'])
        if not isinstance(items, list):
            raise ValueError(f"each_path {self.parameters['each_path']} 不是数组")
        results = []
        for item in items[:self.parameters.get('max_items', 200)]:
            try:
                results.append(self.send(self.address(brief, item), item if self.parameters['method'] != 'GET' else None))
            except ValueError as exc:
                results.append({'status': 0, 'body': None, 'error': str(exc)[:500]})
        if items and all(result.get('error') for result in results):
            raise ValueError('所有请求都失败了：' + results[0]['error'])
        return results

    def pages(self, brief):
        """Page 1, 2, 3 ... until a page is empty; the items of every page are returned as one list."""
        collected, name = [], self.parameters['page_param']
        start = self.parameters.get('first_page', 1)
        for page in range(start, start + self.parameters.get('max_pages', 10)):
            url = self.address(brief)
            url += ('&' if urllib.parse.urlsplit(url).query else '?') + urllib.parse.urlencode({name: page})
            answer = self.send(url)
            items = pointer_value(answer['body'], self.parameters.get('items_path', ''))
            if not isinstance(items, list):
                raise ValueError(f"第 {page} 页的 {self.parameters.get('items_path') or '响应'} 不是数组，请检查 items_path")
            if not items:
                break
            collected += items
        return {'status': 200, 'body': collected}


def secret_value(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f'请先设置环境变量 {name}（接口密钥），不要把密钥写进项目文件')
    return value


def parse_body(raw, kind):
    text = raw.decode('utf-8', 'replace')
    try:
        return json.loads(text) if text.strip() and ('json' in kind or text.lstrip()[:1] in '{[') else text
    except ValueError:
        return text


def usable_key(value):
    return isinstance(value, (str, int, float)) and not isinstance(value, bool) and bool(str(value).strip())


def state_key(parameters, values, content=None):
    """Only a fixed team-wide state key; object identity comes from object.resolve."""
    if parameters.get('key_field') or parameters.get('key_path'):
        raise ValueError('固定 key 存储不能处理动态身份；key_field 应先解析对象，key_path 仅用于 object_context 回填')
    key = parameters.get('key')
    return str('default' if key is None else key).strip(), False


def saved_keys(root, limit=20):
    keys = []
    for path in sorted(root.glob('*.json')) if root.is_dir() else []:
        if path.name.endswith(('.previous.json', '.tmp')):
            continue
        try:
            keys.append(str(json.loads(path.read_text(encoding='utf-8')).get('key')))
        except (OSError, ValueError, AttributeError):
            continue
    return keys[:limit]


def state_file(key, state):
    """<project output>/state/<key>.json."""
    safe = re.sub(r'[^0-9a-z\u4e00-\u9fff_-]+', '_', key.casefold())[:48].strip('_') or 'state'
    if safe != key or safe.split('.')[0] in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(10)), *(f'lpt{i}' for i in range(10))}:
        # Windows file names ignore case and reserve device names, so distinct keys get a distinct suffix.
        safe += '-' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]
    root = Path(state['data']['run']['output_dir']).resolve().parents[1] / 'state'
    return key, root / f'{safe}.json'


class ObjectResolveAgent(ContractAgent):
    def execute(self, values, state):
        return {'object': resolve_object(self.parameters, values['brief'], state, self.name)}


class StateLoadAgent(ContractAgent):
    def execute(self, values, state):
        context = bound_object(self.parameters, values, state, create=not self.parameters.get('require_found', False))
        if context is not None:
            path = object_directory(context, state) / 'state.json'
            if self.parameters.get('key_field') and starts_over(self.parameters, values):
                return {'state': {'found': False, 'key': context['id']}}
            if not path.is_file():
                external = 'external_namespace' in json.loads((path.parent / 'object.json').read_text(encoding='utf-8'))
                if (self.parameters.get('object_context') and not context['is_new'] and not external) or self.parameters.get('require_found') and (external or not context['is_new']):
                    raise ValueError('该业务对象尚无已保存状态，请先完成新建运行')
                return {'state': {'found': False, 'key': context['id']}}
            record = json.loads(path.read_text(encoding='utf-8'))
            if self.parameters.get('value_schema'):
                self.validate(self.parameters['value_schema'], record['value'])
            return {'state': {'found': True, 'key': context['id'], 'value': record['value'], 'saved_at': record['saved_at']}}
        if self.parameters.get('key_field') or self.parameters.get('key_path'):
            state_key(self.parameters, values)  # fail before reset can hide an obsolete identity contract
        if starts_over(self.parameters, values):
            # e.g. mode=new starts over; nothing is deleted, and a new work may not have its ID yet.
            try:
                key = state_key(self.parameters, values)[0]
            except ValueError:
                key = ''
            return {'state': {'found': False, 'key': key}}
        key, _ = state_key(self.parameters, values)
        key, path = state_file(key, state)
        if not path.is_file():
            if self.parameters.get('require_found'):
                known = saved_keys(path.parent)
                raise ValueError(f'找不到“{key}”的保存记录，请检查输入的编号'
                                 + (f"；已保存的有：{'、'.join(known)}" if known else '；目前还没有任何保存的记录'))
            return {'state': {'found': False, 'key': key}}
        record = json.loads(path.read_text(encoding='utf-8'))
        if self.parameters.get('value_schema'):
            try:
                self.validate(self.parameters['value_schema'], record['value'])
            except Exception as exc:
                raise ValueError(f'保存的状态 {path} 与 {self.parameters["value_schema"]} 结构不一致：{getattr(exc, "message", exc)}') from exc
        return {'state': {'found': True, 'key': key, 'value': record['value'], 'saved_at': record['saved_at']}}


def set_pointer(value, pointer, item):
    tokens = [token.replace('~1', '/').replace('~0', '~') for token in pointer.split('/')[1:]]
    parent = value
    for token in tokens[:-1]:
        parent = parent[int(token)] if isinstance(parent, list) else parent[token]
    if isinstance(parent, list):
        parent[int(tokens[-1])] = item
    else:
        parent[tokens[-1]] = item


def starts_over(parameters, values):
    """The run asked to begin again, e.g. mode=new for a new book."""
    field = parameters.get('reset_field')
    return bool(field) and (values.get('brief') or {}).get(field) in parameters.get('reset_values', [])


class StateSaveAgent(ContractAgent):
    def execute(self, values, state):
        if self.parameters.get('key_path') and not self.parameters.get('object_context'):
            raise ValueError('key_path 仅用于 object_context 的程序 ID 回填，不能生成身份')
        context = bound_object(self.parameters, values, state)
        if context is not None:
            path = object_directory(context, state) / 'state.json'
            key, generated = context['id'], bool(self.parameters.get('object_context') and context['is_new'])
            values = {**values, 'value': json.loads(json.dumps(values['value']))}
            if self.parameters.get('key_path'):
                set_pointer(values['value'], self.parameters['key_path'], key)
                self.validate(self.parameters['value_schema'], values['value'])
        else:
            key, generated = state_key(self.parameters, values, values['value'])
            key, path = state_file(key, state)
        path.parent.mkdir(parents=True, exist_ok=True)
        run_id = state['data']['run'].get('id')
        previous = path.with_name(path.stem + '.previous.json')
        existing = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
        if generated and existing is not None and existing.get('run_id') != run_id:
            # A newly generated ID that an earlier run already saved under: never overwrite another work.
            raise ValueError(f'新生成的编号“{key}”已被以前的记录使用，为避免覆盖没有保存；请重新运行以生成新的编号')
        saved_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
        value = values['value']
        fresh = values['object']['is_new'] if self.parameters.get('object_context') else starts_over(self.parameters, values)
        if self.parameters.get('append_path'):
            value, fresh = self.appended(value, existing, previous, run_id, fresh)
        record = {'key': key, 'saved_at': saved_at, 'run_id': run_id, 'value': value, 'started_fresh': fresh}
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        if existing is not None and existing.get('run_id') != run_id:
            # One step of undo: the state before this run. A retry within the run keeps it.
            shutil.copy2(path, previous)
        temporary.replace(path)
        result = {'key': key, 'path': str(path), 'saved_at': saved_at}
        if generated:
            result['generated'] = True  # shown at the end of the run: the user needs it to continue
            if self.parameters.get('key_field'):
                result['field'] = self.parameters['key_field']
        return {'record': result}

    def appended(self, value, existing, previous, run_id, fresh):
        """The model sends only this run's new entries (e.g. this chapter's summary); earlier ones are kept here."""
        pointer = self.parameters['append_path']
        try:
            new = resolve_pointer(value, pointer)
        except (KeyError, IndexError, TypeError, ValueError):
            new = None
        if not isinstance(new, list):
            raise ValueError(f'append_path {pointer} 在要保存的数据中不是数组')
        base = None
        if not fresh and existing is not None:
            if existing.get('run_id') != run_id:
                base = existing
            elif not existing.get('started_fresh') and previous.is_file():
                base = json.loads(previous.read_text(encoding='utf-8'))  # saving again in the same run: no double append
            fresh = bool(existing.get('started_fresh')) if existing.get('run_id') == run_id else fresh
        try:
            old = resolve_pointer(base['value'], pointer) if base else []
        except (KeyError, IndexError, TypeError, ValueError):
            old = []
        merged = (old if isinstance(old, list) else []) + new
        if self.parameters.get('max_items'):
            merged = merged[-self.parameters['max_items']:]  # e.g. only the latest 30 chapter summaries
        value = json.loads(json.dumps(value))
        set_pointer(value, pointer, merged)
        return value, fresh



# ---------- web search ----------

SEARCH_PROVIDERS = {'bocha', 'brave', 'tavily', 'searxng'}


def search_settings():
    """The search service comes from the environment; projects never hold its key."""
    provider = (os.environ.get('ASTRA_SEARCH_PROVIDER') or '').strip().lower()
    if provider not in SEARCH_PROVIDERS:
        raise ValueError('请先配置搜索服务：环境变量 ASTRA_SEARCH_PROVIDER 设为 bocha（博查，中文推荐）、brave、tavily 或 '
                         'searxng（自建），并设置 ASTRA_SEARCH_KEY（searxng 改设 ASTRA_SEARCH_URL）')
    key, url = os.environ.get('ASTRA_SEARCH_KEY', ''), os.environ.get('ASTRA_SEARCH_URL', '')
    if provider != 'searxng' and not key:
        raise ValueError(f'请设置环境变量 ASTRA_SEARCH_KEY（{provider} 的 API Key）')
    if provider == 'searxng' and not url:
        raise ValueError('请设置环境变量 ASTRA_SEARCH_URL（SearXNG 地址，如 http://localhost:8080）')
    return provider, key, url.rstrip('/')


def search_once(provider, key, base, query, count, timeout):
    """[{title, url, snippet, site, date}] from one provider call."""
    def call(url, body=None, headers=None):
        data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        request = urllib.request.Request(url, data=data, method='POST' if body is not None else 'GET',
                                         headers={'Accept': 'application/json', **({'Content-Type': 'application/json'} if body else {}),
                                                  **(headers or {})})
        for attempt in (1, 2, 3):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read(5_000_001).decode('utf-8', 'replace'))
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(attempt * 1.5)
                    continue
                detail = exc.read(300).decode('utf-8', 'replace')
                raise ValueError(f'搜索服务返回 {exc.code}：{detail}') from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 3:
                    time.sleep(attempt * 1.5)
                    continue
                raise ValueError(f'连不上搜索服务：{exc}') from exc
    if provider == 'bocha':
        answer = call('https://api.bochaai.com/v1/web-search', {'query': query, 'count': count, 'summary': True},
                      {'Authorization': f'Bearer {key}'})
        items = ((answer.get('data') or {}).get('webPages') or {}).get('value') or []
        return [{'title': i.get('name', ''), 'url': i.get('url', ''), 'snippet': i.get('summary') or i.get('snippet') or '',
                 'site': i.get('siteName') or '', 'date': i.get('datePublished') or ''} for i in items]
    if provider == 'brave':
        answer = call('https://api.search.brave.com/res/v1/web/search?' + urllib.parse.urlencode({'q': query, 'count': count}),
                      headers={'X-Subscription-Token': key})
        items = (answer.get('web') or {}).get('results') or []
        return [{'title': i.get('title', ''), 'url': i.get('url', ''), 'snippet': re.sub(r'<[^>]+>', '', i.get('description') or ''),
                 'site': (i.get('profile') or {}).get('name') or '', 'date': i.get('age') or ''} for i in items]
    if provider == 'tavily':
        answer = call('https://api.tavily.com/search', {'query': query, 'max_results': count},
                      {'Authorization': f'Bearer {key}'})
        return [{'title': i.get('title', ''), 'url': i.get('url', ''), 'snippet': i.get('content') or '', 'site': '',
                 'date': i.get('published_date') or ''} for i in answer.get('results') or []]
    answer = call(f'{base}/search?' + urllib.parse.urlencode({'q': query, 'format': 'json'}))
    return [{'title': i.get('title', ''), 'url': i.get('url', ''), 'snippet': i.get('content') or '', 'site': i.get('engine') or '',
             'date': i.get('publishedDate') or ''} for i in (answer.get('results') or [])[:count]]


class WebSearchAgent(ContractAgent):
    """Searches the web for one or more queries; optionally reads the top pages in full."""

    def queries(self, values):
        brief = values.get('brief') or {}
        if self.parameters.get('query'):
            found = [filled(self.parameters['query'], {}, brief, '搜索词')]
        elif self.parameters.get('query_field'):
            found = brief.get(self.parameters['query_field'])
        else:
            found = pointer_value(values['topic'], self.parameters.get('queries_path', ''))
        found = found if isinstance(found, list) else [found]
        found = [str(item).strip() for item in found if isinstance(item, (str, int, float)) and str(item).strip()]
        if not found:
            raise ValueError('没有搜索词')
        return found[:self.parameters.get('max_queries', 5)]

    def execute(self, values, state):
        provider, key, base = search_settings()
        count, seen, results, queries = self.parameters.get('max_results', 5), set(), [], self.queries(values)
        for query in queries:
            for item in search_once(provider, key, base, query, count, self.parameters.get('timeout', 30)):
                if item['url'] and item['url'] not in seen:
                    seen.add(item['url'])
                    results.append({'query': query, **item})
        reader = WebFetchAgent.__new__(WebFetchAgent)
        reader.parameters = {'chunk_chars': self.parameters.get('chunk_chars', 6000), 'timeout': 20}
        for item in results[:self.parameters.get('fetch_pages', 0)]:
            try:
                item['text'] = reader.fetch(item['url'])['text'][:self.parameters.get('page_chars', 20000)]
            except Exception as exc:  # a broken page (bad PDF, cut-off download) must not sink the whole search
                item['error'] = f'{type(exc).__name__}: {exc}'[:300]
        parts = [f"【{item['title'] or item['url']}】{item['url']}\n{item.get('text') or item['snippet']}" for item in results]
        chunks = []
        for item in results:
            chunks += [{'text': chunk['text'], 'url': item['url']} for chunk in
                       split_chunks(item.get('text') or item['snippet'], self.parameters.get('chunk_chars', 6000))]
        return {'results': {'queries': queries, 'items': results, 'text': '\n\n'.join(parts),
                            'chunks': [{**chunk, 'index': number} for number, chunk in enumerate(chunks, 1)]}}


def stop_if_rejected(state, what):
    """An outward action never goes ahead after a person's latest review in this run said no."""
    for value in (state.get('data') or {}).values():
        if isinstance(value, dict) and set(value) == {'approved', 'notes', 'round'} and value['approved'] is False:
            raise ValueError(f"人工审阅未通过（第 {value['round']} 轮：{value['notes'] or '无意见'}），不执行{what}")


# ---------- e-mail ----------

EMAIL = re.compile(r'^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$')


def smtp_settings():
    """The mail server comes from the environment, never from project files."""
    host = os.environ.get('ASTRA_SMTP_HOST')
    if not host:
        raise ValueError('请先设置发信服务器：环境变量 ASTRA_SMTP_HOST、ASTRA_SMTP_PORT、ASTRA_SMTP_USER、'
                         'ASTRA_SMTP_PASSWORD（授权码），可选 ASTRA_SMTP_FROM、ASTRA_SMTP_SECURITY（ssl/starttls/none）')
    port = int(os.environ.get('ASTRA_SMTP_PORT', '465'))
    security = (os.environ.get('ASTRA_SMTP_SECURITY') or ('ssl' if port == 465 else 'starttls' if port == 587 else 'none')).strip().lower()
    if security not in ('ssl', 'starttls', 'none'):
        raise ValueError('ASTRA_SMTP_SECURITY 只能是 ssl、starttls 或 none')
    user = os.environ.get('ASTRA_SMTP_USER', '')
    return {'host': host, 'port': port, 'security': security, 'user': user,
            'password': os.environ.get('ASTRA_SMTP_PASSWORD', ''), 'sender': os.environ.get('ASTRA_SMTP_FROM') or user}


class EmailSendAgent(ContractAgent):
    """Sends one message: body from upstream text, optional attachments from files written earlier in the run."""
    MAX_ATTACHMENTS = 20 * 1024 * 1024

    def recipients(self, brief, key, field):
        given = list(self.parameters.get(key) or [])
        if self.parameters.get(field):
            value = brief.get(self.parameters[field])
            given += value if isinstance(value, list) else re.split(r'[,，;；\s]+', str(value or ''))
        given = [item.strip() for item in given if str(item).strip()]
        bad = [item for item in given if not EMAIL.match(item)]
        if bad:
            raise ValueError(f"收件地址格式不对：{'、'.join(bad)}")
        return given

    def execute(self, values, state):
        import smtplib
        import email.utils
        from email.message import EmailMessage
        stop_if_rejected(state, '邮件')
        brief = values.get('brief') or {}
        to = self.recipients(brief, 'to', 'to_field')
        if not to:
            raise ValueError('没有收件人')
        cc = self.recipients(brief, 'cc', 'cc_field')
        body = pointer_value(values['content'], self.parameters.get('text_path', ''))
        if not isinstance(body, str) or not body.strip():
            raise ValueError('邮件正文为空，请检查 text_path')
        subject = re.sub(r'[\r\n]+', ' ', filled(self.parameters['subject'], values['content'], brief, '邮件主题'))
        settings = smtp_settings()
        message = EmailMessage()
        message['Subject'], message['From'], message['To'] = subject, settings['sender'], ', '.join(to)
        if cc:
            message['Cc'] = ', '.join(cc)
        message['Date'] = email.utils.formatdate(localtime=True)
        message['Message-ID'] = email.utils.make_msgid()
        message.set_content(body)
        names, total = [], 0
        for path in self.attachment_paths(values.get('attachments')):
            data = Path(path).read_bytes()
            total += len(data)
            if total > self.MAX_ATTACHMENTS:
                raise ValueError('附件合计超过 20 MB，请改为发送链接或拆分')
            import mimetypes
            kind = (mimetypes.guess_type(path)[0] or 'application/octet-stream').split('/')
            message.add_attachment(data, maintype=kind[0], subtype=kind[1], filename=Path(path).name)
            names.append(Path(path).name)
        client = smtplib.SMTP_SSL if settings['security'] == 'ssl' else smtplib.SMTP
        try:
            with client(settings['host'], settings['port'], timeout=self.parameters.get('timeout', 30)) as server:
                if settings['security'] == 'starttls':
                    server.starttls()
                if settings['user']:
                    server.login(settings['user'], settings['password'])
                server.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise ValueError(f"邮件没有发出：{exc}（服务器 {settings['host']}:{settings['port']}）") from exc
        return {'sent': {'to': to, 'cc': cc, 'subject': subject, 'attachments': names, 'message_id': message['Message-ID'],
                         'sent_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}}

    @staticmethod
    def attachment_paths(files):
        paths = []
        for key, value in (files or {}).items():
            if key == 'details':
                continue  # image.generate / image.edit: the pictures are under "image", details only describe them
            paths += value if isinstance(value, list) else [value]
        missing = [str(path) for path in paths if not (isinstance(path, str) and Path(path).is_file())]
        if missing:
            raise ValueError(f"附件不存在：{'、'.join(missing)}；邮件没有发出")
        return paths


# ---------- people in the loop ----------

class HumanReviewAgent(ContractAgent):
    """Stops the run and shows the work; “通过” approves, anything else comes back as revision notes.

    Each pass asks again, so inside a redo loop the reviewer sees every revised draft.
    """

    def execute(self, values, state):
        passes = sum(1 for record in state.get('executions') or []
                     if record.get('role') == self.name and record.get('status') == 'success')
        token = f'{self.name}#{passes + 1}'
        answer = (state.get('review_answers') or {}).get(token)
        if answer is None:
            from astra_core.runtime.pause import WorkflowPause
            images = self.images(values['content'])
            shown = pointer_value(values['content'], self.parameters.get('text_path', ''))
            if images and not self.parameters.get('text_path') and isinstance(values['content'], dict) \
                    and isinstance(values['content'].get('details'), list):
                # Pictures from image.generate / image.edit: list each file with the prompt that made it.
                text = '\n'.join(f"{number}. {item.get('path')}\n   {item.get('prompt', '')}"
                                 for number, item in enumerate(values['content']['details'], 1) if isinstance(item, dict))
            else:
                text = shown if isinstance(shown, str) else json.dumps(shown, ensure_ascii=False, indent=2)
            root = Path(state['data']['run']['output_dir'])
            root.mkdir(parents=True, exist_ok=True)
            full = root / f'review_{self.name}_{passes + 1}.md'
            full.write_text(text, encoding='utf-8')  # the whole draft, for reading in an editor
            limit = self.parameters.get('excerpt_chars', 1500)
            raise WorkflowPause('awaiting_review', input_errors=[], questions=[], pending_review={
                'agent': self.name, 'token': token, 'round': passes + 1,
                'title': self.parameters.get('title') or '请审阅',
                'excerpt': text[:limit] + ('……' if len(text) > limit else ''), 'file': str(full), 'images': images})
        return {'review': {'approved': answer['approved'], 'notes': answer.get('notes', ''), 'round': passes + 1}}

    def images(self, content):
        """Picture files to show: images_path, or the pictures of an image.generate / image.edit result."""
        path = self.parameters.get('images_path')
        if path is None and isinstance(content, dict) and isinstance(content.get('details'), list):
            path = '/image'
        found = pointer_value(content, path) if path is not None else None
        found = found if isinstance(found, list) else [found]
        return [item for item in found if isinstance(item, str) and Path(item).is_file()]


# ---------- images: a self-hosted image model behind an OpenAI-style images API ----------

# Qwen-Image-2.1's recommended 2K sizes; the blueprint picks a ratio, never raw pixels.
ASPECT_SIZES = {'1:1': (2048, 2048), '4:3': (2400, 1792), '3:4': (1792, 2400), '3:2': (2528, 1696),
                '2:3': (1696, 2528), '16:9': (2752, 1536), '9:16': (1536, 2752)}
TRANSPARENT_PROMPT = ('This is an RGBA image with transparency. {prompt}. '
                      'The image has alpha channel and the background is transparent.')
REFERENCE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.webp')
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_RESPONSE = 512 * 1024 * 1024


class ImageServiceUnavailable(ValueError):
    pass


def image_size(ratio, resolution='2k'):
    width, height = ASPECT_SIZES[ratio]
    if resolution == '1k':  # quicker drafts: half of each side, kept a multiple of 32 as the model expects
        width, height = int(width / 64 + 0.5) * 32, int(height / 64 + 0.5) * 32
    return f'{width}x{height}'


def image_settings():
    """The image service comes from the environment; projects never hold its address or key."""
    url = (os.environ.get('ASTRA_IMAGE_URL') or '').strip().rstrip('/')
    if not url:
        raise ImageServiceUnavailable('绘图服务还没有配置：请设置环境变量 ASTRA_IMAGE_URL（你部署的 OpenAI 兼容图片接口地址，'
                                      '如 http://192.168.1.20:8000/v1），需要时再设 ASTRA_IMAGE_MODEL、ASTRA_IMAGE_KEY')
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise ValueError(f'ASTRA_IMAGE_URL 不是有效地址：{url}')
    if parts.path in ('', '/'):
        url += '/v1'  # only the host was given
    try:
        extra = json.loads(os.environ.get('ASTRA_IMAGE_EXTRA') or '{}')
    except json.JSONDecodeError as exc:
        raise ValueError(f'ASTRA_IMAGE_EXTRA 不是有效的 JSON：{exc}') from exc
    if not isinstance(extra, dict):
        raise ValueError('ASTRA_IMAGE_EXTRA 必须是 JSON 对象，如 {"num_inference_steps": 30}')
    def number(name, default, low, high, kind):
        raw = os.environ.get(name)
        try:
            value = kind(raw) if raw not in (None, '') else default
        except ValueError:
            raise ValueError(f'{name} 必须是数字') from None
        if not low <= value <= high:
            raise ValueError(f'{name} 取 {low}–{high}')
        return value
    return {'url': url, 'model': (os.environ.get('ASTRA_IMAGE_MODEL') or '').strip() or None,
            'key': os.environ.get('ASTRA_IMAGE_KEY', ''), 'extra': extra,
            'timeout': number('ASTRA_IMAGE_TIMEOUT', 600, 10, 7200, float),
            'workers': number('ASTRA_IMAGE_CONCURRENCY', 1, 1, 16, int),
            'max_references': number('ASTRA_IMAGE_MAX_REFERENCES', 4, 1, 16, int),
            # vLLM-Omni repeats "image" for several pictures; OpenAI's own API wants "image[]".
            'edit_field': (os.environ.get('ASTRA_IMAGE_EDIT_FIELD') or 'image').strip(),
            'proxy': os.environ.get('ASTRA_IMAGE_USE_PROXY', '') == '1'}


def image_opener(settings):
    """A self-hosted model is usually on the local network: connect directly unless told to use the proxy."""
    handlers = [] if settings['proxy'] else [urllib.request.ProxyHandler({})]
    return urllib.request.build_opener(*handlers)


def reachable(settings):
    """A quick TCP check, so a machine that is off pauses the run at once instead of after the full timeout."""
    import socket
    if settings['proxy']:
        return
    parts = urllib.parse.urlsplit(settings['url'])
    try:
        socket.create_connection((parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)), timeout=10).close()
    except OSError as exc:
        raise ImageServiceUnavailable(f"连不上绘图服务 {settings['url']}：{exc}。请确认服务已启动、地址和端口正确") from exc


def same_origin(link, base):
    one, two = urllib.parse.urlsplit(link), urllib.parse.urlsplit(base)
    return (one.scheme, one.netloc) == (two.scheme, two.netloc)


def multipart(fields, files):
    """multipart/form-data for /images/edits: plain fields plus (field name, file path) pairs."""
    import mimetypes
    import uuid
    boundary = 'astra' + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode('utf-8'),
                   str(value).encode('utf-8'), b'\r\n']
    for name, path in files:
        kind = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
        filename = Path(path).name.replace('"', '_')
        chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                   f'Content-Type: {kind}\r\n\r\n'.encode('utf-8'), Path(path).read_bytes(), b'\r\n']
    chunks.append(f'--{boundary}--\r\n'.encode('utf-8'))
    return b''.join(chunks), f'multipart/form-data; boundary={boundary}'


def image_call(settings, path, body=None, files=None):
    """One POST to the image service. A busy service is asked again; a request the server may already be
    working on (timed out, or cut off after it was sent) is not, so the GPU never renders it twice."""
    import http.client
    headers = {'Accept': 'application/json'}
    if files is None:
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    else:
        data, headers['Content-Type'] = multipart(body, files)
    request = urllib.request.Request(settings['url'] + path, data=data, method='POST', headers=headers)
    if settings['key']:
        request.add_unredirected_header('Authorization', f"Bearer {settings['key']}")  # never follows a redirect
    slow = f"绘图服务 {settings['timeout']:g} 秒内没有返回，可调大 ASTRA_IMAGE_TIMEOUT"
    for attempt in (1, 2, 3):
        try:
            with image_opener(settings).open(request, timeout=settings['timeout']) as response:
                raw = response.read(MAX_IMAGE_RESPONSE + 1)
            if len(raw) > MAX_IMAGE_RESPONSE:
                raise ValueError('绘图服务的返回超过 512 MB，请减少每次生成的张数')
            try:
                return json.loads(raw.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError(f"绘图服务没有返回 JSON（{settings['url'] + path}），请确认它提供 OpenAI 兼容的图片接口") from None
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode('utf-8', 'replace')
            exc.close()
            if exc.code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(attempt * 5)
                continue
            if exc.code in (502, 503, 504):
                # A gateway in front of a model server that is down or still loading.
                raise ImageServiceUnavailable(f"绘图服务暂时不可用（{exc.code}）：{detail}") from exc
            if exc.code == 404:
                raise ValueError(f"绘图服务没有这个接口（404）：{settings['url'] + path}；ASTRA_IMAGE_URL 应填到 /v1 为止") from exc
            raise ValueError(f'绘图服务返回 {exc.code}：{detail}') from exc
        except TimeoutError as exc:
            raise ValueError(slow) from exc
        except (ConnectionResetError, http.client.RemoteDisconnected) as exc:
            raise ValueError(f'绘图服务在生成过程中断开了连接（{exc}），请查看服务日志（常见原因是显存不足）') from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ValueError(slow) from exc
            if isinstance(exc.reason, (ConnectionResetError, http.client.RemoteDisconnected)):
                raise ValueError(f'绘图服务在生成过程中断开了连接（{exc.reason}），请查看服务日志（常见原因是显存不足）') from exc
            if attempt < 3:
                time.sleep(attempt * 2)
                continue
            raise ImageServiceUnavailable(f"连不上绘图服务 {settings['url']}：{exc.reason}。请确认服务已启动、地址和端口正确") from exc


def image_bytes(item, settings):
    """The picture in one answer entry: base64, a data: URL, or an http(s) link to download."""
    import base64
    try:
        if item.get('b64_json'):
            return base64.b64decode(item['b64_json'], validate=False)
        link = item.get('url') or ''
        if link.startswith('data:'):
            return base64.b64decode(link.split(',', 1)[1])
    except (ValueError, IndexError) as exc:
        raise ValueError(f'绘图服务返回的图片数据无法解码：{exc}') from exc
    if not link:
        raise ValueError('绘图服务的返回中没有图片（每项应有 b64_json 或 url）')
    link = urllib.parse.urljoin(settings['url'] + '/', link)
    if urllib.parse.urlsplit(link).scheme not in ('http', 'https'):
        raise ValueError(f'图片链接只能是 http 或 https：{link}')
    request = urllib.request.Request(link)
    if settings['key'] and same_origin(link, settings['url']):
        request.add_unredirected_header('Authorization', f"Bearer {settings['key']}")  # the key never goes elsewhere
    try:
        with image_opener(settings).open(request, timeout=120) as response:
            return response.read(MAX_IMAGE_RESPONSE)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError(f'下载生成的图片失败：{link}：{exc}') from exc


def image_suffix(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if data[:3] == b'\xff\xd8\xff':
        return '.jpg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return '.webp'
    raise ValueError('绘图服务返回的不是 png、jpg 或 webp 图片')


class ImageGenerateAgent(ContractAgent):
    """Text to image through the self-hosted model: one request per prompt, pictures saved in the run folder.

    Prompts come from a fixed template, a task_input field, or upstream data (one prompt, a list of prompts,
    or a list of objects such as one scene per chapter).
    """
    endpoint = '/images/generations'
    default_name = 'image'

    def prompts(self, values):
        """[(prompt, data for {/pointer} file names, aspect ratio)]."""
        p, brief = self.parameters, values.get('brief') or {}
        if p.get('prompt'):
            found = filled(p['prompt'], {}, brief, '绘图提示词')
        elif p.get('prompt_field'):
            found = brief.get(p['prompt_field'])
        else:
            found = pointer_value(values.get('content'), p.get('prompt_path', ''))
        found = found if isinstance(found, list) else [found]
        key, ratio_key, result = p.get('prompt_key', 'prompt'), p.get('aspect_key'), []
        for number, entry in enumerate(found, 1):
            data = entry if isinstance(entry, dict) else {}
            text = entry.get(key) if isinstance(entry, dict) else entry
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f'第 {number} 项没有绘图提示词' + (f'（字段 {key}）' if isinstance(entry, dict) else ''))
            ratio = data.get(ratio_key) if ratio_key else None
            ratio = ratio if ratio in ASPECT_SIZES else p.get('aspect_ratio')
            result.append((text.strip(), data, ratio))
        return result

    def final_prompt(self, text):
        style = (self.parameters.get('style') or '').strip()
        if style:
            text = f"{text.rstrip(' .。')}. {style}"
        if self.parameters.get('transparent'):
            text = TRANSPARENT_PROMPT.format(prompt=text.rstrip(' .。'))
        return text

    def body(self, settings, prompt, ratio, count, seed):
        p = self.parameters
        body = {'model': settings['model'], 'prompt': prompt, 'n': count, 'response_format': 'b64_json',
                'size': image_size(ratio, p.get('resolution', '2k')) if ratio else None,
                'negative_prompt': p.get('negative_prompt'), 'seed': seed}
        body.update(settings['extra'])  # e.g. {"num_inference_steps": 30}; a null drops a field the server rejects
        return {key: value for key, value in body.items() if value is not None}

    def jobs(self, values):
        """[{prompt, data, ratio, sources}] — one request each."""
        return [{'prompt': prompt, 'data': data, 'ratio': ratio or self.parameters.get('aspect_ratio', '1:1'), 'sources': []}
                for prompt, data, ratio in self.prompts(values)]

    def request(self, settings, job, count, seed):
        return image_call(settings, self.endpoint, self.body(settings, job['final'], job['ratio'], count, seed))

    def ask(self, settings, job, count, seed):
        answer = self.request(settings, job, count, seed)
        entries = answer.get('data') if isinstance(answer, dict) else None
        if not isinstance(entries, list):
            raise ValueError('绘图服务的返回格式不对：缺少 data 列表')
        return [(image_bytes(entry, settings), entry.get('revised_prompt'), seed)
                for entry in entries[:count] if isinstance(entry, dict)]

    def render(self, settings, job):
        """[(picture, revised prompt, seed)] for one job.

        With a fixed seed every picture is its own request (seed, seed+1, ...), so each one can be made again
        exactly; otherwise one request asks for all, and if the server gives fewer, the rest one at a time.
        """
        count, seed = self.parameters.get('count', 1), self.parameters.get('seed')
        if seed is not None:
            pictures = [picture for k in range(count) for picture in self.ask(settings, job, 1, (seed + k) % 2 ** 32)]
        else:
            pictures = self.ask(settings, job, count, None)
            while pictures and len(pictures) < count:
                more = self.ask(settings, job, 1, None)
                if not more:
                    break
                pictures += more
        if not pictures:
            raise ValueError('绘图服务没有返回图片')
        return pictures

    def execute(self, values, state):
        try:
            return self.produce(values, state)
        except ImageServiceUnavailable as exc:
            from astra_core.runtime.pause import WorkflowPause
            raise WorkflowPause('waiting_dependencies', dependency_message=(
                f'前面的步骤已完成并保存。“{self.name}”需要绘图服务，运行在此暂停。\n{exc}\n'
                '配置方法见 docs/通用能力配置与评测.md 的“绘图与改图”一节。')) from exc

    def produce(self, values, state):
        from concurrent.futures import ThreadPoolExecutor
        p, brief = self.parameters, values.get('brief') or {}
        settings = image_settings()
        jobs = self.jobs(values)
        total = len(jobs) * p.get('count', 1)
        if total > p.get('max_images', 20):
            raise ValueError(f"本次要生成 {total} 张图，超过上限 max_images={p.get('max_images', 20)}")
        folder = Path(state['data']['run']['output_dir']) / 'images' / self.name  # each step its own folder
        folder.mkdir(parents=True, exist_ok=True)
        taken = set()
        for number, job in enumerate(jobs, 1):
            job['final'] = self.final_prompt(job['prompt'])
            stem = output_name(p.get('filename', self.default_name), job['data'], brief)
            stem = job.get('prefix', '') + stem
            if len(jobs) > 1 and not re.search(r'\{/', p.get('filename', '')) and not job.get('prefix'):
                stem = f'{stem}_{number}'
            while stem.casefold() in taken:
                stem += f'_{number}'
            taken.add(stem.casefold())
            job['stem'], job['number'] = stem, number
        reachable(settings)
        with ThreadPoolExecutor(max_workers=settings['workers']) as pool:
            rendered = list(pool.map(lambda job: self.render(settings, job), jobs))
        paths, details = [], []
        for job, pictures in zip(jobs, rendered):
            for index, (data, revised, seed) in enumerate(pictures, 1):
                name = job['stem'] + (f'_{index}' if len(pictures) > 1 else '')
                path = folder / (name + image_suffix(data))
                path.write_bytes(data)
                record = {'path': str(path), 'prompt': job['final'], 'item': job['number'],
                          'size': image_size(job['ratio'], p.get('resolution', '2k')) if job['ratio'] else 'original'}
                if seed is not None:
                    record['seed'] = seed
                if job['sources']:
                    record['sources'] = [str(source) for source in job['sources']]
                if isinstance(revised, str) and revised.strip():
                    record['revised_prompt'] = revised
                paths.append(str(path))
                details.append(record)
        (folder / 'images.json').write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'images': {'image': paths, 'details': details}}


class ImageEditAgent(ImageGenerateAgent):
    """Edits or recomposes pictures with the same model: photos given for this run or pictures made earlier.

    each_image=true edits every picture on its own (batch background swaps); otherwise all pictures are
    references for one new picture (e.g. a product plus a scene; ASTRA_IMAGE_MAX_REFERENCES, default 4).
    """
    endpoint = '/images/edits'
    default_name = 'edited'

    def references(self, values):
        p = self.parameters
        if p.get('image_field'):
            found = (values.get('brief') or {}).get(p['image_field'])
        else:
            found = (values.get('images') or {}).get('image')
        found = found if isinstance(found, list) else [found]
        paths = [Path(str(item).strip()).expanduser() for item in found if isinstance(item, str) and item.strip()]
        if not paths:
            raise ValueError('没有要编辑的图片')
        for path in paths:
            if not path.is_file():
                raise ValueError(f'找不到图片：{path}')
            if path.suffix.lower() not in REFERENCE_SUFFIXES:
                raise ValueError(f"只能编辑 png、jpg、webp 图片：{path.name}")
            if path.stat().st_size > MAX_REFERENCE_BYTES:
                raise ValueError(f'图片超过 20 MB：{path.name}')
        return paths

    def jobs(self, values):
        prompts, sources = self.prompts(values), self.references(values)
        if not self.parameters.get('each_image'):
            limit = image_settings()['max_references']
            if len(sources) > limit:
                raise ValueError(f'一次最多参考 {limit} 张图片（ASTRA_IMAGE_MAX_REFERENCES），本次有 {len(sources)} 张；'
                                 '逐张编辑请设 each_image=true')
            return [{'prompt': prompt, 'data': data, 'ratio': ratio, 'sources': sources} for prompt, data, ratio in prompts]
        if len(prompts) not in (1, len(sources)):
            raise ValueError(f'逐张编辑时提示词要么一条（所有图片同样处理），要么与图片一一对应；现在 {len(prompts)} 条提示词、{len(sources)} 张图片')
        pairs = zip(prompts * len(sources) if len(prompts) == 1 else prompts, sources)
        return [{'prompt': prompt, 'data': data, 'ratio': ratio, 'sources': [source], 'prefix': source.stem + '_'}
                for (prompt, data, ratio), source in pairs]

    def request(self, settings, job, count, seed):
        fields = {key: (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value)
                  for key, value in self.body(settings, job['final'], job['ratio'], count, seed).items()}
        return image_call(settings, self.endpoint, fields, [(settings['edit_field'], source) for source in job['sources']])


# ---------- text sources: documents and web pages ----------

def decode_text(raw):
    for bom, encoding in ((b'\xff\xfe\x00\x00', 'utf-32'), (b'\x00\x00\xfe\xff', 'utf-32'),
                          (b'\xff\xfe', 'utf-16'), (b'\xfe\xff', 'utf-16')):
        if raw.startswith(bom):
            return raw.decode(encoding)
    if b'\x00' in raw[:4096]:
        raise ValueError('文件像是 UTF-16 编码但没有 BOM，请另存为 UTF-8')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


class _HtmlText(html.parser.HTMLParser):
    # Page chrome, not content. <form> is not here: ASP.NET pages wrap the whole body in one.
    SKIP = {'script', 'style', 'noscript', 'nav', 'footer', 'svg', 'aside', 'iframe', 'button', 'select', 'textarea'}
    BLOCK = {'p', 'div', 'br', 'li', 'tr', 'section', 'article', 'blockquote', 'pre', 'table', 'ul', 'ol', 'dd', 'dt',
             'header', 'main'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.skipping, self.in_title = [], '', 0, False
        self.content, self.headers, self.headings = 0, [], []

    def handle_starttag(self, tag, attrs):
        if tag in ('article', 'main'):
            self.content += 1
        if tag == 'header':
            # A site banner is chrome; an article's own header holds its headline.
            chrome = not self.content
            self.headers.append(chrome)
            if chrome:
                self.skipping += 1
                return
        if tag in self.SKIP:
            self.skipping += 1
        elif tag == 'title' and not self.skipping and not self.title:
            self.in_title = True
        elif re.fullmatch(r'h[1-6]', tag):
            self.headings.append(len(self.parts))
            self.parts.append('\n\n' + '#' * min(int(tag[1]), 3) + ' ')
        elif tag in self.BLOCK:
            self.parts.append('\n')
        elif tag in ('td', 'th'):
            self.parts.append(' | ')

    def handle_endtag(self, tag):
        if tag in ('article', 'main'):
            self.content = max(0, self.content - 1)
        if tag == 'header' and self.headers:
            if self.headers.pop():
                self.skipping = max(0, self.skipping - 1)
            return
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag == 'title':
            self.in_title = False
        elif re.fullmatch(r'h[1-6]', tag) and self.headings:
            start = self.headings.pop()
            if not ''.join(self.parts[start + 1:]).strip():
                del self.parts[start:]  # an empty heading (an icon) leaves no stray '#'
            self.parts.append('\n')
        elif tag in self.BLOCK:
            self.parts.append('\n')

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skipping:
            self.parts.append(data)

    def text(self):
        lines = [re.sub(r'[ \t　\xa0]+', ' ', line).strip() for line in ''.join(self.parts).splitlines()]
        return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


def html_to_text(markup):
    parser = _HtmlText()
    parser.feed(markup)
    parser.close()
    return parser.title.strip(), parser.text()


def split_chunks(text, size):
    """Paragraph-aligned chunks of at most `size` characters, for llm.map@1."""
    chunks, current = [], ''
    for paragraph in re.split(r'\n\s*\n', text):
        paragraph = paragraph.strip()
        while len(paragraph) > size:
            if current:
                chunks.append(current)
                current = ''
            chunks.append(paragraph[:size])
            paragraph = paragraph[size:]
        if paragraph and current and len(current) + len(paragraph) + 2 > size:
            chunks.append(current)
            current = paragraph
        elif paragraph:
            current = f'{current}\n\n{paragraph}' if current else paragraph
    if current:
        chunks.append(current)
    return [{'index': index, 'text': chunk} for index, chunk in enumerate(chunks, 1)]


W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


CHINESE_DIGITS = '零一二三四五六七八九'


def chinese_number(n):
    if n < 10:
        return CHINESE_DIGITS[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return (CHINESE_DIGITS[tens] if tens > 1 else '') + '十' + (CHINESE_DIGITS[ones] if ones else '')
    return str(n)


def number_text(n, fmt):
    if fmt == 'bullet':
        return '•'
    if fmt in ('chineseCounting', 'chineseCountingThousand', 'ideographTraditional', 'japaneseCounting',
               'ideographDigital', 'taiwaneseCounting', 'taiwaneseCountingThousand'):
        return chinese_number(n)
    if fmt in ('lowerLetter', 'upperLetter'):
        letter = chr(ord('a') + (n - 1) % 26)
        return letter.upper() if fmt == 'upperLetter' else letter
    if fmt in ('lowerRoman', 'upperRoman'):
        values = ((10, 'x'), (9, 'ix'), (5, 'v'), (4, 'iv'), (1, 'i'))
        text, rest = '', n
        for value, symbol in values:
            while rest >= value:
                text, rest = text + symbol, rest - value
        return text.upper() if fmt == 'upperRoman' else text
    if fmt == 'decimalEnclosedCircle' and n <= 20:
        return chr(0x2460 + n - 1)
    return str(n)


def docx_numbering(archive):
    """numId -> {level: (format, text pattern such as '第%1条' or '%1.%2')} from word/numbering.xml."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(archive.read('word/numbering.xml'))
    except KeyError:
        return {}
    abstract = {}
    for node in root.findall(f'{W}abstractNum'):
        levels = {}
        for level in node.findall(f'{W}lvl'):
            fmt, text = level.find(f'{W}numFmt'), level.find(f'{W}lvlText')
            levels[int(level.get(f'{W}ilvl', 0))] = (fmt.get(f'{W}val') if fmt is not None else 'decimal',
                                                     text.get(f'{W}val') if text is not None else '%1.')
        abstract[node.get(f'{W}abstractNumId')] = levels
    result = {}
    for node in root.findall(f'{W}num'):
        link = node.find(f'{W}abstractNumId')
        if link is not None:
            result[node.get(f'{W}numId')] = abstract.get(link.get(f'{W}val'), {})
    return result


def read_docx(path):
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as archive:
        body = ET.fromstring(archive.read('word/document.xml')).find(f'{W}body')
        numbering = docx_numbering(archive)
    counters = {}
    def marker(node):
        """Word's automatic numbering (“第3条”, “1.2”) is not in the text; rebuild it."""
        props = node.find(f'{W}pPr/{W}numPr')
        if props is None:
            return ''
        num, level = props.find(f'{W}numId'), props.find(f'{W}ilvl')
        num = num.get(f'{W}val') if num is not None else None
        level = int(level.get(f'{W}val')) if level is not None else 0
        levels = numbering.get(num)
        if not levels or num == '0':
            return ''
        count = counters.setdefault(num, {})
        count[level] = count.get(level, 0) + 1
        for deeper in [key for key in count if key > level]:
            del count[deeper]
        fmt, pattern = levels.get(level, ('decimal', f'%{level + 1}.'))
        if fmt == 'bullet':
            return '• '
        def fill(match):
            index = int(match.group(1)) - 1
            return number_text(count.get(index, 1), levels.get(index, ('decimal', ''))[0])
        return re.sub(r'%(\d)', fill, pattern) + ' '
    def run_text(node):
        parts = []
        for item in node.iter():
            if item.tag == f'{W}t':
                parts.append(item.text or '')
            elif item.tag == f'{W}tab':
                parts.append('\t')
            elif item.tag in (f'{W}br', f'{W}cr'):
                parts.append('\n')
        return ''.join(parts)
    def paragraph(node):
        text = run_text(node)
        text = marker(node) + text if text.strip() else text
        style = node.find(f'{W}pPr/{W}pStyle')
        name = (style.get(f'{W}val') if style is not None else '') or ''
        # English Word uses Heading1..; Chinese Word and WPS often use the ids 1, 2, 3.
        level = re.fullmatch(r'(?:[Hh]eading|标题)\s*(\d)|(\d)', name)
        if text.strip() and (level or name == 'Title'):
            depth = int(level.group(1) or level.group(2)) if level else 1
            return '#' * min(depth, 3) + ' ' + text.strip()
        return text
    lines = []
    def walk(container):
        for child in container:
            if child.tag == f'{W}p':
                lines.append(paragraph(child))
            elif child.tag == f'{W}tbl':
                for row in child.findall(f'{W}tr'):  # direct rows only: a nested table is read once, inside its cell
                    cells = [re.sub(r'\s+', ' ', run_text(cell)).strip() for cell in row.findall(f'{W}tc')]
                    lines.append(' | '.join(cells))
            elif child.tag == f'{W}sdt':  # content controls wrap ordinary paragraphs
                content = child.find(f'{W}sdtContent')
                if content is not None:
                    walk(content)
    walk(body)
    return '\n\n'.join(line for line in lines if line.strip()), None


OCR_HINT = '安装文字识别组件后可自动识别：pip install rapidocr_onnxruntime pypdfium2'
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp')
_OCR = []


def ocr_engine():
    """RapidOCR, a local Chinese/English OCR, when installed; None otherwise."""
    if not _OCR:
        engine = None
        for module in ('rapidocr_onnxruntime', 'rapidocr'):
            try:
                engine = __import__(module, fromlist=['RapidOCR']).RapidOCR()
                break
            except ImportError:
                continue
        _OCR.append(engine)
    return _OCR[0]


def ocr_image(engine, image):
    """Recognised lines of one image (a path or PNG bytes), in reading order."""
    output = engine(image)
    if hasattr(output, 'txts'):  # rapidocr 2.x
        return '\n'.join(output.txts or [])
    result = output[0] if isinstance(output, tuple) else output  # rapidocr_onnxruntime 1.x: [box, text, score]
    return '\n'.join(line[1] for line in result or [])


def pdf_page_images(path, numbers):
    """PNG bytes of the given PDF pages, rendered at twice the normal size for better recognition."""
    try:
        import pypdfium2
    except ImportError as exc:
        raise ValueError(f'{path.name} 有扫描页，识别需要 pypdfium2：pip install pypdfium2') from exc
    document = pypdfium2.PdfDocument(str(path))
    try:
        for number in numbers:
            buffer = io.BytesIO()
            document[number].render(scale=2).to_pil().save(buffer, format='PNG')
            yield number, buffer.getvalue()
    finally:
        document.close()


def read_pdf(path, ocr=True, ocr_pages=50):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError('读取 PDF 需要 pypdf：pip install pypdf') from exc
    reader = PdfReader(str(path))
    pages = [(page.extract_text() or '').strip() for page in reader.pages]
    blank = [number for number, text in enumerate(pages) if not text]
    engine = ocr_engine() if blank and ocr else None
    if blank and engine is not None:
        # Scanned pages carry no text layer: recognise them (only those, up to a limit).
        try:
            for number, image in pdf_page_images(path, blank[:ocr_pages]):
                pages[number] = ocr_image(engine, image).strip()
        except Exception:
            if not any(pages):
                raise  # nothing readable at all: say why
            # Otherwise a blank page in a text PDF: keep what was read.
    if not any(pages):
        raise ValueError(f'{path.name} 没有可提取的文字，可能是扫描件；' + (OCR_HINT if ocr else '本步骤关闭了文字识别'))
    return '\n\n'.join(page for page in pages if page), len(pages)


def read_image(path):
    engine = ocr_engine()
    if engine is None:
        raise ValueError(f'{path.name} 是图片，需要文字识别；{OCR_HINT}')
    text = ocr_image(engine, str(path)).strip()
    if not text:
        raise ValueError(f'{path.name} 中没有识别出文字')
    return text, None


def read_document(path, ocr=True):
    suffix = path.suffix.lower()
    if suffix in ('.txt', '.md', '.markdown', '.text'):
        return decode_text(path.read_bytes()), None
    if suffix == '.docx':
        return read_docx(path)
    if suffix == '.pdf':
        return read_pdf(path, ocr)
    if suffix in IMAGE_SUFFIXES:
        if not ocr:
            raise ValueError(f'{path.name} 是图片，本步骤关闭了文字识别')
        return read_image(path)
    if suffix in ('.html', '.htm'):
        return html_to_text(decode_text(path.read_bytes()))[1], None
    if suffix == '.doc':
        raise ValueError(f'{path.name} 是旧版 .doc，请另存为 .docx')
    raise ValueError(f'不支持的文档类型 {suffix}；支持 txt、md、docx、pdf、html 和图片（png、jpg 等，需文字识别）；表格请用 csv、xlsx')


def text_result(title_or_files, text, parameters):
    limit = parameters.get('max_chars', 500000)
    if len(text) > limit:
        raise ValueError(f'文本共 {len(text)} 字符，超过上限 {limit}；请拆分文件或提高 max_chars')
    return split_chunks(text, parameters.get('chunk_chars', 6000))


class DocumentReadAgent(ContractAgent):
    def execute(self, values, state):
        field = self.parameters['path_field']
        raw = values['brief'].get(field)
        paths = raw if isinstance(raw, list) else [raw]
        if not paths or not all(isinstance(item, str) and item.strip() for item in paths):
            raise ValueError(f'task_input.{field} 应填写文档路径')
        files, sections, chunks = [], [], []
        for item in paths:
            path = Path(item.strip().strip('"')).expanduser()
            if not path.is_file():
                raise ValueError(f'找不到文档：{path}')
            text, pages = read_document(path, self.parameters.get('ocr', True))
            text = text.strip()
            files.append({'name': path.name, 'format': path.suffix.lower().lstrip('.'), 'chars': len(text), 'text': text,
                          **({'pages': pages} if pages else {})})
            sections.append(f'【文件：{path.name}】\n\n{text}' if len(paths) > 1 else text)
            # Each file is chunked on its own, so a chunk never mixes two contracts and says where it is from.
            chunks += [{**chunk, 'file': path.name} for chunk in split_chunks(text, self.parameters.get('chunk_chars', 6000))]
        text = '\n\n'.join(sections)
        text_result(files, text, self.parameters)  # the size limit
        chunks = [{**chunk, 'index': number} for number, chunk in enumerate(chunks, 1)]
        return {'document': {'files': files, 'text': text, 'chunks': chunks}}


class WebFetchAgent(ContractAgent):
    MAX_BYTES = 5_000_000

    def execute(self, values, state):
        url = self.parameters.get('urls') or self.parameters.get('url')
        if self.parameters.get('url_field'):
            url = (values.get('brief') or {}).get(self.parameters['url_field'])
        if not self.parameters.get('many'):
            return {'page': self.fetch(url)}
        # Several pages, e.g. competitors to compare: each keeps its own address; one failure does not stop the rest.
        urls = url if isinstance(url, list) else [url]
        if not urls:
            raise ValueError('请至少提供一个网址')
        pages, chunks = [], []
        for address in urls[:self.parameters.get('max_pages', 20)]:
            try:
                page = self.fetch(address)
            except ValueError as exc:
                pages.append({'url': str(address), 'title': '', 'text': '', 'error': str(exc)[:500]})
                continue
            pages.append({key: page[key] for key in ('url', 'title', 'text')})
            chunks += [{**chunk, 'url': page['url']} for chunk in page['chunks']]
        if all(page.get('error') for page in pages):
            raise ValueError('所有网页都没有取到：' + pages[0]['error'])
        text = '\n\n'.join(f"【网页：{page['title'] or page['url']}】\n\n{page['text']}" for page in pages if page['text'])
        return {'page': {'pages': pages, 'text': text,
                         'chunks': [{**chunk, 'index': number} for number, chunk in enumerate(chunks, 1)]}}

    def fetch(self, url):
        if not isinstance(url, str) or not re.match(r'^https?://\S+$', url.strip()):
            raise ValueError('网页地址必须以 http:// 或 https:// 开头')
        url = url.strip()
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; AstraFetch/0.1)',
                                                       'Accept': 'text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5'})
        try:
            with urllib.request.urlopen(request, timeout=self.parameters.get('timeout', 30)) as response:
                raw = response.read(self.MAX_BYTES + 1)
                kind = response.headers.get_content_type()
                charset = response.headers.get_content_charset()
                final = response.geturl()
        except urllib.error.HTTPError as exc:
            raise ValueError(f'网页返回 {exc.code}：{url}') from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ValueError(f'无法打开网页 {url}：{exc}') from exc
        if len(raw) > self.MAX_BYTES:
            raise ValueError('网页超过 5 MB')
        if kind == 'application/pdf' or raw[:5] == b'%PDF-':
            import tempfile
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / 'page.pdf'
                target.write_bytes(raw)
                text = read_pdf(target)[0]
            return {'url': final, 'title': '', 'text': text, 'chunks': text_result(None, text, self.parameters)}
        if not charset:
            match = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4096], re.I)
            charset = match.group(1).decode() if match else None
        try:
            try:
                content = raw.decode(charset) if charset else decode_text(raw)
            except (LookupError, UnicodeDecodeError):
                content = decode_text(raw)
        except ValueError:  # binary content: NUL bytes
            raise ValueError(f'{url} 是 {kind or "未知类型"} 文件，不是网页或文字，无法提取正文') from None
        if 'html' in kind or content.lstrip()[:1] == '<':
            title, text = html_to_text(content)
        elif kind.startswith(('text/', 'application/json', 'application/xml')) or kind in ('', 'application/octet-stream'):
            title, text = '', content.strip()
        else:
            raise ValueError(f'{url} 是 {kind} 文件，不是网页或文字，无法提取正文')
        if not text:
            raise ValueError(f'网页 {url} 没有可提取的正文（可能需要登录或由脚本动态生成）')
        return {'url': final, 'title': title, 'text': text, 'chunks': text_result(None, text, self.parameters)}


# ---------- exact table computation ----------

CURRENCY = re.compile(r'[,，\s¥￥$€]')
DECIMAL = re.compile(r'[+-]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|[+-]?0?\.\d+(?:[eE][+-]?\d+)?')


UNITS = {'%': 'percent', '万': 10_000, '亿': 100_000_000}


def to_number(value, where):
    """Exact numbers only: '1,234.5', '¥300', '(200)' as -200, '12%' as 0.12, '1.2万' as 12000.

    Codes such as '007' or 18-digit IDs stay text.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, str) and value.strip():
        text = CURRENCY.sub('', value.strip()).replace('\u2212', '-').replace('－', '-')
        negative = text.startswith('(') and text.endswith(')')  # accounting style
        text = text[1:-1] if negative else text
        unit = UNITS.get(text[-1:]) if len(text) > 1 else None
        text = text[:-1] if unit else text
        if DECIMAL.fullmatch(text):
            if unit:
                from decimal import Decimal
                exact = Decimal(text) / 100 if unit == 'percent' else Decimal(text) * unit
                number = int(exact) if exact == exact.to_integral_value() else float(exact)
            else:
                number = int(text) if re.fullmatch(r'[+-]?\d+', text) else float(text)
            if isinstance(number, float) and not math.isfinite(number):
                raise ValueError(f'{where}的值“{value}”不是有限数字')
            return -number if negative else number
    raise ValueError(f'{where}的值“{value}”不是数字')


def same_key(value):
    """The comparable form of a grouping or join key: 1, 1.0 and '1 ' agree; blanks are None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) or (isinstance(value, str) and DECIMAL.fullmatch(value.strip())):
        number = to_number(value, '')
        return str(int(number) if isinstance(number, float) and number.is_integer() else number)
    return str(value).strip()


def is_number(value):
    try:
        to_number(value, '')
        return True
    except ValueError:
        return False


def compare(left, cmp, right):
    if cmp == 'empty':
        return left in (None, '', [], {})
    if cmp == 'not_empty':
        return left not in (None, '', [], {})
    if cmp == 'contains':
        return str(right) in str(left if left is not None else '')
    if cmp == 'in':
        return left in (right or []) or (is_number(left) and any(is_number(v) and to_number(v, '') == to_number(left, '') for v in right or []))
    if left is None:
        return cmp == 'ne' and right is not None
    if is_number(left) and is_number(right):
        left, right = to_number(left, ''), to_number(right, '')
    else:
        left, right = str(left), str(right)
    return {'eq': left == right, 'ne': left != right, 'gt': left > right, 'gte': left >= right,
            'lt': left < right, 'lte': left <= right}[cmp]


def power(base, exponent):
    if abs(exponent) > 100 or abs(base) > 1e100:
        raise ValueError('乘方的数值过大')
    return base ** exponent


def round_half_up(x, n=0):
    """四舍五入: 2.5 -> 3 and 2.675 -> 2.68, unlike Python's round() on floats."""
    from decimal import Decimal, ROUND_HALF_UP
    exact = Decimal(str(x)).quantize(Decimal(1).scaleb(-int(n)), rounding=ROUND_HALF_UP)
    return int(exact) if int(n) <= 0 else float(exact)


DATE_FORMS = (r'(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?(?:[ T].*)?', r'(\d{4})(\d{2})(\d{2})')


def to_date(value, where):
    """'2026-9-1', '2026/09/01', '2026年9月1日', '2026-09-01T08:00:00' or an Excel date cell."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime) else value.date()
    for form in DATE_FORMS:
        match = re.fullmatch(form, str(value if value is not None else '').strip())
        if match:
            try:
                return datetime.date(*map(int, match.groups()))
            except ValueError:
                break
    raise ValueError(f'{where}的值“{value}”不是日期')


def text_of(value):
    return '' if value is None else str(value)


FUNCTIONS = {'round': round_half_up, 'abs': abs, 'min': min, 'max': max,
             'int': int, 'float': float, 'str': str, 'len': len,
             'left': lambda s, n: text_of(s)[:max(int(n), 0)], 'right': lambda s, n: text_of(s)[-int(n):] if int(n) > 0 else '',
             'replace': lambda s, a, b: text_of(s).replace(text_of(a), text_of(b)), 'trim': lambda s: text_of(s).strip()}
DATE_FUNCTIONS = {'date': lambda d: d.isoformat(), 'year': lambda d: d.year, 'month': lambda d: d.month,
                  'year_month': lambda d: f'{d.year:04d}-{d.month:02d}'}
BINARY = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
          ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
          ast.Pow: power}
COMPARE = {ast.Eq: 'eq', ast.NotEq: 'ne', ast.Gt: 'gt', ast.GtE: 'gte', ast.Lt: 'lt', ast.LtE: 'lte'}


def check_expression(expr):
    """Parse a derive expression; only arithmetic, comparisons, conditionals and a few functions are allowed."""
    tree = ast.parse(expr, mode='eval')
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.USub, ast.UAdd, ast.Constant, ast.Name, ast.Load,
               ast.Call, ast.IfExp, ast.Compare, ast.BoolOp, ast.And, ast.Or, ast.Not, *BINARY, *COMPARE)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError(f'表达式不支持 {type(node).__name__}：{expr}')
        names = {*FUNCTIONS, *DATE_FUNCTIONS, 'days_between', 'col', 'concat', 'if_empty'}
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in names):
            raise ValueError(f'表达式只能调用 {"、".join(sorted(names))}：{expr}')
    return tree


def evaluate(tree, row, where):
    def value(node):
        if isinstance(node, ast.Expression):
            return value(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in row:
                raise ValueError(f'{where}没有列“{node.id}”')
            return row[node.id]
        if isinstance(node, ast.BinOp):
            left, right = value(node.left), value(node.right)
            try:
                return BINARY[type(node.op)](to_number(left, where), to_number(right, where))
            except ZeroDivisionError:
                raise ValueError(f'{where}除数为 0') from None
        if isinstance(node, ast.UnaryOp):
            operand = value(node.operand)
            if isinstance(node.op, ast.Not):
                return not operand
            number = to_number(operand, where)
            return -number if isinstance(node.op, ast.USub) else number
        if isinstance(node, ast.Compare):
            left = value(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = value(comparator)
                if not compare(left, COMPARE[type(op)], right):
                    return False
                left = right
            return True
        if isinstance(node, ast.BoolOp):
            results = (value(item) for item in node.values)
            return all(results) if isinstance(node.op, ast.And) else any(results)
        if isinstance(node, ast.IfExp):
            return value(node.body) if value(node.test) else value(node.orelse)
        name, args = node.func.id, node.args
        if name == 'col':
            key = value(args[0])
            if key not in row:
                raise ValueError(f'{where}没有列“{key}”')
            return row[key]
        if name == 'concat':
            return ''.join('' if (v := value(a)) is None else str(v) for a in args)
        if name == 'if_empty':
            first = value(args[0])
            return value(args[1]) if first in (None, '') else first
        values = [value(a) for a in args]
        if name in DATE_FUNCTIONS:
            return DATE_FUNCTIONS[name](to_date(values[0], where))
        if name == 'days_between':
            return (to_date(values[1], where) - to_date(values[0], where)).days
        if name in ('left', 'right'):
            values[1] = to_number(values[1], where)
        if name in ('round', 'abs', 'min', 'max', 'int', 'float'):
            values = [to_number(v, where) for v in values]
        return FUNCTIONS[name](*values)
    result = value(tree)
    if isinstance(result, float) and not math.isfinite(result):
        raise ValueError(f'{where}计算结果不是有限数字')
    return round(result, 10) if isinstance(result, float) else result  # 0.1 + 0.2 is 0.3, not 0.30000000000000004


class TableComputeAgent(ContractAgent):
    def execute(self, values, state):
        rows = resolve_pointer(values['rows'], self.parameters.get('rows_path', ''))
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError('要计算的数据必须是对象数组，请检查 rows_path')
        rows = [dict(row) for row in rows]
        for number, step in enumerate(self.parameters['steps'], 1):
            rows = getattr(self, 'step_' + step['op'])(rows, step, values, f'第 {number} 步（{step["op"]}）')
        return {'result': rows}

    def step_filter(self, rows, step, values, label):
        test = all if step.get('mode', 'all') == 'all' else any
        return [row for row in rows if test(compare(row.get(c['column']), c['cmp'], c.get('value')) for c in step['where'])]

    def step_derive(self, rows, step, values, label):
        tree = check_expression(step['expr'])
        for index, row in enumerate(rows, 1):
            row[step['column']] = evaluate(tree, row, f'{label}第 {index} 行')
        return rows

    def step_sort(self, rows, step, values, label):
        for key in reversed(step['by']):
            column = key['column']
            numbers = [row for row in rows if row.get(column) not in (None, '') and is_number(row[column])]
            texts = [row for row in rows if row.get(column) not in (None, '') and not is_number(row[column])]
            missing = [row for row in rows if row.get(column) in (None, '')]
            numbers.sort(key=lambda row: to_number(row[column], ''), reverse=key.get('desc', False))
            texts.sort(key=lambda row: str(row[column]), reverse=key.get('desc', False))
            rows = numbers + texts + missing  # numbers by value, then text, blanks last in either direction
        return rows

    def step_group(self, rows, step, values, label):
        groups, shown = {}, {}
        for row in rows:
            # 1, 1.0 and '1 ' are one group, as in join; the first spelling is the one reported.
            key = tuple(same_key(row.get(column)) for column in step.get('by', []))
            groups.setdefault(key, []).append(row)
            shown.setdefault(key, tuple(row.get(column).strip() if isinstance(row.get(column), str) else row.get(column)
                                        for column in step.get('by', [])))
        if not step.get('by') and not groups:
            groups[()], shown[()] = [], ()
        result = []
        for key, members in groups.items():
            out = dict(zip(step.get('by', []), shown[key]))
            for aggregate in step['aggregates']:
                fn, column = aggregate['fn'], aggregate.get('column')
                if fn == 'count':
                    out[aggregate['as']] = len(members) if not column else sum(1 for row in members if row.get(column) not in (None, ''))
                    continue
                present = [row.get(column) for row in members if row.get(column) not in (None, '')]
                if fn == 'count_distinct':
                    out[aggregate['as']] = len({json.dumps(v, ensure_ascii=False, sort_keys=True) for v in present})
                    continue
                numbers = [to_number(v, f'{label}列“{column}”') for v in present]
                if fn == 'sum':
                    out[aggregate['as']] = round(sum(numbers), 10)
                elif not numbers:
                    out[aggregate['as']] = None
                elif fn == 'avg':
                    out[aggregate['as']] = round(sum(numbers) / len(numbers), 10)
                else:
                    out[aggregate['as']] = min(numbers) if fn == 'min' else max(numbers)
            result.append(out)
        return result

    def step_select(self, rows, step, values, label):
        rename = step.get('rename', {})
        return [{rename.get(column, column): row.get(column) for column in step['columns']} for row in rows]

    def step_limit(self, rows, step, values, label):
        return rows[:step['n']]

    def step_window(self, rows, step, values, label):
        """Keep every row and add a column computed over its group: running total, share, rank, total, row number."""
        fn, column, target = step['fn'], step.get('column'), step['as']
        groups = {}
        for row in rows:
            groups.setdefault(tuple(same_key(row.get(name)) for name in step.get('by', [])), []).append(row)
        for members in groups.values():
            numbers = [to_number(row.get(column), f'{label}列“{column}”') if row.get(column) not in (None, '') else None
                       for row in members] if column else []
            if fn == 'row_number':
                for position, row in enumerate(members, 1):
                    row[target] = position
            elif fn == 'cumsum':
                running = 0
                for row, number in zip(members, numbers):
                    running = round(running + (number or 0), 10)
                    row[target] = running
            elif fn in ('total', 'share'):
                total = round(sum(number or 0 for number in numbers), 10)
                for row, number in zip(members, numbers):
                    if fn == 'total':
                        row[target] = total
                    else:
                        row[target] = None if number is None or total == 0 else round(number / total, 10)
            elif fn == 'rank':
                ordered = sorted((number for number in numbers if number is not None), reverse=step.get('desc', True))
                for row, number in zip(members, numbers):
                    row[target] = None if number is None else ordered.index(number) + 1  # ties share a rank: 1, 2, 2, 4
        return rows

    def step_append(self, rows, step, values, label):
        """Rows of the lookup table added below, e.g. this month's records after the history."""
        extra = resolve_pointer(values['lookup'], self.parameters.get('lookup_path', ''))
        if not isinstance(extra, list) or not all(isinstance(row, dict) for row in extra):
            raise ValueError(f'{label}：要追加的表必须是对象数组，请检查 lookup_path')
        return rows + [dict(row) for row in extra]

    def step_join(self, rows, step, values, label):
        lookup = resolve_pointer(values['lookup'], self.parameters.get('lookup_path', ''))
        if not isinstance(lookup, list):
            raise ValueError(f'{label}：关联表必须是对象数组，请检查 lookup_path')
        def key(row):
            values = tuple(same_key(row.get(column)) for column in step['on'])
            return None if None in values else values  # a blank key matches nothing
        index = {}
        for row in lookup:
            if key(row) is not None:
                index.setdefault(key(row), row)
        result = []
        for row in rows:
            match = index.get(key(row)) if key(row) is not None else None
            if match is None:
                if step.get('how', 'left') == 'left':
                    result.append(row)
                continue
            merged = dict(row)
            for column, value in match.items():
                if column in step['on']:
                    continue
                merged[column if column not in merged else f'{column}_关联'] = value
            result.append(merged)
        return result


# ---------- control flow ----------

def condition_holds(value, when):
    op, expected = when['op'], when.get('value')
    if op == 'is_true':
        return value is True
    if op == 'is_false':
        return value is False
    if op in ('empty', 'not_empty', 'in'):
        return compare(value, op, expected)
    return compare(value, {'equals': 'eq', 'not_equals': 'ne'}.get(op, op), expected)


def pointer_value(value, pointer):
    try:
        return resolve_pointer(value, pointer or '')
    except (KeyError, IndexError, TypeError, ValueError):
        return None  # the reviewer left the field out: treat it as empty, not as a crash


class RouteAgent(ContractAgent):
    def holds(self, condition, values):
        """One condition; it may compare against another field of the result or a task_input field."""
        subject = pointer_value(values['subject'], condition.get('field', self.parameters.get('field', '')))
        if 'value_path' in condition:
            expected = pointer_value(values['subject'], condition['value_path'])  # e.g. score < /pass_mark
        elif 'value_field' in condition:
            expected = (values.get('brief') or {}).get(condition['value_field'])  # e.g. score < task_input.min_score
        else:
            expected = condition.get('value')
        return condition_holds(subject, {'op': condition['op'], 'value': expected})

    def describe(self, condition):
        target = (condition.get('value_path') or (f"task_input.{condition['value_field']}" if 'value_field' in condition
                  else json.dumps(condition.get('value'), ensure_ascii=False)))
        return f"{condition.get('field', self.parameters.get('field')) or '结果'} {condition['op']} {target}"

    def matches(self, case, values):
        if 'all' in case:
            return all(self.holds(item, values) for item in case['all']), ' 且 '.join(map(self.describe, case['all']))
        if 'any' in case:
            return any(self.holds(item, values) for item in case['any']), ' 或 '.join(map(self.describe, case['any']))
        return self.holds(case['when'], values), self.describe(case['when'])

    def rounds_so_far(self, state, backward):
        """Redo count of this loop; it restarts when an enclosing loop has gone back before this loop began."""
        previous = state['data'].get(self.outputs['decision']) or {}
        rounds = previous.get('round', 0)
        targets = [backward.index(case['goto']) for case in self.parameters['cases'] if backward and case['goto'] in backward]
        executions = state.get('executions') or []
        mine = [number for number, record in enumerate(executions) if record.get('role') == self.name
                and record.get('status') == 'success']
        if rounds and targets and mine:
            earlier = set(backward[:min(targets)])
            if any(record.get('stage') in earlier for record in executions[mine[-1] + 1:]):
                return 0
        return rounds

    def execute(self, values, state):
        limit = self.parameters.get('max_loops', 2)
        # Only jumps back (redo) are limited; a jump forward just skips stages. Older projects lack the list.
        backward = self.parameters.get('backward_targets')
        rounds = self.rounds_so_far(state, backward)
        for case in self.parameters['cases']:
            hit, label = self.matches(case, values)
            if not hit:
                continue
            if backward is not None and case['goto'] not in backward:
                return {'decision': {'goto': case['goto'], 'round': rounds, 'reason': f'{label}，跳到 {case["goto"]}'},
                        NEXT_STAGE: case['goto']}
            if rounds >= limit:
                if self.parameters.get('on_exhausted', 'continue') == 'fail':
                    raise ValueError(f'已跳转 {rounds} 次，条件“{label}”仍成立，停止运行')
                return {'decision': {'goto': None, 'round': rounds, 'reason': f'已达到最多 {limit} 次，按原顺序继续'}}
            return {'decision': {'goto': case['goto'], 'round': rounds + 1, 'reason': f'{label}，转到 {case["goto"]}'},
                    NEXT_STAGE: case['goto']}
        return {'decision': {'goto': None, 'round': rounds, 'reason': '条件不成立，按原顺序继续'}}



# ---------- files on disk: read-only scan, then guarded cleanup ----------

WINDOWS_SYSTEM = {'windows', 'program files', 'program files (x86)', 'programdata', '$recycle.bin',
                  'system volume information', 'recovery', 'boot', 'perflogs'}
POSIX_SYSTEM = {'usr', 'etc', 'bin', 'sbin', 'lib', 'lib32', 'lib64', 'boot', 'proc', 'sys', 'dev', 'opt', 'var',
                'system', 'library', 'applications', 'private', 'cores', 'snap', 'srv'}
POSIX_TEMP = {('var', 'tmp'), ('private', 'tmp'), ('private', 'var', 'folders')}


def root_problem(value):
    """Why a folder may not be scanned or cleaned, or None. Kept identical to the designer's design-time check."""
    import ntpath
    import posixpath
    raw = str(value).strip().strip('"')
    if raw.startswith('~'):
        raw = os.path.expanduser(raw)
    windows = bool(re.match(r'^[A-Za-z]:[\\/]', raw)) or raw.startswith('\\\\')
    flavour = ntpath if windows else posixpath
    if not flavour.isabs(raw):
        return '必须是绝对路径'
    parts = [part.casefold() for part in re.split(r'[\\/]+', flavour.splitdrive(flavour.normpath(raw))[1]) if part]
    if not parts:
        return '不能是磁盘根目录'
    first = parts[0]
    if windows:
        if first in WINDOWS_SYSTEM:
            return '不能是系统目录'
        if first == 'users' and len(parts) <= 2:
            return '不能是用户目录本身，请指定其中具体的文件夹'
        if first == 'users' and len(parts) >= 3 and parts[2] == 'appdata' and (
                len(parts) <= 4 or parts[3:5] == ['roaming', 'microsoft']):
            return '不能是 AppData 本身或系统配置目录，请指定如 AppData\\Local\\Temp 这样的具体缓存目录'
        return None
    if first in POSIX_SYSTEM and not any(tuple(parts[:len(item)]) == item for item in POSIX_TEMP):
        return '不能是系统目录'
    if first in {'home', 'users'} and len(parts) <= 2 or parts == ['root']:
        return '不能是用户目录本身，请指定其中具体的文件夹'
    return None


def resolved_root(value, label):
    problem = root_problem(value)
    if problem:
        raise ValueError(f'{label} {problem}：{value}')
    path = Path(str(value).strip().strip('"')).expanduser().resolve()
    problem = root_problem(path)  # a link could lead somewhere protected
    if problem:
        raise ValueError(f'{label} 实际指向 {path}，{problem}')
    return path


def entry_of(path, root, now):
    info = path.stat()
    modified = datetime.datetime.fromtimestamp(info.st_mtime, datetime.timezone.utc)
    return {'path': str(path), 'name': path.name, 'ext': path.suffix.lower().lstrip('.'),
            'size_bytes': info.st_size, 'size_mb': round(info.st_size / 1_048_576, 3),
            'modified': modified.astimezone().isoformat(timespec='seconds'),
            'age_days': max(0, (now - modified).days), 'root': str(root), 'dir': str(path.parent)}


def is_link(path):
    """Symbolic links and Windows junctions both lead outside the folder being processed."""
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return True


def excluded(relative, patterns):
    """Wildcard patterns match the relative path; plain names match a whole folder or file name."""
    names = [name.casefold() for name in relative.split('/')]  # Windows names ignore case
    return any(fnmatch.fnmatch(relative, pattern) or (not any(ch in pattern for ch in '*?[') and pattern.casefold() in names)
               for pattern in patterns)


ALWAYS_SKIPPED = ['.git', '.svn', '.hg', '.astra_quarantine', 'desktop.ini', 'Thumbs.db', '.DS_Store', '$RECYCLE.BIN',
                  'System Volume Information']


class FileScanAgent(ContractAgent):
    """Read-only: lists files so a later step can decide what to do with them."""

    def execute(self, values, state):
        roots = self.parameters.get('roots') or []
        if self.parameters.get('roots_field'):
            given = (values.get('brief') or {}).get(self.parameters['roots_field'])
            roots = given if isinstance(given, list) else [given]
        if not roots or not all(isinstance(item, (str, Path)) and str(item).strip() for item in roots):
            raise ValueError('请提供要扫描的目录')
        extensions = {str(item).lower().lstrip('.') for item in self.parameters.get('extensions', [])}
        exclude = [str(item).replace('\\', '/') for item in self.parameters.get('exclude', [])]
        if not self.parameters.get('include_hidden'):
            exclude += ALWAYS_SKIPPED  # version control and system bookkeeping are never cleanup candidates
        limit, minimum = self.parameters.get('max_files', 20000), self.parameters.get('min_bytes', 0)
        min_age = self.parameters.get('min_age_days')
        depth_limit, now = self.parameters.get('max_depth'), datetime.datetime.now(datetime.timezone.utc)
        own = project_root(state)
        entries = []
        for item in roots:
            root = resolved_root(item, '扫描目录')
            if not root.is_dir():
                raise ValueError(f'找不到目录：{root}')
            stack = [(root, 0)]
            while stack:
                directory, depth = stack.pop()
                try:
                    children = list(directory.iterdir())
                except (PermissionError, OSError):
                    continue  # locked or system-protected folders are simply not scanned
                for child in children:
                    if is_link(child) or child == own:
                        continue  # never follow links, and never list this project's own records
                    relative = child.relative_to(root).as_posix()
                    if excluded(relative, exclude):
                        continue
                    if child.is_dir():
                        if depth_limit is None or depth + 1 < depth_limit:
                            stack.append((child, depth + 1))
                        continue
                    if not child.is_file():
                        continue
                    if extensions and child.suffix.lower().lstrip('.') not in extensions:
                        continue
                    try:
                        entry = entry_of(child, root, now)
                    except OSError:
                        continue
                    if entry['size_bytes'] < minimum or (min_age is not None and entry['age_days'] < min_age):
                        continue
                    if len(entries) >= limit:
                        raise ValueError(f'匹配的文件超过 {limit} 个，请缩小目录范围或提高 max_files')
                    entries.append(entry)
        return {'files': entries}


def project_root(state):
    """The generated project's folder: its state, runs and quarantine must never be cleaned by itself."""
    return Path(state['data']['run']['output_dir']).resolve().parents[2]


QUARANTINE = '.astra_quarantine'


def batch_of(path):
    """The quarantine batch folder (<root>/.astra_quarantine/<run id>) holding a quarantined file."""
    path = Path(path)
    while path.parent.name not in (QUARANTINE, 'cleanup_quarantine') and path.parent != path:
        path = path.parent
    return path


class FileCleanupAgent(ContractAgent):
    """Acts on the list the workflow produced: dry run by default, quarantine before deleting, restore on request.

    Moving or deleting always stops first with the exact list; after “确认” only that list is processed.
    """

    def execute(self, values, state):
        roots = [resolved_root(item, 'allowed_roots') for item in self.parameters['allowed_roots']]
        mode = self.parameters.get('mode', 'dry_run')
        field = self.parameters.get('execute_field')
        acting = mode != 'dry_run'
        if field and (values.get('brief') or {}).get(field) not in self.parameters.get('execute_values', [True, 'yes', '是', 'true']):
            acting = False  # this run only reports what would be done
        if mode == 'restore':
            rows, column = self.quarantined(roots, values, state), 'path'
        else:
            rows = resolve_pointer(values['files'], self.parameters.get('rows_path', ''))
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError('要清理的清单必须是对象数组，请检查 rows_path')
            column = self.parameters.get('path_column', 'path')
        plan_file = Path(state['data']['run']['output_dir']) / f'cleanup_plan_{self.name}.json'
        if acting and self.name in (state.get('confirmed_actions') or []) and plan_file.is_file():
            plan = json.loads(plan_file.read_text(encoding='utf-8'))
            planned = {item['path']: item for item in plan['items']}
            handled, skipped, _ = self.process([{'path': path, **item} for path, item in planned.items()], 'path',
                                               roots, mode, state, planned)
            purged = self.purge(plan.get('purge', []))
            return {'result': self.report(mode, plan.get('candidates', len(planned)), handled,
                                          plan.get('skipped', []) + skipped, purged, state)}
        preview, excluded, stamps = self.process(rows, column, roots, 'preview', state)
        expired = self.expired(roots, state) if mode in ('quarantine', 'delete') else []
        if acting and (preview or expired):
            self.ask(plan_file, mode, preview, stamps, excluded, len(rows), expired)
        for item in preview:
            item['action'] = 'dry_run' if mode != 'restore' else 'restore_preview'
        return {'result': self.report('dry_run' if not acting else mode, len(rows), preview, excluded,
                                      [{**item, 'done': False} for item in expired], state)}

    def ask(self, plan_file, mode, preview, stamps, excluded, candidates, expired):
        from astra_core.runtime.pause import WorkflowPause
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        items = [{**item, 'mtime_ns': stamps[item['path']]} for item in preview]
        plan_file.write_text(json.dumps({'mode': mode, 'candidates': candidates, 'items': items, 'skipped': excluded,
                                         'purge': expired}, ensure_ascii=False, indent=2), encoding='utf-8')
        size = sum(item['size_bytes'] for item in preview)
        raise WorkflowPause('awaiting_confirmation', input_errors=[], questions=[], pending_confirmation={
            'agent': self.name, 'kind': 'cleanup', 'mode': mode, 'count': len(preview),
            'mb': round(size / 1_048_576, 2), 'items': [item['path'] for item in preview[:30]], 'plan': str(plan_file),
            'purge_count': len(expired), 'purge_mb': round(sum(item['size_bytes'] for item in expired) / 1_048_576, 2)})

    def quarantined(self, roots, values, state):
        """Files recorded in the quarantine manifests under the allowed folders, newest batch first."""
        wanted = None
        if 'files' in values:
            rows = resolve_pointer(values['files'], self.parameters.get('rows_path', ''))
            column = self.parameters.get('path_column', 'path')
            if isinstance(rows, list) and rows:
                wanted = {str(Path(str(row.get(column, '')).strip().strip('"')).expanduser().resolve()) for row in rows
                          if isinstance(row, dict)}
        found = []
        for folder in [root / QUARANTINE for root in roots] + [project_root(state) / 'output' / 'cleanup_quarantine']:
            for manifest in sorted(folder.glob('*/manifest.csv'), reverse=True) if folder.is_dir() else []:
                with manifest.open(encoding='utf-8-sig', newline='') as handle:
                    for line in list(csv.reader(handle))[1:]:
                        if len(line) >= 2 and (wanted is None or line[0] in wanted):
                            found.append({'path': line[0], 'from': line[1]})
        return found

    def expired(self, roots, state):
        """Quarantine batches older than purge_after_days, deleted for good once the user confirms."""
        days = self.parameters.get('purge_after_days')
        if not days:
            return []
        today, result = datetime.datetime.now(datetime.timezone.utc).date(), []
        for folder in [root / QUARANTINE for root in roots] + [project_root(state) / 'output' / 'cleanup_quarantine']:
            for batch in sorted(folder.iterdir()) if folder.is_dir() else []:
                match = re.match(r'(\d{4})(\d{2})(\d{2})T', batch.name)
                if not batch.is_dir() or is_link(batch) or not match:
                    continue
                if (today - datetime.date(*map(int, match.groups()))).days >= days:
                    size = sum(item.stat().st_size for item in batch.rglob('*') if item.is_file() and not is_link(item))
                    result.append({'dir': str(batch), 'size_bytes': size})
        return result

    def purge(self, batches):
        done = []
        for batch in batches:
            folder = Path(batch['dir'])
            if folder.is_dir() and not is_link(folder) and folder.parent.name in (QUARANTINE, 'cleanup_quarantine'):
                shutil.rmtree(folder, ignore_errors=True)
                done.append({**batch, 'done': not folder.exists()})
        return done

    def process(self, rows, column, roots, mode, state, planned=None):
        """mode 'preview' only checks; quarantine, delete and restore act."""
        keep_days = self.parameters.get('keep_newer_than_days') if self.parameters.get('mode') != 'restore' else None
        now = datetime.datetime.now(datetime.timezone.utc)
        own = project_root(state)
        run_id = str(state['data']['run'].get('id'))
        handled, skipped, total, stamps = [], [], 0, {}
        restoring = self.parameters.get('mode') == 'restore'
        for row in rows:
            raw = row.get(column)
            if not isinstance(raw, str) or not raw.strip():
                skipped.append({'path': str(raw), 'reason': f'清单缺少 {column}'})
                continue
            path = Path(raw.strip().strip('"')).expanduser()
            source = Path(row['from']) if restoring else path  # the file that is actually moved or deleted
            if is_link(source):
                skipped.append({'path': str(path), 'reason': '是链接或目录联接，不处理'})
                continue
            target = path.resolve()
            index = next((number for number, root in enumerate(roots) if target.is_relative_to(root)), None)
            if index is None:
                skipped.append({'path': str(target), 'reason': '不在允许清理的目录内'})
                continue
            if not restoring and QUARANTINE in target.relative_to(roots[index]).parts:
                skipped.append({'path': str(target), 'reason': '是隔离区中的文件，不处理'})
                continue
            if target.is_relative_to(own) and not restoring:
                skipped.append({'path': str(target), 'reason': '属于本项目自己的记录，不处理'})
                continue
            if restoring and target.exists():
                skipped.append({'path': str(target), 'reason': '原位置已有同名文件，未还原'})
                continue
            quarantines = [root / QUARANTINE for root in roots] + [own / 'output' / 'cleanup_quarantine']
            if restoring and not any(source.resolve().is_relative_to(folder.resolve()) for folder in quarantines):
                # The record file only says where things were; it never lets a restore take files from elsewhere.
                skipped.append({'path': str(target), 'reason': '记录中的来源不在隔离区内，不处理'})
                continue
            if not source.is_file():
                skipped.append({'path': str(target), 'reason': '隔离区中已没有这个文件' if restoring else '不存在或不是文件'})
                continue
            info = source.stat()
            expected = planned.get(str(target)) if planned is not None else None
            if planned is not None and (expected is None or (expected['size_bytes'], expected['mtime_ns']) != (info.st_size, info.st_mtime_ns)):
                skipped.append({'path': str(target), 'reason': '确认后文件有变化，未处理'})
                continue
            if keep_days is not None:
                age = (now - datetime.datetime.fromtimestamp(info.st_mtime, datetime.timezone.utc)).days
                if age < keep_days:
                    skipped.append({'path': str(target), 'reason': f'最近 {keep_days} 天内修改过'})
                    continue
            if len(handled) >= self.parameters.get('max_files', 500):
                skipped.append({'path': str(target), 'reason': '超过本次处理数量上限'})
                continue
            if self.parameters.get('max_total_mb') and (total + info.st_size) / 1_048_576 > self.parameters['max_total_mb']:
                skipped.append({'path': str(target), 'reason': '超过本次处理容量上限'})
                continue
            record = {'path': str(target), 'size_bytes': info.st_size, 'action': mode}
            if restoring:
                record['from'] = str(source)
            try:
                if mode == 'quarantine':
                    # Inside the allowed folder, so the move stays on the same drive and frees nothing elsewhere.
                    destination = roots[index] / QUARANTINE / run_id / target.relative_to(roots[index])
                    while destination.exists():
                        destination = destination.with_name(f'{destination.stem}_{time.time_ns()}{destination.suffix}')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(destination))
                    record['moved_to'] = str(destination)
                elif mode == 'delete':
                    target.unlink()
                elif mode == 'restore':
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source), str(target))
                    record['moved_to'] = str(target)
            except OSError as exc:  # e.g. the file is open in another program or read-only
                hint = '（文件只读、正在被其他程序使用或没有权限）' if isinstance(exc, PermissionError) else ''
                skipped.append({'path': str(target), 'reason': f'无法处理：{exc.strerror or exc}{hint}'})
                continue
            handled.append(record)
            stamps[str(target)] = info.st_mtime_ns
            total += info.st_size
            if mode in ('quarantine', 'delete') and self.parameters.get('remove_empty_dirs'):
                folder = target.parent
                while folder != roots[index] and folder.is_relative_to(roots[index]):
                    try:
                        folder.rmdir()  # only succeeds when the folder is now empty
                    except OSError:
                        break
                    folder = folder.parent
        if mode == 'restore':
            self.forget(handled)
        return handled, skipped, stamps

    def forget(self, restored):
        """Drop restored files from their manifests; an emptied batch folder is removed."""
        by_batch = {}
        for item in restored:
            by_batch.setdefault(batch_of(item['from']), set()).add(item['from'])
        for batch, sources in by_batch.items():
            manifest = batch / 'manifest.csv'
            if manifest.is_file():
                with manifest.open(encoding='utf-8-sig', newline='') as handle:
                    lines = list(csv.reader(handle))
                kept = [line for line in lines[1:] if len(line) < 2 or line[1] not in sources]
                with manifest.open('w', encoding='utf-8-sig', newline='') as handle:
                    csv.writer(handle).writerows(lines[:1] + kept)
                if not kept:
                    shutil.rmtree(batch, ignore_errors=True)

    def report(self, mode, candidates, handled, skipped, purged, state):
        total = sum(item['size_bytes'] for item in handled)
        released = sum(item['size_bytes'] for item in purged if item.get('done'))
        public = [{key: item[key] for key in ('path', 'size_bytes', 'action', 'moved_to') if key in item} for item in handled]
        result = {'mode': mode, 'candidates': candidates, 'handled': public, 'skipped': skipped,
                  'freed_bytes': (total if mode == 'delete' else 0) + released,
                  'freed_mb': round(((total if mode == 'delete' else 0) + released) / 1_048_576, 2),
                  'affected_bytes': total, 'affected_mb': round(total / 1_048_576, 2)}
        if purged:
            result['purged'] = purged
        if mode == 'quarantine' and handled:
            batches = list(dict.fromkeys(batch_of(item['moved_to']) for item in handled))
            result['quarantine_dir'], result['quarantine_dirs'] = str(batches[0]), [str(item) for item in batches]
            for batch in batches:
                # Where each file came from, so it can be put back (mode restore) or by hand.
                manifest = batch / 'manifest.csv'
                fresh = not manifest.is_file()
                with manifest.open('a', encoding='utf-8-sig' if fresh else 'utf-8', newline='') as handle:
                    writer = csv.writer(handle)
                    if fresh:
                        writer.writerow(['原位置', '隔离位置', '大小（字节）'])
                    writer.writerows([item['path'], item['moved_to'], item['size_bytes']] for item in handled
                                     if batch_of(item['moved_to']) == batch)
        # A report beside the run's other files, listed when the run ends.
        report = Path(state['data']['run']['output_dir']) / 'cleanup_report.csv'
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(['处理方式', '文件', '大小（字节）', '说明'])
            labels = {'dry_run': '试算（未改动）', 'quarantine': '已移入隔离区', 'delete': '已删除', 'restore': '已还原',
                      'restore_preview': '可还原（未改动）'}
            writer.writerows([labels.get(item['action'], item['action']), item['path'], item['size_bytes'],
                              item.get('moved_to', '')] for item in handled)
            writer.writerows(['跳过', item['path'], '', item['reason']] for item in skipped)
            writer.writerows(['已清空隔离批次' if item.get('done') else '到期的隔离批次（未清空）', item['dir'],
                              item['size_bytes'], ''] for item in purged)
        result['report'] = str(report)
        return result
