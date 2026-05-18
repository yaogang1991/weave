# Weave

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/Version-0.3.7-orange.svg)](pyproject.toml)

**智能多Agent编排 · 自主软件开发系统**

基于 [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents) 架构。Weave 编排多个 LLM Agent（规划器、生成器、评估器），通过 LLM 驱动的动态 DAG 生成与执行，自动化完整软件开发生命周期。

> DAG 是织机，Agent 是梭子，编排（orchestrate）本意是编织。多个 Agent 按角色协作，把需求编织成完整的软件。

[English](README.md) | [架构文档](ARCHITECTURE.md) | [贡献指南](CONTRIBUTING.md) | [更新日志](CHANGELOG.md) | [路线图](docs/roadmap.md)

---

## 为什么选择 Weave？

| 问题 | Weave 的回答 |
|------|-------------|
| 单Agent工具无法处理复杂任务 | 多Agent DAG 编排，支持并行执行 |
| 硬编码工作流在边界情况下失效 | LLM 驱动的规划器实时自适应 |
| 仅云端方案锁定基础设施 | 完全自托管，仅需 Token 成本 |
| 生成代码无质量保证 | 契约驱动的自动评估与检查 |

## 核心特性

- **LLM 驱动的 DAG 编排** -- 规划 Agent 动态生成执行 DAG，实时适应失败情况
- **多模型路由** -- 为不同 Agent 角色分配不同 LLM（如 Opus 规划、Sonnet 编码）
- **Agent 记忆** -- 跨会话持久化记忆，支持作用域提升（PRIVATE → SESSION → GLOBAL）
- **自学习** -- 从执行历史自动分析模式，向规划器反馈优化提示
- **影响分析** -- 执行前预测影响范围，执行后验证变更匹配度
- **DAG 模板** -- 可复用 YAML 模板，跳过 LLM 规划处理重复任务模式
- **技能系统** -- 基于 YAML 的提示模板，支持变量替换的单 Agent 调用
- **MCP 集成** -- Model Context Protocol 客户端，支持 stdio 传输的工具发现与执行
- **Web 控制台** -- 实时 DAG 监控、任务管理、告警仪表盘
- **审批工作流** -- 高风险操作的人工审批门控
- **多后端** -- 本地或 Git Worktree 隔离，支持 Docker 沙箱

## 快速开始

### 前提条件

- Python 3.11+
- Anthropic API Key（或 OpenAI 兼容端点）

### 安装

```bash
git clone https://github.com/yaogang1991/weave.git
cd weave
pip install -r requirements.txt
```

### 运行

```bash
# 设置 API Key
export ANTHROPIC_API_KEY="sk-ant-..."

# 一键规划 + 执行
python main.py run "Build a REST API for todo items"
```

或先规划，再执行：

```bash
python main.py plan "Build a REST API for user authentication"
python main.py execute ./data/plans/plan_xxx.json
```

## 使用方式

### 交互模式

```bash
# 规划并执行
python main.py run "Add OAuth2 support to the API"

# 使用项目自定义 Agent
python main.py run "Design login page" --project ./my-project --max-parallel 5

# 使用 DAG 模板（跳过 LLM 规划）
python main.py run "Build Todo API" --template build_api --var feature=Todo --var language=Python
```

### Worker 模式（无人值守）

```bash
# 终端 1：启动 Worker
python main.py worker --concurrency 1

# 终端 2：提交任务
python main.py submit "Build a REST API for user auth"

# 终端 3：监控
python main.py list --status running
python main.py tickets --status pending

# 非交互模式
export WEAVE_NON_INTERACTIVE=true
python main.py worker --non-interactive
```

### MCP 服务器模式

```bash
python main.py serve
```

### Web 控制台

```bash
python main.py viz
# 浏览器访问 http://localhost:8765
```

### 命令参考

| 命令 | 说明 |
|------|------|
| `run "<需求>"` | 一键规划 + 执行 |
| `plan "<需求>"` | 生成执行计划（DAG） |
| `execute <计划>` | 执行已保存的计划 |
| `submit "<需求>"` | 提交任务到队列 |
| `worker` | 启动 Worker（队列消费者） |
| `status <id>` | 查看任务状态 |
| `list` | 列出任务 |
| `cancel <id>` | 取消任务 |
| `recover` | 恢复孤儿任务 |
| `tickets` | 查看审批票据 |
| `approve <id>` | 批准票据 |
| `reject <id>` | 拒绝票据 |
| `templates` | 列出 DAG 模板 |
| `skills` | 列出可用技能 |
| `skill <name>` | 调用技能 |
| `serve` | 启动 MCP 服务器 |
| `viz` | 启动 Web 控制台 |
| `memory-search` | 搜索 Agent 记忆 |
| `memory-add` | 添加记忆条目 |
| `memory-stats` | 记忆统计 |
| `learning-analyze` | 触发模式分析 |
| `learning-insights` | 查看学习洞察 |
| `impact-predict` | 预测变更影响 |
| `impact-graph` | 显示依赖图 |
| `console` | 交互式管理控制台 |

## 架构

