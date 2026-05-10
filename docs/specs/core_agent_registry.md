# SPEC: core/agent_registry.py

## Purpose

Capability-based agent discovery registry. The orchestrator queries this registry to discover available worker agents rather than hardcoding agent types. Ships with three default agents (planner, generator, evaluator) and supports extension via YAML config files or programmatic registration.

## Public Interfaces

### `AgentRegistry`

```python
class AgentRegistry:
    def __init__(self) -> None
    def register(self, capability: AgentCapability) -> None
    def register_factory(self, agent_id: str, factory: Callable) -> None
    def get(self, agent_id: str) -> AgentCapability | None
    def list_agents(self) -> list[AgentCapability]
    def has_agent(self, agent_id: str) -> bool
    def get_factory(self, agent_id: str) -> Callable | None
    def unregister(self, agent_id: str) -> None
    def load_from_yaml(self, path: str | Path) -> None
    def load_from_directory(self, dir_path: str | Path) -> None
    def to_prompt_description(self) -> str
    def __repr__(self) -> str
```

#### Methods Detail

| Method | Signature | Returns | Raises | Description |
|--------|-----------|---------|--------|-------------|
| `__init__` | `() -> None` | - | - | Creates empty registry, calls `_register_defaults()` |
| `register` | `(capability: AgentCapability) -> None` | - | - | Registers or overwrites an agent capability keyed by `capability.id` |
| `register_factory` | `(agent_id: str, factory: Callable) -> None` | - | - | Registers a factory function: `factory(task_description, artifacts) -> AgentInstance` |
| `get` | `(agent_id: str) -> AgentCapability | None` | Capability or `None` | - | Look up agent by ID |
| `list_agents` | `() -> list[AgentCapability]` | List of all agents | - | Returns all registered capabilities |
| `has_agent` | `(agent_id: str) -> bool` | Boolean | - | Check existence |
| `get_factory` | `(agent_id: str) -> Callable | None` | Factory or `None` | - | Retrieve factory for agent instantiation |
| `unregister` | `(agent_id: str) -> None` | - | `ValueError` for protected agents | Removes agent and its factory. Protected: `planner`, `generator`, `evaluator` |
| `load_from_yaml` | `(path: str | Path) -> None` | - | `FileNotFoundError` if missing | Loads agents from YAML file with `agents:` list |
| `load_from_directory` | `(dir_path: str | Path) -> None` | - | - | Loads all `*.yaml` files from directory. No-op if directory missing |
| `to_prompt_description` | `() -> str` | Formatted string | - | Generates prompt-injectable description of all agents |
| `__repr__` | `() -> str` | String | - | `AgentRegistry(agents=[...])` |

### Default Agents

Registered automatically at `__init__` time:

| ID | Name | Skills | Input Schema | Output Schema |
|----|------|--------|-------------|---------------|
| `planner` | Planner | `requirement_analysis`, `task_decomposition`, `architecture_design`, `technical_decision`, `interface_definition`, `planning` | `user_requirements`, `project_context` | `plan`, `specification`, `architecture_doc`, `sprint_contract` |
| `generator` | Generator | `code_writing`, `file_editing`, `test_writing`, `debugging`, `git_operations`, `implementation` | `plan`, `existing_code`, `feedback` | `code`, `tests`, `git_commit`, `implementation_artifact` |
| `evaluator` | Evaluator | `test_execution`, `quality_assessment`, `code_review`, `performance_analysis`, `security_scan`, `evaluation` | `code`, `tests`, `sprint_contract`, `implementation_artifact` | `evaluation_report`, `pass_fail_verdict`, `feedback`, `score` |

## Data Flow

```
Initialization:
  AgentRegistry() -> _register_defaults() -> planner/generator/evaluator registered

Extension:
  YAML file (agents.yaml) -> load_from_yaml() -> AgentCapability(**agent_def) -> register()
  OR
  Code -> register(AgentCapability(...)) / register_factory(id, factory)

Discovery:
  Orchestrator -> list_agents() / to_prompt_description() -> injects into LLM prompt
  Orchestrator -> get(agent_id) -> retrieves capability for DAG node creation

Execution:
  DAG engine -> AgentPool.get_or_create(agent_type) -> WorkerAgent(capability) -> agent instance
```

## Error Codes

No numeric error codes. Exceptions:
- `ValueError(f"Cannot unregister protected agent: {agent_id}")` -- Raised by `unregister()` for `planner`, `generator`, or `evaluator`.
- `FileNotFoundError(f"Agent config not found: {path}")` -- Raised by `load_from_yaml()` when file does not exist.

## Dependencies

- `core.models.AgentCapability` -- Data model for capabilities.
- `pyyaml` (`yaml.safe_load`) -- For YAML config loading.
- Python stdlib: `json`, `pathlib.Path`, `typing.Callable`

## Configuration

### YAML Format

```yaml
agents:
  - id: custom_agent
    name: Custom Agent
    description: Description of what this agent does
    skills:
      - skill_one
      - skill_two
    input_schema:
      - input_type
    output_schema:
      - output_type
    constraints:
      - constraint description
    system_prompt: "Optional custom system prompt"
```

### File Locations

- Default agents: hardcoded in `_register_defaults()`.
- Project-specific agents: `.harness/agents.yaml` (loaded by orchestrator at startup).

## Extension Points

1. **Custom agents via YAML**: Place YAML files in `.harness/` directory or load via `load_from_yaml()` / `load_from_directory()`.
2. **Custom agents via code**: Call `register(AgentCapability(...))` and optionally `register_factory(agent_id, factory)`.
3. **Factory functions**: Register a `Callable` via `register_factory()` for custom agent instantiation logic.

## Invariants

1. The three default agents (`planner`, `generator`, `evaluator`) are always present after `__init__` and cannot be removed via `unregister()`.
2. `_agents` is keyed by `AgentCapability.id` -- registering with a duplicate ID overwrites the previous entry.
3. `_factories` is keyed by `agent_id` -- independent from `_agents`; a factory may not exist for every registered agent.
4. `load_from_directory()` silently skips non-existent directories.
5. `to_prompt_description()` includes all registered agents including defaults.
6. `get()` returns `None` for unknown agent IDs (no exception).
