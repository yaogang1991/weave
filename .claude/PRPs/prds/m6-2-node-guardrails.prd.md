# M6.2: Node Guardrails — Permission Control at DAG Node Boundary

## Problem Statement

M6.1 切换默认 backend 到 `claude_code` 后，Claude Code 管理自己的工具调用，Weave 看不到单个 tool call。现有 `Guardrails.check_and_execute()` 完全失效 — 所有 PermissionMode（PLAN/DEFAULT/ACCEPT_EDITS/AUTO/DONT_ASK）和 RiskLevel 分层对外部 backend 路径无任何保护。NodeExecutor 没有 pre/post check，外部 agent 可以不受约束地修改任何文件。

## Evidence

- `core/node_executor.py` — 零 guardrail 引用，`_execute_with_timeout()` 直接调用 `backend_registry.execute_for_node()`
- `guardrails/policy.py` — `check_and_execute()` 只在 `agent/agent_pool.py` 的 tool-call 路径中被消费
- `control_plane/execution_factory.py:130-159` — Guardrails 实例只传给 AgentPool，不传给 NodeExecutor 或 DAGExecutionEngine
- ADR-0017 Phase 2 明确要求 guardrails 提升到 node 级别

## Proposed Solution

在 `NodeExecutor` 的外部 backend 执行路径中插入 `NodeGuardrails` 类，提供确定性的 pre-check（执行前门控）和 post-check（执行后验证）。Pre-check 基于 agent_type、denied_commands、workspace 路径做静态评估。Post-check 基于 `protected_paths` 检测越界文件修改。不调 LLM，纯规则匹配。

## Key Hypothesis

We believe node-level deterministic guardrails will effectively protect external backend execution for Weave developers.
We'll know we're right when pre-check blocks CRITICAL nodes and post-check detects out-of-bound file changes, with no regression in existing tests.

## What We're NOT Building

- BuiltinBackend 路径的 guardrails 变更 — M6.3 scope（BuiltinBackend 简化为 LightweightLLMCaller 时一起清理）
- AgentPool 中 tool-call guardrails 的移除 — M6.3 scope
- LLM 驱动的风险评估 — 过早引入，确定性规则已覆盖主要场景
- `max_changed_files` 等额外 post-check 规则 — 未来增量加
- `owned_files` 契约检查 — Orchestrator 规划精度不足，会产生大量噪声警告，推迟到精度提升后
- DAGNode 新增 `risk_level` 字段 — 不需要，agent_type 推导足够

## Success Metrics

| Metric | Target | How Measured |
|--------|--------|--------------|
| Pre-check 阻止 | CRITICAL 风险节点（denied_commands 命中）被阻止 | 单元测试 |
| Post-check 阻止 | protected_paths 文件被修改时节点失败 | 单元测试 |
| 事件发射 | guardrail_blocked 事件正确发射 | 单元测试 |
| Builtin 路径无影响 | 现有 guardrails 测试全部通过 | `python -m pytest tests/test_guardrails*.py tests/test_personal_guardrails.py` |
| 无回归 | 全量测试通过 | `python -m pytest -v --tb=short` |

---

## Users & Context

**Primary User**
- **Who**: Weave 维护者/开发者
- **Current behavior**: 使用 `python main.py run "..." --project .` 时，claude_code backend 执行 DAG 节点，无任何安全检查
- **Trigger**: M6.1 完成后默认 backend 切换到 claude_code，tool-call guardrails 对主路径完全失效
- **Success state**: 外部 backend 执行有 node 级别的 pre/post check 保护

**Job to Be Done**

When Weave delegates a DAG node to an external backend, I want node-level safety checks before and after execution, so I can trust automated changes stay within safe boundaries.

**Non-Users**
- 仅使用 BuiltinBackend 的场景（planner/evaluator 节点不做文件操作，不需要 node guardrails）
- 交互式 CLI 用户（手动审批每个 tool call）

---

## Solution Detail

### Core Capabilities (MoSCoW)

