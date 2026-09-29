# Astra Designer 使用手册

适用版本：当前仓库，含需求探索与共识构建｜更新日期：2026-09-07

本手册面向没有 Agent、项目蓝图或命令行经验的用户。示例使用 Windows PowerShell，并假设 Astra 位于 `D:\Projects\Astra`；如果你的目录不同，请替换该路径。

**名称说明：**本文所指工具是 Astra Designer。Python 模块名为 `astra_designer`，不是 `astra_design`；安装后也可使用 `astra-designer` 命令。为避免混淆，全文统一使用 `python -m astra_designer`。

## 目录

1. [认识 Astra Designer](#1-认识-astra-designer)
2. [安装与准备](#2-安装与准备)
3. [先跑一个不需要模型的示例](#3-先跑一个不需要模型的示例)
4. [配置模型：以 Ollama 为例](#4-配置模型以-ollama-为例)
5. [通过对话设计第一个业务项目](#5-通过对话设计第一个业务项目)
6. [查看文件与运行结果](#6-查看文件与运行结果)
7. [继续会话、恢复运行与修改项目](#7-继续会话恢复运行与修改项目)
8. [命令速查](#8-命令速查)
9. [常见问题](#9-常见问题)
10. [进阶：文件方式与独立运行模型](#10-进阶文件方式与独立运行模型)

## 1. 认识 Astra Designer

你先通过问答描述并确认需求，不必提供文件。确认的内容包括团队方案、每次运行的输入规范和交付成果；规划时再按内置能力检查能否实现，缺少实现的部分生成待接入节点。设计器借助大语言模型规划处理步骤，生成蓝图。蓝图通过检查后，可以生成项目，再执行项目并检查结果是否满足预设要求。

```text
描述目标 → 探索问答 → 确认需求与交付成果 → 能力检查 → 蓝图 → 生成项目 → 运行与验收
```

### 1.1 先了解这几个词

| 名称 | 可以怎样理解 |
|---|---|
| LLM / 模型 | 理解你的目标、生成设计方案的语言模型；部分业务步骤也会调用它 |
| Agent | 完成一个具体步骤的执行单元，例如读取、分类或统计 |
| 工作流（Workflow） | 各个步骤的执行顺序和数据传递关系 |
| 蓝图（Blueprint） | 项目设计说明，记录目标、输入、步骤及验收标准 |
| Schema | 数据格式规则，例如每条反馈必须有 `id` 和 `text` |
| 能力目录 | 已经登记、可以复用的 Agent 或模板列表 |
| 验收 | 按蓝图中的规则检查结果，例如分类数量正确、报告文件存在 |
| 检查点 | 保存下来的执行进度，用于失败后继续运行 |

### 1.2 当前适合做什么

适合由判断、写作、检查和确定性处理组成的业务团队：长文写作（逐章生成、跨运行续写）、文档与网页资料整理、表格统计与报表、调用已知接口、按规则扫描和清理本机指定目录等。内置能力见 README 的“内置能力”表；语义处理由配置型 LLM Agent 完成。

目前采用串行工作流。尚不支持任意需求自动生成 Python 实现、自动安装外部能力、远程部署或自动修改业务逻辑。`chat` 是业务设计问答入口，不是通用闲聊助手；内置能力做不到的外部服务（图像生成、邮件、远程数据库等）会生成待接入节点，接入前运行到该步骤会暂停，前面的步骤照常完成。

**设计成功、执行完成、业务验收通过是三个不同结果。**蓝图合法不代表项目已经运行；运行完成也不代表结果符合业务要求。

## 2. 安装与准备

### 2.1 打开终端并进入项目目录

在 Windows 开始菜单搜索并打开 PowerShell，输入：

```powershell
cd D:\Projects\Astra
python --version
```

需要 Python **3.11 或更高版本**。如果提示找不到 `python`，先安装 Python 并使其可从终端访问，再重新打开 PowerShell。

### 2.2 创建独立环境并安装

首次使用时执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m astra_designer --help
```

虚拟环境用于将 Astra 的依赖与其他 Python 项目分开。安装需要下载依赖；最后一条命令应显示 `config`、`chat`、`design`、`validate`、`generate`、`capabilities`、`trial` 等子命令。

如果 PowerShell 不允许执行激活脚本，可以直接使用虚拟环境中的 Python，无需更改系统策略：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m astra_designer --help
```

采用这种方式时，后文所有 `python` 都替换为 `.\.venv\Scripts\python.exe`。

以后重新打开终端，只需进入 Astra 目录并激活已创建的环境，不必重复创建或安装。全文命令默认在 Astra 根目录执行，这也决定了默认模型配置和本地能力目录的位置。

如果中文输出乱码，可在当前 PowerShell 中先执行 `$env:PYTHONUTF8 = "1"`，再运行命令；也可将命令前缀改为 `python -X utf8 -m astra_designer`。

## 3. 先跑一个不需要模型的示例

这一步确认安装和执行流程正常，**不需要 Ollama、API Key 或真实模型请求**。示例使用固定关键词分类和预设建议。

### 3.1 检查示例蓝图

```powershell
python -m astra_designer validate examples/blueprints/feedback_analysis/blueprint.yaml
```

输出中的 `valid` 应为 `true`。这表示蓝图通过静态检查，还没有执行业务流程。

### 3.2 生成并运行

```powershell
python -m astra_designer generate examples/blueprints/feedback_analysis/blueprint.yaml --output workspace/tutorial/feedback_analysis --run
```

该命令会生成项目、读取 6 条示例反馈、分类统计并写出报告。试运行结果应包含：

```json
{
  "status": "passed",
  "passed": true
}
```

实际输出还有其他字段。示例预期数量为：性能 2 条、功能 2 条、易用性 1 条、其他 1 条。

业务报告位于：

```text
workspace/tutorial/feedback_analysis/output/runs/<运行ID>/feedback_report.md
```

可以用文本编辑器打开 Markdown（`.md`）文件。若执行失败，先按第 9 节排查环境，再进入真实模型设计流程。

### 3.3 再次运行已有项目

```powershell
python -m astra_designer trial workspace/tutorial/feedback_analysis/project.yaml
```

不要重复使用同一目标目录执行 `generate`：生成器拒绝覆盖已有目录。只想再跑一次时使用 `trial`；需要生成另一份时，可将输出改为 `workspace/tutorial-v2/feedback_analysis`，最后一级目录仍须与蓝图项目名一致。

另有 `python scripts/demo_designer.py` 可演示完整的设计、澄清、生成和验收流程；其中模型是离线替身，不代表真实模型的设计质量。

## 4. 配置模型：以 Ollama 为例

### 4.1 准备可用的 Ollama 服务

本节假设你已安装 Ollama，并下载了一个可用的文本模型。打开 Ollama，确认服务可用。在浏览器打开以下地址，可以查看服务提供的模型列表：

```text
http://localhost:11434/v1/models
```

记下列表中的模型 `id`，稍后原样填入。模型名必须与本机实际提供的名称一致。

Astra 的 `base_url` 填写 API 基础地址 `http://localhost:11434/v1`，程序自动追加 `/chat/completions`。本地 Ollama 默认无需认证。接口及认证说明见 [Ollama 兼容接口文档](https://docs.ollama.com/api/openai-compatibility)和[认证文档](https://docs.ollama.com/api/authentication)。

### 4.2 在 Astra 中填写配置

```powershell
python -m astra_designer config
```

按提示逐项填写：

| 提示 | 本地 Ollama 填写示例 | 说明 |
|---|---|---|
| API 基础地址 `base_url` | `http://localhost:11434/v1` | 填到 `/v1`；不要追加 `/chat/completions`，也不要填原生 `/api/chat` |
| 模型名 | 本机模型列表中的完整 `id` | 不要照抄不存在的模型名 |
| 超时秒数 | `180` | 单次模型请求等待上限；此值只是起步配置，可按机器速度调整 |
| 输出 token 上限 | `8192` | 限制模型生成长度；不是输入字数，也不保证模型一定生成这么多 |
| 上限字段 | `max_tokens` | Ollama 配置使用该字段 |

提示中的方括号表示当前值；直接回车保留当前值。配置保存在：

```text
.astra-designer/model.json
```

**保存配置只检查参数格式，不会验证模型能否成功响应。**首次设计时才会实际调用模型。

### 4.3 API Key 怎么填写

启动对话并首次调用模型时，可能提示输入 API Key。本地免认证 Ollama 直接回车即可；有认证的服务填写服务方提供的密钥。隐藏输入时屏幕不显示字符，这是正常现象。

交互输入的密钥只在本次程序中使用，不写入配置文件。若采用非交互命令，需要认证的服务可通过当前 PowerShell 会话的环境变量提供密钥：

```powershell
$env:ASTRA_DESIGNER_API_KEY = "替换为你的密钥"
```

该写法可能留在终端历史中；优先使用交互隐藏输入，不要把密钥放进蓝图或提交到项目文件。

### 4.4 其他兼容模型服务

同样使用 `config`，填写服务方提供的完整 Chat Completions 兼容接口和模型名。输出上限字段需按服务要求选择 `max_tokens` 或 `max_completion_tokens`。

默认情况下，项目中的 LLM Agent 复用工作区模型配置。设计与运行想用不同模型时，参见第 10.2 节。

## 5. 通过对话设计第一个业务项目

### 5.1 默认方式：先探索和确认需求

```powershell
python -m astra_designer chat --session workspace/designs/my-requirements
```

直接描述业务目标并回答问题，不需要先 `/input`。需求整理完成后输入“继续”获取团队方案，检查后输入“确认”，再 `/plan`。

用 `/requirements` 查看当前需求、待解决问题和任务输入规范（每条需求的原话依据保存在会话目录的 `requirements.yaml`）；某一轮处理失败时，界面会直接显示原因，`/requirements` 顶部也会显示。确认后直接输入修改意见会撤销当前确认，形成新版本。重新打开同一个 `--session` 可以继续。探索阶段支持通用业务描述，不需要样例文件；确认后 `/plan` 规划蓝图，完成后 `/generate`。`/input` 只用于为客户反馈示例添加 JSON 样例，一般不需要。

完整说明见 [需求探索与共识构建](需求探索与共识构建使用说明.md)。需求探索不设累计调用次数上限。方案分为需求核对、团队角色、输入规范三步，每步最多自动修正一次，通过后自动继续；失败后可手动 `/retry` 或 `/continue` 从失败步骤恢复，累计次数保留用于统计。

**以下 5.2–5.7 记录的是已移除的旧版直接蓝图对话，仅供了解历史行为；请按 5.1 的流程操作。** 其中 `/input`、`/show`、`/generate`、`/run` 在默认对话中仍可使用，但需先建立并确认需求。非交互场景可使用 `design` 子命令。

### 5.2 启动并固定会话目录

```powershell
python -m astra_designer chat --session workspace/designs/my-first-design
```

程序显示 `你>` 后，以下 `/input`、`/show` 等命令应输入在这个对话中，**不是 PowerShell 中**。

### 5.3 添加样例输入

先输入：

```text
/input
```

依次回答：

| 提示 | 输入 |
|---|---|
| 资源标识 | `customer_feedback`，也可直接回车 |
| JSON 文件路径 | `examples/blueprints/feedback_analysis/fixtures/feedback.json` |
| Schema | `feedback_records`，也可直接回车 |

成功后会提示“输入已添加”。用 `/inputs` 可以查看当前资源。

样例文件的内容形式如下：

```json
[
  {"id": "F001", "text": "页面加载很慢"},
  {"id": "F002", "text": "希望支持导出分析结果"}
]
```

使用自己的文件时，应保存为 UTF-8 JSON，并满足选择的 Schema；把 Excel 或 CSV 文件改成 `.json` 后缀并不会转换格式。首次练习请使用仓库完整的 6 条样例数据。

### 5.4 输入清楚的业务目标

复制下面整段作为一次输入：

```text
请创建名为 feedback_analysis 的项目，读取 customer_feedback。按有序关键词分类：包含“慢”或“卡顿”归为性能；包含“导出”或“功能”归为功能；包含“按钮”或“操作”归为易用性；其余归为其他。统计各类数量，生成 Markdown 改进建议报告，建议使用固定模板。本次样例验收要求：性能 2 条、功能 2 条、易用性 1 条、其他 1 条，并且报告文件存在。请优先使用已有确定性能力。
```

好的目标应交代：**输入是什么、如何处理、输出是什么、怎样判断正确**。设计器主要依据目标、资源声明和你的回答规划，不要假设它已自动理解文件中的所有业务含义。

此示例在“设计”时调用模型，在生成后的“业务执行”中使用确定性 Agent。日后需要按语义理解反馈时，可明确要求复用 `feedback.semantic_classify@1`；这类项目运行时也会调用模型，结果需要实际验收。

### 5.5 回答补充问题

设计器可能要求确认分类规则或验收要求，直接回答当前问题即可。模型输出不固定，不保证与你看到的示例措辞相同。

| 设计状态 | 含义 | 你需要做什么 |
|---|---|---|
| `needs_clarification` | 信息不足 | 回答问题；必要时先 `/input` 补充资源 |
| `ready` | 蓝图通过检查 | `/show` 查看，然后 `/generate` |
| `unsupported` | 当前能力不能完成目标 | `/new` 开始范围更明确的新目标 |
| `failed` | 模型调用或设计检查未成功 | 排查原因后 `/retry` |

每个设计会话的模型请求次数上限由 `.astra-designer/settings.json` 的 `max_plan_calls` 决定（默认 6，`null` 表示不限，也可用环境变量 `ASTRA_DESIGNER_MAX_PLAN_CALLS` 覆盖）；模型截断、连接失败等模型侧错误不计入；`/retry` 不会重置计数。达到上限后可调整该设置，或检查目标和模型配置后新建会话。

### 5.6 查看蓝图并生成

设计状态为 `ready` 后输入：

```text
/show
```

重点核对项目名、输入来源、分类规则及验收数量。确认后输入：

```text
/generate
```

第一次使用建议直接回车接受默认目录。程序会显示实际项目路径，例如：

```text
workspace/designs/my-first-design/candidate/feedback_analysis/project.yaml
```

若模型使用了其他项目名，以实际蓝图和终端输出为准。生成完成不代表项目已经执行。

### 5.7 执行与退出

```text
/run
```

验收通过时显示 `业务验收: True`，并打印报告路径及运行 ID。记录这两个信息，便于查看结果和恢复。

```text
/quit
```

已提交的设计会话会保留。尚未提交给设计器的输入与答案可能没有保存，退出前应完成当前一轮问答。

## 6. 查看文件与运行结果

### 6.1 三类目录不要混淆

| 类型 | 典型位置 | 用途 |
|---|---|---|
| 设计会话 | `workspace/designs/my-first-design` | 保存问答、输入快照和蓝图版本 |
| 生成项目 | 会话中的 `candidate/feedback_analysis` | 保存可运行配置与业务文件 |
| 单次运行 | 项目中的 `output/runs/<运行ID>` | 保存本次执行状态与业务产物 |

设计器显示的 `blueprint_path` 是本次蓝图的准确位置。不要根据目录名称猜测版本文件路径。

### 6.2 生成项目里有什么

```text
feedback_analysis/
├── project.yaml             项目入口
├── blueprint.yaml           可追溯的业务蓝图
├── configs/workflow.yaml    工作流配置
├── configs/agents.yaml      LLM Agent 配置（包含此类 Agent 时生成）
├── agents/                  Agent 实现
├── data/                    生成时复制的输入数据
├── prompts/                 提示词
├── schemas/                 数据格式规则
├── tests/acceptance.yaml    业务验收条件
├── capabilities.lock.json   使用的能力版本与摘要
├── generation.json         生成文件的校验摘要
└── output/                  执行后产生的结果
```

输入是生成时的副本。修改最初提供的 JSON 文件，不会自动更新已生成项目。

### 6.3 应该打开哪个报告

| 文件 | 用途 |
|---|---|
| `output/trials/<试运行ID>/report.json` | 总览：是否通过、尝试次数、运行 ID、验收结果、耗时 |
| `output/runs/<运行ID>/acceptance_report.json` | 各条验收条件是否通过 |
| `output/runs/<运行ID>/feedback_report.md` | 客户反馈示例的业务报告 |
| `output/runs/<运行ID>/state.json` | 保存的运行状态和检查点 |
| `output/runs/<运行ID>/orchestrator.log` | 排查具体失败原因的执行日志 |

其他业务项目的交付文件名可能不同，以蓝图和运行结果为准。`passed: true` 只表示已声明的规则通过，仍应阅读交付物并判断其是否满足实际业务需要。

## 7. 继续会话、恢复运行与修改项目

### 7.1 继续设计问答

在 PowerShell 中：

```powershell
python -m astra_designer chat --session workspace/designs/my-first-design
```

或在已打开的对话中：

```text
/resume workspace/designs/my-first-design
```

这是恢复**设计会话**，不会恢复业务执行。对话会自动查找默认 `candidate/<项目名>` 下的项目；如果生成时用了自定义目录，重新打开会话后请通过 `trial` 指定实际项目路径运行。

### 7.2 从检查点恢复业务执行

先修复失败原因，例如重新启动模型服务，再在 PowerShell 中执行：

```powershell
python -m astra_designer trial workspace/designs/my-first-design/candidate/feedback_analysis/project.yaml --resume <运行ID>
```

或在对应项目的设计对话中输入：

```text
/run-resume <运行ID>
```

用报告里的实际 `run_id` 替换占位符，不要使用试运行目录的 ID。恢复会跳过已经成功并保存进度的 Agent；已完成的运行直接读取保存状态。若想从头重新执行，使用不带 `--resume` 的 `trial`。

### 7.3 设置有限重试

```powershell
python -m astra_designer trial workspace/tutorial/feedback_analysis/project.yaml --timeout 120 --max-attempts 2
```

`--timeout` 是本次所有执行尝试共用的总秒数，默认 600，最多 3600；它与模型配置中的单次请求超时不同。`--max-attempts` 包含首次执行，默认 1，最多 3。

仅执行失败且已有运行 ID 时自动恢复检查点。验收失败、超时、进程异常不会自动重试。超时报告可能没有运行 ID，需要核对该项目 `output/runs` 中的记录。

### 7.4 修改需求或输入

对话中的已完成设计目前不能直接增量修改。新手可用 `/new` 开始新目标，重新添加输入并描述变化，然后生成到新目录。

熟悉蓝图后，也可以在独立设计目录维护蓝图与输入，先 `validate`，再生成新版本。不要直接改动旧生成项目的文件或删除校验记录：试运行会发现生成文件变化并拒绝执行。保留旧版本，便于比较结果。

## 8. 命令速查

### 8.1 PowerShell 命令

| 命令（统一前缀为 `python -m astra_designer`） | 用途 |
|---|---|
| `--help` | 查看全部命令 |
| `config` | 配置模型 |
| `chat` | 开始需求探索问答 |
| `chat --session <会话目录>` | 指定或继续会话 |
| `capabilities` | 列出可复用能力 |
| `capabilities "客户反馈"` | 按关键词检索 |
| `capabilities --show feedback.semantic_classify@1` | 查看能力详情 |
| `validate <蓝图路径>` | 检查蓝图 |
| `generate <蓝图路径> --output <新项目目录>` | 生成项目 |
| `trial <project.yaml路径>` | 试运行并验收 |

### 8.2 对话命令

| 命令 | 用途 |
|---|---|
| `/help` | 查看帮助 |
| `/requirements` | 查看需求、待解决问题和任务输入规范 |
| `继续`、`/team`、`/select 方案ID` | 获取团队方案、查看角色分工、切换方案 |
| `确认` 或 `/confirm` | 确认当前需求与所选方案 |
| `/plan` | 规划蓝图 |
| `/input`、`/inputs` | 添加输入、查看输入 |
| `/capabilities 关键词` | 检索能力 |
| `/show`、`/generate` | 查看蓝图、生成项目 |
| `/run`、`/run-resume <运行ID>` | 新运行、恢复运行 |
| `/retry` | 重试未完成的需求处理，不是重试业务运行 |
| `/resume <会话路径>` | 继续设计会话 |
| `/new` | 开始新目标 |
| `/config` | 修改模型配置 |
| `/quit` | 退出 |

本地能力目录默认为 Astra 根目录下的 `workspace/capabilities/`。能力名中的 `@1` 是版本号；新手直接复用即可，模板编写参见[第四阶段说明](设计器第四阶段使用说明.md)。

## 9. 常见问题

| 现象 | 建议处理 |
|---|---|
| `No module named astra_designer` | 确认模块名，进入 Astra 根目录，使用安装依赖时的同一个 Python 环境 |
| 提示尚未配置模型 | 在 Astra 根目录运行 `config`，确认 `.astra-designer/model.json` 存在 |
| Ollama 连接失败 | 确认服务已启动，并能打开本地模型列表地址；检查端口与完整接口路径 |
| 接口返回 404 | 检查 `base_url` 是否包含服务所需的版本或代理前缀；Astra 会追加 `/chat/completions` |
| 模型不存在 | 使用服务模型列表中的完整模型名，注意版本或标签 |
| 401 / 403 | 核对服务是否要求密钥及密钥权限；本地免认证 Ollama 通常无需密钥 |
| 修改配置后仍使用旧值 | 已设置的 `ASTRA_DESIGNER_*` 环境变量优先于文件；运行模型还可能被 `ASTRA_RUNTIME_*` 覆盖 |
| 输入不符合 Schema | 检查 JSON 语法、字段名和字段类型；先用仓库样例验证流程 |
| 模型输出无法解析或被截断 | 简化目标，检查服务的结构化输出表现与输出长度配置；排查后再 `/retry` |
| 设计调用次数已用完 | 检查失败原因，再用新会话；反复 `/retry` 不会增加次数 |
| 项目目录已存在 | 执行已有项目用 `trial`；重新生成请选择不存在的新目录 |
| 目标目录名必须与项目名一致 | 最后一级使用蓝图项目名，例如 `workspace/v2/feedback_analysis` |
| 生成文件或能力定义已变化 | 从经过检查的蓝图与能力定义生成新项目，不要移除锁定文件绕过检查 |
| 运行完成但验收未通过 | 查看验收报告的预期值和实际值；核对输入是否匹配预期数量，重试不会自动修正业务规则 |
| 执行超时 | 查看原运行日志；按实际需要调整模型请求超时及试运行总超时，然后决定恢复或重新运行 |

试运行报告还会用 `load_failed` 表示加载或恢复失败，用 `process_failed` 表示进程异常。先核对路径、运行 ID 和 Python 环境，再查看日志。

需要求助时，提供命令、Python 版本、模型名称、报告状态和脱敏后的错误信息；不要提供 API Key。

## 10. 进阶：文件方式与独立运行模型

首次使用可跳过本节。

### 10.1 从目标文件设计

创建 `workspace/request/`，用文本编辑器保存以下 UTF-8 文件。将第 5.3 节目标保存为 `goal.txt`；将完整样例反馈复制到同目录的 `feedback.json`；创建 `inputs.json`：

```json
{
  "customer_feedback": {
    "path": "feedback.json",
    "schema": "feedback_records"
  }
}
```

输入路径相对于 `inputs.json` 所在目录。执行：

```powershell
python -m astra_designer design --goal-file workspace/request/goal.txt --inputs workspace/request/inputs.json --session workspace/designs/file-design
```

该命令不会弹出密钥输入，需要认证时先提供环境变量。如果返回补充问题，将实际问题 `id` 与答案写入 `answers.json`，例如问题 ID 确实为 `rules` 时：

```json
{"rules": "采用目标中给出的关键词规则与固定数量验收"}
```

继续同一会话：

```powershell
python -m astra_designer design --session workspace/designs/file-design --answers workspace/request/answers.json
```

`ready` 后使用输出的 `blueprint_path` 执行 `generate`。设计命令退出码为：准备完成 0、需要补充信息 2、不支持 3、失败 1。需要补充信息不等于程序故障。

### 10.2 为业务执行单独配置模型

通常不需要此设置。若设计使用一个服务、运行使用另一个服务，可在当前 PowerShell 窗口设置：

```powershell
$env:ASTRA_RUNTIME_BASE_URL = "http://localhost:11434/v1"
$env:ASTRA_RUNTIME_MODEL = "替换为本机实际模型名"
$env:ASTRA_RUNTIME_TIMEOUT = "180"
$env:ASTRA_RUNTIME_MAX_OUTPUT_TOKENS = "8192"
$env:ASTRA_RUNTIME_TOKEN_PARAMETER = "max_tokens"
```

`ASTRA_RUNTIME_BASE_URL` 与 `ASTRA_RUNTIME_MODEL` 必须同时设置。独立运行配置不会自动继承设计器的 API Key，需要认证时另设 `ASTRA_RUNTIME_API_KEY`。

这些变量只影响当前终端及其启动的程序。关闭该终端可结束这次临时设置。使用自定义配置文件时，`config --file <路径>` 与 `chat --config <路径>` 需对应；独立 `trial` 没有 `--config` 参数，应使用默认工作区配置或上述运行环境变量。

### 10.3 使用边界

配置型 LLM Agent 会将其声明的输入发送给配置的模型服务，使用前应确认服务与数据范围。子进程用于避免项目导入状态串用，并非安全沙箱，只应执行可信项目。

检查点恢复不能保证外部操作只发生一次：如果操作完成后尚未保存进度就退出，恢复时可能重放。涉及业务写入时，需由相应工具实现幂等控制。

详细技术行为参见[第五阶段说明](设计器第五阶段使用说明.md)；完整升级背景参见[升级方案](Astra_智能业务项目设计器升级方案.md)。

### 导出易读的对话 TXT

在对话中输入（必须提供 `.txt` 文件路径）：

```text
/export "D:\导出\我的对话.txt"
```

也可以退出会话后独立导出，无需配置或调用模型：

```powershell
python -m astra_designer export --session "workspace/designs/my-requirements" --output "D:\导出\我的对话.txt"
```

TXT 文件仅在执行导出时生成，不自动维护 TXT 副本。独立导出不修改原会话；交互 CLI 会另外记录本轮命令及输出。导出路径支持中文、空格和相对路径（相对当前工作目录），不存在的父目录会自动创建；已有文件不会被覆盖，请换一个文件名。

TXT 使用支持中文的 UTF-8 编码。需求对话 CLI 在 `cli-history.json` 中保存实际输入与显示文字，导出包含需求清单、回复预览、重试和失败提示；“继续”保留原文，不替换为内部预算请求。动态进度刷新、密钥和 `/config` 配置向导不纳入记录。当前 `/export` 命令完成后才记入历史，因此它本身出现在下一次导出中。

旧需求会话在启用 CLI 记录前仅有发言、回复摘要和追问，导出以历史摘要注明这一限制，失败状态也会显示；无法恢复此前未保存的完整终端输出。旧版蓝图会话仍仅包含已保存的目标、历史问答和最近回复。更新代码后，请退出运行中的 CLI，再用原会话路径恢复，以启用记录。
