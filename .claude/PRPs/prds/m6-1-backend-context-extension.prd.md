# M6.1: BackendContext Extension + Default Backend Switch

## Problem Statement

Weave 的 memory 注入分散在 `AgentPool.WorkerAgent.execute()` 和 `AgentWorker.run()` 两处，agent_type 不一致（`capability.id` vs `"shared"`），且仅 BuiltinBackend 能获得 memory。外部 backend（ClaudeCodeBackend、CodexBackend）通过 BackendContext 执行，但 BackendContext 不携带 memory 和项目上下文，导致 Claude Code / Codex 执行时缺少相关知识。同时默认 agent backend 为 builtin，违背 Brain/Hands 分离（ADR-0017）。

## Evidence

- `agent/agent_pool.py` line 233 用 `self.capability.id` 注入 memory，`agent/worker.py` 用 `"shared"`——agent_type 不一致
- `core/backend_models.py` BackendContext 无 memory 相关字段
- `agent/backends/claude_code.py` `_build_prompt()` 不注入任何 memory 或项目上下文
- `core/node_executor.py` line 608 默认 backend 硬编码为 `'builtin'`
- ADR-0017 明确 Brain/Hands 分离要求统一接口

## Proposed Solution

扩展 `BackendContext` 新增 `memory_prompt` 和 `project_context` 字段，在 `NodeExecutor` 构建 context 时统一注入 memory 和项目配置。修改 ClaudeCodeBackend 和 CodexBackend 的 `_build_prompt()` 使用新字段。将默认 agent backend 从 `builtin` 切换为 `claude_code`。

## Key Hypothesis

We believe 统一 BackendContext 注入 will 让所有 backend 正确接收 memory 和项目上下文 for Weave 开发者.
We'll know we're right when ClaudeCodeBackend 执行 DAG 节点时 prompt 中包含 memory 和 project context，且默认走 claude_code 后 DAG 执行不回归.

## What We're NOT Building

- Guardrails 提升（tool-call → node 级别）— M6.2 scope
- AgentWorker 简化 / LightweightLLMCaller — M6.3 scope
- BuiltinBackend 重写 — M6.3 scope
- NodeExecutor 参数分组（NodeExecutorConfig）— M6.3 scope
- 旧 memory 注入路径移除（agent_pool/worker）— M6.3 随 AgentWorker 一起清理
- Memory 注入质量验证（不验证 memory 是否提升执行质量，只验证注入链路通畅）

## Success Metrics

| Metric | Target | How Measured |
|--------|--------|--------------|
| BackendContext 新字段注入 | ClaudeCodeBackend prompt 包含 memory + project context | 单元测试 + 手动验证 |
| 默认 backend 切换 | 新 DAG 节点默认走 claude_code | 单元测试 |
| Memory 注入一致性 | NodeExecutor 使用 node.agent_type 获取 memory | 单元测试 |
| 无回归 | 现有测试全部通过 | `python -m pytest` |

---

## Users & Context

**Primary User**
- **Who**: Weave solo maintainer 及未来使用 Weave 的开发者
- **Current behavior**: 外部 backend 执行时拿不到 memory，默认走 builtin 路径
- **Trigger**: 使用 `python main.py run "..." --project .` 时，ClaudeCodeBackend 缺少上下文
- **Success state**: 所有 backend 统一获得 memory 和项目上下文，默认走 claude_code

**Job to Be Done**

When Weave dispatches a DAG node to an external backend, I want it to carry memory and project context, so the external agent can leverage accumulated knowledge.

**Non-Users**
- 仅使用 builtin backend 的离线环境（M6.1 不改变 builtin 行为）
- 不使用 memory 功能的场景

---

## Solution Detail

### Core Capabilities (MoSCoW)

| Priority | Capability | Rationale |
|----------|------------|-----------|
| Must | BackendContext 新增 memory_prompt + project_context | 统一注入接口的基础 |
| Must | NodeExecutor 注入 memory + project context | 注入点唯一化，使用 node.agent_type |
| Must | ClaudeCodeBackend 使用新 context 字段 | 主要外部 backend 需要上下文 |
| Must | 默认 agent backend 改为 claude_code（支持 WEAVE_DEFAULT_AGENT_BACKEND） | 落地 Brain/Hands 分离 |
| Must | 测试覆盖 | 防止回归 |
| Should | CodexBackend 使用新 context 字段 | 第二外部 backend 同步更新 |
| Won't | BuiltinBackend 重写 | M6.3 scope |
| Won't | Guardrails 变更 | M6.2 scope |
| Won't | 旧注入路径移除 | M6.3 scope，BuiltinBackend 暂时重复注入无害 |
| Won't | NodeExecutor 参数分组 | M6.3 scope |

