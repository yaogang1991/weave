# Module SPEC: Impact Analysis

---
**Module:** `analysis/dependency_graph.py`, `analysis/impact_predictor.py`, `analysis/change_verifier.py`
**Last Updated:** 2026-05-10
**Status:** IMPLEMENTED
---

## Purpose

Pre-execution impact prediction and post-execution change verification. Before running a task, the system predicts which files will be affected based on keyword matching and dependency graph analysis. After execution, it verifies actual changes against the prediction, computes coverage metrics, and stores results in memory for future prediction improvement.

## Public Interfaces

### DependencyGraph (`analysis/dependency_graph.py`)

```python
class DependencyGraph:
    """Build and query file-level dependency graphs from project structure."""

    def __init__(self, project_path: str | Path) -> None

    def build(self) -> dict[str, set[str]]
    def get_dependents(self, file_path: str) -> set[str]    # Transitive
    def get_dependencies(self, file_path: str) -> set[str]  # Transitive
    def get_module_files(self, module_name: str) -> list[str]
    def to_dict(self) -> dict[str, list[str]]
```

### ImpactPredictor (`analysis/impact_predictor.py`)

```python
class ImpactPredictor:
    """Predict file/module impact from a natural-language requirement."""

    def __init__(
        self,
        llm_config: Any | None = None,
        memory_manager: Any | None = None,
    ) -> None

    async def predict(
        self,
        requirement: str,
        project_path: str,
    ) -> ImpactScope

    def predict_static(
        self,
        requirement: str,
        project_path: str,
    ) -> ImpactScope
```

### ChangeVerifier (`analysis/change_verifier.py`)

```python
class ChangeVerifier:
    """Verify that actual changes match predicted impact scope."""

    def __init__(
        self,
        project_path: str,
        coverage_threshold: float = 0.7,
    ) -> None

    def capture_snapshot(self) -> dict[str, float]
    def verify(
        self,
        impact_scope: ImpactScope,
        before_snapshot: dict[str, float],
        after_snapshot: dict[str, float] | None = None,
    ) -> VerificationResult
```

## Data Models (`core/models.py`)

```python
class ImpactRiskLevel(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"

class ImpactScope(BaseModel):
    id: str
    requirement: str
    predicted_files: list[str]
    predicted_modules: list[str]
    risk_level: ImpactRiskLevel
    confidence: float
    reasoning: str

class VerificationResult(BaseModel):
    id: str
    impact_scope_id: str
    expected_files: list[str]
    actual_changed_files: list[str]
    covered_files: list[str]
    unexpected_files: list[str]
    missed_files: list[str]
    coverage: float
    prediction_accuracy: float
    passes: bool
    notes: str
```

## Data Flow

```
执行前 (control_plane/service.py):
  ImpactPredictor.predict(requirement, project_path)
    ├─ 检查历史记忆 (ImpactScope from memory, confidence >= 0.7)
    ├─ [缓存命中] → 返回历史预测
    └─ [缓存未命中] → predict_static()
          ├─ DependencyGraph(project_path).build()
          │     ├─ 扫描所有 .py 文件
          │     ├─ ast 解析 import 语句
          │     └─ 构建双向依赖图 (graph + reverse)
          ├─ _keyword_match_files(requirement, project_path)
          │     └─ 关键词 vs 文件名/路径匹配
          ├─ _expand_with_dependencies(matches, dep_graph, depth=1)
          │     └─ 依赖图传递性扩展
          └─ _compute_risk_level(file_count, module_count)
    → ImpactScope (存入 job.metadata)

  ChangeVerifier.capture_snapshot()
    → {rel_path: mtime, ...}

执行 DAG ...

执行后:
  ChangeVerifier.capture_snapshot()  (after)
  ChangeVerifier.verify(impact_scope, before, after)
    ├─ 计算覆盖率: |predicted ∩ actual| / |actual|
    ├─ 计算准确率: |predicted ∩ actual| / |predicted|
    └─ passes = coverage >= threshold
    → VerificationResult

  结果存入 GLOBAL EXPERIENCE 记忆 (供未来预测参考)
```

## Risk Level Computation

```
| predicted_files | predicted_modules | Risk Level |
|-----------------|-------------------|------------|
| 0               | —                 | LOW        |
| ≤ 2             | ≤ 1               | LOW        |
| ≤ 5             | ≤ 2               | MEDIUM     |
| ≤ 15            | ≤ 4               | HIGH       |
| > 15            | > 4               | CRITICAL   |
```

## Dependency Graph Construction

1. Scan all `.py` files (skip `.venv`, `__pycache__`, `.git`, etc.)
2. Build module → file mapping (`core/models` → `core/models.py`)
3. Parse imports via `ast.walk`:
   - `import X` → `X`
   - `from X import Y` → `X` (absolute, level=0)
   - `from . import Y` → resolved to absolute module (level>0)
   - `from ..X import Y` → resolved with parent traversal
4. Build forward graph (file → dependencies) and reverse graph (file → dependents)
5. Transitive queries via DFS traversal

## Error Handling

| Error | Condition | Handling |
|-------|-----------|----------|
| Parse error | Invalid Python syntax | File skipped, logged at DEBUG |
| Project path missing | No `.py` files found | Returns empty graph |
| Historical lookup fails | Memory I/O error | Logged at DEBUG, falls back to static |
| Snapshot capture error | File stat fails | File skipped |

## Dependencies

### Imports From
- `core/models.py` — ImpactRiskLevel, ImpactScope, VerificationResult
- `core/config.py` — ImpactConfig
- `memory/manager.py` — Historical prediction lookup

### Imported By
- `control_plane/service.py` — Pre/post execution hooks
- `main.py` — CLI commands (impact-predict, impact-graph, impact-history)
- `visualizer/server.py` — REST API endpoints

## Configuration

| Env Var | Config Key | Default | Description |
|---------|-----------|---------|-------------|
| `WEAVE_IMPACT_ENABLED` | `impact.enabled` | `true` | Enable impact analysis |
| `WEAVE_IMPACT_PATH` | `impact.base_path` | `./data/impact` | Analysis data directory |
| — | `impact.coverage_threshold` | `0.7` | Pass threshold for verification |
| — | `impact.max_predicted_files` | `50` | Max predicted files |
| — | `impact.confidence_threshold` | `0.5` | Min confidence threshold |

## Invariants

- Dependency graph is rebuilt on each call (no caching between calls)
- Import resolution only handles Python files; other languages are ignored
- `coverage_threshold` uses `coverage` (predicted ∩ actual / actual), not accuracy
- Verification passes if `coverage >= threshold`, regardless of unexpected files
