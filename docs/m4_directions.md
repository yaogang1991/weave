# M4 Milestone: 两轮方向调研与战略决策

*生成日期: 2026-05-18*
*基于: 4 轮深度研究，80+ 来源*

---

## 背景

Weave 完成到 M3.6 + 架构重构后，面临方向选择：是继续自建所有能力（项目理解、代理循环、工具系统），还是转型为纯编排层，利用成熟的编码代理（Claude Code、Codex）作为执行后端。

经过 4 轮深度研究，结论是 **M4 应走方向 B：Weave 做纯编排，Agent 做 Worker**。

---

## 方向 A：自建项目理解能力（未采纳）

### 目标

增强 Weave 对现有代码库的理解能力，使其能在已有项目上执行任务（修 bug、加功能），而非仅支持绿地开发。

### 规划内容

1. **项目初始化器** — `project analyze` CLI 命令，自动扫描目标项目，框架/语言检测，生成 `.weave/config.yaml` 和项目知识文件
2. **增强依赖图** — 多语言（tree-sitter）、多边类型（CALLS/INHERITS/DECORATES/TESTS）、增量更新
3. **上下文路由** — BM25 + 图距离融合排序，Token 预算感知的上下文选择
4. **项目感知代理** — Viewer/Editor 代理分离，检查点机制

### 为什么没有采纳

- **本质上是重建 Claude Code** — 项目理解、代码探索、文件编辑、依赖分析，Claude Code 已经做到业界最好，自建需要 3-6 个月且永远追不上 Anthropic/OpenAI 的迭代速度
- **违背 Managed Agents 精神** — Anthropic 明确说 "Harnesses encode assumptions that go stale as models improve"，自建代理循环编码了"模型不会自己探索代码库"的假设
- **竞争定位错误** — 在项目理解赛道上和 Claude Code 竞争是以弱击强，Weave 的真正价值不在编码能力

### 保留的部分

- **项目配置检测**（P1）— 简单的框架/语言自动检测，用于生成 worker 提示中的项目上下文
- **影响分析**（已有）— `analysis/dependency_graph.py` 保留，用于编排器做依赖感知的任务分解
- 研究报告保存在 `docs/research-project-understanding.md`

---

## 方向 B：Weave 做纯编排 + Agent 做 Worker（采纳）

### 核心理念

Weave 放弃自建代理循环，转而成为 **Anthropic Managed Agents 架构的开源自托管实现**。Claude Code / Codex 作为 Worker（brain + hands），Weave 作为 Meta-harness（编排层）。

### 与 Managed Agents 的映射

| Managed Agents 概念 | Weave 对应 |
|---|---|
| Meta-harness（元编排层） | Weave DAG Engine + Orchestrator |
| Brain（Claude + harness） | Claude Code / Codex Worker |
| Hands（sandbox + tools） | Worker 自带的文件/命令/搜索工具 |
| Session（事件日志） | Weave 的 JSONL event store |
| Many brains | DAG 并行节点（多 worker 同时执行） |
| Many hands | Worker 连接不同项目/环境 |
| `execute(name, input) -> string` | `Backend.execute_node(node, ctx) -> NodeResult` |
| `wake(sessionId)` | 失败节点恢复（新 brain 接管） |

### 关键技术验证

**Claude Code 作为 Worker** — 完全可行：

```bash
# Headless 模式（最简用法）
claude -p "修复 src/auth.py 中的登录 bug" \
  --output-format json --permission-mode auto --bare

# Agent SDK（完整程序控制）
from claude_agent_sdk import query, ClaudeAgentOptions
options = ClaudeAgentOptions(
    allowed_tools=["Read", "Edit", "Bash", "Write", "Glob", "Grep"],
    permission_mode="auto",
    max_turns_usd=5.0,
    cwd="/path/to/project",
)
async for message in query(prompt="实现功能", options=options):
    ...
```

**Codex 作为 Worker** — 可行：

```bash
# Headless 模式
codex exec "修复这个 bug" --sandbox workspace-write --json

# 作为 MCP Server（被其他代理调用）
codex mcp  # 暴露 codex() 和 codex-reply() 工具
```

