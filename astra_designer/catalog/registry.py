from importlib.resources import files
import json
import re
from copy import deepcopy
from contextvars import ContextVar
from functools import wraps
from astra_designer.catalog.loader import digest, load_recipes

_snapshot = ContextVar('designer_capability_snapshot', default=None)


def with_catalog(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _snapshot.get() is not None:
            return function(*args, **kwargs)
        token = _snapshot.set(capabilities())
        try:
            return function(*args, **kwargs)
        finally:
            _snapshot.reset(token)
    return wrapped


def resource_text(name: str) -> str:
    return files("astra_designer.resources").joinpath(name).read_text(encoding="utf-8")


def schemas() -> dict:
    result = json.loads(resource_text("data.schemas.json"))
    owners = {}
    for spec in capabilities().values():
        for name, schema in spec.get('pack_schemas', {}).items():
            owner = spec['pack']['name']
            if name in result and owners.get(name) != owner:
                raise ValueError('能力包 Schema 冲突: ' + name)
            owners[name], result[name] = owner, schema
    return result


def capabilities() -> dict:
    if _snapshot.get() is not None:
        return deepcopy(_snapshot.get())
    builtin = json.loads(resource_text("capabilities.json"))
    from astra_designer.catalog.pending import IDENTIFIER, specification
    builtin[IDENTIFIER] = specification()
    metadata = {
        'object.resolve@1': ('创建或选择业务对象', '程序创建唯一 ID 或校验已有对象，提供跨运行稳定身份，供状态和文件归档使用；模型不生成编号', ['对象', '身份', '编号', '唯一', '多项目', '跨运行']),
        IDENTIFIER: ('待接入能力', '声明内置能力做不到的外部服务（视频生成、远程数据库、即时通讯私信、登录后的系统等）；端口由 input_schemas/output_schemas 决定；可生成项目，接入前运行到该步骤暂停', ['接入', '图像', '邮件', '通知', '外部服务']),
        'feedback.read@1': ('读取客户反馈', '读取本地 JSON 反馈记录', ['读取', '反馈', 'JSON']),
        'feedback.classify@1': ('关键词分类', '按有序关键词规则分类客户反馈', ['分类', '关键词', '规则']),
        'feedback.count@1': ('分类统计', '统计反馈类别频次', ['统计', '频次', '反馈']),
        'feedback.report@1': ('反馈报告', '仅用于客户反馈统计：把 feedback_counts 写成含预设改进建议的 Markdown 报告，不是通用报告能力', ['报告', '建议', 'Markdown']),
        'document.write@1': ('写入文档文件', '把上游数据中的文本写成 txt、md、docx 或 pdf 文件，保存在本次运行的输出目录；输入端口 content 的 Schema 由 input_schema 决定，输出 files 为格式到文件路径的映射', ['文档', '文件', '导出', 'txt', 'docx', 'pdf', 'Word', 'Markdown']),
        'table.read@1': ('读取表格', '读取每次运行提供的 CSV 或 Excel（xlsx）文件，按表头输出行记录；文件路径来自 task_input 字段', ['表格', 'Excel', 'CSV', 'xlsx', '读取', '导入']),
        'table.write@1': ('写入表格', '把上游的行记录写成 CSV 或 Excel（xlsx）文件，保存在本次运行的输出目录；输入端口 rows 的 Schema 由 input_schema 决定', ['表格', 'Excel', 'CSV', 'xlsx', '导出', '文件']),
        'http.request@1': ('调用 HTTP 接口', '按设计时确定的地址调用 HTTP/JSON 接口（查询服务、发送 Webhook 通知等），输出状态码和响应体；密钥只从环境变量读取', ['HTTP', '接口', 'API', 'Webhook', '通知', '查询']),
        'document.read@1': ('读取文档', '读取每次运行提交的 txt、md、docx、pdf、html 文件和图片（可多个；扫描件与图片自动文字识别），输出全文和按段落切分的 chunks，长文档可交给 llm.map@1 逐段处理', ['文档', '读取', 'Word', 'docx', 'pdf', 'txt', '合同', '论文', '导入']),
        'web.fetch@1': ('抓取网页', '抓取网页并提取标题和正文（去掉脚本、样式和导航），输出全文和 chunks；地址固定或来自 task_input 字段', ['网页', '抓取', '链接', 'URL', '文章', '爬取']),
        'table.compute@1': ('表格计算', '对行记录做确定性的筛选、公式派生列、排序、分组汇总、选列改名、取前 N 行和按键关联，数字精确，不经过模型', ['表格', '计算', '统计', '汇总', '筛选', '排序', '分组', '求和', '公式']),
        'flow.route@1': ('条件跳转', '根据上游结果决定下一步：满足条件时跳回前面的阶段重做（有最大次数）或跳过后面的阶段，否则继续', ['条件', '分支', '重做', '返工', '循环', '判断', '跳转']),
        'fs.scan@1': ('扫描文件', '只读扫描指定目录下的文件，输出路径、大小、修改时间和距今天数；可按扩展名、最小大小、深度和排除规则筛选，不修改任何文件', ['文件', '扫描', '磁盘', '清理', '目录', '缓存']),
        'fs.cleanup@1': ('清理文件', '按上游给出的清单处理文件：默认只试算（dry_run），可移入同一文件夹下的隔离区（quarantine，可还原）、永久删除（delete），或把隔离区的文件还原（restore）；可定期清空过期的隔离批次；只允许在 allowed_roots 指定的目录内操作，真正改动前先列出清单请用户确认', ['清理', '删除', '回收', '磁盘', '空间', '缓存']),
        'state.load@1': ('读取团队状态', '在工作流开头读取上次运行保存的团队状态（跨运行记忆，如小说进度、人物设定），保存在项目本地；没有状态或本次要求重新开始时 found=false', ['状态', '记忆', '继续', '续写', '本地存储', 'JSON']),
        'state.save@1': ('保存团队状态', '在工作流末尾把更新后的状态保存到项目本地，供下次运行的 state.load@1 读取；每次保存保留上一版备份', ['状态', '记忆', '保存', '本地存储', 'JSON']),
        'llm.map@1': ('逐项模型处理', '对上游数组的每一项分别调用模型处理（如按大纲逐章写作、逐条分析），输出结果数组；可附带前几项结果保持连贯', ['LLM', '逐项', '分章', '批量', '循环', '长文本']),
        'web.search@1': ('网络搜索', '用搜索引擎（博查、Brave、Tavily 或自建 SearXNG，环境变量配置）按搜索词找网页，输出标题、网址、摘要，可读取靠前网页的全文；搜索词来自固定模板、本次输入或上游生成', ['搜索', '调研', '检索', '资料', '新闻', '网页', '竞品']),
        'email.send@1': ('发送邮件', '通过发信服务器（环境变量配置，支持 QQ、163、企业邮箱等 SMTP）发送一封邮件：正文取上游文本，附件可取本次运行写出的文件；收件人固定或每次输入', ['邮件', '发送', '通知', '附件', 'SMTP', '报告']),
        'human.review@1': ('人工审阅', '运行到这一步暂停，把上游成果（如草稿、方案）展示给人审阅：输入“通过”继续，否则把修改意见作为 notes 输出，可配合 flow.route@1 跳回返工', ['审阅', '审核', '审批', '人工', '确认', '修改意见', '审稿']),
        'image.generate@1': ('绘图', '用自己部署的绘图模型（OpenAI 兼容图片接口，环境变量配置）按提示词生成图片：海报、插图、配图、商品图、Logo 等；提示词来自固定模板、本次输入或上游模型（可逐项生成，如每章一张），可选画面比例、候选张数、透明背景；输出图片文件清单', ['绘图', '图片', '图像', '海报', '插图', '配图', '生成', 'Logo']),
        'image.edit@1': ('改图', '用同一绘图模型编辑图片：本次提交的照片或上游生成的图片，按指令换背景、改元素、改风格，或以多张图片为参考合成新图（默认最多 4 张）；可逐张批量处理；输出图片文件清单', ['改图', '图片编辑', '换背景', '修图', '参考图', '商品图']),
        'llm.transform@1': ('结构化模型处理', '按提示词对上游数据做语义处理并返回 JSON；端口由 input_schemas/output_schemas 决定；不调用工具或外部服务', ['LLM', '语义', '分析']),
    }
    result = {}
    for identifier, spec in builtin.items():
        title, description, tags = metadata[identifier]
        result[identifier] = {**spec, 'base': identifier, 'title': title, 'description': description, 'tags': tags,
                              'source': 'builtin', 'defaults': {}, 'sha256': digest(spec)}
    for identifier, recipe in load_recipes(builtin).items():
        base = deepcopy(result[recipe['base']])
        if recipe['base'] == 'llm.transform@1':
            base['inputs'] = recipe['parameters']['input_schemas']
            base['outputs'] = recipe['parameters']['output_schemas']
        result[identifier] = {**base, 'title': recipe['title'], 'description': recipe['description'], 'tags': recipe['tags'],
                              'source': 'local', 'defaults': recipe['parameters'],
                              'sha256': digest({'recipe': recipe, 'base_sha256': base['sha256']})}
    from astra_core.capability_packs import installed_packs, readiness
    for pack in installed_packs():
        manifest = pack['manifest']
        for cap in manifest['capabilities']:
            if cap['id'] in result:
                raise ValueError('能力包不能覆盖已有能力: ' + cap['id'])
            result[cap['id']] = {**cap, 'base': cap['id'], 'class': 'PackAgent', 'source': 'pack',
                'defaults': {}, 'sha256': digest({'pack': pack['sha256'], 'capability': cap['id']}),
                'availability': readiness(cap), 'pack_schemas': manifest['schemas'],
                'pack': {'name': manifest['name'], 'version': manifest['version'], 'root': pack['root'],
                         'sha256': pack['sha256'], 'permissions': manifest['permissions']}}
    return result


def capability_for(agent):
    spec = capabilities().get(agent['capability'])
    if spec:
        spec['effective_parameters'] = {**spec['defaults'], **agent['parameters']}
    if spec and spec['base'] in {'llm.transform@1', 'integration.pending@1'}:
        spec['inputs'] = spec['effective_parameters'].get('input_schemas', {})
        spec['outputs'] = spec['effective_parameters'].get('output_schemas', {})
    if spec and spec['base'] == 'llm.map@1':
        spec['inputs'] = spec['effective_parameters'].get('input_schemas', {})
        spec['outputs'] = spec['effective_parameters'].get('output_schemas', {})
    if spec and spec['base'] == 'table.write@1':
        spec['inputs'] = {'rows': spec['effective_parameters'].get('input_schema')}
        if re.search(r'\{[a-z][a-z0-9_]*\}', str(spec['effective_parameters'].get('filename', ''))):
            spec['inputs']['brief'] = 'task_input'  # the file name uses task_input fields
        spec['outputs'] = {'files': 'document_files'}
    if spec and spec['base'] == 'http.request@1':
        params = spec['effective_parameters']
        spec['inputs'] = {}
        if params.get('body_schema'):
            spec['inputs']['body'] = params['body_schema']
        if re.search(r'\{[a-z][a-z0-9_]*\}', str(params.get('url', ''))):
            spec['inputs']['brief'] = 'task_input'  # URL placeholders are filled from task_input fields
        spec['outputs'] = {'response': 'http_responses' if params.get('each_path') else 'http_response'}
    if spec and spec['base'] == 'fs.scan@1':
        spec['inputs'] = {'brief': 'task_input'} if spec['effective_parameters'].get('roots_field') else {}
    if spec and spec['base'] == 'fs.cleanup@1':
        params = spec['effective_parameters']
        # restore reads the quarantine's own records; a list of files is optional there.
        spec['inputs'] = {'files': params.get('input_schema')} if params.get('input_schema') or params.get('mode') != 'restore' else {}
        if params.get('execute_field'):
            spec['inputs']['brief'] = 'task_input'  # the run decides whether to actually clean
    if spec and spec['base'] == 'web.fetch@1':
        spec['inputs'] = {'brief': 'task_input'} if spec['effective_parameters'].get('url_field') else {}
        spec['outputs'] = {'page': 'web_pages' if spec['effective_parameters'].get('many') else 'web_page'}
    if spec and spec['base'] == 'table.compute@1':
        params = spec['effective_parameters']
        spec['inputs'] = {'rows': params.get('input_schema')}
        if params.get('lookup_schema') or any(isinstance(step, dict) and step.get('op') in ('join', 'append')
                                              for step in params.get('steps', [])):
            spec['inputs']['lookup'] = params.get('lookup_schema')
    if spec and spec['base'] == 'flow.route@1':
        spec['inputs'] = {'subject': spec['effective_parameters'].get('subject_schema')}
        conditions = [item for case in spec['effective_parameters'].get('cases', []) if isinstance(case, dict)
                      for item in ([case.get('when')] + case.get('all', []) + case.get('any', [])) if isinstance(item, dict)]
        if any('value_field' in item for item in conditions):
            spec['inputs']['brief'] = 'task_input'  # a threshold chosen for this run, e.g. the pass mark
    if spec and spec['base'] in {'state.load@1', 'state.save@1'}:
        params = spec['effective_parameters']
        spec['inputs'] = {'value': params.get('value_schema')} if spec['base'] == 'state.save@1' else {}
        if params.get('key_field') or params.get('reset_field'):
            spec['inputs']['brief'] = 'task_input'  # the state key or the reset choice comes from this run's input
    if spec and spec['base'] == 'web.search@1':
        params = spec['effective_parameters']
        spec['inputs'] = {'topic': params['queries_schema']} if params.get('queries_schema') else {}
        if params.get('query_field') or re.search(r'\{[a-z][a-z0-9_]*\}', str(params.get('query', ''))):
            spec['inputs']['brief'] = 'task_input'
    if spec and spec['base'] == 'email.send@1':
        params = spec['effective_parameters']
        spec['inputs'] = {'content': params.get('input_schema')}
        if params.get('attachments_schema'):
            spec['inputs']['attachments'] = params['attachments_schema']
        if params.get('to_field') or params.get('cc_field') or re.search(r'\{[a-z][a-z0-9_]*\}', str(params.get('subject', ''))):
            spec['inputs']['brief'] = 'task_input'  # recipients or the subject come from this run's input
    if spec and spec['base'] == 'human.review@1':
        spec['inputs'] = {'content': spec['effective_parameters'].get('input_schema')}
    if spec and spec['base'] in {'image.generate@1', 'image.edit@1'}:
        params = spec['effective_parameters']
        spec['inputs'] = {'content': params['input_schema']} if params.get('input_schema') else {}
        if spec['base'] == 'image.edit@1' and params.get('images_schema'):
            spec['inputs']['images'] = params['images_schema']
        if params.get('prompt_field') or params.get('image_field') or any(
                re.search(r'\{[a-z][a-z0-9_]*\}', str(params.get(name, ''))) for name in ('prompt', 'filename')):
            spec['inputs']['brief'] = 'task_input'  # the prompt, the photos or the file name come from this run's input
    if spec and spec['base'] == 'document.write@1':
        params = spec['effective_parameters']
        spec['inputs'] = {'content': params.get('input_schema')}
        if params.get('key_field') or params.get('format_field') or params.get('reset_field') or re.search(r'\{[a-z][a-z0-9_]*\}', str(params.get('filename', ''))):
            spec['inputs']['brief'] = 'task_input'  # format, starting over or parts of the file name come from task_input
        spec['outputs'] = {'files': 'document_file_lists' if params.get('per_item') else 'document_files'}
    if spec and spec['base'] in {'state.load@1', 'state.save@1', 'document.write@1'} and spec['effective_parameters'].get('object_context'):
        spec['inputs']['object'] = 'business_object'
    return spec


def catalog_lock(blueprint):
    catalog = capabilities()
    ids = sorted({a['capability'] for s in blueprint['stages'] for a in s['agents']})
    return {'version': 1, 'capabilities': {key: {'sha256': catalog[key]['sha256'], 'base': catalog[key]['base'],
                                               'source': catalog[key]['source']} for key in ids}}
