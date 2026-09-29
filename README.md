# Astra：从业务目标到可重复运行的 AI 工作团队

<p align="center">
  <img src="docs/assets/astra-poster.png" alt="Astra 宣传海报：从业务目标到业务成果，业务项目设计器与 Agent 工作流运行时" width="640" />
</p>

**Astra 帮助你把自然语言描述的业务需求，整理成有明确分工、输入、执行步骤和交付物的 Agent 工作流，并保存为可以反复使用的业务项目。**

你可以描述“我需要一个客户反馈分析团队”，与设计器确认处理规则和报告要求，再生成项目。以后每次运行这个项目，只需按要求提交本次资料，由已配置的工作流执行并保存结果。

Astra 包含两个相互配合的部分：

| 部分 | 负责什么 | 你得到什么 |
|---|---|---|
| **Astra Designer（业务项目设计器）** | 通过问答澄清需求、提出团队分工、规划并校验流程、生成项目文件 | 需求记录、团队方案、项目蓝图和业务项目 |
| **Astra Runtime（工作流运行时）** | 加载项目、执行阶段与 Agent、调用工具、保存结果和运行进度 | 实际交付物、执行记录和可恢复的状态 |

```text
描述业务目标 → 澄清需求 → 确认团队方案 → 规划蓝图 → 生成项目
                                                       ↓
                                 提交本次资料 → 执行工作流 → 查看成果
                                                       ↑
                                              下次继续使用这个项目
```

当前版本适合开发者预览和有限业务场景试用。已实现能力可以直接组合；缺少外部工具时，设计器会标明待接入事项。真实模型的理解能力、已接入工具和验收规则，共同决定最终交付质量。

## 阅读导航

