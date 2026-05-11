# Developer Guide

How to extend, debug, and maintain the Harness.

---

## Adding a New Agent Type

### 1. Register the agent

Edit `core/agent_registry.py` in `_register_defaults()` (for built-in) or add to `.harness/agents.yaml` (for project-specific):

```python
# Built-in agent (core/agent_registry.py)
self.register(AgentCapability(
    id="security_auditor",
    name="Security Auditor",
    description="Reviews code for security vulnerabilities",
    skills=["security_audit", "dependency_scan", "owasp_top10"],
    constraints=["Only reads code, does not modify"],
    input_schema={"type": "object", "properties": {"code_path": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"findings": {"type": "array"}}},
))
```

```yaml
# Project-specific (.harness/agents.yaml)
agents:
  - id: security_auditor
    name: Security Auditor
    skills: [security_audit, dependency_scan]
    constraints: [Read-only access]
```

### 2. Add system prompt

Edit `agent/agent_pool.py` — add entry to `SYSTEM_PROMPTS` dict:

```python
SYSTEM_PROMPTS = {
    # ... existing prompts ...
    "security_auditor": (
        "You are a Security Auditor agent. Your job is to review code for "
        "security vulnerabilities following OWASP guidelines. "
        "You have read-only access to the codebase.\n"
        "Always output findings as a structured list with severity levels."
    ),
}
```

### 3. Update orchestrator prompt

The orchestrator automatically discovers agents via `AgentRegistry.to_prompt_description()`, but you may want to add planning rules in `orchestrator/intelligent_orchestrator.py`:

```python
# In the planning prompt template, add guidance:
"- For security review tasks, assign 'security_auditor' agent"
```

---

## Adding a New Tool

### 1. Implement the tool function

Edit `tools/registry.py`. Tool functions are called with `**kwargs` (each argument from the tool call) and should return a string or `ToolResult`:

```python
def _tool_search_code(pattern: str, path: str = ".") -> str:
    """Search code using regex patterns."""
    try:
        # ... implementation ...
        return result_text
    except Exception as e:
        return f"Error: {e}"
```

### 2. Register in ToolRegistry

```python
self.register("search_code", _tool_search_code, schema={
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Regex pattern"},
        "path": {"type": "string", "description": "Directory to search"},
    },
    "required": ["pattern"],
})
```

### 3. Add risk classification

Edit `guardrails/policy.py` — add to `RISK_MAP`:

```python
RISK_MAP = {
    # ... existing entries ...
    "search_code": RiskLevel.LOW,  # Read-only operation
}
```

---

## Adding a New Execution Backend

### 1. Implement the backend

Create a new file in `backend/`, extending `ExecutionBackend`:

```python
# backend/my_backend.py
from backend.base import ExecutionBackend

class MyBackend(ExecutionBackend):
    def setup(self, job_id: str, run_id: str) -> Path:
        """Set up the execution environment. Returns work directory path."""
        ...

    def get_work_dir(self, job_id: str, run_id: str) -> Path:
        """Return the current working directory for agent tools."""
        ...

    def cleanup(self, job_id: str, run_id: str) -> None:
        """Clean up after successful execution."""
        ...

    def preserve(self, job_id: str, run_id: str, reason: str = "") -> Path | None:
        """Preserve the environment for debugging. Returns archive path."""
        ...
```

### 2. Register in BackendManager

Edit `backend/lifecycle.py` — add your backend to `_get_workspace_backend()`:

```python
from backend.my_backend import MyBackend

def _get_workspace_backend(self, ws_type: WorkspaceIsolation) -> ExecutionBackend:
    if ws_type == WorkspaceIsolation.WORKTREE:
        ...
    elif ws_type == WorkspaceIsolation.LOCAL:
        ...
    return backend
```

### 3. Configure

Add a new `WorkspaceIsolation` enum value or map risk levels in `workspace_by_risk`. Note: `HARNESS_DEFAULT_BACKEND` only accepts `local` or `worktree` (the built-in `WorkspaceIsolation` values).

---

## Debugging a Failed DAG Execution

### 1. Check job status

```bash
python main.py status <job_id>
```

Look for `status`, `last_error`, and `runs[].status` / `runs[].session_id` fields.

### 2. Read the event log

Use `session_id` from the status output (under `runs[].session_id`):

```bash
cat ./data/events/<session_id>.jsonl | python -m json.tool
```

Key event types to look for:
- `started` / `completed` / `failed` — Individual node outcomes
- `heartbeat_missed` / `unhealthy_killed` — Health issues
- `health_alert` — Watchdog alerts

### 3. Check node error details

Failed nodes store error info in the event log. Search for failed events:

```bash
grep '"failed"' ./data/events/<session_id>.jsonl | python -m json.tool
```

### 4. Inspect dead-letter jobs

```bash
python main.py list --status dead_letter
```

Dead-letter jobs are stored in `./data/jobs/` with status `dead_letter`. To inspect:

```bash
cat ./data/jobs/<job_id>.json | python -m json.tool
```

Dead letter files contain: original requirement, failure history, last error, attempt count.

### 5. Check approval tickets

```bash
python main.py tickets --status pending
```

Pending tickets may indicate the job is blocked waiting for approval.

---

## Common Patterns

### Atomic File Writes

Follow the pattern from `control_plane/repository.py`:

```python
import os
import json
from pathlib import Path

def atomic_write(path: str, data: dict) -> None:
    """Write JSON atomically via temp file + rename."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)
```

### Event Emission

Follow the pattern from `session/store.py`:

```python
event = {
    "timestamp": datetime.utcnow().isoformat(),
    "event_type": "domain.action",
    "data": {...}
}
store.append(event)
```

### Status Transitions

Status transitions are strict — illegal moves raise `ValueError`. Always use the defined transitions in `control_plane/models.py`.

---

## Project Structure Conventions

- **Data models**: All in `core/models.py` (single source of truth)
- **Event naming**: `{domain}.{action}` (e.g., `workflow.stage_start`, `node.heartbeat`)
- **Type annotations**: Python 3.10+ syntax (`str | None`, `list[dict]`)
- **Docstrings**: English, Google-style
- **User-facing docs**: Chinese (README, ARCHITECTURE)
- **Error handling**: Tools return `ToolResult`, never raise exceptions
- **No circular imports**: Layer by responsibility (`core/` → `agent/` → `orchestrator/` → `tools/`)

---

## Configuring Multi-Model Routing (M3.1)

### How it works

Different agent types can use different LLM models. The `ModelRoutingConfig` maps agent types to model assignments, with a fallback chain for resilience.

### Via environment variables

```bash
# Use a stronger model for planning
export HARNESS_PLANNER_MODEL="claude-opus-4-6"

# Use OpenAI for generation
export HARNESS_GENERATOR_MODEL="gpt-4"

# Fallback chain if primary model is unavailable
export HARNESS_MODEL_FALLBACK="claude-sonnet-4-6,gpt-4"
```

### Via config.yaml

```yaml
model_routing:
  routing:
    planner:
      provider: anthropic
      model: claude-opus-4-6
    generator:
      provider: openai
      model: gpt-4
      temperature: 0.5
  fallback_chain:
    - claude-sonnet-4-6
```

### Supported agent type keys

`planner`, `generator`, `evaluator`, `orchestrator` — plus any project-specific agent IDs registered in `.harness/agents.yaml`.

---

## Using the Memory System (M3.2)

### Memory scopes

```
PRIVATE  → per-agent, not shared (e.g., generator's coding notes)
SESSION  → shared within one execution session (across agents)
GLOBAL   → cross-session persistent (e.g., learned project conventions)
```

### API: store memories

```python
from memory.manager import MemoryManager
from core.models import MemoryType, MemoryScope

manager = MemoryManager(config.memory)

# Store a fact
entry = manager.store_learning(
    agent_type="generator",
    content="Project uses Flask with blueprints pattern",
    memory_type=MemoryType.FACT,
    scope=MemoryScope.GLOBAL,
)

# Store a task outcome (auto-called by agent_pool after execution)
manager.store_task_outcome(
    agent_type="generator",
    task_description="Implement user auth API",
    result_summary="All 12 tests passing",
    success=True,
    session_id="session_abc",
    node_id="node_1",
)
```

### API: retrieve memories

```python
# Get relevant memories for an agent
entries = manager.get_context_for_agent(
    agent_type="generator",
    task_description="Add rate limiting to the API",
)

# Format for prompt injection
prompt_section = manager.format_memory_prompt(entries)
# Output:
# ## Relevant Memory
# - [FACT] Project uses Flask with blueprints pattern
# - [EXPERIENCE] Task 'Implement user auth API' succeeded. All 12 tests passing
```

### API: memory sharing

```python
from memory.sharing import MemorySharing

sharing = MemorySharing(manager)

# Share upstream agent's memories with downstream agent
shared = sharing.share_with_downstream(
    from_agent="planner",
    to_agent="generator",
    session_id="session_abc",
    dag=dag,
    node_id="node_2",
)

# Promote a session memory to global
sharing.promote_to_global(memory_id="mem_abc123")
```

### Maintenance

Memory maintenance runs automatically before each execution via `control_plane/service.py`. You can also run it manually:

```bash
python main.py memory-cleanup
```