```
┌──────────────────────────────────────────────────────────────┐
│                        编排层 (Orchestrator)                   │
│   Planner  ·  Generator  ·  Evaluator                        │
├──────────────────────────────────────────────────────────────┤
│              会话管理器 (Append-Only Event Log)                 │
├──────────────────────────────────────────────────────────────┤
│                     Weave 核心 (Dumb Loop)                     │
│   Agent Worker  ←  Tool Registry  ←  Guardrails              │
├──────────────────────────────────────────────────────────────┤
│   Sandbox  ·  Git  ·  Reporter                                │
├──────────────────────────────────────────────────────────────┤
│   Memory  ·  Learning  ·  Impact Analysis                     │
└──────────────────────────────────────────────────────────────┘
```

**四层架构：** 编排器 → 会话管理器 → Weave 核心 → 执行层

完整架构文档见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 配置

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | -- | Anthropic API Key（必需） |
| `OPENAI_API_KEY` | -- | OpenAI API Key（备选） |
| `WEAVE_MODEL` | `claude-sonnet-4-6` | 默认 LLM 模型 |
| `WEAVE_DEFAULT_BACKEND` | `local` | 执行后端（`local`/`worktree`） |
| `WEAVE_NON_INTERACTIVE` | `false` | 禁用交互式提示 |
| `WEAVE_PLANNER_MODEL` | -- | 覆盖规划 Agent 模型 |
| `WEAVE_GENERATOR_MODEL` | -- | 覆盖生成 Agent 模型 |

### 项目配置

在项目中创建 `.weave/config.yaml`：

```yaml
guardrails:
  permission_mode: default
  max_file_size: 100000

memory:
  enabled: true
  max_entries: 500

backend:
  type: local
```

完整配置参考见 [docs/config_reference.md](docs/config_reference.md)。

## 自定义 Agent

在 `.weave/agents.yaml` 中注册项目专属 Agent：

```yaml
agents:
  - id: ui_designer
    name: UI Designer
    skills: [ui_design, react_component_dev, tailwind_css]
    constraints: [Only modifies frontend/src/]
```

编排 Agent 会自动发现并在规划时分配这些 Agent。

## 安全模型

| 层级 | 组件 | 职责 |
|------|------|------|
| 模型层 | Agent Worker | Constitutional AI，不确定时暂停 |
| 工具层 | Tool Registry | 最小权限，允许/拒绝列表 |
| Weave 层 | Guardrails | 权限模式（plan/default/auto/dont_ask） |
| 执行层 | Sandbox | Docker 隔离，凭证代理 |

## 模块概览

| 模块 | 职责 |
|------|------|
| `core/` | 领域模型、配置、DAG 引擎、LLM 客户端/路由、Watchdog |
| `cli/` | CLI 命令处理器 |
| `session/` | 事件存储、状态恢复、检查点 |
| `agent/` | LLM API 调用、Agent 池、系统提示 |
| `tools/` | 内置工具 + 命令执行器 + MCP 集成 |
| `guardrails/` | 风险分类、权限控制 |
| `evaluator/` | 自动评估（检查器、Lint、执行器） |
| `orchestrator/` | 工作流编排、计划验证、提示模板 |
| `memory/` | Agent 记忆（存储、检索、共享） |
| `learning/` | 执行模式分析与优化 |
| `templates/` | 可复用 DAG 模板（YAML + 变量） |
| `analysis/` | 依赖图、影响预测、变更验证 |
| `visualizer/` | Web 控制台（FastAPI + WebSocket） |
| `backend/` | 执行后端（local/worktree）+ 沙箱 |
| `control_plane/` | 任务队列、Worker、执行 Hooks、审批 |
| `mcp/` | Model Context Protocol 客户端 |
| `skills/` | YAML 技能定义，支持变量替换 |

## 与 Anthropic Managed Agents 对比

| 特性 | Anthropic Managed Agents | Weave |
|------|-------------------------|-------|
| 运行位置 | Anthropic 云端 | 本地 / 自托管 |
| 定价 | $0.08/session-hour + tokens | 仅 Token 成本 |
| 会话 | 托管事件日志 | 本地 JSONL |
| 沙箱 | 托管容器 | Docker / 本地 |
| LLM | 仅 Claude | Claude / OpenAI 兼容 |
| MCP | 原生支持 | 客户端集成 |
| 自定义 Agent | 有限 | 完整 YAML 注册 |

## 已知限制

- 单用户场景（无多租户）
- 基于文件的存储（无需外部数据库）
- 单机执行（无分布式模式）
- 影响分析仅支持 Python import 解析

## 文档

- [架构文档](ARCHITECTURE.md) -- 完整系统架构与组件详情
- [贡献指南](CONTRIBUTING.md) -- 开发环境与 PR 流程
- [更新日志](CHANGELOG.md) -- 版本历史
- [路线图](docs/roadmap.md) -- 里程碑历史与未来计划
- [配置参考](docs/config_reference.md) -- 全部配置选项
- [开发指南](docs/dev_guide.md) -- 扩展 Agent、工具与后端
- [规格文档](docs/specs/) -- 模块级工程规格
- [ADR](docs/adrs/) -- 架构决策记录

## 许可证

[Apache License 2.0](LICENSE)

Copyright 2026 yaogang1991
