# Example Business Project

This is a minimal business project that can be loaded by the agent runtime.
It is intentionally small and does not depend on an LLM SDK or external tools.

Run it with:

```bash
python main.py business/example_project/project.yaml
```

Project layout:

```text
project.yaml             Project manifest loaded by the runtime
configs/workflow.yaml    Workflow, stages, agents, hooks, and outputs
configs/model.json       Project model settings and API key environment variable name
agents/                  Business agents
tools/                   Optional business tools
data/                    Business input and intermediate data
prompts/                 Optional prompt files
output/                  Runtime artifacts
```

The workflow has two simple stages:

1. `draft`: `DraftAgent` creates a small structured draft.
2. `review`: `ReviewAgent` reads the draft and marks it accepted.
# Example Project

This project demonstrates the minimum distributable business-project layout for the runtime.

```python
from astra import engine

runtime = engine.load("business/example_project/project.yaml")
runtime.inspect()
state = runtime.run()
runtime.run_stage("draft")
runtime.resume(state["data"]["run"]["id"])
```

Each `runtime.run()` call writes its logs, reports, raw agent outputs, and `run.json`
to `business/example_project/output/runs/<run_id>/`.

Project-level Hook, Tool, and Adapter integrations are declared through the single
`extensions` list in `project.yaml`. An extension exports:

```python
def register_extension(context):
    ...
```

The `distribution` section records the package version, compatible runtime range,
and business resources to include when this project is copied or packaged.

## 模型配置

`project.yaml` 通过 `model_config: configs/model.json` 引用项目模型配置，引擎加载项目时读取并校验。

示例默认将 `base_url` 和 `model` 留空，方便后续自行配置。当前两个 Agent 都是确定性实现，不调用模型，因此无需填写即可运行。

接入配置型 LLM Agent 时，请填写 `base_url`（例如 `https://api.openai.com/v1`）和模型 ID，并根据服务调整参数。Astra 自动追加 `/chat/completions`，不要把它写入基础地址。`api_key_env` 是密钥环境变量名（默认 `ASTRA_RUNTIME_API_KEY`），不要将密钥写入 JSON。配置留白时，LLM 节点会明确报错，不会自动使用设计器模型。

同时设置 `ASTRA_RUNTIME_BASE_URL` 与 `ASTRA_RUNTIME_MODEL` 可覆盖项目配置。修改文件后需重新加载项目；重新执行 CLI 命令会重新加载。

完整说明见 [业务项目模型配置](../../docs/业务项目模型配置.md)。
