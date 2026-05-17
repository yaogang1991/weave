# Module SPEC: Self-Learning

---
**Module:** `learning/analyzer.py`, `learning/optimizer.py`, `learning/scheduler.py`
**Last Updated:** 2026-05-10
**Status:** IMPLEMENTED
---

## Purpose

Automated execution pattern analysis that learns from historical task outcomes. The system periodically analyzes failure patterns, success patterns, agent performance, and planning quality to generate actionable insights. High-confidence insights are stored as persistent memories that influence future orchestrator planning.

## Public Interfaces

### LearningAnalyzer (`learning/analyzer.py`)

```python
class LearningAnalyzer:
    """Analyze execution patterns from metrics and memory to generate insights."""

    def __init__(
        self,
        metrics_collector: Any | None = None,
        memory_manager: MemoryManager | None = None,
    ) -> None

    def analyze(self) -> list[LearningInsight]
```

Produces insights in four categories:
1. **Failure patterns** — recurring errors, low success rates, high retry rates
2. **Success patterns** — effective strategies, high-performing agents
3. **Agent performance** — per-agent success rates, low performers
4. **Planning quality** — duration variance, timeout patterns

### LearningOptimizer (`learning/optimizer.py`)

```python
class LearningOptimizer:
    """Convert LearningInsight objects into MemoryEntry objects."""

    def __init__(self, memory_manager: MemoryManager) -> None

    def optimize(
        self,
        insights: list[LearningInsight],
        confidence_threshold: float = 0.7,
    ) -> list[MemoryEntry]

    def get_planning_hints(self, requirement: str = "") -> str
    def get_agent_hints(self, agent_type: str, task: str = "") -> str
```

### LearningScheduler (`learning/scheduler.py`)

```python
class LearningScheduler:
    """Manages learning analysis lifecycle."""

    def __init__(
        self,
        config: LearningConfig,
        analyzer: LearningAnalyzer,
        optimizer: LearningOptimizer,
    ) -> None

    def maybe_run_analysis(self) -> dict[str, Any] | None
    def run_analysis(self) -> dict[str, Any]
    def get_status(self) -> dict[str, Any]
```

## Data Models (`core/models.py`)

```python
class LearningCategory(str, Enum):
    EXECUTION       = "execution"
    AGENT_SELECTION = "agent_selection"
    PLANNING        = "planning"

class InsightType(str, Enum):
    PATTERN        = "pattern"
    RECOMMENDATION = "recommendation"
    ANTI_PATTERN   = "anti_pattern"

class LearningInsight(BaseModel):
    id: str
    category: LearningCategory
    insight_type: InsightType
    description: str
    evidence: dict[str, Any]
    confidence: float          # 0.0–1.0
    impact: str                # "low", "medium", "high"
    applies_to: list[str]      # Agent types this applies to
```

## Data Flow

```
定期触发:
  LearningScheduler.maybe_run_analysis()
    ├─ 检查时间间隔 (analysis_interval_hours)
    ├─ 检查最小样本数 (min_samples)
    └─ 触发分析 → run_analysis()

分析流水线:
  run_analysis()
    ├─ 1. Analyzer.analyze()
    │     ├─ _analyze_failure_patterns()    ← MetricsCollector
    │     ├─ _analyze_success_patterns()    ← MetricsCollector + MemoryManager
    │     ├─ _analyze_agent_performance()   ← MemoryManager (EXPERIENCE entries)
    │     └─ _analyze_planning_quality()    ← MetricsCollector
    │
    ├─ 2. Optimizer.optimize(insights)
    │     ├─ 过滤低置信度 (< confidence_threshold)
    │     ├─ ANTI_PATTERN → EXPERIENCE memory (GLOBAL)
    │     ├─ PATTERN → FACT memory (GLOBAL)
    │     └─ Agent-specific → PRIVATE memory
    │
    └─ 3. Save state (.last_analysis)

规划时注入:
  Orchestrator.plan()
    └─ Optimizer.get_planning_hints(requirement)
          └─ 查询 GLOBAL 记忆中的学习洞察
          └─ 格式化为 "## Learned Insights for Planning"
          └─ 注入编排 Agent system prompt
```

## Insight → Memory Mapping

| InsightType | MemoryType | MemoryScope | Agent |
|-------------|-----------|-------------|-------|
| `PATTERN` | `FACT` | `GLOBAL` | `"shared"` |
| `RECOMMENDATION` | `FACT` | `GLOBAL` | `"shared"` |
| `ANTI_PATTERN` | `EXPERIENCE` | `GLOBAL` | `"shared"` |
| *Agent-specific* | `FACT` | `PRIVATE` | applies_to[0] |

## Error Handling

| Error | Condition | Handling |
|-------|-----------|----------|
| Metrics unavailable | `metrics_collector` is None | Returns empty dict, analysis continues with partial data |
| Memory unavailable | `memory_manager` is None | Returns empty list, analysis continues |
| Insight storage fails | Disk I/O error | Logged at WARNING, insight skipped |

## Dependencies

### Imports From
- `core/models.py` — LearningInsight, LearningCategory, InsightType, MemoryEntry, MemoryScope, MemoryType
- `core/config.py` — LearningConfig
- `memory/manager.py` — MemoryManager, _extract_keywords
- `monitoring/metrics.py` — MetricsCollector

### Imported By
- `control_plane/service.py` — Scheduler initialization and periodic triggering
- `orchestrator/intelligent_orchestrator.py` — Planning hints injection

## Configuration

| Env Var | Config Key | Default | Description |
|---------|-----------|---------|-------------|
| `WEAVE_LEARNING_PATH` | `learning.base_path` | `./data/learning` | Analysis state directory |
| — | `learning.enabled` | `true` | Enable self-learning |
| — | `learning.analysis_interval_hours` | `6.0` | Min hours between analyses |
| — | `learning.min_samples` | `5` | Min executions before analysis |
| — | `learning.max_insights` | `100` | Max insights per analysis |
| — | `learning.confidence_threshold` | `0.7` | Min confidence to store insight |

## Invariants

- `maybe_run_analysis` is thread-safe (uses `threading.Lock`)
- Analysis state is persisted atomically (`.last_analysis` via temp + replace)
- Insights below `confidence_threshold` are discarded, not stored
- Confidence is bounded: `min(sample_count / denominator, 1.0)`