### User Flow

```
1. NodeExecutor._execute_with_timeout(node, artifacts, workspace_path)
2. 构建 BackendContext:
   a. MemoryManager.get_context_for_agent(node.agent_type, node.task_description, session_id)
      → format_memory_prompt() → memory_prompt
   b. ProjectConfig.to_summary() → project_context
      (输出 project_context 字段: language/framework/test_runner/conventions)
   c. BackendContext(..., memory_prompt=..., project_context=...)
3. backend_name = node.backend or config.default_agent_backend ("claude_code")
   (支持 WEAVE_DEFAULT_AGENT_BACKEND 环境变量覆盖)
4. BackendRegistry.execute_for_node(backend_name, context)
   → health check → fallback to builtin if unhealthy
5. Backend._build_prompt(context) 使用 memory_prompt + project_context
```

---

## Technical Approach

**Feasibility**: HIGH

**Architecture Notes**
- 所有改动在 `core/` 和 `agent/backends/` 层，不碰 `tools/`、`orchestrator/`、`integrations/`
- BackendContext 新字段有默认值（`""`），向后兼容
- `BackendRegistry.execute_for_node()` 已有 health check + fallback，默认 backend 切换安全
- `ProjectConfig.to_summary()` 只输出 `project_context` 字段（language/framework/test_runner/conventions）
- Memory 注入使用 `node.agent_type`（非旧的 `"shared"`），与 memory 存储分区对齐
- NodeExecutor 构造在 `core/dag_engine.py:195`（非 execution_factory.py），DAGExecutionEngine 已持有 memory_manager
- 旧注入路径（agent_pool/worker）不动，M6.3 随 AgentWorker 一起清理

**Technical Risks**

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| NodeExecutor 不持有 memory_manager | CONFIRMED | 构造函数注入，DAGExecutionEngine 已持有，透传即可 |
| ProjectConfig.to_summary() 不存在 | CONFIRMED | 新增方法，只格式化 project_context 字段 |
| 默认 backend 切换后 builtin 路径回归 | L | fallback 已存在 + WEAVE_DEFAULT_AGENT_BACKEND 环境变量回退 |
| config.py default_backend 是 workspace isolation | CONFIRMED | 新增 `default_agent_backend` 字段，不修改原字段 |

---

## Implementation Phases

| # | Phase | Description | Status | Parallel | Depends | PRP Plan |
|---|-------|-------------|--------|----------|---------|----------|
| 1 | BackendContext 扩展 | 新增 memory_prompt + project_context 字段 | pending | - | - | - |
| 2 | ProjectConfig.to_summary() | 新增方法，只输出 project_context 字段 | pending | with 1 | - | - |
| 3 | NodeExecutor 注入 | __init__ 加 memory_manager + project_config 参数，构建 context 时填充 | pending | - | 1 | - |
| 4 | dag_engine.py 透传 | DAGExecutionEngine.__init__ 加 project_config，透传 memory_manager + project_config 到 NodeExecutor | pending | - | 3 | - |
| 5 | Backend 使用新字段 | ClaudeCodeBackend + CodexBackend _build_prompt() 使用新字段 | pending | with 4 | 1 | - |
| 6 | 默认 backend 切换 | config.py 新增 default_agent_backend → DAGEngineConfig → NodeExecutor | pending | with 5 | - | - |
| 7 | 测试 | 新字段序列化、注入逻辑、backend prompt、默认选择、回归 | pending | - | 1-6 | - |

### Phase Details

**Phase 1: BackendContext 扩展**
- **Goal**: BackendContext 携带 memory 和 project context
- **Scope**: `core/backend_models.py` line 76 后新增 2 个带默认值字段
  ```python
  memory_prompt: str = ""
  project_context: str = ""
  ```