| Priority | Capability | Rationale |
|----------|------------|-----------|
| Must | `NodeGuardrails` 类（pre_check + post_check） | 核心安全机制，独立于 NodeExecutor 可测试 |
| Must | Pre-check: agent_type 门控 | planner/evaluator 不做文件操作，跳过检查 |
| Must | Pre-check: denied_commands 匹配 task_description | 阻止不合规任务执行 |
| Must | Pre-check: workspace 路径校验 | 防止越界写入 |
| Must | Post-check: protected_paths 检测 | 阻止敏感文件被修改 |
| Must | `GuardrailBlockedException` + 不重试失败路径 | 被阻止的任务不应消耗 retry budget |
| Must | `guardrail_blocked` 事件发射 | 审计和监控需要 |
| Must | 测试覆盖 | 防止回归 |
| Should | `GuardrailsConfig.protected_paths` 可通过 .weave/config.yaml 自定义 | 项目特定的受保护路径 |
| Won't | owned_files 契约检查 | Orchestrator 规划精度不足，噪声高 |
| Won't | LLM 驱动的风险评估 | 过早引入 |
| Won't | BuiltinBackend 路径变更 | M6.3 scope |
| Won't | AgentPool tool-call guardrails 移除 | M6.3 scope |
| Won't | DAGNode.risk_level 字段 | agent_type 推导足够 |
| Won't | max_changed_files 规则 | 未来增量加 |

### MVP Scope

`NodeGuardrails` 类 + pre/post check + `GuardrailBlockedException` + 事件发射。仅对外部 backend 路径生效。

### User Flow

```
1. NodeExecutor._execute_with_timeout(node, artifacts, workspace_path)
2. backend_name = getattr(node, 'backend', self._default_agent_backend)
3. IF backend_name != "builtin" AND self._node_guardrails:
   a. Pre-check:
      - agent_type in ("planner", "evaluator") → skip (allowed)
      - workspace_path outside project → blocked
      - denied_commands matched in task_description → blocked
   b. If blocked → GuardrailBlockedException → node FAILED (no retry)
4. BackendRegistry.execute_for_node(backend_name, context)
5. Post-check:
   - BackendResult.artifacts matched against protected_paths → blocked
   - If blocked → node FAILED (result preserved in error for debugging)
   - Emit guardrail_blocked event
6. Continue to evaluation pipeline
```

---

## Technical Approach

**Feasibility**: HIGH

**Architecture Notes**
- 所有改动在 `guardrails/` 和 `core/` 层，不碰 `tools/`、`orchestrator/`、`integrations/`
- `NodeGuardrails` 独立类，与 `NodeIsolationGuard`（注入检测）平行，关注点分离
- Pre/post check 是确定性规则，零 LLM 调用，不影响执行延迟
- `GuardrailsConfig` 扩展 `protected_paths` 字段，向后兼容（默认值列表）
- NodeExecutor 只加一个 `__init__` 参数（`node_guardrails`），改动面小
- `GuardrailBlockedException` 继承 `Exception`，在 NodeExecutor 的异常处理中被捕获
- BuiltinBackend 路径完全不受影响（`backend_name != "builtin"` 门控）

**Technical Risks**

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Post-check 依赖 git diff（无 git 时 BackendResult.artifacts 为空） | M | 空 artifacts 列表跳过 post-check，不误报 |
| denied_commands 匹配 task_description 过于粗糙（误报） | L | 精确匹配 `denied` 字符串出现在 description.lower() 中；用户可通过 config 清空 denied_commands |
| protected_paths glob 匹配在 Windows 路径上不一致 | L | 使用 pathlib.PurePath 做路径比较，不硬编码分隔符 |
| NodeExecutor 参数继续增长 | M | M6.3 大重构时做 NodeExecutorConfig 分组，M6.2 只加一个参数 |

---

## Implementation Phases

<!--
  STATUS: pending | in-progress | complete
  PARALLEL: phases that can run concurrently (e.g., "with 3" or "-")
  DEPENDS: phases that must complete first (e.g., "1, 2" or "-")
  PRP: link to generated plan file once created
-->

| # | Phase | Description | Status | Parallel | Depends | PRP Plan |
|---|-------|-------------|--------|----------|---------|----------|
| 1 | NodeGuardrails 类 | 新建 `guardrails/node_guardrails.py`，实现 pre_check + post_check | in-progress | - | - | `.claude/PRPs/plans/m6-2-node-guardrails.plan.md` |
| 2 | GuardrailBlockedException | `core/exceptions.py` 新增异常类 | in-progress | with 1 | - | `.claude/PRPs/plans/m6-2-node-guardrails.plan.md` |
| 3 | GuardrailsConfig 扩展 | `core/project_config.py` 新增 `protected_paths` 字段 | in-progress | with 1 | - | `.claude/PRPs/plans/m6-2-node-guardrails.plan.md` |
| 4 | NodeExecutor 集成 | pre/post check 调用 + 事件发射 + 异常处理 | in-progress | - | 1, 2 | `.claude/PRPs/plans/m6-2-node-guardrails.plan.md` |
| 5 | 依赖注入 | execution_factory.py 创建 NodeGuardrails，透传到 NodeExecutor | in-progress | with 4 | 1, 3 | `.claude/PRPs/plans/m6-2-node-guardrails.plan.md` |
| 6 | 测试 | NodeGuardrails 单元测试 + NodeExecutor 集成测试 + 回归 | in-progress | - | 1-5 | `.claude/PRPs/plans/m6-2-node-guardrails.plan.md` |

