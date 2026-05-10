# Module SPEC: DAG Templates

---
**Module:** `templates/library.py`
**Last Updated:** 2026-05-10
**Status:** IMPLEMENTED
---

## Purpose

Reusable YAML-based DAG templates with variable substitution. Allows common task patterns (build API, fix bug, refactor) to skip LLM planning entirely, reducing latency and token cost. Templates define node structures, edges, and variable placeholders that get resolved at instantiation time.

## Public Interfaces

### TemplateRegistry (`templates/library.py`)

```python
class TemplateRegistry:
    """Discover, load, and instantiate DAG templates."""

    def __init__(self, templates_dir: str | Path | None = None) -> None

    def list_templates(self) -> list[DAGTemplate]
    def get_template(self, name: str) -> DAGTemplate | None

    def instantiate(
        self,
        template_name: str,
        variables: dict[str, str] | None = None,
    ) -> DAG

    def validate_substitution(
        self,
        template_name: str,
        dag: DAG,
        variables: dict[str, str] | None = None,
    ) -> list[str]
```

## Data Model (`core/models.py`)

```python
class DAGTemplate(BaseModel):
    name: str
    description: str = ""
    version: str = "1.0"
    category: str = "general"
    variables: dict[str, str] = {}       # name → default_value
    nodes: list[dict[str, Any]]           # List of node definitions
    edges: list[dict[str, Any]]           # List of {from, to} dicts
    reasoning_template: str = ""
```

## Template YAML Format

```yaml
name: build_api
description: "Build a REST API with implementation and testing"
version: "1.0"
category: backend
variables:
  feature: "Feature"
  module: "app"

nodes:
  - id: "plan_{feature}"
    agent_type: "planner"
    task_description: "Design API schema and endpoints for {feature}"
  - id: "impl_{feature}"
    agent_type: "generator"
    task_description: "Implement {feature} REST API in {module}"
  - id: "test_{feature}"
    agent_type: "generator"
    task_description: "Write integration tests for {feature} API"
  - id: "eval_{feature}"
    agent_type: "evaluator"
    task_description: "Evaluate {feature} API: test results, code quality"

edges:
  - from: "plan_{feature}"
    to: "impl_{feature}"
  - from: "impl_{feature}"
    to: "test_{feature}"
  - from: "test_{feature}"
    to: "eval_{feature}"

reasoning_template: "Build {feature} API: plan → implement → test → evaluate"
```

## Data Flow

```
用户调用:
  python main.py run "Build API" --template build_api --var feature=Todo --var language=Python

内部流程:
  main.py cmd_run()
    ├─ 解析 --template 和 --var 参数
    └─ orchestrator.plan_from_template(template_name, variables)
          ├─ TemplateRegistry.get_template("build_api")
          │     └─ _load_yaml() → DAGTemplate 模型
          ├─ TemplateRegistry.instantiate("build_api", {"feature": "Todo", "language": "Python"})
          │     ├─ 合并默认变量和用户变量
          │     ├─ _substitute(): 替换所有 {var} 占位符
          │     ├─ 构建 DAGNode 列表
          │     └─ 构建 DAGEdge 列表 → 返回 DAG
          └─ validate_substitution() → 检查残留占位符
```

## Variable Substitution

- Pattern: `{var_name}` — matches `\{(\w+)\}`
- Unresolved placeholders (missing variable, no default) are kept as-is and logged as warnings
- No recursive substitution (variables in variable values are not expanded)
- Variables are merged: template defaults ← user-provided values

## Built-in Templates

| Template | Nodes | Edges | Description |
|----------|-------|-------|-------------|
| `build_api` | 4 | 3 | REST API: plan → implement → test → evaluate |
| `add_feature` | 3 | 2 | Feature: implement → test → evaluate |
| `fix_bug` | 3 | 2 | Bug fix: analyze → fix → verify |
| `refactor` | 4 | 3 | Refactor: analyze → refactor → test → evaluate |
| `add_tests` | 2 | 1 | Tests: write → verify |
| `add_auth` | 4 | 3 | Auth: plan → implement → test → evaluate |
| `setup_project` | 3 | 2 | Scaffold: initialize → configure → verify |

## Error Handling

| Error | Condition | Handling |
|-------|-----------|----------|
| `ValueError` | Template not found | Raised from `instantiate` |
| `ValueError` | Edge has empty from/to | Raised from `instantiate` |
| `ValueError` | YAML is not a mapping | Raised from `_load_yaml` |
| `ValueError` | Missing name or nodes | Raised from `_load_yaml` |
| Warning | Malformed YAML file | Logged, file skipped |
| Warning | Unresolved `{var}` | Logged via `validate_substitution` |

## Dependencies

### Imports From
- `core/models.py` — DAG, DAGNode, DAGEdge, DAGTemplate

### Imported By
- `orchestrator/intelligent_orchestrator.py` — `plan_from_template` method
- `main.py` — `cmd_templates`, template CLI flags
- `visualizer/server.py` — Template REST API endpoints

## Invariants

- Templates are cached in-memory after first load
- All template YAML files use `yaml.safe_load` (no arbitrary code execution)
- Node IDs within a template must be unique
- Edge references must point to valid node IDs after substitution
