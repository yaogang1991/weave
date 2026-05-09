# Backend Module SPEC

## Purpose

Provides the execution backend abstraction layer. Each backend type (local, worktree, docker) implements a common lifecycle interface for preparing an execution environment, returning a working directory, and cleaning up or preserving the workspace after execution. The `BackendManager` selects backends based on configuration or risk level and manages their lifecycle.

Sources: `backend/base.py`, `backend/local.py`, `backend/worktree.py`, `backend/docker_stub.py`, `backend/lifecycle.py`

---

## Public Interfaces

### base.py

#### Enum: `BackendType(str, Enum)`

| Value | Description |
|---|---|
| `LOCAL` | Execute directly in the main repo directory. |
| `WORKTREE` | Execute in an isolated git worktree. |
| `DOCKER` | Execute in a Docker container (reserved for future implementation). |

#### Abstract class: `ExecutionBackend(abc.ABC)`

```python
class ExecutionBackend(abc.ABC):
    backend_type: BackendType

    def __init__(
        self,
        repo_root: str | None = None,
        base_path: str = "./data/backends",
    )
```

**Constructor fields:**
- `repo_root: Path | None` -- Path to the repository root (optional).
- `base_path: Path` -- Base directory for backend data (default `"./data/backends"`). Created on init if missing.

**Abstract methods:**

| Method | Signature | Description |
|---|---|---|
| `setup` | `(job_id: str, run_id: str) -> Path` | Prepare execution environment, return working directory. |
| `get_work_dir` | `(job_id: str, run_id: str) -> Path` | Get working directory without creating it. |
| `cleanup` | `(job_id: str, run_id: str) -> None` | Clean up on success. Delete temp files, release resources. |
| `preserve` | `(job_id: str, run_id: str, reason: str = "") -> Path` | Preserve execution scene on failure for debugging. Returns preserved path. |
| `is_available` | `() -> bool` | Check if the backend is usable (e.g., git is installed). |

---

### local.py -- `LocalBackend(ExecutionBackend)`

```python
class LocalBackend(ExecutionBackend):
    backend_type = BackendType.LOCAL
```

| Method | Behavior |
|---|---|
| `setup(job_id, run_id) -> Path` | If `repo_root` is set, returns it directly. Otherwise creates and returns `base_path / job_id / run_id`. |
| `get_work_dir(job_id, run_id) -> Path` | Same logic as `setup` without creating the directory. |
| `cleanup(job_id, run_id) -> None` | No-op if `repo_root` is set. Otherwise removes `base_path / job_id / run_id` via `shutil.rmtree`. |
| `preserve(job_id, run_id, reason) -> Path` | No-op if `repo_root` is set. Otherwise moves work dir to `base_path / "_preserved" / job_id / run_id`. |
| `is_available() -> bool` | Always returns `True`. |

---

### worktree.py -- `WorktreeBackend(ExecutionBackend)`

```python
class WorktreeBackend(ExecutionBackend):
    backend_type = BackendType.WORKTREE

    def __init__(
        self,
        repo_root: str | None = None,
        base_path: str = "./data/worktrees",
    )
```

**Additional fields:**
- `worktrees: dict[str, Path]` -- Map of `run_id` to worktree path.

| Method | Behavior |
|---|---|
| `setup(job_id, run_id) -> Path` | Removes existing worktree if present. Runs `git worktree add --detach <path>`. Stores path in `worktrees` dict. Returns the path. Raises `RuntimeError` on git failure. |
| `get_work_dir(job_id, run_id) -> Path` | Returns from `worktrees` dict or falls back to `base_path / job_id / run_id`. |
| `cleanup(job_id, run_id) -> None` | Runs `git worktree remove --force <path>`. Falls back to `shutil.rmtree` on failure. Cleans up empty parent directories. |
| `preserve(job_id, run_id, reason) -> Path` | Writes a `.PRESERVED` marker file with timestamp and reason. Removes from active `worktrees` dict but does NOT remove the git worktree. Returns the preserved path. |
| `is_available() -> bool` | Runs `git worktree list` and checks return code. Returns `False` if git is not installed or times out (5s). |

**Additional method:**
- `list_active_worktrees() -> list[dict]` -- Parse `git worktree list --porcelain` output into list of dicts with keys `path`, `head`, `branch`, `detached`.

**Requirement:** git >= 2.15 (worktree support).

---

### docker_stub.py -- `DockerBackend(ExecutionBackend)`

```python
class DockerBackend(ExecutionBackend):
    backend_type = BackendType.DOCKER
```

All methods raise `NotImplementedError("DockerBackend not yet implemented (planned for M3)")`.
`is_available()` returns `False`.

This is a placeholder for future container-based isolation.

---

### lifecycle.py -- `BackendManager`

```python
class BackendManager:
    def __init__(
        self,
        default_backend: str = "local",
        repo_root: str | None = None,
        base_path: str = "./data/backends",
        risk_backend_map: dict[str, str] | None = None,
    )
```