### Phase Details

**Phase 1: NodeGuardrails 类**
- **Goal**: 独立可测试的 node 级权限控制类
- **Scope**: `guardrails/node_guardrails.py`
  ```python
  class NodeGuardrails:
      def __init__(self, config: GuardrailsConfig, project_dir: str | None): ...
      def pre_check(self, node: DAGNode, workspace_path: str | None) -> GuardrailResult: ...
      def post_check(self, result: BackendResult, workspace_path: str | None) -> GuardrailResult: ...
  ```
  - `pre_check()`: agent_type 门控 → workspace 校验 → denied_commands 匹配
  - `post_check()`: BackendResult.artifacts 逐个匹配 protected_paths（fnmatch）
  - 复用 `GuardrailResult`（allowed/blocked），不引入新状态
- **Success signal**: 独立测试全部通过，pre_check 对 planner/evaluator 返回 allowed，对 denied_commands 命中返回 blocked

**Phase 2: GuardrailBlockedException**
- **Goal**: 区分 guardrails 阻止和其他执行错误
- **Scope**: `core/exceptions.py`
  ```python
  class GuardrailBlockedException(Exception):
      def __init__(self, reason: str, phase: str = "pre"): ...
  ```
  - `phase`: "pre" 或 "post"，用于事件发射和调试
- **Success signal**: 异常可被 NodeExecutor 捕获，与 PendingApprovalError/RateLimitError 并列

**Phase 3: GuardrailsConfig 扩展**
- **Goal**: 配置文件支持 protected_paths
- **Scope**: `core/project_config.py`
  ```python
  class GuardrailsConfig(BaseModel):
      denied_commands: list[str] = Field(default_factory=list)
      approval_policy: str = "accept_edits"
      protected_paths: list[str] = Field(default_factory=lambda: [
          ".env", ".env.*",
          "credentials*",
          "id_rsa", "id_ed25519",
          ".ssh/", ".gnupg/",
          ".git/config",
      ])
  ```
- **Success signal**: 默认值生效，`.weave/config.yaml` 可覆盖

**Phase 4: NodeExecutor 集成**
- **Goal**: 在执行管道中调用 pre/post check
- **Scope**: `core/node_executor.py`
  - `__init__` 新增 `node_guardrails: NodeGuardrails | None = None`
  - `_execute_with_timeout()` 中 backend_name != "builtin" 时执行 pre_check
  - `execute_node()` 中 execute 和 evaluate 之间执行 post_check
  - `GuardrailBlockedException` 走不消耗 retry budget 的失败路径
  - 发射 `guardrail_blocked` 事件
- **Success signal**: mock NodeGuardrails 时节点被正确阻止，事件正确发射

**Phase 5: 依赖注入**
- **Goal**: NodeGuardrails 从 execution_factory 创建并透传到 NodeExecutor
- **Scope**: `control_plane/execution_factory.py`, `core/dag_engine.py`
  - `execution_factory.py`: 创建 `NodeGuardrails(config=project_config.guardrails, project_dir=project_dir)`
  - `DAGExecutionEngine.__init__`: 新增 `node_guardrails` 参数，存为 `self._node_guardrails`
  - `DAGExecutionEngine` 创建 NodeExecutor 处: 透传 `node_guardrails`
- **Success signal**: 端到端执行时 NodeExecutor 持有 NodeGuardrails 实例

**Phase 6: 测试**
- **Goal**: 覆盖所有新增逻辑，确认无回归
- **Scope**:
  - `tests/test_node_guardrails.py` — 新测试文件
    - pre_check: agent_type 门控、workspace 校验、denied_commands 匹配、空配置 fallback
    - post_check: protected_paths 命中、空 artifacts、glob 模式匹配、Windows 路径
  - NodeExecutor 集成测试: pre-check 阻止不重试、post-check 阻止保留 result、事件发射
  - 回归: `python -m pytest -v --tb=short` 全量通过
- **Success signal**: 所有新测试通过，现有测试无回归

### Parallelism Notes

Phase 1/2/3 无依赖，可并行。Phase 4 依赖 1+2。Phase 5 依赖 1+3。Phase 6 依赖 1-5。

---

## Decisions Log

