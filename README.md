# Unattended Software Development Harness

基于 [Anthropic Managed Agents](https://www.anthropic.com/engineering/managed-agents) 架构理念实现的自托管无人看守软件开发工作流 Harness。

## 核心设计原则

1. **Artifact-Centric** — 所有状态外化到事件日志和文件产物，模型上下文只是缓存
2. **Minimal by Design** — 以 "dumb loop" 为核心，按需添加复杂度
3. **Defense-in-Depth** — 工具层 + Harness 层 + 执行层多层防御
4. **Trust-First** — 审计日志、回滚、监控作为一等公民
5. **Human-on-the-Loop** — 计划级人类监督，执行级自动运行
6. **Contract-Driven** — 预定义成功标准，自动化评估

## 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        Orchestrator Layer                        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Planner   │  │  Generator  │  │     Evaluator       │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│              Session Manager (Append-Only Event Log)             │
├─────────────────────────────────────────────────────────────────┤
│                      Harness Core (Dumb Loop)                    │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Agent     │  │   Tool      │  │     Guardrails      │  │
│  │   Worker    │◄─┤   Registry  │◄─┤  (Permission/Risk)  │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Sandbox   │  │   Git       │  │     Reporter        │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 安装依赖

```bash
cd harness
pip install -r requirements.txt
```

### 2. 设置 API Key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# 或者使用 OpenAI 兼容模型
export HARNESS_MODEL="gpt-4"
```

### 3. 运行工作流

```bash
# 一键规划并执行
python main.py run "Build a REST API for todo items"

# 或先规划再执行
python main.py plan "Build a REST API for todo items"
python main.py execute ./data/plans/plan_xxx.json
```

## 工作流编排

Harness 采用 **智能多 Agent 编排**：由 LLM 动态生成 DAG 执行计划，支持并行执行与失败自适应。

```bash
python main.py run "Add OAuth2 support" --project ./my-project --max-parallel 5
```

Agent 类型（默认）：
- **planner** — 架构师，负责需求分析和设计
- **generator** — 工程师，负责编码实现
- **evaluator** — QA，负责测试和审查

项目可通过 `.harness/agents.yaml` 扩展自定义 Agent。

## 安全模型

受 Anthropic 四层安全架构启发：

| 层级 | 组件 | 功能 |
|------|------|------|
| 模型层 | Agent Worker | Constitutional AI，不确定性时暂停 |
| 工具层 | Tool Registry | 最小权限，allow/deny 列表 |
| Harness层 | Guardrails | 权限模式（plan/default/auto/dontAsk） |
| 执行层 | Sandbox | Docker 隔离，凭证代理 |

## 模块说明

| 模块 | 职责 |
|------|------|
| `core/` | Pydantic 模型、配置管理 |
| `session/` | 事件存储、状态恢复、checkpoint |
| `agent/` | LLM API 调用、dumb loop |
| `tools/` | 内置工具 + MCP 集成 |
| `guardrails/` | 风险分级、权限控制 |
| `evaluator/` | 自动化评估、测试执行 |
| `orchestrator/` | 工作流编排、Stage 流转 |
| `reporter/` | 审计日志、报告生成 |

## 与 Anthropic Managed Agents 的关系

本项目是 Anthropic Managed Agents **理念的自托管实现**：

| 特性 | Anthropic Managed Agents | 本 Harness |
|------|-------------------------|-----------|
| 运行位置 | Anthropic Cloud | 本地/自托管 |
| 定价 | $0.08/session-hour + tokens | 仅 token 费用 |
| Session | 托管事件日志 | 本地 JSONL |
| Sandbox | 托管容器 | Docker/本地 |
| LLM | Claude 系列 | Claude/OpenAI 兼容 |
| MCP | 原生支持 | 客户端集成 |

适合场景：
- 需要完全控制基础设施的企业
- 本地开发/原型验证
- CI/CD 集成
- 自定义安全策略

## 许可证

MIT
