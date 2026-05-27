# Brain/Hands Migration Plan

*ADR-0017 的执行计划。每个 Phase 可独立实现和验证。*

---

## Phase 1: 接口变更（1-2 周）

### 1.1 BackendContext 扩展

**文件**: `core/backend_models.py`

新增两个字段：

```python
class BackendContext(BaseModel):
    # ... 现有字段不变 ...
    memory_prompt: str = ""          # 从 MemoryManager.get_context_for_agent() 注入
    project_context: str = ""        # 从 ProjectConfig 加载的 .weave/config.yaml 摘要
```

**为什么**: 当前 memory 注入在 `AgentPool.WorkerAgent.execute()`（用 `capability.id`）和 `AgentWorker.run()`（用 `"shared"`）两处，agent_type 不一致。统一注入到 BackendContext 后，所有 backend（builtin/claude_code/codex）都能获得 memory 和 project context。

### 1.2 NodeExecutor 注入 memory + project context

**文件**: `core/node_executor.py`（约第 608 行）

在构建 `BackendContext` 之前，增加 memory 和 project context 注入：

```python
# 注入 memory（需确认 NodeExecutor 是否持有 memory_manager 引用）
memory_prompt = ""
if self._memory_manager and self._memory_manager.config.enabled:
    entries = self._memory_manager.get_context_for_agent(
        agent_type=node.agent_type,
        task_description=node.task_description,
        session_id=self._session_id,
    )
    memory_prompt = self._memory_manager.format_memory_prompt(entries)

# 注入 project context
project_context = ""
if self._project_config:
    project_context = self._project_config.to_prompt_section()

context = BackendContext(
    ...,
    memory_prompt=memory_prompt,       # 新增
    project_context=project_context,   # 新增
)
```

**需确认**: `NodeExecutor` 当前是否持有 `memory_manager` 引用。如不持有，需通过构造函数注入。

### 1.3 ClaudeCodeBackend 使用新 context 字段

**文件**: `agent/backends/claude_code.py`

修改 `_build_prompt(self, context: BackendContext) -> str`：

```python
# 新增: memory context
if context.memory_prompt:
    parts.append(f"\n## Relevant Memory\n{context.memory_prompt}")

# 新增: project context
if context.project_context:
    parts.append(f"\n## Project Context\n{context.project_context}")
```

### 1.4 默认 backend 改为 claude_code

**文件**: `core/node_executor.py`（第 608 行）

```python
# 当前:
backend_name = getattr(node, 'backend', 'builtin')
# 改为:
backend_name = getattr(node, 'backend', self._default_backend)
```

`self._default_backend` 从 `WeaveConfig` 读取，默认值从 `'builtin'` 改为 `'claude_code'`。

**文件**: `core/config.py`

```python
class WeaveConfig:
    default_backend: str = "claude_code"  # 原来是 "builtin"
```

**回退逻辑**: `BackendRegistry.execute_for_node()` 已有 health check + fallback 到 builtin 的逻辑，无需修改。

### 1.5 CodexBackend 同步更新

**文件**: `agent/backends/codex.py`

同样在 `_build_prompt()` 中使用 `context.memory_prompt` 和 `context.project_context`。

### Phase 1 验证

```bash
python -m pytest -v --tb=short
python main.py run "Add a hello world endpoint" --project .
```

---

## Phase 2: Guardrails 提升（2-3 周）

### 2.1 NodeExecutor 增加 pre_check

**文件**: `core/node_executor.py`

在调用 `backend.execute()` 之前：

```python
async def _pre_execute_check(self, node: DAGNode, workspace_path: str) -> GuardrailResult:
    risk_level = self._risk_assessor.assess(node)
    if risk_level == RiskLevel.CRITICAL:
        return GuardrailResult(blocked=True, reason="...")
    return GuardrailResult(blocked=False)
```

### 2.2 NodeExecutor 增加 post_check

在 `backend.execute()` 返回后：

```python
async def _post_execute_check(self, node: DAGNode, workspace_path: str,
                               result: BackendResult) -> GuardrailResult:
    # 验证变更文件在预期范围内
    # 验证无敏感文件被修改
    return GuardrailResult(blocked=False)
```

### 2.3 移除 tools/registry.py 中的 guardrail 集成

- 外部 backend 路径：Weave 看不到 tool call，guardrails 在 node 级别生效
- Builtin backend 路径（规划/评估）：planner/evaluator 不使用文件工具，不需要 tool-call 级别 guardrail

### Phase 2 验证

```bash
python -m pytest tests/test_guardrails.py tests/test_node_executor.py -v
```

---

## Phase 3: AgentPool 重构（3-4 周）

### 3.1 AgentWorker 简化为 LightweightLLMCaller

**文件**: `agent/worker.py`

简化为无工具循环的单次 LLM 调用器：

```python
class LightweightLLMCaller:
    def call(self, system_prompt: str, user_message: str,
             cancel_event: threading.Event | None = None) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        response = self.llm.call(messages, tools=None, cancel_event=cancel_event)
        return response.get("content", "")
```

移除：工具循环、stuck detector、context manager、artifact tracking、output monitor、tool validation。

### 3.2 BuiltinBackend 使用简化后的 LLMCaller

**文件**: `agent/backends/builtin.py`

从包装 AgentPool 闭包，改为使用 `LightweightLLMCaller`。

### 3.3 AgentPool 重构

**文件**: `agent/agent_pool.py`

`get_executor()` 返回 `WorkerAgent` 闭包 -> `get_backend_for_node()` 返回 `AgentBackend` 实例。

```python
def get_backend_for_node(self, node: DAGNode) -> AgentBackend:
    if node.agent_type in ("planner", "evaluator"):
        return self._builtin_backend  # 轻量 LLM 调用
    return self._backend_registry.get_backend(
        getattr(node, 'backend', self._default_backend)
    )
```

### Phase 3 验证

```bash
python -m pytest -v --tb=short
python -m pytest tests/test_agent_pool.py tests/test_dag_engine.py -v
```

---

## Phase 4: 清理（4-6 周）

### 4.1 tools/ 精简

移除执行路径工具注册（write、edit、bash），保留 glob/grep/read 供影响分析。

### 4.2 prompts.py 简化

保留 planner/evaluator 提示，移除 generator 提示。

### 4.3 评估 stale 模块

- `core/stuck_detector.py` -> BuiltinBackend 无工具循环后不再需要
- `core/context.py` -> 仅 AgentWorker 使用
- `core/output_monitor.py` -> 仅 AgentWorker 使用

### 4.4 文档更新

- `CONTEXT.md` -- 添加 Brain/Hands 术语
- `CLAUDE.md` -- 更新架构描述
- `docs/roadmap.md` -- 添加架构迁移里程碑

### Phase 4 验证

```bash
python -m pytest -v --tb=short
python main.py run "Fix the logging bug in auth module" --project .
```

---

## 风险缓解

| 风险 | 缓解措施 |
|------|---------|
| Claude Code SDK API 变更 | SDK fallback 到 CLI 已实现 |
| DAG 模板假设 AgentWorker 工具能力 | Phase 4 审查所有 templates/*.yaml |
| Memory 注入路径变更影响检索 | Phase 1 对比新旧 memory 注入结果 |
| 测试覆盖下降 | Phase 3 为 LightweightLLMCaller 写测试 |
| 回归风险 | 每个 Phase 独立 PR，完整 CI 跑过再合入 |
