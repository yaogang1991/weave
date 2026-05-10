# Agent Pool Module SPEC

## Purpose

Manages a pool of independent worker agent instances. Each worker receives isolated LLM context, a filtered tool set based on its agent type, and optional guardrails enforcement on every tool call. The pool creates agents on demand and provides a callable executor interface for the DAG engine.

Sources: `agent/agent_pool.py`, `agent/worker.py`

---

## Public Interfaces

### Class: `WorkerAgent`

```python
class WorkerAgent:
    def __init__(
        self,
        capability: AgentCapability,
        llm_config: LLMConfig,
        session_store: SessionStore,
        tool_registry: ToolRegistry,
        guardrails: Guardrails | None = None,
        max_iterations: int = 50,
        timeout: int = 120,
        max_context_tokens: int = 100_000,
    )
```

**Constructor fields:**
- `capability: AgentCapability` -- Agent type descriptor (id, name, description, system_prompt).
- `llm_config: LLMConfig` -- LLM configuration.
- `session_store: SessionStore` -- Session event store.
- `tool_registry: ToolRegistry` -- Registry of available tools.
- `guardrails: Guardrails | None` -- Optional guardrail enforcement layer.
- `max_iterations: int` -- Maximum agent loop iterations (default 50).
- `timeout: int` -- Wall-clock timeout in seconds for a single task execution (default 120).
- `system_prompt: str` -- Resolved system prompt (from capability or built-in defaults).
- `worker: AgentWorker` -- Internal `AgentWorker` instance.
- `tools: list[dict]` -- Filtered tool schemas based on agent type allowlist.

#### Class constants

| Constant | Type | Description |
|---|---|---|
| `SYSTEM_PROMPTS` | `dict[str, str]` | Built-in system prompts for `planner`, `generator`, `evaluator`. |
| `TOOL_ALLOWLIST` | `dict[str, set[str]]` | Tool names permitted per agent type. |

**Tool allowlist mapping:**
- `planner`: `{"read", "glob", "grep"}`
- `generator`: `{"read", "write", "edit", "bash", "glob", "grep", "git"}`
- `evaluator`: `{"read", "bash", "glob", "grep"}`

#### `async execute(task: str, input_artifacts: list[HandoffArtifact], session_id: str) -> dict[str, Any]`

Execute this agent's task with isolated context. Each call starts fresh -- previous executions do not pollute the context window.

**Parameters:**
- `task: str` -- Natural language task description.
- `input_artifacts: list[HandoffArtifact]` -- Artifacts from upstream DAG nodes.
- `session_id: str` -- Session identifier for event logging.

**Returns** `dict[str, Any]` with keys:
- `"status"`: `"completed"` or `"timeout"`
- `"summary"`: Final assistant message content or timeout message.
- `"artifacts"`: `list[str]` of file paths created/modified.
- `"output"`: Final assistant message content.

#### `_execute_tool(name: str, arguments: dict)`

Execute a tool through guardrails (if configured) or directly via `ToolRegistry.execute`.

#### `_format_artifacts(artifacts: list[HandoffArtifact]) -> str`

Format input artifacts as markdown context string for the agent prompt.

---

### Class: `AgentPool`

```python
class AgentPool:
    def __init__(
        self,
        llm_config: LLMConfig,
        session_store: SessionStore,
        agent_registry: AgentRegistry,
        tool_registry: ToolRegistry | None = None,
        guardrails: Guardrails | None = None,
        max_iterations: int = 50,
        timeout: int = 120,
        max_context_tokens: int = 100_000,
    )
```

**Constructor fields:**
- `llm_config`, `session_store`, `agent_registry`, `tool_registry`, `guardrails`, `max_iterations`, `timeout`, `max_context_tokens` -- Propagated to created `WorkerAgent` instances.
- `_instances: dict[str, WorkerAgent]` -- Cache of created worker instances keyed by agent type.

#### `get_or_create(agent_type: str) -> WorkerAgent`

Get an existing or create a new `WorkerAgent` for the given type. Looks up the agent's `AgentCapability` from the registry. Raises `ValueError` if the agent type is unknown.

#### `get_executor(session_id: str) -> Callable`

Return an async callable for the DAG engine with signature:
```python
async def executor(node: DAGNode, artifacts: list[HandoffArtifact]) -> dict
```
Internally calls `get_or_create(node.agent_type)` then `worker.execute(...)`.

#### `reset_context(agent_type: str) -> None`

Remove a cached worker instance (for context isolation between tasks).

#### `reset_all() -> None`

Remove all cached worker instances.

---

### Class: `AgentWorker`

```python
class AgentWorker:
    def __init__(
        self,
        config: LLMConfig,
        session_store: SessionStore,
        max_context_tokens: int = 100_000,
    )
```

**Constructor fields:**
- `config: LLMConfig` -- LLM configuration.
- `session_store: SessionStore` -- Session event store.
- `llm: LLMClient` -- Internal LLM client.
- `max_context_tokens: int` -- Token budget for context window management.
- `artifacts: list[str]` -- File paths tracked from write/edit tool calls.