- **Success signal**: BackendContext 能序列化/反序列化新字段，现有构造不报错

**Phase 2: ProjectConfig.to_summary()**
- **Goal**: 从 .weave/config.yaml 的 project_context 生成文本摘要
- **Scope**: `core/project_config.py` 新增 `to_summary() -> str` 方法
  - 只输出 `self.project_context`（language, framework, test_runner, conventions）
  - 默认值全空时返回空字符串 `""`
  - 格式：`"Language: python\nFramework: fastapi\nTest runner: pytest\nConventions: ..."`
- **Success signal**: 有配置时返回非空字符串，无配置时返回空字符串

**Phase 3: NodeExecutor 注入**
- **Goal**: NodeExecutor 持有 memory_manager 和 project_config，构建 context 时注入
- **Scope**: `core/node_executor.py`
  - `__init__` 新增 `memory_manager: MemoryManager | None = None`（param #18）
  - `__init__` 新增 `project_config: ProjectConfig | None = None`（param #19）
  - `_execute_with_timeout()` 构建 BackendContext 前（line 597）新增注入：
    ```python
    memory_prompt = ""
    if self._memory_manager and self._memory_manager.config.enabled:
        entries = self._memory_manager.get_context_for_agent(
            agent_type=node.agent_type,
            task_description=node.task_description,
            session_id=self._session_id,
        )
        memory_prompt = self._memory_manager.format_memory_prompt(entries)

    project_context = ""
    if self._project_config:
        project_context = self._project_config.to_summary()
    ```
  - BackendContext 构造新增两个参数：`memory_prompt=memory_prompt, project_context=project_context`
- **Success signal**: mock MemoryManager 时 context.memory_prompt 非空

**Phase 4: dag_engine.py 透传**
- **Goal**: DAGExecutionEngine 将 memory_manager 和 project_config 透传给 NodeExecutor
- **Scope**: `core/dag_engine.py`
  - `DAGExecutionEngine.__init__`（line 126）新增 `project_config: ProjectConfig | None = None`
  - 存为 `self._project_config = project_config`
  - NodeExecutor 构造处（line 195-213）新增：
    ```python
    memory_manager=memory_manager,  # 已有参数，只需透传
    project_config=project_config,
    ```
- **Success signal**: NodeExecutor 构造时不报错，端到端执行时拿到 memory_manager

**Phase 5: Backend 使用新字段**
- **Goal**: ClaudeCodeBackend 和 CodexBackend prompt 中包含 memory + project context
- **Scope**:
  - `agent/backends/claude_code.py` `_build_prompt()`（line 408-452）末尾 append：
    ```python
    if context.memory_prompt:
        parts.append(f"\n{context.memory_prompt}")
    if context.project_context:
        parts.append(f"\n## Project Context\n{context.project_context}")
    ```
  - `agent/backends/codex.py` `_build_prompt()`（line 116-133）同上
- **Success signal**: _build_prompt() 输出包含 "Relevant Memory" 和 "Project Context" section

**Phase 6: 默认 backend 切换**
- **Goal**: 新 DAG 节点默认走 claude_code backend
- **Scope**:
  - `core/config.py` 新增字段（在 default_backend 附近）：
    ```python
    default_agent_backend: str = Field(
        default_factory=lambda: os.getenv("WEAVE_DEFAULT_AGENT_BACKEND", "claude_code")
    )
    ```
  - `core/dag_engine.py` `DAGEngineConfig.__init__` 新增 `default_agent_backend: str = "claude_code"`
  - `core/dag_engine.py:195` NodeExecutor 构造处新增 `default_agent_backend=cfg.default_agent_backend`
  - `core/node_executor.py` `__init__` 新增 `default_agent_backend: str = "claude_code"`，存为 `self._default_agent_backend`
  - `core/node_executor.py` line 608：
    ```python
    backend_name = getattr(node, 'backend', self._default_agent_backend)
    ```
  - `control_plane/execution_factory.py:210` DAGEngineConfig 构造处新增 `default_agent_backend=_cfg.default_agent_backend`
- **Success signal**: 无 backend 指定的节点走 claude_code，`WEAVE_DEFAULT_AGENT_BACKEND=builtin` 可回退

