# Weave — Agent Guide

> 本文档供 AI Coding Agent 阅读。如果你对人类用户可见的概览感兴趣，请查看 `README.md`；如果你需要架构细节，请查看 `ARCHITECTURE.md`。

---

## 项目概述

本项目是一个**自托管的无人看守软件开发工作流系统**，基于 [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents) 架构理念实现。它通过编排多个 LLM Agent 来自动化完成软件需求分析、设计、编码、测试和交付的全流程。

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
weave/
├── core/                          # 核心模型与引擎
│   ├── models.py                  # 统一重导出（领域模型拆分到 *_models.py）
│   ├── dag_models.py              # DAG 相关模型
│   ├── event_models.py            # 事件与 Session 模型
│   ├── guardrail_models.py        # Guardrail 模型
│   ├── memory_models.py           # 记忆模型
│   ├── analysis_models.py         # 影响分析模型
│   ├── eval_models.py             # 评估模型
│   ├── tool_models.py             # 工具模型
│   ├── mcp_models.py              # MCP 模型
│   ├── artifact_handoff.py        # HandoffArtifact 交接
│   ├── exceptions.py              # 自定义异常
│   ├── config.py                  # 配置管理
│   ├── agent_registry.py          # Agent 能力注册表
│   ├── llm_client.py              # 统一 LLM 客户端
│   ├── llm_router.py              # 多模型路由
│   ├── dag_engine.py              # DAG 引擎
│   ├── node_executor.py           # 单节点执行
│   ├── quality_gate.py            # 质量检查
│   ├── retry_policy.py            # 重试策略
│   ├── watchdog.py                # 心跳监控
│   └── project_config.py          # 项目配置加载
├── cli/                           # CLI 命令（从 main.py 拆分）
│   ├── execution.py               # plan/execute/run/viz
│   ├── jobs.py                    # submit/status/list/cancel/worker/recover
│   ├── approval.py                # tickets/approve/reject
│   ├── memory.py                  # memory-search/list/stats/add/cleanup
│   ├── learning.py                # learning-analyze/insights/status
│   ├── impact.py                  # impact-predict/graph/history
│   ├── skills.py                  # skills/skill/templates
│   └── utils.py                   # 共享工具
├── agent/                         # Agent Worker 层
│   ├── worker.py                  # 单 Agent LLM 调用循环
│   ├── agent_pool.py              # Agent 实例池（独立上下文、记忆注入/提取）
│   └── prompts.py                 # Agent system prompts
├── orchestrator/                  # 编排层
│   ├── intelligent_orchestrator.py # 智能编排 Agent
│   ├── plan_validator.py          # DAG 验证与自动修复
│   ├── llm_utils.py               # LLM 工具函数
│   └── prompts/                   # Prompt 模板 (planning/adaptation/replan)
├── control_plane/                 # 任务控制面
│   ├── models.py                  # Job/Run 数据模型
│   ├── repository.py              # 持久化存储
│   ├── service.py                 # RunService
│   ├── hooks.py                   # Execution Hooks
│   ├── execution_factory.py       # 编排器+引擎工厂
│   ├── job_lifecycle.py           # Job 状态管理
│   ├── run_lifecycle.py           # Run 状态管理
│   ├── worker.py                  # Worker 队列消费者
│   ├── worker_executor.py         # Worker 内 Job 执行
│   ├── worker_recovery.py         # 孤儿 Job 恢复
│   └── approval.py                # 审批票据
├── tools/                         # 工具层
│   ├── registry.py                # 工具注册表
│   └── command_runner.py          # 命令执行
├── guardrails/                    # 安全与权限
│   └── policy.py                  # 四层防御策略
├── evaluator/                     # 自动化评估
│   ├── engine.py                  # 评估编排
│   ├── runner.py                  # 测试/lint 执行器
│   ├── models.py                  # 评估模型
│   ├── artifact.py                # Artifact 评估
│   ├── checkers/                  # 条件检查器
│   └── lint/                      # Lint 解析
├── backend/                       # 执行后端
│   ├── base.py                    # 抽象接口
│   ├── local.py                   # 本地执行
│   ├── worktree.py                # Git worktree 隔离
│   ├── sandbox.py                 # SandboxProvider
│   ├── docker_stub.py             # Docker stub
│   └── lifecycle.py               # BackendManager
├── mcp/                           # Model Context Protocol
│   └── client.py                  # MCP 客户端
├── skills/                        # 技能系统
│   └── registry.py                # SkillRegistry
├── session/                       # 状态持久化
│   └── store.py                   # JSONL 事件存储 + snapshot
├── memory/                        # Agent 记忆
│   ├── store.py                   # 持久化存储
│   ├── manager.py                 # 高层 API
│   └── sharing.py                 # 跨 Agent 共享
├── learning/                      # 自学习
│   ├── analyzer.py                # 模式分析
│   ├── optimizer.py               # 洞察转换
│   └── scheduler.py               # 定期调度
├── templates/                     # DAG 模板
│   ├── library.py                 # TemplateRegistry
│   └── *.yaml                     # 7 个内置模板
├── analysis/                      # 影响分析
│   ├── dependency_graph.py
│   ├── impact_predictor.py
│   └── change_verifier.py
├── monitoring/                    # 监控
│   ├── metrics.py
│   └── alerts.py
├── visualizer/                    # Web 控制台
│   ├── server.py
│   ├── cli_renderer.py
│   ├── event_bridge.py
│   └── static/
├── reporter/
│   └── logger.py
├── projects/
│   └── example/agents.yaml
├── docs/
├── tests/
├── main.py                        # CLI 入口
├── README.md                      # 面向用户说明（中文）
├── ARCHITECTURE.md                # 架构设计（中文）
├── CONTRIBUTING.md                # 贡献指南
└── CLAUDE.md                      # Claude Code 指引
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
export WEAVE_MODEL="gpt-4"
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

