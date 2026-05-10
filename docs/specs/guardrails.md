# Guardrails Module SPEC

## Purpose

Defense-in-depth permission system that controls which tools agents can execute. Provides a unified tri-state evaluation (`allowed`, `blocked`, `pending_approval`) for every tool call, with mode-specific routing. Supports automated approval flows via `ApprovalRepository` integration and a personal-mode variant for interactive CLI use.

Source: `guardrails/policy.py`

---

## Public Interfaces

### Dataclass: `GuardrailResult`

```python
@dataclass
class GuardrailResult:
    decision: Literal["allowed", "blocked", "pending_approval"]
    reason: str = ""
    ticket_id: str | None = None  # set when decision == "pending_approval"
```

**Properties:**
- `is_allowed -> bool`
- `is_blocked -> bool`
- `is_pending -> bool`

**Methods:**
- `__repr__() -> str` -- Includes `ticket_id` when present.
- `__iter__() -> iterator` -- Backward-compat: yields `(is_allowed, reason)` tuple.

---

### Class: `Guardrails`

```python
class Guardrails:
    def __init__(self, policy: GuardrailPolicy, tool_registry: ToolRegistry)
```

**Constructor fields:**
- `policy: GuardrailPolicy` -- Guardrail policy configuration.
- `tool_registry: ToolRegistry` -- Tool registry for executing allowed tools.
- `_pending_approvals: dict[str, str]` -- Internal tracking of pending approvals.

**Class constant:**

`RISK_MAP: dict[str, RiskLevel]` -- Maps tool names to risk levels:

| Tool | Risk Level |
|---|---|
| `read`, `glob`, `grep` | `LOW` |
| `write`, `edit`, `git` | `MEDIUM` |
| `bash` | `HIGH` |

Unregistered tools default to `HIGH`.

#### `evaluate(tool_name: str, arguments: dict) -> GuardrailResult`

Unified tri-state evaluation entry-point. Processing order:

1. **Deny-list check** (always enforced): If `tool_name` in `policy.denied_tools` --> `blocked`. If tool is `bash` and command contains a denied pattern --> `blocked`.
2. **Mode-based routing**: Dispatches to mode-specific evaluator based on `policy.mode`.

Mode behaviors:

| Mode | LOW | MEDIUM | HIGH | CRITICAL |
|---|---|---|---|---|
| `DONT_ASK` | allowed if in `allowed_tools`, else blocked | same | same | same |
| `PLAN` | allowed | blocked | blocked | blocked |
| `AUTO` | allowed | allowed if `write`/`edit` + `auto_approve_read`, else pending | pending | pending |
| `ACCEPT_EDITS` | allowed | allowed | pending | pending |
| `DEFAULT` | allowed | pending | pending | pending |

#### `check_and_execute(tool_name: str, arguments: dict, job_id: str = "", run_id: str | None = None, approval_repo: ApprovalRepository | None = None) -> ToolResult | GuardrailResult`

Unified execution entry-point. Evaluates the tool call, then:
- `allowed` --> executes via `tool_registry.execute()`, returns `ToolResult`.
- `pending_approval` + `approval_repo` + `job_id` --> creates an `ApprovalTicket`, attaches `ticket_id` to result, returns `GuardrailResult`.
- `blocked` or `pending_approval` without repo --> returns `GuardrailResult`.

#### `guarded_execute(tool_name: str, arguments: dict, *, job_id: str = "", run_id: str | None = None, approval_repo: ApprovalRepository | None = None) -> ToolResult`

**DEPRECATED** -- Use `check_and_execute`. Backward-compat wrapper that coerces `GuardrailResult` back into an error `ToolResult`.

#### `check_session_limits(iteration: int, errors: list) -> tuple[bool, str]`

Check if session has exceeded safety limits. Returns `(True, "Within limits")` if OK, `(False, reason)` if limits exceeded.

Limits:
- `iteration >= policy.max_iterations` --> `False`.
- `len(errors) >= 5` --> `False`.

#### `format_approval_request(tool_name: str, arguments: dict) -> str`