**Constructor fields:**
- `default_backend_type: BackendType` -- Resolved from `default_backend` string.
- `repo_root: str | None` -- Repository root path.
- `base_path: str` -- Base directory for backend data.
- `risk_backend_map: dict[str, str]` -- Risk level to backend type mapping. Default: `{"low": "local", "medium": "local", "high": "worktree", "critical": "worktree"}`.
- `_backends: dict[str, ExecutionBackend]` -- Cached backend instances by type string.
- `_active_runs: dict[str, ExecutionBackend]` -- Map of `run_id` to the backend handling it.

**Public methods:**

| Method | Signature | Description |
|---|---|---|
| `setup` | `(job_id: str, run_id: str, backend_type: str \| None = None, risk_level: str \| None = None) -> Path` | Resolve backend type, check availability, setup, track in `_active_runs`. Falls back to local if worktree is unavailable. |
| `get_work_dir` | `(job_id: str, run_id: str) -> Path \| None` | Return working directory from the active backend. |
| `cleanup` | `(job_id: str, run_id) -> None` | Remove from `_active_runs`, call backend's `cleanup`. |
| `preserve` | `(job_id: str, run_id: str, reason: str = "") -> Path \| None` | Remove from `_active_runs`, call backend's `preserve`. |
| `get_active_runs` | `() -> dict[str, str]` | Return `{run_id: backend_type_value}` for all active runs. |

**Resolution logic** (`_resolve_backend_type`):
1. If `backend_type` is explicitly provided, use it.
2. If `risk_level` is provided and in `risk_backend_map`, use the mapped backend.
3. Otherwise use `default_backend_type`.

---

## Data Flow

```
BackendManager.setup(job_id, run_id, risk_level)
       |
       +---> _resolve_backend_type(explicit, risk_level) --> BackendType
       +---> _get_backend(BackendType) --> ExecutionBackend instance
       +---> backend.is_available()
       |         |
       |         +---> False (worktree): fallback to LocalBackend
       |         +---> False (other): raise RuntimeError
       +---> backend.setup(job_id, run_id) --> Path (work_dir)
       +---> _active_runs[run_id] = backend
       |
       v
  Path (work_dir) returned to caller

  [On success:]
  BackendManager.cleanup(job_id, run_id)
       +---> _active_runs.pop(run_id) --> backend
       +---> backend.cleanup(job_id, run_id)

  [On failure:]
  BackendManager.preserve(job_id, run_id, reason)
       +---> _active_runs.pop(run_id) --> backend
       +---> backend.preserve(job_id, run_id, reason) --> Path
```

---

## Error Codes

| Condition | Error Type | Detail |
|---|---|---|
| Unknown backend type string | `ValueError` | `"Unknown backend type: {type}"` (from `BackendType` enum) |
| `git worktree add` failure | `RuntimeError` | `"git worktree add failed: {stderr}"` |
| Backend unavailable (non-worktree) | `RuntimeError` | `"Backend {type} is not available"` |
| Docker backend methods | `NotImplementedError` | `"DockerBackend not yet implemented (planned for M3)"` |

---

## Dependencies

| Dependency | Module | Usage |
|---|---|---|
| `subprocess` | stdlib | Running git commands (worktree backend). |
| `shutil` | stdlib | Directory removal and move operations. |
| `pathlib.Path` | stdlib | Path handling throughout. |
| `datetime` | stdlib | Timestamps in `.PRESERVED` marker. |

No external dependencies.

---

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `default_backend` | `"local"` | Backend used when no explicit type or risk level is provided. |
| `repo_root` | `None` | Repository root. If `None`, backends use `base_path` subdirectories. |
| `base_path` | `"./data/backends"` | Root directory for backend data files. |
| `risk_backend_map` | `{"low": "local", "medium": "local", "high": "worktree", "critical": "worktree"}` | Risk-level to backend mapping. |

---

## Extension Points

1. **Docker backend**: Implement `DockerBackend` methods for container-based isolation (planned for M3).
2. **Custom backends**: Subclass `ExecutionBackend`, register a new `BackendType` enum value, and add instantiation logic in `BackendManager._get_backend`.
3. **Risk-backend mapping**: Override `risk_backend_map` to change which risk levels trigger which backends.
4. **Preservation strategy**: Override `preserve()` in a backend subclass to implement custom debugging-scene preservation (e.g., tarball, snapshot).

---

## Invariants

1. All backend methods are synchronous (filesystem/git operations only).
2. `setup()` is called exactly once per `run_id` before execution; `cleanup()` or `preserve()` is called exactly once after.
3. `LocalBackend.is_available()` always returns `True`.
4. `WorktreeBackend.setup()` uses `--detach` to avoid creating new branches.
5. `BackendManager` tracks active runs in `_active_runs`; every `setup` adds an entry, and `cleanup`/`preserve` removes it.
6. When the selected worktree backend is unavailable, `BackendManager` silently falls back to `LocalBackend`.
7. `DockerBackend` is a stub -- all methods raise `NotImplementedError`.
8. `preserve()` writes a `.PRESERVED` marker file with UTC timestamp and reason for traceability.