**已有生产验证** — 多个系统在这样做：
- **claude-mpm** — 47+ 代理，PM 编排 Claude Code 子进程
- **AI University 15-agent 系统** — `claude -p` 子进程 + `Promise.allSettled()` 并行
- **OpenAI 官方** — Codex MCP + Agents SDK 编排模式
- **Tembo** — CLI-agnostic，编排任何编码代理

### Weave 的差异化（vs 竞品）

| | Tembo | claude-mpm | OpenHands | **Weave** |
|---|---|---|---|---|
| 编排模式 | 线性/触发 | PM -> Worker | CodeAct 循环 | **DAG 拓扑排序** |
| 任务分解 | 人工定义 | Claude 自己规划 | 代理自行决定 | **LLM 动态生成 DAG** |
| 并行支持 | 有 | 有限 | 有 | **拓扑排序 + asyncio.gather** |
| 失败恢复 | 重试 | 重试 | 重试 | **节点级重试 + 自适应重规划** |
| 质量门控 | 无 | 有 | 无 | **节点间自动验证** |
| 自托管 | 否 | 是 | 是 | **是** |
| 代理无关 | 是 | 否 | 否 | **是（Backend 抽象）** |

**三大差异化**：

1. **LLM 动态 DAG 生成** — 输入自然语言需求，一次性分解为带依赖关系的 DAG（最大化并行、最小化依赖、预算感知）。没人做这个。
2. **节点级质量门控 + 自适应重规划** — 失败不是简单重试，而是 LLM 分析原因后生成新的局部 DAG。
3. **自托管 + 代理无关 + DAG 编排** — 三者同时具备，目前无其他开源项目做到。

---

## M4 实施计划

### 阶段 1：Worker 后端（2-3 周）

**目标**：实现 `ClaudeCodeBackend` 和 `CodexBackend`，替代自建代理循环。

#### 新增模块

```
backend/
├── claude_code.py    # Claude Code Agent SDK 后端
└── codex_backend.py  # Codex CLI 后端
```

#### 核心接口

```python
# backend/claude_code.py
class ClaudeCodeBackend(ExecutionBackend):
    """用 Claude Code Agent SDK 作为执行后端"""

    async def execute_node(self, node: DAGNode, context: dict) -> NodeResult:
        options = ClaudeAgentOptions(
            allowed_tools=self._map_tools(node.allowed_tools),
            permission_mode="auto",
            max_turns_usd=node.budget_usd or 5.0,
            cwd=context["workspace_path"],
            model=node.model or "claude-sonnet-4-6",
        )
        async for message in query(prompt=node.prompt, options=options):
            ...
        return NodeResult(status="completed", output=result_text, cost_usd=cost)
```

```python
# backend/codex_backend.py
class CodexBackend(ExecutionBackend):
    """用 Codex CLI headless 模式作为执行后端"""

    async def execute_node(self, node: DAGNode, context: dict) -> NodeResult:
        cmd = ["codex", "exec", "--sandbox", "workspace-write",
               "--json", "--model", node.model or "gpt-5",
               "--cd", context["workspace_path"], node.prompt]
        # JSONL 事件流解析
        ...
```

#### CLI 命令

```bash
# 选择 worker 后端
weave run "修复认证 bug" --project ./my-app --worker claude-code
weave run "添加 OAuth2" --project ./my-app --worker codex

# 无人值守
weave submit "修复 #42" --worker claude-code
weave worker --concurrency 3
```

#### 配置

```yaml
# .weave/config.yaml
worker:
  backend: claude-code  # claude-code | codex | built-in
  model: claude-sonnet-4-6
  budget_per_node_usd: 5.0
  max_turns: 50
  timeout: 480
  allowed_tools:
    - Read
    - Edit
    - Write
    - Bash
    - Glob
    - Grep
```

### 阶段 2：项目上下文注入（1-2 周）

**目标**：让 Worker 在执行时拥有项目上下文，而不是 Weave 自己理解项目。

#### 方案

