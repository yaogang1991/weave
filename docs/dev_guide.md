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

Edit `tools/registry.py`. Tool functions receive `(params: dict)` and return `ToolResult`:

```python
def _tool_search_code(params: dict) -> ToolResult:
    """Search code using regex patterns."""
    pattern = params.get("pattern", "")
    path = params.get("path", ".")
    try:
        # ... implementation ...
        return ToolResult(success=True, output=result_text)
    except Exception as e:
        return ToolResult(success=False, error=str(e))
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
    async def setup(self, job_id: str, run_id: str, config: dict) -> str:
        """Set up the execution environment. Returns work directory path."""
        ...

    def get_work_dir(self) -> str:
        """Return the current working directory for agent tools."""
        ...

    async def cleanup(self) -> None:
        """Clean up after successful execution."""
        ...

    async def preserve(self) -> str:
        """Preserve the environment for debugging. Returns archive path."""
        ...

    def is_available(self) -> bool:
        """Check if this backend can be used."""
        ...
```

### 2. Register in BackendManager

Edit `backend/lifecycle.py` — add to the backend registry:

```python
from backend.my_backend import MyBackend

class BackendManager:
    BACKEND_TYPES = {
        "local": LocalBackend,
        "worktree": WorktreeBackend,
        "my_backend": MyBackend,  # Add here
    }
```

### 3. Configure

Set `HARNESS_DEFAULT_BACKEND=my_backend` or add to risk mapping.

---

## Debugging a Failed DAG Execution

### 1. Check job status

```bash
python main.py status <job_id>
```

Look for `status`, `run_status`, and `progress` fields.

### 2. Read the event log

```bash
cat ./data/events/<job_id>.jsonl | python -m json.tool
```

Key event types to look for:
- `workflow.stage_start` / `workflow.stage_end` — DAG level transitions
- `node.started` / `node.completed` / `node.failed` — Individual node outcomes
- `node.heartbeat` / `node.unhealthy_killed` — Health issues
- `workflow.health_alert` — Watchdog alerts

### 3. Check node error details

Failed nodes store error info in the DAG execution result. Use `--detail` flag:

```bash
python main.py status <job_id> --detail
```

### 4. Inspect dead-letter jobs

```bash
python main.py list --status dead_letter
cat ./data/queue/dead/<job_id>.json | python -m json.tool
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
