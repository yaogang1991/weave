# M7.2.5 — 安全移除 M6 废弃代码

**Date:** 2026-06-11
**Issue:** #1088
**Parent:** #1008 (M7.2 — 架构清理与技术债务偿还)
**Depends on:** #1086 (已关闭)

## 摘要

M6 迁移后保留了 ~1915 行废弃代码（`AgentPool`, `AgentWorker`, `OutputMonitor`, `StuckDetector`）。这些代码仍有活跃引用，需按依赖拓扑逆序移除。移除后 BuiltinBackend 变为 lightweight-only，generator 节点完全依赖外部后端。

## 决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 回退策略 | 直接移除 pool 回退 | generator 节点默认走 claude_code 后端，pool 回退极少使用 |
| 移除方式 | 一次性全量移除（单 PR） | 依赖链无中间态，分阶段反而导致不一致 |
| 测试处理 | 删除相关测试 | 测试直接测试废弃代码，无保留价值 |
| prompts.py | 保留 SYSTEM_PROMPTS，移除 TOOL_ALLOWLIST | SYSTEM_PROMPTS 仍被 BuiltinBackend 和 planner.py 使用 |

## 删除文件清单

| 文件 | 行数 | 移除原因 |
|------|------|----------|
| `agent/agent_pool.py` | 702 | AgentPool/WorkerAgent/ExecutionContext 全部废弃 |
| `agent/worker.py` | 646 | AgentWorker 废弃，仅被 agent_pool.py 引用 |
| `guardrails/output_monitor.py` | 197 | 仅被 worker.py 引用（已标注 DEPRECATED） |
| `core/stuck_detector.py` | 173 | 仅被 worker.py 引用（已标注 DEPRECATED） |

**小计：1718 行源代码**

## 修改文件清单

### 核心变更

| 文件 | 修改内容 |
|------|----------|
| `agent/backends/builtin.py` | 移除 `pool` 参数、`_execute_pool()`、`_ensure_closure()`；`execute()` 仅走 lightweight 路径 |
| `agent/backends/registry.py` | 移除 `from_pool()` 工厂方法 |
| `control_plane/execution_factory.py` | 移除 AgentPool import 和创建；直接构造 BuiltinBackend(lightweight_caller=...) |
| `cli/execution.py` | 移除 AgentPool import 和创建；使用 BuiltinBackend + BackendRegistry |
| `agent/prompts.py` | 移除 `TOOL_ALLOWLIST`（仅 agent_pool.py 使用），保留 `SYSTEM_PROMPTS` |

### Docstring/注释清理

| 文件 | 修改内容 |
|------|----------|
| `core/node_executor.py` | 更新引用 AgentPool/AgentWorker 的注释 |
| `core/context.py` | 更新引用 AgentWorker 的注释 |
| `core/backend_models.py` | 更新引用 AgentPool 的注释 |
| `core/activity_detector.py` | 更新引用 stuck_detector 的注释 |
| `core/llm_client.py` | 更新引用 AgentWorker 的注释 |
| `core/exceptions.py` | 更新引用 AgentWorker.run() 的注释 |

## 测试文件处理

### 删除的测试文件（直接测试废弃模块）

~20 个测试文件将删除，包括：
- `tests/test_worker.py`
- `tests/test_stuck_detector.py`
- `tests/test_output_monitor.py`
- `tests/test_132_refactor.py`（测试 AgentPool API）
- `tests/test_execution_context.py`（测试 ExecutionContext）
- `tests/test_parallel_artifact_isolation.py`（测试 AgentPool 隔离性）
- `tests/test_empty_call_auto_retry.py`（测试 AgentWorker 重试）
- `tests/test_empty_tool_call_breaker.py`
- `tests/test_degenerate_empty_args.py`
- `tests/test_733_degenerate_recovery.py`
- `tests/test_739_tool_exec_progress.py`
- `tests/test_evaluator_file_exists.py`（部分 AgentWorker 测试）
- `tests/test_fault_tolerance_hemostasis.py`（部分 AgentWorker 测试）
- `tests/test_malformed_args_logging.py`
- `tests/test_tool_arg_validation.py`
- `tests/test_tool_call_id.py`
- `tests/test_worker_memory.py`
- `tests/test_injection_regression.py`（OutputMonitor 层测试）
- `tests/test_file_path_constraints.py`（_inject_file_path_constraints 测试）

### 更新的测试文件

- `tests/test_execution_factory.py` — mock AgentPool 的测试改为验证 BuiltinBackend 构造
- `tests/test_m6_4_cleanup.py` — 保留 SYSTEM_PROMPTS 导入测试，移除 OutputMonitor/StuckDetector 测试
- `tests/test_llm_dag_integration.py` — 移除 AgentPool 引用，改用 BuiltinBackend

### 需检查的测试文件（可能整文件删除或仅移除部分引用）

- `tests/test_cross_file_refactoring.py`
- `tests/test_naming_convention.py`
- `tests/test_runtime_context.py`
- `tests/test_edit_efficiency.py`
- `tests/test_eval_dedup.py`
- `tests/test_evaluator_file_contracts.py`
- `tests/test_feature_complexity_decomposition.py`
- `tests/test_retry_incremental_fix.py`
- `tests/test_retry_naming_guidance.py`

## BuiltinBackend 变更详情

```python
# 之前：
class BuiltinBackend(AgentBackend):
    def __init__(self, lightweight_caller=..., pool=None, ...):
        self._pool = pool
        self._executor_closure = None

    async def execute(self, context):
        if lightweight and planner/evaluator:
            return await self._execute_lightweight(context)
        return await self._execute_pool(context)  # pool fallback

# 之后：
class BuiltinBackend(AgentBackend):
    def __init__(self, lightweight_caller=..., session_store=..., session_id=...):
        ...  # 移除 pool 参数

    async def execute(self, context):
        return await self._execute_lightweight(context)  # 仅 lightweight
```

## Generator 节点降级行为

- `default_agent_backend` 已默认为 `"claude_code"`
- 外部后端不可用 → BackendRegistry 回退到 BuiltinBackend → lightweight 路径
- Lightweight 路径对 generator 节点产出文本输出（无工具循环）
- NodeExecutor 质量门控（零输出检测）会捕获失败
- DAGEngine 的 `adapt_to_failure()` 提供失败恢复

## 移除顺序（依赖拓扑逆序）

```
Step 1: agent/backends/builtin.py — 去 pool 化
Step 2: agent/backends/registry.py — 移除 from_pool()
Step 3: control_plane/execution_factory.py — 移除 AgentPool
Step 4: cli/execution.py — 移除 AgentPool
Step 5: agent/prompts.py — 移除 TOOL_ALLOWLIST
Step 6: 删除 agent/agent_pool.py
Step 7: 删除 agent/worker.py
Step 8: 删除 guardrails/output_monitor.py
Step 9: 删除 core/stuck_detector.py
Step 10: 删除/更新测试文件
Step 11: 清理注释和 docstring
Step 12: 全量测试验证
```

## 验收标准

- [ ] `grep -rn "AgentPool\|AgentWorker\|OutputMonitor\|StuckDetector\|TOOL_ALLOWLIST"` 仅返回 shim 或注释
- [ ] 删除 ~1718 行源代码 + ~3000+ 行测试代码
- [ ] 所有测试通过 (`python -m pytest -v --tb=short`)
- [ ] Lint 通过 (`flake8 --max-line-length=100`)
