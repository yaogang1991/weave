# ADR 0011: YAML DAG Templates with Variable Substitution

**Status:** Accepted
**Date:** 2026-05-10
**Deciders:** Project Lead

## Context

Every task currently requires LLM-based planning via `IntelligentOrchestrator.plan()`, which consumes tokens and adds 5-15 seconds of latency. Many tasks follow recurring patterns (build API, fix bug, add tests) that share the same DAG structure with different parameters.

## Decision

We use **YAML template files** with `{variable}` placeholders, loaded and instantiated by `TemplateRegistry`.

- Templates are `.yaml`/`.yml` files in `templates/` directory
- Variable placeholders use `{var_name}` syntax, resolved via regex substitution
- `instantiate()` merges template defaults with user-provided variables
- If a `--template` flag is provided, `plan_from_template()` is called instead of LLM planning

```yaml
name: build_api
variables:
  feature: "Feature"
nodes:
  - id: "impl_{feature}"
    agent_type: "generator"
    task_description: "Implement {feature}"
```

```bash
python main.py run "Build Todo API" --template build_api --var feature=Todo
```

## Consequences

**Positive:**
- Zero-latency planning — no LLM call needed for template-based tasks
- Zero token cost — template instantiation is pure string substitution
- Reproducible — same template + variables always produces the same DAG
- User-extensible — drop a YAML file in `templates/` to add new patterns

**Negative:**
- No dynamic adaptation — templates cannot adjust based on codebase state
- Variable substitution is flat — no conditionals or loops in templates
- Missing variables produce warnings but don't fail (graceful degradation)

## Alternatives Considered

- **LLM-based template selection**: Let the LLM choose a template. Rejected — defeats the purpose of skipping the LLM call.
- **Python DSL for templates**: Full programming language for template logic. Rejected — YAML is sufficient for the current use case and much simpler to write/maintain.
- **JSON Schema templates**: More structured but harder to hand-edit. Rejected — YAML is more human-friendly for this use case.
