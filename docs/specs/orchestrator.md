# Orchestrator Module SPEC

## Purpose

LLM-driven orchestrator that generates execution DAGs from user requirements, adapts to node failures, and re-plans remaining work. The orchestrator is itself an agent -- it uses LLM reasoning for task decomposition and agent assignment rather than hardcoded state-machine rules.

Source: `orchestrator/intelligent_orchestrator.py`

Supporting modules:
- `orchestrator/llm_utils.py` -- Token/size management and JSON extraction utilities
- `orchestrator/plan_validator.py` -- DAG structural validation and auto-fix
- `orchestrator/prompts/` -- Prompt templates as markdown files:
  - `planning.md` -- Initial DAG generation prompt
  - `adaptation.md` -- Failure decision prompt
  - `replan.md` -- Replanning after partial execution prompt

---

## Public Interfaces

### Class: `IntelligentOrchestrator`

```python
class IntelligentOrchestrator:
    def __init__(
        self,
        llm_config: LLMConfig,
        session_store: SessionStore,
        agent_registry: AgentRegistry,
    )
```

**Constructor fields:**
- `llm_config: LLMConfig` -- LLM configuration for the orchestrator's own API calls.
- `session_store: SessionStore` -- Session event store for recording orchestrator decisions.
- `agent_registry: AgentRegistry` -- Registry of available agent types for planning.
- `llm: LLMClient` -- Internal LLM client instance (derived from `llm_config`).

#### `async plan(requirement: str, project_context: dict | None = None) -> DAG`

Generate an execution DAG from user requirements.

Steps:
1. Discover available agents via `AgentRegistry.to_prompt_description()`.
2. Build planning prompt from `PLANNING_PROMPT_TEMPLATE`.
3. Call LLM to produce a JSON plan.
4. Parse JSON via `_extract_json`, validate as `OrchestratorPlan`.
5. Validate every `agent_type` in the plan exists in the registry (raises `ValueError` otherwise).
6. Convert `OrchestratorPlan` to `DAG` via `_plan_to_dag`.

#### `async adapt_to_failure(dag: DAG, failed_node_id: str, error: str = "") -> FailureDecision`

Handle a failed node by asking the orchestrator LLM to decide the next action.

Returns one of: `retry`, `skip`, `abort`, `replan`.

On JSON parse failure: defaults to `retry` if `retry_count < max_retries`, otherwise `abort`.

#### `async replan(dag: DAG, failed_node_id: str, requirement: str = "") -> DAG`

Re-plan remaining work after a node failure. Collects summaries of already-executed nodes (status `success` or `failed`), builds a replanning prompt, calls the LLM, validates the new plan, and returns a new DAG containing only nodes that still need execution.

#### `_plan_to_dag(plan: OrchestratorPlan) -> DAG`

Converts an `OrchestratorPlan` model into an executable `DAG`. Each node definition becomes a `DAGNode`; each edge definition becomes a `DAGEdge` via `dag.add_edge`.

#### `_extract_json(text: str) -> dict`

Extract JSON from LLM response text (utility function from `orchestrator/llm_utils.py`). Three strategies:
1. Extract from `` ```json ... ``` `` code block.
2. Extract from generic `` ``` ... ``` `` code block.
3. Brace-matching scan for first top-level `{...}` object.

Raises `json.JSONDecodeError` if no valid JSON found.

---

## Prompt Templates (class constants)

| Constant | Purpose |
|---|---|
| `PLANNING_PROMPT_TEMPLATE` | Initial DAG generation. Receives `{agent_descriptions}`. |
| `ADAPTATION_PROMPT_TEMPLATE` | Failure decision. Receives `{node_id}`, `{agent_type}`, `{task}`, `{error}`, `{retry_count}`, `{dag_status}`. |
| `REPLAN_PROMPT_TEMPLATE` | Replanning after partial execution. Receives `{executed_nodes}`, `{failed_node}`, `{failed_error}`, `{agent_descriptions}`. |

---

## Data Flow

```
User requirement string
       |
       v
  plan(requirement)
       |
       +---> AgentRegistry.to_prompt_description()  --> agent list string
       +---> LLMClient.call(messages, tools=[])     --> raw LLM response
       +---> _extract_json(response)                 --> dict
       +---> OrchestratorPlan(**dict)                --> validated plan
       +---> _plan_to_dag(plan)                      --> DAG
       |
       v
  DAG (returned to caller)

  adapt_to_failure(dag, node_id, error)
       |
       +---> LLMClient.call(messages, tools=[])     --> raw LLM response
       +---> _extract_json(response)                 --> dict
       +---> FailureDecision(**dict)                 --> decision
       |
       v
  FailureDecision(action="retry"|"skip"|"abort"|"replan")

  replan(dag, failed_node_id, requirement)
       |
       +---> Collect executed node summaries from DAG
       +---> LLMClient.call(messages, tools=[])     --> raw LLM response
       +---> _extract_json + OrchestratorPlan        --> validated plan
       +---> _plan_to_dag(plan)                      --> new DAG
       |
       v
  DAG (new plan for remaining work)
```

---

## Error Codes

| Condition | Error Type | Detail |
|---|---|---|
| Plan references unregistered agent | `ValueError` | `"Plan references unregistered agent: {type}. Available: [...]"` |
| Replan references unregistered agent | `ValueError` | `"Replan references unregistered agent: {type}. Available: [...]"` |
| LLM response contains no valid JSON | `json.JSONDecodeError` | `"No valid JSON object found in LLM response"` |
| LLM API failure during planning/adaptation/replan | Propagated from `LLMClient` | Unhandled; caller must catch. |

---

## Dependencies

| Dependency | Module | Usage |
|---|---|---|
| `DAG`, `DAGNode`, `DAGEdge`, `FailureDecision`, `AgentCapability`, `OrchestratorPlan` | `core.models` | Data structures for plans and DAGs. |
| `AgentRegistry` | `core.agent_registry` | Agent discovery and validation. |
| `LLMConfig` | `core.config` | LLM configuration. |
| `LLMClient` | `core.llm_client` | Synchronous LLM API calls. |
| `SessionStore` | `session.store` | Event logging. |
| `llm_utils` | `orchestrator.llm_utils` | Token counting, JSON extraction, model context window management. |
| `plan_validator` | `orchestrator.plan_validator` | DAG structural validation and auto-fix. |

---

## Configuration

Configuration is provided at construction time via `LLMConfig`. No file-based configuration specific to the orchestrator.

---

## Extension Points

1. **Custom prompt templates**: Override `PLANNING_PROMPT_TEMPLATE`, `ADAPTATION_PROMPT_TEMPLATE`, or `REPLAN_PROMPT_TEMPLATE` class attributes to change planning behavior.
2. **New agent types**: Add agents to `AgentRegistry` (via `.weave/agents.yaml` or `_register_defaults()`); the orchestrator auto-discovers them through `to_prompt_description()`.
3. **Plan post-processing**: Subclass `IntelligentOrchestrator` and override `_plan_to_dag()` to inject custom DAG validation or transformation.

---

## Invariants

1. Every `agent_type` in a generated plan must exist in `AgentRegistry` at plan time (validated before DAG construction).
2. DAG returned by `plan()` or `replan()` is acyclic by construction (LLM is instructed; no cycle detection is applied programmatically).
3. `_extract_json` always returns the first valid JSON object found, never an array.
4. `adapt_to_failure` never raises on parse failure -- it falls back to `retry` or `abort` based on retry count.
5. The orchestrator never executes tasks itself; it only plans, adapts, and re-plans.
6. All LLM calls pass `tools=[]` -- the orchestrator has no tool access.