Format a human-readable approval request string for CLI display.

---

### Class: `PersonalGuardrails(Guardrails)`

```python
class PersonalGuardrails(Guardrails):
    def __init__(
        self,
        policy: PersonalGuardrailPolicy,
        tool_registry: ToolRegistry,
        non_interactive: bool = False,
        approval_repo: ApprovalRepository | None = None,
    )
```

**Additional constructor fields:**
- `personal_policy: PersonalGuardrailPolicy` -- Personal-mode policy (whitelist, auto-approve settings).
- `non_interactive: bool` -- If `True`, never blocks on stdin for confirmation.
- `approval_repo: ApprovalRepository | None` -- Optional approval repository.

#### `evaluate(tool_name: str, arguments: dict) -> GuardrailResult`

Overrides the base `evaluate` with personal-mode logic:

1. **Bash whitelist**: If tool is `bash` and command matches a whitelist pattern or prefix --> `allowed`.
2. **Tool whitelist**: If tool name in `personal_policy.whitelist_commands` --> `allowed`.
3. **Risk-level routing**:
   - `LOW` --> `allowed`
   - `MEDIUM` --> `allowed` (reversible ops)
   - `HIGH` --> `allowed` if `auto_approve_high`, else `pending_approval` (non-interactive: no stdin blocking)
   - `CRITICAL` --> `pending_approval`

#### `_is_whitelisted(command: str) -> bool`

Check command against `personal_policy.whitelist_patterns` (regex or prefix match) and `personal_policy.whitelist_commands` (prefix match). Invalid regex patterns fall back to prefix matching.

#### `request_confirmation(tool_name: str, arguments: dict) -> bool`

Interactive CLI confirmation via stdin. Returns `False` immediately in non-interactive mode. Uses `select.select` with `personal_policy.confirmation_timeout_sec` timeout. Returns `True` on `y`/`yes`/`ok`/empty input.

#### `guarded_execute_with_confirmation(tool_name: str, arguments: dict) -> ToolResult`

**DEPRECATED** -- Use `check_and_execute`. Falls back to `request_confirmation` for `pending_approval` results.

---

## Data Flow

```
Tool call request (tool_name, arguments)
       |
       v
Guardrails.evaluate(tool_name, arguments)
       |
       +---> RISK_MAP lookup --> RiskLevel
       +---> Deny-list check
       |         +---> denied_tools --> GuardrailResult("blocked")
       |         +---> bash denied_commands --> GuardrailResult("blocked")
       +---> Mode-based routing --> GuardrailResult
       |
       v
GuardrailResult("allowed" | "blocked" | "pending_approval")

Guardrails.check_and_execute(tool_name, arguments, ...)
       |
       +---> evaluate(tool_name, arguments) --> GuardrailResult
       |
       +---> "allowed"
       |       +---> tool_registry.execute(tool_name, arguments) --> ToolResult
       |
       +---> "pending_approval" + approval_repo + job_id
       |       +---> approval_repo.create_ticket(...) --> ApprovalTicket
       |       +---> GuardrailResult(pending_approval, ticket_id=...)
       |
       +---> "blocked" or "pending_approval" (no repo)
               +---> GuardrailResult returned as-is
```

**Personal mode flow:**
```
PersonalGuardrails.evaluate(tool_name, arguments)
       |
       +---> Bash whitelist check --> GuardrailResult("allowed")
       +---> Tool whitelist check --> GuardrailResult("allowed")
       +---> Risk-level routing
               LOW      --> "allowed"
               MEDIUM   --> "allowed"
               HIGH     --> "allowed" (auto_approve_high) or "pending_approval"
               CRITICAL --> "pending_approval"
```

---

## Error Codes

Guardrails do not raise exceptions. All outcomes are communicated through `GuardrailResult` or `ToolResult`.