- Worker（Claude Code）自带 CLAUDE.md、MCP 工具、Glob/Grep/Read 全套能力
- Weave 的职责是**组装提示**，注入项目上下文到 Worker 的 prompt 中
- 利用已有的 `core/project_config.py` 加载 `.weave/config.yaml`
- 编排器在 `plan()` 时将项目配置注入每个节点的 prompt

#### 项目配置检测

```bash
# 自动检测项目类型，生成 .weave/config.yaml
weave project init --project ./my-app
# 输出: 检测到 FastAPI + Python 3.11 + pytest
# 生成: .weave/config.yaml (language, framework, test_runner, conventions)
```

### 阶段 3：DAG 编排质量提升（2-3 周）

**目标**：让 `IntelligentOrchestrator.plan()` 成为业内最好的任务分解器。

#### 增强

- **预算感知规划** — 根据总预算智能分配每节点预算，Sonnet 做执行，Opus 做审查
- **依赖感知分解** — 利用 `analysis/dependency_graph.py` 做依赖感知的节点划分
- **自适应重规划** — `adapt_to_failure()` 生成诊断节点 -> 分析原因 -> 局部新 DAG
- **渐进式质量** — 首轮 Sonnet 快速执行，失败自动升级 Opus

### 阶段 4：混合后端 + 企业集成（2-3 周）

**目标**：同一 DAG 中混用不同后端，接入 GitHub/GitLab。

#### 混合后端

```yaml
# DAG 节点可指定不同 worker
nodes:
  - id: analyze
    worker: claude-code  # 用 Claude 分析项目
  - id: implement
    worker: codex        # 用 Codex 实现
  - id: review
    worker: claude-code  # 用 Claude 审查
```

#### 企业集成

- GitHub Issue -> DAG 自动分解
- PR 创建和审查
- GitLab/Jira webhook 触发
- CI/CD 管道集成

---

## M4 可以简化的现有模块

采用方向 B 后，以下模块可以逐步简化（非删除，保留轻量路径）：

| 模块 | 当前用途 | M4 变化 |
|------|---------|---------|
| `agent/worker.py` | 自建代理循环 | 保留轻量路径（规划节点等），主要用 Claude Code SDK |
| `agent/prompts.py` | 系统提示 | Worker 自带 CLAUDE.md，Weave 只需组装任务描述 |
| `tools/registry.py` | 工具注册 | Worker 自带工具，Weave 不再管理 |
| `tools/command_runner.py` | 命令执行 | Worker 自带 Bash 工具 |
| `core/llm_client.py` | LLM 调用 | 保留（编排器规划/重规划仍需直接调用 LLM） |
| `agent/agent_pool.py` | 代理池 | 保留（管理多个 worker 实例的生命周期） |
| `backend/local.py` | 本地后端 | 保留（作为最简后端） |
| `backend/worktree.py` | Worktree 隔离 | 保留（每个 Worker 用独立 worktree） |

---

## 市场定位

```
+--------------------------------------------------+
|  应用层: Cursor, Copilot, Claude Code             |  <- 个人开发者工具（70%+ 份额，已锁定）
+--------------------------------------------------+
|  编排层: OpenHands, Tembo, Blocks, Weave          |  <- Weave 在这里
|  差异化: DAG 拓扑 + LLM 动态分解 + 自适应重规划     |
+--------------------------------------------------+
|  基础设施层: MCP, AGENTS.md, 代码图               |  <- 标准和协议（MCP = 97M+ 月下载）
+--------------------------------------------------+
```

Weave 是 **Anthropic Managed Agents 的开源自托管版**：
- Managed Agents（云端）<-> Weave（自托管）
- 两者做同样的事：编排多个 brain，管理 session，调度 hands
- 差异：一个在 Anthropic 云上，一个在你自己的机器上

---

## 研究报告索引

- `docs/research-project-understanding.md` — 项目理解技术方案（方向 A 的详细研究）
- `docs/research-strategy-direction.md` — 战略方向评估（竞争分析、市场定位）
- 本文档 — M4 决策总结和实施计划