**Phase 7: 测试**
- **Goal**: 覆盖所有新增逻辑，确认无回归
- **Scope**:
  - BackendContext 新字段序列化/反序列化
  - NodeExecutor memory 注入（mock MemoryManager，验证 agent_type=node.agent_type）
  - ProjectConfig.to_summary()（有配置/无配置）
  - ClaudeCodeBackend._build_prompt() 输出包含 memory + project context
  - 默认 backend 选择逻辑（claude_code 默认 / 环境变量覆盖 / fallback）
  - `python -m pytest -v --tb=short` 全量回归
- **Success signal**: 所有新测试通过，现有测试无回归

### Parallelism Notes

Phase 1 和 Phase 2 可并行（无依赖）。Phase 3 依赖 Phase 1。Phase 4 依赖 Phase 3。Phase 5 依赖 Phase 1。Phase 6 独立于 3-5（只改 config + node_executor 默认值）。Phase 7 最后。

---

## Decisions Log

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| 1 | Memory 注入位置 | NodeExecutor 构建 BackendContext 时 | 统一入口，所有 backend 受益 |
| 2 | Memory agent_type | `node.agent_type` | 与 memory 存储分区对齐，`"shared"` 是旧 bug |
| 3 | ProjectConfig.to_summary() 范围 | 只输出 project_context 字段 | 该字段专为 LLM 注入设计，runtime/guardrails 被 DAG engine 直接消费 |
| 4 | 默认 backend 配置字段名 | `default_agent_backend` | `default_backend` 已用于 workspace isolation |
| 5 | 环境变量覆盖 | `WEAVE_DEFAULT_AGENT_BACKEND` | 与 `WEAVE_DEFAULT_BACKEND` 模式一致，降低切换风险 |
| 6 | Fallback 逻辑 | 不修改 | BackendRegistry 已有 health check → fallback to builtin |
| 7 | BuiltinBackend 范围 | M6.1 不改动 | 重写在 M6.3 |
| 8 | 旧注入路径 | 不动 | BuiltinBackend 暂时重复注入，无害，M6.3 随 AgentWorker 清理 |
| 9 | NodeExecutor 参数分组 | M6.1 不做 | M6.3 大重构时再做 NodeExecutorConfig |
| 10 | NodeExecutor 创建位置 | `core/dag_engine.py:195`（非 execution_factory.py） | DAGExecutionEngine 已持有 memory_manager，只需加 project_config |
| 11 | CodexBackend 优先级 | Should | 使用量低于 ClaudeCodeBackend，但改动成本低 |
| 12 | default_agent_backend 存放位置 | `DAGEngineConfig` | DAG 引擎 tunable，与 max_parallel 同级 |
| 13 | project_config 加载方式 | `ProjectConfig.load(work_dir)` 在 execution_factory 中 | 与现有 load_project_guardrails 共存，M6.1 不重构 |
| 14 | BuiltinBackend 重复注入 | 不存在 | BuiltinBackend 不读 context.memory_prompt，两条路径不交叉 |

---

## Research Summary

**Technical Context**
- `core/backend_models.py:56-76` — BackendContext 当前 9 个字段，无 memory
- `core/node_executor.py:70-119` — `__init__` 17 个参数，不持有 memory_manager
- `core/node_executor.py:597-607` — BackendContext 构建处，需在此注入
- `core/node_executor.py:608` — `backend_name = getattr(node, 'backend', 'builtin')` 硬编码
- `core/dag_engine.py:126-145` — `DAGExecutionEngine.__init__` 已有 memory_manager
- `core/dag_engine.py:195-213` — NodeExecutor 创建处，需透传新参数
- `core/config.py:745-750` — `default_backend` 是 workspace isolation（WEAVE_DEFAULT_BACKEND）
- `core/project_config.py:64-74` — `ProjectContext` 模型已有 language/framework/test_runner/conventions
- `agent/backends/claude_code.py:408-452` — `_build_prompt()` 当前不使用 memory
- `agent/backends/codex.py:116-133` — 同上
- `memory/manager.py:245-321` — `get_context_for_agent()` + `format_memory_prompt()` 已存在
- `agent/backends/registry.py:23-28` — `BackendRegistry.__init__` 已有 fallback 机制

---

*Generated: 2026-05-27*
*Status: VALIDATED — grill-me + grill-with-docs completed, 14 decisions resolved*