#### `run(session_id: str, system_prompt: str, user_message: str, tools: list[dict], tool_executor, max_iterations: int = 50) -> Iterator[AgentMessage]`

Synchronous generator that runs the agent loop: call LLM, execute tool calls, feed results back. Yields `AgentMessage` for each assistant response. Stops when no tool calls remain or `max_iterations` is reached.

#### `_truncate_messages(messages: list[dict], max_tokens: int) -> list[dict]`

Truncate oldest messages when exceeding token budget. Keeps system prompt and the last 20 messages (~10 tool exchanges).

#### `_call_with_retry(messages: list[dict], tools: list[dict], max_retries: int = 3) -> dict`

Call LLM with exponential backoff (2^attempt seconds) for transient errors. Transient markers: `"rate"`, `"timeout"`, `"connection"`, `"overload"`, `"429"`, `"503"`, `"502"`.

#### `_track_artifact(tool_name: str, arguments: dict) -> None`

Track file paths from successful `write` or `edit` tool calls.

---

## Data Flow

```
DAGEngine calls pool.get_executor(session_id)
       |
       v
executor(node, artifacts)  [async callable]
       |
       +---> pool.get_or_create(node.agent_type)
       |         |
       |         +---> AgentRegistry.get(agent_type) --> AgentCapability
       |         +---> WorkerAgent(capability, ...)    --> cached instance
       |
       +---> worker.execute(task, artifacts, session_id)
               |
               +---> _format_artifacts(artifacts)      --> context string
               +---> _run_with_tools(prompt, session_id) [async, offloaded to thread]
                       |
                       +---> AgentWorker.run(...)       [sync Iterator[AgentMessage]]
                       |         |
                       |         for each iteration:
                       |           +---> _truncate_messages(...)
                       |           +---> _call_with_retry(messages, tools)  --> LLM response
                       |           +---> emit AGENT_MESSAGE event
                       |           +---> if tool_calls:
                       |           |       for each tool_call:
                       |           |         emit AGENT_TOOL_USE event
                       |           |         _execute_tool(name, args) --> ToolResult
                       |           |         emit AGENT_TOOL_RESULT event
                       |           |         _track_artifact(...)
                       |           +---> yield AgentMessage
                       |
                       +---> asyncio.wait_for(..., timeout) --> result dict
               |
               v
         {"status", "summary", "artifacts", "output"}
```

---

## Error Codes

| Condition | Error Type | Detail |
|---|---|---|
| Unknown agent type in `get_or_create` | `ValueError` | `"Unknown agent type: {agent_type}"` |
| Agent execution timeout | -- | Returns `{"status": "timeout", ...}` (no exception) |
| LLM API transient error (after max retries) | Propagated from `LLMClient` | The exception from the last LLM call attempt. |
| LLM API non-transient error | Propagated from `LLMClient` | Immediate propagation without retry. |

---

## Dependencies

| Dependency | Module | Usage |
|---|---|---|
| `AgentMessage`, `DAGNode`, `HandoffArtifact`, `AgentCapability` | `core.models` | Data structures. |
| `LLMConfig` | `core.config` | LLM configuration. |
| `AgentRegistry` | `core.agent_registry` | Agent type discovery. |
| `SessionStore` | `session.store` | Event logging. |
| `AgentWorker` | `agent.worker` | Low-level agent loop. |
| `ToolRegistry` | `tools.registry` | Tool execution. |
| `Guardrails` | `guardrails.policy` | Permission enforcement. |

---

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `max_iterations` | 50 | Maximum tool-call iterations per agent task. |
| `timeout` | 120 | Wall-clock seconds per task execution. |
| `max_context_tokens` | 100_000 | Token budget for context window truncation. |

All parameters are set at `AgentPool` construction and propagated to each `WorkerAgent`.

---

## Extension Points

1. **New agent types**: Register in `AgentRegistry`, add a `SYSTEM_PROMPTS` entry and `TOOL_ALLOWLIST` entry in `WorkerAgent` for custom prompts and tool sets.
2. **Custom tool filtering**: Subclass `WorkerAgent` and override `__init__` to change tool filtering logic.
3. **Alternative execution strategies**: Replace `_run_with_tools` in a subclass to change how the agent loop is dispatched (e.g., streaming, event-driven).

---

## Invariants

1. Each `WorkerAgent.execute()` call starts with a fresh context -- no state leaks between executions.
2. Tool calls always go through `_execute_tool`, which routes through guardrails when configured.
3. `AgentWorker.run()` is synchronous; `WorkerAgent._run_with_tools()` wraps it via `asyncio.to_thread` for async compatibility.
4. `artifacts` list in `AgentWorker` is reset at the start of each `run()` call.
5. `AgentPool._instances` caches workers by agent type -- calling `get_or_create` twice with the same type returns the same instance until `reset_context` is called.
6. Timeout is enforced at the `WorkerAgent` level via `asyncio.wait_for`, not inside `AgentWorker.run()`.
