# Unattended Software Development Harness — Agent Guide

> 本文档供 AI Coding Agent 阅读。如果你对人类用户可见的概览感兴趣，请查看 `README.md`；如果你需要架构细节，请查看 `ARCHITECTURE.md`。

---

## 项目概述

本项目是一个**自托管的无人看守软件开发工作流 Harness**，基于 [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents) 架构理念实现。它通过编排多个 LLM Agent 来自动化完成软件需求分析、设计、编码、测试和交付的全流程。

项目采用 LLM 动态生成 DAG 的智能多 Agent 编排架构（支持并行执行、失败自适应）。

---

## 技术栈

- **语言**：Python 3.11+
- **核心依赖**：
  - `anthropic>=0.40.0` — Anthropic API 客户端
  - `openai>=1.50.0` — OpenAI 兼容 API 客户端
  - `pydantic>=2.0.0` — 数据模型与配置校验
  - `pyyaml>=6.0` — YAML 工作流与 Agent 配置解析
- **开发/测试依赖**：
  - `pytest>=8.0.0`
  - `pytest-cov>=5.0.0`
  - `flake8>=7.0.0`
- **包管理**：仅使用 `requirements.txt` 为主，项目当前同时存在 `pyproject.toml`（用于工具配置）。

---

## 项目结构

```
harness/
├── core/                          # 核心模型与引擎
│   ├── models.py                  # 所有 Pydantic 数据模型（DAG、Event、Session、Guardrail 等）
│   ├── config.py                  # 配置管理（HarnessConfig、LLMConfig、SandboxConfig、MCPConfig）
│   ├── agent_registry.py          # Agent 能力注册表（默认 planner/generator/evaluator + 项目自定义）
│   ├── llm_client.py              # 统一 LLM 客户端（Anthropic / OpenAI）
│   └── dag_engine.py              # DAG 拓扑调度与并行执行引擎
├── agent/                         # Agent Worker 层
│   ├── worker.py                  # 单 Agent LLM 调用循环
│   └── agent_pool.py              # Agent 实例池（独立上下文、延迟创建）
├── orchestrator/                  # 编排层
│   └── intelligent_orchestrator.py # 智能编排 Agent（LLM 驱动规划与失败处理）
├── tools/                         # 工具层
│   └── registry.py                # 工具注册表（read/write/edit/bash/glob/grep/git + MCP 扩展位）
├── guardrails/                    # 安全与权限
│   └── policy.py                  # 四层防御策略（RiskLevel、PermissionMode、Guardrails）
├── evaluator/                     # 自动化评估
│   └── engine.py                  # 成功标准检查器（pytest、flake8/ruff、coverage、文件存在性）
├── session/                       # 状态持久化
│   └── store.py                   # 追加式 JSONL 事件存储与状态恢复
├── reporter/                      # 报告与审计
│   └── logger.py                  # Session Markdown 报告生成器
├── projects/                      # 项目自定义 Agent 示例
│   └── example/
│       └── agents.yaml            # 自定义 Agent 配置样例（ui_designer/db_admin/security_auditor）
├── main.py                        # CLI 入口
├── requirements.txt               # 依赖列表
├── README.md                      # 面向人类的项目说明（中文）
└── ARCHITECTURE.md                # 架构设计文档（中文）
```

---

## 构建与运行命令

### 安装依赖

```bash
pip install -r requirements.txt
```

### 环境变量配置

至少设置以下之一：

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# 或
export OPENAI_API_KEY="sk-..."
# 可选：指定模型
export HARNESS_MODEL="gpt-4"
```

### 使用方式

```bash
# 生成执行计划（不执行）
python main.py plan "Build a REST API for user authentication"

# 执行已保存的计划
python main.py execute ./data/plans/plan_xxx.json

# 一键规划 + 执行
python main.py run "Add OAuth2 support" --project ./my-project

# 调整并行度（默认 3）
python main.py run "..." --max-parallel 5
```

### 项目自定义 Agent

在项目根目录创建 `.harness/agents.yaml`，编排器会自动加载：

```yaml
agents:
  - id: ui_designer
    name: UI Designer
    skills: [ui_design, react_component_dev]
    constraints: [Only modifies frontend/src/]