| # | Decision | Choice | Alternatives | Rationale |
|---|----------|--------|--------------|-----------|
| 1 | Pre-check 风险评估方式 | agent_type 推导 + denied_commands 匹配 | A: DAGNode.risk_level 由 Orchestrator 填入 | Orchestrator 职责是 DAG 规划不是安全分类；LLM 风险判断不稳定；guardrails 应是确定性规则 |
| 2 | Post-check 规则 | 只做 protected_paths 检测 | 加 owned_files 契约检查 | owned_files 经常不完整（Orchestrator 无法预知所有文件变更），噪声高 |
| 3 | Post-check 发现问题时 | 阻止节点完成（FAILED） | B: 警告不阻止 / C: 分级 | 越界文件是真实安全问题，必须阻止 |
| 4 | GuardrailsConfig 扩展 | 只加 protected_paths | 加 max_changed_files 等 | protected_paths 解决真实安全问题；max_changed_files 阈值难定，M6.2 不引入 |
| 5 | 生效范围 | 所有非 builtin backend | 只对 claude_code | "看不到 tool call" 的问题适用于所有外部 backend；白名单维护成本高 |
| 6 | Builtin 路径 | 不动 | 也加 node-level check | Builtin 路径已有更精确的 tool-call 级别 guardrails；M6.3 简化 BuiltinBackend 时一起清理 |
| 7 | 检查逻辑封装 | 独立 `NodeGuardrails` 类 | 内联在 NodeExecutor | 职责分离、独立可测试、与 NodeIsolationGuard/OutputMonitor 架构一致 |
| 8 | 异常类型 | 新增 `GuardrailBlockedException` | 复用 PendingApprovalError | Blocked 是硬拒绝不重试；PendingApproval 等待后可恢复；语义不同 |
| 9 | 异常路径 | 不消耗 retry budget | 消耗 retry budget | 被 guardrails 阻止说明任务本身不合规，重试不改变结果 |
| 10 | 依赖注入路径 | execution_factory 创建 → DAGExecutionEngine 透传 → NodeExecutor | NodeExecutor 从 project_config 自行创建 | 与 M6.1 模式一致；便于 mock 测试；创建逻辑不在 NodeExecutor 内 |
| 11 | 事件发射 | blocked 时发射 `guardrail_blocked` | 所有 check 结果都发射 | 通过是常态，发射会噪声太大；只发射异常情况用于审计 |
| 12 | Post-check 位置 | execute 和 evaluate 之间 | evaluate 之后 | 阻止的节点不应浪费 evaluation 资源 |
| 13 | denied_commands 匹配 | 对 task_description 做 substring 匹配 | 对实际 bash 命令匹配 | 外部 backend 执行工具 opaquely，Weave 看不到实际命令；task_description 是唯一可匹配的文本 |
| 14 | protected_paths 默认值 | .env, credentials, SSH keys, .git/config | 不设默认 / 更激进列表 | 覆盖最通用的 secrets 文件；用户可通过 config 追加项目特定路径 |

---

## Research Summary

**Technical Context**
- `guardrails/policy.py` — 498 行，`Guardrails` 类 + `PersonalGuardrails` 子类，tool-call 级别，5 种 PermissionMode，`RISK_MAP` 映射 7 个工具
- `guardrails/node_isolation.py` — `NodeIsolationGuard` 在 artifact handoff 边界做注入检测，不做权限控制
- `guardrails/output_monitor.py` — `OutputMonitor` 在 tool output 做注入检测，不做权限控制
- `core/node_executor.py:742` — NodeExecutor 无 guardrail 引用，`_execute_with_timeout()` 直接调 `backend_registry.execute_for_node()`
- `core/node_executor.py:629` — `backend_name = getattr(node, 'backend', self._default_agent_backend)`
- `core/guardrail_models.py` — `RiskLevel`, `PermissionMode`, `GuardrailPolicy`, `PersonalGuardrailPolicy` 模型
- `core/project_config.py:57-62` — `GuardrailsConfig` 当前只有 `denied_commands` 和 `approval_policy`
- `core/exceptions.py` — 已有 `PendingApprovalError`, `RateLimitError`, `NodeTimeoutError`, `BudgetExhaustedError`
- `agent/agent_pool.py:72-84` — `WorkerAgent.__init__` 持有 `guardrails`，在每次 tool call 时调用 `check_and_execute()`
- `control_plane/execution_factory.py:130-159` — 创建 Guardrails 实例只传给 AgentPool
- `core/backend_models.py:20-54` — `BackendResult.artifacts` 携带 git diff 发现的变更文件列表
- `agent/backends/claude_code.py:461-488` — `_discover_artifacts()` 使用 `git diff --name-only --diff-filter=ACMR`
- `core/artifact_handoff.py:101-116` — `NodeIsolationGuard` 集成模式参考

---

*Generated: 2026-05-27*
*Status: VALIDATED — grill-me (12 decisions) + grill-with-docs (3 CONTEXT.md updates) completed*
