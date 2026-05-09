# Module SPEC Template

---
**Module:** `path/to/module.py`
**Last Updated:** YYYY-MM-DD
**Status:** [IMPLEMENTED | PARTIAL | PLANNED]
---

## Purpose

One paragraph describing what this module does and why it exists.

## Public Interfaces

### Classes

```python
class ClassName(BaseModel):
    """Description."""
    field: type  # description
```

### Functions

```python
def function_name(param: type) -> return_type:
    """Description."""
```

## Data Flow

```
Input → Processing → Output
  │         │           │
  │         └─ what happens inside
  └─ where input comes from
              └─ where output goes
```

## Error Codes / Exceptions

| Error | Condition | Handling |
|-------|-----------|----------|
| `Exxxx` | When | What happens |

## Dependencies

### Imports From
- `module.path` — what is used

### Imported By
- `module.path` — how it's used

## Configuration

| Env Var | Config Key | Default | Description |
|---------|-----------|---------|-------------|
| `ENV_VAR` | `config.field` | `default` | Description |

## Extension Points

- How to extend this module

## Invariants

- Rules that must always hold true