| Decision | Reason Pattern |
|---|---|
| `blocked` | `"Tool '{name}' is explicitly denied"` |
| `blocked` | `"Command contains denied pattern: '{pattern}'"` |
| `blocked` | `"Tool '{name}' not in allowed list (dont_ask mode)"` |
| `blocked` | `"Tool '{name}' requires write access (plan mode is read-only)"` |
| `blocked` | `"Unknown risk for '{name}'"` |
| `pending_approval` | `"Medium risk '{name}' needs approval (default mode)"` |
| `pending_approval` | `"HIGH risk action '{name}' (auto mode)"` |
| `pending_approval` | `"HIGH risk action '{name}' requires confirmation (personal mode)"` |
| `pending_approval` | `"CRITICAL action '{name}' requires explicit confirmation"` |

When `guarded_execute` (deprecated) is used, `GuardrailResult` is coerced to:
- `ToolResult(success=False, error="Blocked by guardrails: {reason}")`
- `ToolResult(success=False, error="Blocked by guardrails: Pending approval required: {reason} (ticket: {id})")`

---

## Dependencies

| Dependency | Module | Usage |
|---|---|---|
| `GuardrailPolicy`, `PermissionMode`, `PersonalGuardrailPolicy`, `RiskLevel`, `ToolResult` | `core.models` | Policy configuration and result types. |
| `ToolRegistry` | `tools.registry` | Executing allowed tools. |
| `ApprovalRepository` | `control_plane.approval` | Creating approval tickets (optional, TYPE_CHECKING import). |
| `re` | stdlib | Whitelist pattern matching. |
| `select`, `sys` | stdlib | Interactive stdin confirmation. |
| `warnings` | stdlib | Deprecation warnings for legacy methods. |

---

## Configuration

Configuration is provided via `GuardrailPolicy` and `PersonalGuardrailPolicy` from `core.models`.

**GuardrailPolicy** (inferred from usage):
- `mode: PermissionMode` -- One of `DONT_ASK`, `PLAN`, `AUTO`, `ACCEPT_EDITS`, `DEFAULT`.
- `denied_tools: set[str]` -- Tools that are always blocked.
- `denied_commands: list[str]` -- Substring patterns denied for bash.
- `allowed_tools: set[str]` -- Pre-approved tools (DONT_ASK mode).
- `auto_approve_read: bool` -- Auto-approve read-classified medium tools in AUTO mode.
- `max_iterations: int` -- Session iteration limit.

**PersonalGuardrailPolicy** (inferred from usage):
- `whitelist_commands: set[str]` -- Tool names and command prefixes that bypass risk evaluation.
- `whitelist_patterns: list[str]` -- Regex patterns for bash command whitelisting.
- `auto_approve_high: bool` -- Auto-approve HIGH risk tools.
- `confirmation_timeout_sec: int` -- Timeout for interactive confirmation.

---

## Extension Points

1. **New permission modes**: Subclass `Guardrails` and add a new mode evaluator method, then add routing in `evaluate`.
2. **Custom risk mapping**: Override `RISK_MAP` class attribute to assign different risk levels to tools.
3. **MCP tool guardrails**: MCP tools that are not in `RISK_MAP` default to `HIGH` risk; register them explicitly to customize.
4. **Approval backend**: Pass an `ApprovalRepository` to `check_and_execute` for automated ticket creation and tracking.
5. **Custom deny patterns**: Extend `denied_commands` in the policy for domain-specific command restrictions.

---

## Invariants

1. Deny-list checks are always enforced regardless of permission mode -- they are evaluated first.
2. Tools not present in `RISK_MAP` default to `RiskLevel.HIGH`.
3. `evaluate()` is pure -- it never executes tools, only returns a decision.
4. `check_and_execute()` is the only method that both evaluates and executes; it returns `ToolResult` on `allowed` and `GuardrailResult` otherwise.
5. `PersonalGuardrails._is_whitelisted` handles invalid regex patterns gracefully by falling back to prefix matching.
6. `request_confirmation` in non-interactive mode returns `False` immediately without reading stdin.
7. `guarded_execute` and `guarded_execute_with_confirmation` emit `DeprecationWarning` on every call.
8. `check_session_limits` is independent of the tool evaluation flow -- it is called by the agent loop, not by `evaluate`.