在项目根目录创建 `.weave/agents.yaml`，编排器会自动加载：

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
   - 按职责分层（`core/`, `cli/`, `agent/`, `orchestrator/`, `tools/` 等），禁止循环导入。
   - 数据模型按领域拆分到 `core/*_models.py`，统一通过 `core/models.py` 重导出。
   - CLI 命令处理器拆分到 `cli/` 子模块。
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
| Weave层 | Guardrails | PermissionMode：plan / default / accept_edits / auto / dont_ask |
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
5. **默认三 Agent**：`planner`（架构师）、`generator`（工程师）、`evaluator`（QA）为出厂设置，不可注销。项目可通过 `.weave/agents.yaml` 扩展。
6. **Checkpoint**：`session/store.py` 支持通过复制事件日志创建命名检查点，但当前未在编排层自动触发（配置中预留 `checkpoint_interval`）。

---

## 部署与运行环境

- 纯 Python 脚本项目，**无容器化配置、无 CI/CD 配置文件、无打包脚本**。
- 运行时产生本地数据目录：
  - `./data/events/` — Session 事件日志（JSONL）
  - `./data/artifacts/` — 各 Session 的产物文件
  - `./data/reports/` — Markdown 报告
  - `./data/plans/` — 生成的 DAG 计划（JSON）
  - `./data/jobs/` — Job 存储（含 dead_letter）
  - `./data/backends/` — 后端数据（worktrees）
  - `./data/memory/` — Agent 记忆
  - `./data/learning/` — 学习分析状态
  - `./data/impact/` — 影响分析数据
- 如需生产部署，建议：
  - 将 `data/` 挂载到持久化卷。
  - 为 Sandbox 启用 Docker 运行时（`SandboxConfig.runtime` 可配置为 `docker`/`bubblewrap`/`direct`）。

---

## 对 Agent 的提示

- 修改数据模型时，编辑对应的 `core/*_models.py` 文件（如 `dag_models.py`、`event_models.py`），并确保在 `core/models.py` 中重导出。
- 修改编排相关代码时，注意 `dag_engine.py`、`intelligent_orchestrator.py` 和 `agent_pool.py` 的联动。
- 修改 CLI 时，编辑 `cli/` 下对应的命令模块。
- 如需新增工具，在 `tools/registry.py` 中注册，并在 `guardrails/policy.py` 的 `RISK_MAP` 中标注风险等级。
- 如需新增默认 Agent 类型，在 `core/agent_registry.py` 的 `_register_defaults()` 中添加，并同步更新 `agent/prompts.py` 和 `orchestrator/prompts/planning.md`。
- 如需新增 Execution Hook，继承 `control_plane/hooks.py` 的 `ExecutionHook`，并在 `control_plane/service.py` 的 `_register_hooks()` 中注册。
- 如需新增 CLI 命令，在 `cli/` 下添加处理函数，并在 `main.py` 中注册 subparser。
- 本项目文档以**中文**为主（`README.md`、`ARCHITECTURE.md`），但代码注释和文档字符串以**英文**为主。修改代码时保持这一惯例：文档字符串用英文，面向用户的消息/日志可保留中文或英文。