- **第一次使用：** [Astra 能做什么](#astra-能做什么) → [安装与快速体验](#安装与快速体验) → [设计你的第一个工作团队](#设计你的第一个工作团队)。
- **已经有业务项目：** [配置业务项目的模型](#配置业务项目的模型) → [日常运行与恢复](#日常运行与恢复) → [文件和成果在哪里](#文件和成果在哪里)。
- **开发者：** [开发者指南](#开发者指南) → [内置能力](#内置能力) → [测试与依赖管理](#测试与依赖管理)。
- **遇到问题：** [常见问题](#常见问题)和[详细文档](#详细文档)。

## Astra 能做什么

### 适合的业务场景

Astra 更适合目标相对稳定、输入可以说明清楚、结果有检查标准的重复性工作。

| 场景 | 可以组织的工作流程 | 使用前需要明确 |
|---|---|---|
| 客户反馈分析 | 读取反馈 → 分类 → 汇总 → 生成改进建议 | 分类规则、输入字段、报告内容 |
| 文档整理与报告 | 读取文档 → 分段分析 → 汇总 → 导出报告 | 文件格式、分析目标、交付格式 |
| 表格处理 | 读取 CSV / Excel → 筛选和计算 → 汇总 → 导出 | 表头、计算规则、关联字段 |
| 内容与文字创作 | 读取资料或历史设定 → 策划 → 写作 → 校对 → 保存文稿与状态 | 风格、篇幅、连续性要求、检查标准 |
| 公开网页整理 | 获取指定公开网页 → 提取正文 → 分析 → 输出文档 | 已知网址、信息范围、输出要求 |
| 已知业务接口调用 | 整理输入 → 调用 HTTP 接口 → 处理响应 | 接口地址、认证方式、请求与响应约定 |

这些是能力组合方向，不代表每类任务都已经有开箱即用的成品团队。实际可用能力可通过 `uv run astra-designer capabilities` 查看。

### 你不需要先理解所有技术术语

| 术语 | 通俗解释 | 示例 |
|---|---|---|
| 业务项目 | 一套保存下来的工作方法及其文件 | 客户反馈分析项目 |
| 团队 / Agent | 承担具体职责的执行单元，可以使用模型，也可以执行确定性程序 | 分类 Agent、统计 Agent、写作 Agent |
| Stage（阶段） | 一个可以观察、检查和重跑的业务步骤 | 资料准备、内容生产、结果检查 |
| Tool（工具） | 完成具体操作的程序能力 | 读取 Excel、写出 PDF、调用接口 |
| 蓝图 | 对项目目标、流程、数据和验收要求的结构化说明 | `blueprint.yaml` |
| 运行 | 用一批资料执行一次项目 | 分析本周客户反馈 |
| 验收 | 按预先声明的规则检查结果 | 报告存在、分类数量正确、输出字段齐全 |

普通使用者可以从对话和命令行开始，不必先编写 Python 或 YAML。接入新的外部服务、开发自定义工具时，则需要开发者参与。

### 当前能力边界

- 工作流主要按顺序执行，支持条件跳转和有次数限制的返工；当前不支持自动生成并行工作流。
- 支持执行前资料收集与确认；这不等于支持在任意业务阶段插入人工审稿或审批。
- 能获取指定公开网页；当前不提供通用联网搜索、登录网站和浏览器操作能力。
- 图像生成、远程数据库、邮件等尚未接入的服务，需要开发对应实现。`integration.pending@1` 只声明接口和接入要求。
- 设计器基于已有能力生成项目，不会仅凭角色名称或提示词获得新的工具能力，也不会自动编写所有缺失的服务适配器。
- **蓝图通过校验、项目可以运行、业务成果合格，是三个不同的判断。** 结构正确不代表内容质量已达标。

## 安装与快速体验

### 1. 准备环境

准备好 Astra 源码目录、终端和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。uv 用于管理 Python 环境与依赖。

项目默认使用 Python 3.12，包声明支持 Python 3.11 及以上。首次同步需要下载依赖；本机缺少适用 Python 时，uv 可下载解释器。

下文命令以 **Windows PowerShell** 为例。先打开终端，进入你保存的 Astra 源码根目录，也就是包含 `pyproject.toml` 的文件夹。所有终端命令默认在该目录执行。

```powershell
# 确认 uv 已安装
uv --version

# 创建环境并安装项目及依赖
uv sync --locked
```

无需手动创建或激活虚拟环境。后续统一通过 `uv run` 启动命令。

### 2. 先运行一个不需要模型的示例

```powershell
uv run astra run business/example_project/project.yaml
```

该示例使用确定性 Agent 演示草稿与检查流程，不需要模型服务或 API Key。运行结束后，终端会显示运行 ID 和运行记录位置；示例主要用于确认环境和引擎可以工作。

也可以先检查项目：

```powershell
uv run astra inspect business/example_project/project.yaml
```

`inspect` 用于检查项目配置、资源和依赖，不代表已经验证业务成果。

### 3. 体验“蓝图 → 项目 → 执行 → 验收”

仓库提供现成的客户反馈分析蓝图。以下流程不需要让模型重新设计：

```powershell
uv run astra-designer validate business/feedback_analysis/blueprint.yaml
uv run astra-designer generate business/feedback_analysis/blueprint.yaml --output workspace/tutorial/feedback_analysis
uv run astra-designer trial workspace/tutorial/feedback_analysis/project.yaml --timeout 120 --max-attempts 2
```

按顺序执行，前一步成功后再继续。生成目录必须不存在；如果之前已经生成过，直接运行最后一条命令，或换一个父目录，保留末级项目名 `feedback_analysis`。

这个示例使用固定数据和确定性处理规则。试运行报告会告诉你执行是否完成、声明的验收规则是否通过，以及结果保存位置。

## 设计你的第一个工作团队

### 1. 配置设计器使用的模型

设计器在需求理解、团队方案和蓝图规划时需要调用模型。准备一个 Astra 支持的 Chat Completions 兼容模型服务、服务地址、模型 ID，以及服务要求的 API Key。

```powershell
uv run astra-designer config
```

按照终端提示选择或添加服务与模型。配置时主要关注：

| 配置项 | 填什么 |
|---|---|
| `base_url` | 服务的 API 基础地址，例如本地 Ollama 的 `http://localhost:11434/v1` |
| `model` | 服务实际提供的模型 ID，不能随意填写 |
| API Key | 按提示隐藏输入，或通过配置指定的环境变量提供；免认证本地服务可留空 |
| 其他参数 | 先使用配置器提供的值，再按模型服务的支持情况调整 |

Astra 会在基础地址后追加 `/chat/completions`，不要重复填写完整聊天接口路径。模型服务需要事先启动，并提供所选模型。

模型参数保存在 `.astra-designer/model.json`，交互输入的密钥只在当前程序中使用，不写入配置文件。因此，在单独配置命令中输入过密钥后，重新启动聊天仍可能需要提供密钥。

聊天中的 `/model` 可以选择模型，`/config` 可以编辑高级参数，交互终端支持 Ctrl+L 快速切换。详细操作见[模型选择与终端界面](docs/设计器模型选择与终端界面.md)。

### 2. 开始对话并描述目标

```powershell
uv run astra-designer chat
```

如果希望自己命名会话目录：

```powershell
uv run astra-designer chat --session workspace/designs/my-feedback-team
```

在设计器中输入你的目标，例如：

> 我想建立一个可以每周使用的客户反馈分析团队。每次由我提供反馈表格，团队识别主要问题、统计各类数量，生成 Markdown 分析报告和分类结果表。报告要能追溯到原始反馈。先和我确认输入字段、分类规则及验收标准，再设计团队。

描述时尽量说明四件事：**输入是什么、要完成什么、交付什么、怎样判断完成。** 不确定的地方可以直接说明，设计器会进一步询问。

这里设计的是可以重复使用的团队。具体文件、题材、时间范围等通常属于每次运行时提交的资料，无需在最初设计时固定下来。

### 3. 澄清需求并确认团队

回答设计器提出的问题。需求整理完成后，输入“继续”获取团队方案，检查角色、分工、输入和交付要求，再输入“确认”。

以下是**设计器对话中的操作**，不要粘贴到 PowerShell；应在对应步骤完成后逐项输入：

| 顺序 | 输入内容 | 作用 |
|---|---|---|
| 1 | `/requirements` | 查看当前需求及仍需解决的问题 |
| 2 | `继续` | 基于需求获取团队方案 |
| 3 | `/team` | 查看角色与分工 |
| 4 | `确认` 或 `/confirm` | 接受当前需求与所选方案 |
| 5 | `/plan` | 检查能力并规划项目蓝图 |
| 6 | `/show` | 查看已经生成的蓝图 |
| 7 | `/generate` | 将蓝图生成项目文件 |

如果方案不符合预期，直接描述修改意见，待更新后重新确认。需求或团队变更后，需要重新规划对应蓝图。

一般业务需求无需先执行 `/input`。该命令用于添加可选的本地 JSON 验证样例；每次业务运行要提交的资料由任务输入规范描述。

### 4. 生成项目并记录路径

`/generate` 会询问新项目目录以及项目模型的配置方式。第一次使用可以接受默认目录，但请记录终端显示的 **`project.yaml` 实际路径**。默认项目可能保存在设计会话内部，不一定在 `business/` 下。

模型配置有两种选择：

- **留白，稍后配置：** 先生成项目，再填写项目中的 `configs/model.json`。
- **复制设计器当前配置：** 复制地址、模型和参数，形成独立项目配置；不会复制密钥。

生成器不会覆盖已有项目。需要新版本时，使用新的父目录，并保持最后一级目录名与蓝图中的项目名一致。

如果蓝图列出“待接入能力”，项目文件仍可以生成，但相关业务需要完成接入才能执行。检查生成的 README 和 `integrations.json`，了解缺少什么。

### 5. 提交资料并运行

先按下一节完成业务模型配置，再在终端运行生成的项目：

```powershell
# 把引号中的路径替换为生成器实际显示的路径
uv run astra run "你的项目目录"
```

对包含资料收集阶段的项目，交互终端会按照工作流询问本次资料并请求确认，然后继续执行。运行结束后打开终端列出的交付文件，检查是否满足需求。

设计器中的 `/run` 是试运行与验收入口，适用于已经具备所需资料、配置和依赖的生成项目。它不替代日常运行时的交互资料收集；新手首次使用需要接单的团队时，优先使用上面的 `astra run`。

## 配置业务项目的模型

**设计器模型负责设计团队；项目模型负责团队执行。新生成项目有独立的模型配置。**

生成项目的 `project.yaml` 通常包含：

```yaml
model_config: configs/model.json
```

如果生成时选择留白，编辑该项目的 `configs/model.json`，填写基础地址与模型。例如使用已经启动的本地兼容服务：

```json
{
  "base_url": "http://localhost:11434/v1",
  "model": "替换为服务实际提供的模型ID",
  "api_key_env": "ASTRA_RUNTIME_API_KEY",
  "timeout": 300,
  "max_output_tokens": 8192,
  "token_parameter": "max_tokens",
  "stream": false,
  "response_format": "text",
  "reasoning_effort": "auto"
}
```

参数需要符合你的模型服务要求。`api_key_env` 填的是**密钥环境变量的名称**，不是密钥本身；文件中不能添加 `api_key` 保存密钥。认证服务需要在启动项目的环境中提供对应变量，免认证本地服务可不设置。

如果生成时复制了设计器配置，密钥变量默认是 `ASTRA_DESIGNER_API_KEY`。设计时隐藏输入的密钥不会自动传给项目试运行；需提供项目配置指定的环境变量，或修改为项目专用变量名。

修改模型文件后，重新执行 CLI 命令；通过 Python 使用时重新 `engine.load()`。模型配置留白的项目可以加载和收集资料，但执行到模型节点时会报错，不会自动改用设计器模型。

运行时配置优先级如下：

1. 同时设置 `ASTRA_RUNTIME_BASE_URL` 和 `ASTRA_RUNTIME_MODEL` 时，使用运行时环境配置。
2. 否则，项目声明了 `model_config` 时，使用项目模型文件。
3. 只有未声明项目模型文件的旧项目，才保留设计器工作区配置的回退行为。

仅设置 `ASTRA_RUNTIME_STREAM` 等单个调优变量不会覆盖项目模型文件。详细参数、配置迁移和优先级见[业务项目模型配置](docs/业务项目模型配置.md)。

## 日常运行与恢复

以下命令中的项目路径、资料文件和运行 ID 都需要替换成实际值。

### 开始一次新任务

```powershell
uv run astra run "你的项目目录"
```

每次运行建立独立运行记录。对于已声明任务输入的项目，也可以通过 JSON 文件提交资料：

```powershell
uv run astra run "你的项目目录" --input "本次资料.json"
```

JSON 必须是对象，字段应符合该项目的任务输入规范，没有适用于所有团队的通用字段模板。输入文件不会改写项目配置；如果资料或确认仍不齐全，工作流会继续请求补充。

### 恢复尚未完成的任务

```powershell
uv run astra resume "你的项目目录" "实际运行ID"

# 恢复时提交补充资料
uv run astra resume "你的项目目录" "实际运行ID" --input "补充资料.json"
```

先解决原来的问题，例如补齐输入、填写模型配置或接入缺失工具，再恢复。引擎从已保存进度继续，跳过已经成功并保存的 Agent。已完成的运行恢复时返回保存状态；想重新执行，应使用 `run`。

对于包含跨运行状态读写的团队，新一次任务还可以读取项目保存的长期资料，例如小说人物设定。**开始新任务、恢复中断任务、读取长期记忆，是不同的操作。** 是否使用长期记忆由项目工作流决定。

### 查看状态和详细日志

运行器默认显示简洁进度。排查问题时：

```powershell
uv run astra run "你的项目目录" --verbose
```

程序集成场景可使用 `--json` 获取结构化输出。`--json` 或非交互终端默认不读取标准输入，缺少资料时会返回等待状态；需要终端问答时可显式使用 `--interactive`。

| 状态或提示 | 含义 | 下一步 |
|---|---|---|
| `waiting_input` | 缺少本次任务资料 | 按字段要求补充 |
| `awaiting_confirmation` | 等待确认本次资料 | 核对后确认 |
| `waiting_dependencies` | 需要的能力尚未接入 | 完成接入后恢复 |
| `completed` | 工作流执行结束 | 查看交付物，检查业务质量 |
| 执行失败 | 某个步骤未完成 | 查看原因，修复后恢复或重新运行 |

### 试运行与业务验收

对于设计器生成的项目：

```powershell
uv run astra-designer trial "你的项目目录"

# 恢复指定运行，并执行试运行验收流程
uv run astra-designer trial "你的项目目录" --resume "实际运行ID"
```

`trial` 同时接受项目目录和 `project.yaml` 文件路径；传入目录时，会读取该目录下的 `project.yaml`。

`trial` 检查生成文件一致性、在子进程中限时执行，并按声明的规则验收。`--timeout` 默认 600 秒、最多 3600 秒；`--max-attempts` 默认 1、最多 3，包含首次尝试。只有符合条件的执行失败会自动恢复重试，验收失败不会自动改写业务规则。

**`astra run` 负责执行业务工作流，不自动执行设计器的验收流程。** `trial` 的规则通过也只表示声明的检查通过，内容的事实准确性、风格和实际业务效果仍需要合适的检查标准。

## 找回设计会话与导出对话

设计会话保存需求和方案；运行记录保存某次业务执行。恢复会话不会自动恢复业务运行。

```powershell
# 列出默认 workspace/designs 下的历史会话及恢复命令
uv run astra-designer --session --list

# 恢复指定设计会话
uv run astra-designer chat --session workspace/designs/my-feedback-team

# 搜索自定义会话目录
uv run astra-designer --session "D:/我的会话" --list

# 导出已保存的会话，不调用模型
uv run astra-designer export --session workspace/designs/my-feedback-team --output workspace/exports/feedback-team.txt
```

聊天中也可以使用 `/export "workspace/exports/feedback-team.txt"`。导出文件必须是 `.txt`，已有文件不会被覆盖。旧会话如果只保存了摘要，导出无法还原此前没有记录的完整显示内容。

常用对话命令：

| 命令 | 用途 |
|---|---|
| `/help` | 查看当前版本支持的命令 |
| `/requirements`、`/team` | 查看需求和团队 |
| `/select 方案ID` | 切换团队方案 |
| `/confirm`、`/plan` | 确认设计、规划蓝图 |
| `/show`、`/generate` | 查看蓝图、生成项目 |
| `/run`、`/run-resume 运行ID` | 试运行、恢复试运行 |
| `/retry` | 重试未完成的需求或方案处理，不是重跑业务项目 |
| `/model`、`/config`、`/status` | 选择模型、编辑高级参数、查看状态 |
| `/capabilities 关键词` | 检索已登记能力 |
| `/new`、`/resume 会话目录` | 开始新目标、恢复会话 |
| `/quit` | 退出设计器 |

## 文件和成果在哪里

### 三类文件分别保存

| 类型 | 典型位置 | 主要内容 |
|---|---|---|
| 设计会话 | `workspace/designs/<会话目录>/` | 用户问答、需求、团队方案、蓝图版本和诊断记录 |
| 业务项目 | `business/<项目名>/` 或生成器显示的目录 | 工作流、Agent、提示词、模型配置和数据规则 |
| 单次运行 | `<项目目录>/output/runs/<运行ID>/` | 业务交付物、执行状态、日志和报告 |

生成项目通常包含以下文件，具体内容取决于选用能力：

```text
你的项目/
├── README.md                 项目使用说明
├── project.yaml              运行入口
├── blueprint.yaml            业务蓝图
├── configs/
│   ├── workflow.yaml         阶段与 Agent 配置
│   ├── model.json            项目模型参数
│   └── agents.yaml           配置型 LLM Agent 参数（按需生成）
├── agents/                   Agent 实现
├── tools/                    项目工具
├── prompts/                  提示词
├── data/                     固定输入资源
├── schemas/                  数据结构规则
├── tests/acceptance.yaml      业务验收规则
├── capabilities.lock.json    能力版本与摘要
├── generation.json           生成文件校验记录
└── output/                   运行后产生的资料
```

### 应该打开哪个文件

- **看业务成果：** 打开运行结束时列出的报告、文稿或表格，文件名由项目决定。
- **看执行进度与恢复依据：** 查看 `output/runs/<运行ID>/run.json` 和 `state.json`。
- **查执行问题：** 查看同目录中的 `orchestrator.log`、执行报告和 `agents/` 中的原始输出。
- **看试运行结论：** 查看 `output/trials/<试运行ID>/report.json`；验收流程生成的详细结果位于相应运行目录的 `acceptance_report.json`。
- **找跨运行记忆：** 使用 `state.load` / `state.save` 的项目将长期状态保存在 `output/state/`。

备份团队时，应保留项目文件和需要继续使用的长期状态；备份设计过程时，还应保留对应会话目录。不要把 `workspace/` 和项目 `output/` 一概当成缓存删除。

## 内置能力

设计器根据已登记能力组合工作流。下表便于了解范围，完整契约以命令输出为准：

```powershell
uv run astra-designer capabilities
uv run astra-designer capabilities "文档"
uv run astra-designer capabilities --show llm.transform@1
uv run astra-designer frameworks
```

| 能力 | 主要用途 |
|---|---|
| `llm.transform@1` | 对输入做语义分析、写作或转换，返回符合数据规则的 JSON |
| `llm.map@1` | 对数组逐项调用模型，可携带前几项结果，例如分段分析、逐章生成 |
| `document.read@1` | 读取 txt、md、docx、pdf、html 的文本；扫描版 PDF 需要先做文字识别 |
| `document.write@1` | 将文本或分章结果导出为 txt、md、docx、pdf |
| `table.read@1` | 读取 CSV 和 xlsx 表格 |
| `table.compute@1` | 按明确规则筛选、计算、排序、分组汇总和按键关联 |
| `table.write@1` | 写出 CSV 或 xlsx |
| `web.fetch@1` | 获取指定公开网页的标题与正文 |
| `http.request@1` | 调用设计时确定地址的 HTTP / JSON 接口 |
| `state.load@1` / `state.save@1` | 保存和读取跨运行状态，例如小说设定与进度；可按团队生成的编号（如作品 ID）分别保存，凭编号继续 |
| `fs.scan@1` | 只读扫描目录，按规则获取文件清单 |
| `fs.cleanup@1` | 按清单试算、隔离或删除文件；默认试算，实际变更需确认并受目录约束 |
| `flow.route@1` | 条件跳转、跳过阶段或有上限的返工 |
| `feedback.*@1` | 客户反馈示例中的读取、分类、统计与报告 |
| `integration.pending@1` | 声明待接入能力，列明输入输出及接入要求 |

本地能力模板默认位于 `workspace/capabilities/`，可用 `ASTRA_CAPABILITY_DIRS` 指定其他目录。新增模板可以复用和配置已登记能力，不能仅通过添加能力名称实现不存在的工具。

## 开发者指南

### 技术栈与 NVIDIA 生态接入方向

Astra 将工作流编排、Agent 执行框架与业务能力分层组织。NVIDIA 生态的接入规划沿用这一结构，优先通过已有能力包机制扩展具体业务功能。

| 层次 | 技术与职责 | 当前状态 |
|---|---|---|
| 工作流与设计器 | Astra Runtime / Designer：规划、生成、执行、校验与恢复业务项目 | 已实现 |
| Agent 执行框架 | Astra 原生 Agent、配置型 LLM Agent，以及可选的 Qwen Agent 运行时适配器 | 已实现；Qwen 尚无 Designer 生成适配器 |
| 可执行能力扩展 | 能力包声明输入输出、参数与依赖，由执行入口调用 Python 实现、SDK、CLI 或外部服务 | 已实现，见[能力包说明](docs/能力包.md) |
| NVIDIA 技能来源 | [NVIDIA Agent Skills](https://github.com/NVIDIA/skills)：提供 NVIDIA 产品的使用指令、参考资料与工作流指导 | 规划接入，实现技能加载与执行支持 |
| NVIDIA 产品能力 | 将 NeMo Retriever 检索、cuOpt 优化求解等具体能力封装为 Astra 能力包或工具 | 扩展方向，尚未接入 |

NVIDIA Skills 是可移植技能目录；实际检索、计算等操作由背后的产品 SDK、CLI 或服务完成。因此，接入时先选择具体业务能力，定义结构化输入输出，并复用 Astra 的依赖检查、包版本与哈希锁定、缺依赖暂停和恢复机制。技能内容可作为实现与配置指南。

未来将独立出 `astra_nvidia` 可作为 NVIDIA 服务与 SDK 集成包的名称，其职责是封装具体产品能力；通用技能加载逻辑归入通用技能层。上述名称均为规划建议，目前未新增对应模块或依赖。

### 架构与源码入口

```text
Astra/
├── astra/                    稳定公开 API
├── astra_core/               项目加载、编排、工具、Hook、模型服务与运行状态
├── astra_designer/           需求探索、团队方案、蓝图、生成与验收
├── astra_qwen/               可选 Qwen 运行时适配器
├── astra_openai/             预留适配器
├── business/                 可运行的业务项目
├── workspace/                本地设计记录、能力模板及工作资料
├── docs/                     使用说明与架构文档
├── scripts/                  演示与测试入口
├── tests/                    自动化测试
├── pyproject.toml            包与依赖声明
└── uv.lock                   依赖锁文件
```

运行时采用 **Stage → Agent → Tool** 的结构。阶段定义业务步骤，Agent 负责领域处理，工具执行文件读写或外部调用；Hook 处理横跨生命周期的扩展逻辑。

`project.yaml` 是引擎与业务项目之间的加载契约，项目资源路径以它所在目录为基准。项目可以位于 `business/` 之外，但其 Python 包与依赖必须可导入。

### 使用 Python API

```python
from astra import engine

runtime = engine.load("business/example_project/project.yaml")

# 检查配置与依赖，不执行业务工作流
info = runtime.inspect()

# 开始一次完整运行
state = runtime.run()
run_id = state["data"]["run"]["id"]

# 恢复这次运行；已完成时直接返回保存状态
resumed = runtime.resume(run_id)
```

也可以使用 `runtime.run_stage("实际阶段ID")` 单独运行一个阶段。它创建独立运行，默认不进入后续阶段，也不会隐式执行工作流开头的资料收集阶段。

每次运行使用独立的工具、模型配置、Hook 和日志对象。引擎不依赖隐式的全局当前项目，也不修改进程工作目录。业务代码应使用明确的项目路径，避免依赖启动位置。

### 创建和扩展业务项目

可以先由设计器生成项目，再阅读其结构；手工编写项目时，可参考 [example_project](business/example_project/README.md) 和[架构说明](docs/当前架构技术说明.md)。

一个项目清单的主要字段如下；其中 Python 包、目录与配置文件需要实际存在：

```yaml
name: my_project
package: business.my_project
workflow_config: configs/workflow.yaml
model_config: configs/model.json

tools_dir: tools
hooks_dir: hooks
extensions:
  - astra_core.extensions.default_tools
  - astra_core.extensions.project_tools
  - astra_core.extensions.project_hooks
  - astra_core.extensions.workflow_artifacts

paths:
  data: data
  prompts: prompts
  output: output

initial_task: 执行业务任务
```

常见扩展方式：

| 目标 | 实现位置与约定 |
|---|---|
| 新增业务处理逻辑 | 在项目 `agents/` 中实现 `BaseAgent`，在工作流中注册 |
| 接入新的数据源或外部服务 | 在项目 `tools/` 中实现并注册工具，声明依赖与认证方式 |
| 添加执行前后检查、记录或通知 | 在项目 `hooks/` 中注册生命周期 Hook |
| 集成 SDK 或共享运行时对象 | 编写 Extension，并在 `project.yaml` 中按依赖顺序声明 |
| 让设计器复用能力 | 提供能力契约、实现和验证；已有能力可进一步封装为本地模板 |

业务 Agent 的基础形式：

```python
from astra_core.core.base_agent import BaseAgent


class SummaryAgent(BaseAgent):
    def __init__(self, name="summary_agent"):
        super().__init__(name=name)

    def run(self, state):
        source = state.get("data", {}).get("source_text", "")
        return {
            "status": "success",
            "data": {"character_count": len(source)},
        }
```

这是一个确定性处理示例。返回的 `data` 会合并到工作流共享数据区；Agent 的导入、角色、阶段与上下游字段仍需在 `configs/workflow.yaml` 中配置。状态应可 JSON 序列化，业务字段应避免命名冲突。

Extension 导出 `register_extension(context)`。`context` 提供当前项目、编排器、输出目录，以及 `provide()` / `require()` 服务传递接口。先加载工具，再加载依赖工具的 Adapter；具体规则见[当前架构技术说明](docs/当前架构技术说明.md)。

### 可选 SDK 适配

仅在项目使用 Qwen 运行时适配器时安装对应依赖：

```powershell
uv sync --extra qwen
uv run --extra qwen astra run "你的项目目录/project.yaml"
```

后续运行仍需携带 `--extra qwen`，以保持该可选依赖被选中。Qwen 的运行时适配不等于设计器支持自动生成 Qwen Agent；当前可自动生成的框架以 `astra-designer frameworks` 输出为准，OpenAI Adapter 目录为预留。

### 生成一致性、恢复和项目分发

- 修改设计时，从更新后的蓝图生成到新目录，保留旧版本用于比较。生成器不覆盖已有文件。
- 设计器试运行会检查生成文件和能力摘要。手工修改工作流或 Agent 后，可能无法通过原生成快照的一致性检查；应通过更新能力与蓝图重新生成，或按手工项目方式验证。
- `configs/model.json` 是允许修改的项目配置，不因正常修改而触发生成文件变化错误。
- 检查点在运行和 Agent 边界保存，不能代替外部系统事务。如果外部操作成功后、进度保存前发生中断，恢复时可能重放；有副作用的工具需自行实现幂等控制。同一运行 ID 应由一个执行者恢复。
- 项目分发应包含清单、工作流、实现、提示词、Schema 和必要数据，并声明依赖。运行所需凭证由目标环境提供；既往日志和业务结果按实际迁移需要处理。
- 试运行的子进程隔离不是操作系统安全沙箱。执行项目会运行其代码，模型节点会将声明的输入发送至配置的模型服务。

## 测试与依赖管理

执行仓库的离线回归测试：

```powershell
uv run python -X utf8 scripts/run_tests.py
```

统一入口执行 `scripts/test_*.py` 和 `tests/` 下的 unittest 测试，失败时返回非零退出码。覆盖项目加载、工具与 Hook、状态恢复、项目隔离、设计器生成和验收等已实现行为；Qwen 适配测试使用本地 SDK 替身。

离线测试不代表真实模型对所有业务的理解和交付质量已经通过验证。修改模型行为或能力后，还应使用相关业务样例验证结果。

维护依赖时：

```powershell
# 修改依赖声明后更新锁文件并同步
uv lock
uv sync --locked
```

添加或移除依赖使用 `uv add 包名`、`uv remove 包名`，提交时一并提交 `pyproject.toml` 与 `uv.lock`。`.venv/`、缓存和本地运行产物不属于源码；不要将密钥提交到仓库。

## 常见问题

| 问题 | 处理方式 |
|---|---|
| 找不到 `uv` | 先安装 uv，重新打开终端并执行 `uv --version` |
| 找不到 Astra 命令或模块 | 在源码根目录执行 `uv sync --locked`，之后使用 `uv run` 启动 |
| Windows 中文乱码 | 先执行 `$env:PYTHONUTF8 = "1"`，再启动命令 |
| 不知道斜杠命令在哪里输入 | `/plan`、`/generate` 等输入在设计器对话中；`uv run ...` 输入在终端中 |
| 接口 404 或无法连接 | 检查服务是否启动、端口与 `base_url` 是否正确，不要重复添加 `/chat/completions` |
| 401 / 403 或模型不存在 | 检查服务权限、密钥来源，以及服务实际提供的模型 ID |
| 设计器能用，但项目提示模型未配置 | 检查项目 `configs/model.json`，新项目不会自动回退到设计器模型 |
| 已复制模型配置，运行仍缺密钥 | 查看项目的 `api_key_env`，在运行环境中提供对应变量；隐藏输入的密钥没有复制到项目 |
| 修改模型参数后没有变化 | 重新启动命令或重新加载项目，并检查环境变量是否覆盖文件配置 |
| 模型回复被截断、JSON 无法解析或请求超时 | 查看错误和会话诊断，检查输出长度、服务支持的格式及超时配置，处理后再重试 |
| 蓝图规划达到调用上限 | 查看失败原因；规划预算由 `max_plan_calls` 控制，详见需求探索文档。反复重试不会自动清零计数 |
| 项目目录已存在 | 执行已有项目，或生成到新的同名末级目录，避免覆盖旧版本 |
| 运行停在等待资料或依赖 | 补齐输入、完成确认或接入工具，再恢复该运行 |
| 运行结束但没有通过验收 | 查看验收报告中的预期值与实际值；检查输入、业务规则和交付物 |
| PDF 中文字体不可用 | 检查本机中文字体，或用 `ASTRA_PDF_FONT` 指定可用的 TTF 字体 |
| 生成文件被修改，试运行拒绝执行 | 核对改动，从更新后的蓝图和能力重新生成，不要删除校验文件绕过检查 |

需要排查设计问题时，查看会话的 `diagnostics/`、`/requirements` 和 `/status`；需要排查执行问题时，查看项目的运行日志。提供问题信息时附上命令、模型名称、运行或会话路径，以及脱敏后的错误文本。

## 详细文档

| 文档 | 适合什么时候阅读 |
|---|---|
| [文档索引](docs/README.md) | 查找当前说明，区分历史设计记录 |
| [Designer 使用手册](docs/Astra_Designer_使用手册.md) | 查看更完整的操作示例；其中已标注的旧流程仅供历史参考 |
| [需求探索与共识构建](docs/需求探索与共识构建使用说明.md) | 了解需求版本、确认、追溯和重试 |
| [团队方案](docs/团队协商使用说明.md) | 了解角色分工、方案选择和确认 |
| [任务输入与交互规则](docs/任务输入与交互规则.md) | 了解每次任务的资料收集与确认 |
| [业务项目模型配置](docs/业务项目模型配置.md) | 配置执行模型、调整参数和迁移旧配置 |
| [模型选择与终端界面](docs/设计器模型选择与终端界面.md) | 使用模型选择器和终端快捷操作 |
| [待接入能力](docs/待接入能力.md) | 理解缺失能力以及如何接入真实实现 |
| [执行框架适配](docs/执行框架适配使用说明.md) | 了解框架支持范围和选择规则 |
| [设计器 Prompt 与产品上下文](docs/设计器Prompt与产品上下文.md) | 维护设计器的模型指令与上下文 |
| [当前架构技术说明](docs/当前架构技术说明.md) | 开发 Agent、工具、Hook 和运行时扩展 |

历史升级提案与阶段记录用于了解演进背景，部分内容已过时；当前使用方式以本 README、当前命令帮助和文档索引列出的说明为准。
