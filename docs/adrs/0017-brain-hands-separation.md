# ADR 0017: Brain/Hands Separation — Weave as Pure Orchestrator

**Status:** Accepted
**Date:** 2026-05-26
**Deciders:** Project Lead

## Context

Weave M1–M3 构建了一套完整的自建 Agent 循环：`AgentWorker`（616 行）管理 LLM 调用循环，`tools/registry.py`（599 行）注册和执行工具，`agent/prompts.py`（169 行）管理系统提示。这套体系等同于 Weave 自己实现了一个 coding agent。

Anthropic 2026-04 发布 [Managed Agents 工程博客](https://www.anthropic.com/engineering/managed-agents)，明确指出：

> "Harnesses encode assumptions that go stale as models improve."

核心架构是 Brain（LLM + harness）与 Hands（sandbox + tools）解耦，通过 `execute(name, input) → string` 统一接口连接。Session（事件日志）作为独立持久层。

开源项目 Multica（32K stars）验证了这一模式：它 **从不管理 Agent 循环**，仅通过 Daemon 调用已安装的 Claude Code / Codex 等 CLI，自己只管任务分配、进度追踪和 Skill 复用。

Weave 当前的 `AgentWorker` 是一个 harness——它假设模型不能自己管理工具、上下文和 stuck 检测。但 Claude Code 已经证明了它可以。继续维护这套自建循环 = 在 Anthropic 明确说过时的方向上投入。

## Decision

Weave 成为 **纯编排层（meta-harness）**，放弃自建 Agent LLM 循环，执行交给 ClaudeCodeBackend / CodexBackend。`BuiltinBackend` 保留为规划/评估后端（无工具循环的轻量 LLM 调用）。

### 受影响模块

| 模块 | 行数 | 变更 |
|------|------|------|
| `agent/worker.py` | 616 | 简化为无工具循环的 LLM caller（保留用于 planner/evaluator 节点） |
| `tools/registry.py` | 599 | 不再是执行路径，保留 glob/grep 供编排层影响分析使用 |
| `tools/command_runner.py` | 131 | 同上 |
| `agent/prompts.py` | 169 | 简化——Worker 自带 CLAUDE.md，Weave 只组装任务描述 |
| `agent/agent_pool.py` | 689 | 重构围绕 BackendContext，不再绑定 AgentWorker |
| `guardrails/policy.py` | — | 从 tool-call 级别提升到 node 级别 |
| `core/backend_models.py` | — | BackendContext 扩展 memory_prompt、project_context 字段 |
| `core/node_executor.py` | 742 | 注入 memory/project context 到 BackendContext |

### 不变模块

以下模块不涉及此次迁移：

- `core/dag_engine.py` — DAG 拓扑执行，不碰 Agent 循环
- `orchestrator/` — 规划逻辑，不碰 Agent 循环
- `integrations/` — GitHub 集成，不碰 Agent 循环
- `control_plane/` — Job 生命周期管理
- `session/store.py` — JSONL 事件存储
- `memory/`, `learning/`, `analysis/` — 知识系统

### BackendContext 扩展

```python
class BackendContext(BaseModel):
    node: Any
    artifacts: list[HandoffArtifact]
    session_id: str
    workspace_path: str | None
    job_id: str
    run_id: str | None
    cancel_event: threading.Event | None
    progress_callback: Callable | None
    progress_tracker: Any | None
    # 新增
    memory_prompt: str = ""       # 从 MemoryManager 注入
    project_context: str = ""     # 从 .weave/config.yaml 加载
```

当前 memory 注入分散在 `AgentPool.WorkerAgent.execute()`（用 `capability.id`）和 `AgentWorker.run()`（用 `"shared"`），agent_type 不一致。统一注入到 BackendContext 后，所有 backend 都能获得 memory。

### Guardrails 提升

当前 guardrails 在 `tools/registry.py` 的 `check_and_execute()` 中拦截每个 tool call。外部 backend 的工具调用在 Worker 内部，Weave 看不到。

迁移方向：

```
迁移前:  ToolRegistry.execute(tool_name, args) → guardrail_check → execute
迁移后:  NodeExecutor.execute_node(node) → pre_check(risk, workspace) → backend.execute() → post_check(changes)
```

### BuiltinBackend 定位

保留为 **规划/评估后端**：

- `AgentWorker` 简化为无工具循环的 LLM caller
- 仅用于 planner / evaluator 节点（需要 LLM 但不需要文件操作）
- 作为零依赖回退（Claude Code / Codex 不可用时）
- `tools/` 仅保留 glob/grep 供编排层影响分析

### 迁移分期

| Phase | 周数 | 内容 |
|-------|------|------|
| **Phase 1: 接口变更** | 1–2 | BackendContext 扩展、NodeExecutor 注入 memory、ClaudeCodeBackend 使用新字段、默认 backend 改为 claude_code |
| **Phase 2: Guardrails 提升** | 2–3 | NodeExecutor 增加 pre/post check、移除 tools/ 中的 guardrail、node 级别生效 |
| **Phase 3: AgentPool 重构** | 3–4 | get_executor() → get_backend()、AgentWorker 去工具循环、AgentPool 变为 BackendContext 工厂 |
| **Phase 4: 清理** | 4–6 | tools/ 精简、prompts.py 简化、stuck_detector/context 评估、文档更新 |

## Consequences

### Positive

- **与 Managed Agents 理念对齐** — Weave 真正成为 meta-harness
- **消除 ~1500 行过时代码的维护负担** — stuck detector、context manager、output monitor 等随 AgentWorker 一起简化
- **自动解决 5+ 个 AgentWorker 层面的 bug** — timeout、stall、health tracker 等问题不再存在
- **跟随模型进步** — Claude Code 的工具管理、上下文管理、重试逻辑随模型升级而升级，Weave 无需维护
- **差异化更清晰** — Weave 的 DAG 编排、重规划、记忆、学习是 Multica 没有的能力

### Negative

- **强依赖外部 Agent** — 无 Claude Code / Codex 时只能做规划/评估，不能执行
- **丢失细粒度控制** — Weave 无法拦截单个 tool call，只能在 node 级别做 guardrails
- **迁移风险** — Phase 2/3 改动面大，需要充分测试

### Risks

- Claude Code SDK API 变更可能导致 Backend 适配工作
- 部分 DAG 模板假设 AgentWorker 的工具能力，需要审查
- Memory 注入路径变更可能影响已有记忆的检索

## Alternatives Considered

1. **保留完整 BuiltinBackend 但降低优先级** — 不采纳。维护两套体系增加复杂度，且会继续在 AgentWorker 上修 bug。
2. **完全废弃 BuiltinBackend** — 不采纳。零依赖模式和规划/评估节点仍需轻量 LLM 调用能力。

## References

- [Scaling Managed Agents: Decoupling the brain from the hands](https://www.anthropic.com/engineering/managed-agents) — Anthropic 官方工程博客
- [multica-ai/multica](https://github.com/multica-ai/multica) — 开源 Managed Agents 平台参考实现
- `docs/adrs/0007-backend-abstraction.md` — ExecutionBackend 抽象
- `docs/adrs/0016-integration-layer-architecture.md` — Integration Layer 设计
- Issue #948 — 架构审计触发点