This performs: expired entry cleanup → capacity enforcement → relevance recomputation.

---

## Creating DAG Templates (M3.4)

### Template structure

Templates are YAML files in `templates/` with variable placeholders `{var_name}`:

```yaml
# templates/my_feature.yaml
name: my_feature
description: "Add a new feature with implementation and testing"
version: "1.0"
category: development
variables:
  module: "app"
  feature: "Feature"

nodes:
  - id: "impl_{module}_{feature}"
    agent_type: "generator"
    task_description: "Implement {feature} in {module} module"
  - id: "test_{module}_{feature}"
    agent_type: "generator"
    task_description: "Write tests for {feature} in {module}"

edges:
  - from: "impl_{module}_{feature}"
    to: "test_{module}_{feature}"

reasoning_template: "Implement {feature} then test it"
```

### Using templates

```bash
# List available templates
python main.py templates

# Plan from template
python main.py plan "Add search" --template my_feature --var module=user --var feature=Search

# Run from template
python main.py run "Add search" --template my_feature --var module=user --var feature=Search
```

### Built-in templates

| Template | Nodes | Description |
|----------|-------|-------------|
| `build_api` | 4 | Build a REST API |
| `add_feature` | 3 | Add a new feature |
| `fix_bug` | 3 | Analyze and fix a bug |
| `refactor` | 4 | Refactor code |
| `add_tests` | 2 | Add test coverage |
| `add_auth` | 4 | Add authentication |
| `setup_project` | 3 | Project scaffolding |

---

## Extending Impact Analysis (M3.5)

### How it works

1. `DependencyGraph` — scans Python imports via `ast`, builds bidirectional file dependency graph
2. `ImpactPredictor` — keyword matches requirement against file names, expands with dependencies
3. `ChangeVerifier` — compares pre/post execution file snapshots, validates coverage

### Customize the dependency graph

The graph only handles Python files by default. To extend for other languages:

```python
from analysis.dependency_graph import DependencyGraph

class TypeScriptDependencyGraph(DependencyGraph):
    def _find_source_files(self) -> list[str]:
        """Override to scan .ts/.tsx files."""
        results = []
        for path in self.project_path.rglob("*.ts"):
            if "node_modules" in path.parts:
                continue
            results.append(str(path.relative_to(self.project_path)))
        return sorted(results)

    def _parse_imports(self, abs_path: Path) -> list[str]:
        """Override for TypeScript import syntax."""
        # Parse: import { X } from './module'
        source = abs_path.read_text(encoding="utf-8", errors="replace")
        import re
        return re.findall(r"from\s+['\"](.+?)['\"]", source)
```

### Access analysis results

```bash
# Predict impact before execution
python main.py impact-predict "Refactor the DAG engine" --project .

# View dependency graph
python main.py impact-graph --project .

# Review past predictions
python main.py impact-history
```

---

## Creating an Execution Hook

Execution hooks are lifecycle callbacks that run before/after DAG execution, decoupling subsystems from the core execution flow in `RunService`.

### 1. Implement the hook

Create a class extending `ExecutionHook` in `control_plane/hooks.py`:

```python
from control_plane.hooks import ExecutionHook, ExecutionContext

class MyCustomHook(ExecutionHook):
    def __init__(self, my_dependency: Any = None) -> None:
        self._my_dep = my_dependency

    async def before_execution(self, ctx: ExecutionContext) -> None:
        # Read ctx.job, ctx.work_dir, etc.
        # Write to ctx.metadata (persisted to job) or ctx._state (internal)
        ctx.metadata["my_custom_data"] = "value"

    async def after_execution(self, ctx: ExecutionContext, result_dag: Any) -> None:
        # Read ctx._state from before_execution
        # Access ctx.memory_manager (set by MemoryHook)
        pass
```

### 2. Register the hook

Edit `control_plane/service.py` in `_register_hooks()`:

```python
def _register_hooks(self) -> None:
    self._hooks = [
        MemoryHook(),                            # Must be first
        LearningHook(repository=self.repository),
        ImpactHook(llm_config=self.llm_config),
        MyCustomHook(my_dependency=self.some_dep),  # Add yours
    ]
```

### 3. Key invariants

- **Ordering matters**: Hooks run in registration order. `MemoryHook` must run first to set `ctx.memory_manager`.
- **Errors are swallowed**: Hook errors are logged, never raised. Core execution always proceeds.
- **Metadata persistence**: `ctx.metadata` is persisted to `job.metadata` after both before_hooks and after_hooks.
- **State isolation**: `ctx._state` is per-job, not shared across concurrent executions.
- **Dependency injection**: Pass external dependencies via constructor, not module-level globals.