```

---

## 代码风格指南

1. **注释与文档字符串**：模块级、类级、方法级均使用英文文档字符串，遵循 Google-style / PEP 257 风格。代码中的行内注释少量使用中文（如 `出厂设置`）。
2. **类型注解**：全面使用 Python 3.10+ 类型注解（`str | None`、`list[dict[str, Any]]` 等）。
3. **模型层**：所有数据模型必须使用 `pydantic.BaseModel`，支持 `model_dump()` 序列化。
4. **事件命名**：遵循 Anthropic 的 `{domain}.{action}` 约定，如 `workflow.stage_start`、`agent.tool_use`。
5. **文件组织**：
   - 按职责分层（`core/`, `agent/`, `orchestrator/`, `tools/` 等），禁止循环导入。
   - 所有数据模型统一在 `core/models.py`。
   - 入口文件 `main.py` 通过 `sys.path.insert(0, str(Path(__file__).parent))` 自引用项目根目录。
6. **错误处理**：
   - 工具层返回 `ToolResult` 封装成功/失败，避免抛异常中断主循环。
   - DAG 引擎内部异常通过 `traceback.format_exc()` 捕获并写入节点 `error` 字段。

---

## 测试策略

> 当前项目已包含 `tests/` 目录与多组测试用例，可直接运行 `pytest`。

**Evaluator 引擎**（`evaluator/engine.py`）内置了对测试的自动化检查逻辑：

- `tests_pass` → 调用 `python -m pytest -v --tb=short`
- `coverage > X%` → 调用 `python -m pytest --cov=. --cov-report=term-missing`
- `lint_clean` → 优先调用 `flake8 --max-line-length=100`，回退到 `ruff check`

如果你在 Harness 之外为 Harness 本身编写测试，建议：
- 使用 `pytest` 作为测试框架。
- 覆盖 `core/dag_engine.py` 的拓扑排序与失败处理逻辑。
- 覆盖 `core/agent_registry.py` 的注册/加载/验证逻辑。
- 使用 `tmp_path` fixture 测试 `session/store.py` 的 JSONL 追加与状态恢复。

---

## 安全与权限模型

Guardrails（`guardrails/policy.py`）实现四层防御：

| 层级 | 组件 | 说明 |
|------|------|------|
| 模型层 | Agent Worker | 不确定性时暂停，Constitutional AI |
| 工具层 | Tool Registry | 最小权限，allow/deny 列表 |
| Harness层 | Guardrails | PermissionMode：plan / default / accept_edits / auto / dont_ask |
| 执行层 | Sandbox | Docker 隔离，凭证代理（配置中预留，当前实现以本地子进程为主） |

**PermissionMode 含义**：
- `plan`：只读模式（只允许 read/glob/grep）
- `default`：每次操作都需要显式批准（除了读取）
- `accept_edits`：自动批准文件编辑（write/edit），高风险操作仍需批准
- `auto`：基于 RiskLevel 自动审批低风险操作
- `dont_ask`：仅允许预授权工具列表中的工具

当前默认使用 `ACCEPT_EDITS` 模式（见 `main.py`）。

---

## 关键设计约定

1. **Artifact-Centric**：所有状态外化到事件日志（`./data/events/*.jsonl`）和文件产物（`./data/artifacts/`），Agent 的上下文窗口只是缓存。
2. **Append-Only**：SessionStore 是追加式 JSONL，不允许修改历史事件。状态通过重放事件重建。
3. **Context Isolation**：每个 Worker Agent 拥有独立上下文，任务之间不共享消息历史，交接通过 `HandoffArtifact` 完成。
4. **DAG 并行约束**：同层节点通过 `asyncio.gather` 并行执行，`max_parallel` 由信号量控制（默认 3~5）。
5. **默认三 Agent**：`planner`（架构师）、`generator`（工程师）、`evaluator`（QA）为出厂设置，不可注销。项目可通过 `.harness/agents.yaml` 扩展。
6. **Checkpoint**：`session/store.py` 支持通过复制事件日志创建命名检查点，但当前未在编排层自动触发（配置中预留 `checkpoint_interval`）。

---

## 部署与运行环境

- 纯 Python 脚本项目，**无容器化配置、无 CI/CD 配置文件、无打包脚本**。
- 运行时产生本地数据目录：
  - `./data/events/` — Session 事件日志（JSONL）
  - `./data/artifacts/` — 各 Session 的产物文件
  - `./data/reports/` — Markdown 报告
  - `./data/plans/` — 生成的 DAG 计划（JSON）
- 如需生产部署，建议：
  - 将 `data/` 挂载到持久化卷。
  - 为 Sandbox 启用 Docker 运行时（当前配置中 `SandboxConfig.runtime` 默认 `"docker"`，但 `tools/registry.py` 中的 `bash` 工具当前直接调用 `subprocess.run`）。

---

## 对 Agent 的提示

- 修改数据模型时，编辑 `core/models.py`（唯一的模型源文件）。
- 修改编排相关代码时，注意 `dag_engine.py`、`intelligent_orchestrator.py` 和 `agent_pool.py` 的联动。
- 如需新增工具，在 `tools/registry.py` 中注册，并在 `guardrails/policy.py` 的 `RISK_MAP` 中标注风险等级。
- 如需新增默认 Agent 类型，在 `core/agent_registry.py` 的 `_register_defaults()` 中添加，并同步更新 `orchestrator/intelligent_orchestrator.py` 的 prompt 模板中的规划规则。
- 本项目文档以**中文**为主（`README.md`、`ARCHITECTURE.md`），但代码注释和文档字符串以**英文**为主。修改代码时保持这一惯例：文档字符串用英文，面向用户的消息/日志可保留中文或英文。
