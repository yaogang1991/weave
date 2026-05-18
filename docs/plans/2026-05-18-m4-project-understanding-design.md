# M4: Project Understanding — Design Document

*Created: 2026-05-18 | Status: Approved*

## Executive Summary

M4 enables Weave to understand existing projects before executing tasks. A new `project/` top-level module provides project indexing (auto + manual), a multi-language dependency graph (tree-sitter, 7 edge types), context routing (BM25 + graph distance), and project-aware agent workflows (Viewer/Editor separation).

**Core principle**: Structure layer first (deterministic, cheap), semantic layer second (LLM, on-demand).

---

## Architecture Decision

**Approach B: New `project/` top-level module**

Rationale (from codebase architecture analysis):
- Project understanding is a separate domain from impact analysis (which stays in `analysis/`)
- `analysis/` currently has 3 files with focused responsibility; overloading it with 8-10 more files breaks cohesion
- A dedicated module provides clear seams: `project/` builds knowledge, `analysis/` uses it, `orchestrator/` consumes it
- `analysis/dependency_graph.py` migrates to `project/graph/engine.py`; old path becomes thin adapter

---

## Module Layout

```
project/                          # NEW top-level module
├── __init__.py                   # Exports ProjectIndexer, ProjectIndex
├── indexer.py                    # Orchestrates detection + summarization + graph
├── models.py                     # ProjectIndex, ModuleSummary, TechStack, CodeConventions
├── stack_detector.py             # Auto-detect tech stack (deterministic)
├── module_summarizer.py          # Module summaries (AST + optional LLM)
├── convention_extractor.py       # Code conventions extraction
├── graph/
│   ├── __init__.py               # Exports ProjectGraph
│   ├── engine.py                 # Multi-edge-type graph engine
│   ├── models.py                 # EdgeType, GraphNode, GraphEdge
│   ├── base_parser.py            # Parser abstract base class
│   ├── python_parser.py          # Python AST + tree-sitter hybrid
│   ├── js_ts_parser.py           # JS/TS tree-sitter parser
│   ├── go_parser.py              # Go tree-sitter parser
│   └── rust_parser.py            # Rust tree-sitter parser
├── context/
│   ├── __init__.py               # Exports ContextRouter
│   ├── router.py                 # BM25 + graph distance fusion ranking
│   └── selector.py               # FULL/SIGNATURES/COMPRESSED output modes
└── agents/
    ├── __init__.py
    ├── viewer.py                 # Read-only analysis agent
    └── prompts.py                # Project-aware agent prompt templates
```

**Index storage**:

```
.weave/
├── config.yaml                   # Existing, extended with project_context
├── index/
│   ├── graph.json                # Serialized dependency graph
│   ├── modules.json              # Module summary index
│   ├── tech_stack.json           # Detected tech stack
│   └── conventions.json          # Extracted code conventions
└── context/                      # LLM-generated context files (later phase)
    ├── architecture.md           # Architecture overview (~1000 tokens)
    └── modules/                  # Per-module navigation guides
```

---

## M4.0 — Project Onboarder

**Goal**: Scan existing project, auto-generate project index. Auto-triggers on first run/plan, also available as manual command.

### Data Models (`project/models.py`)

```python
class TechStack(BaseModel):
    languages: dict[str, float]        # {"Python": 0.7, "JavaScript": 0.3}
    frameworks: list[str]              # ["FastAPI", "React"]
    test_runners: list[str]            # ["pytest", "jest"]
    linters: list[str]                 # ["flake8", "ruff"]
    build_tools: list[str]             # ["pip", "npm"]
    package_managers: list[str]        # ["pip", "yarn"]

class ModuleSummary(BaseModel):
    path: str
    language: str
    size_lines: int
    classes: list[str]
    functions: list[str]
    exports: list[str]
    imports: list[str]
    docstring: str | None

class CodeConventions(BaseModel):
    indent_style: str                  # "space" | "tab"
    indent_size: int                   # 2, 4
    naming_style: str                  # "snake_case" | "camelCase"
    max_line_length: int | None
    import_style: str                  # "absolute" | "relative" | "mixed"
    type_hints: bool
    docstring_style: str | None        # "google" | "numpy" | "sphinx"

class ProjectIndex(BaseModel):
    root_path: str
    tech_stack: TechStack
    structure: str                      # Directory tree text
    module_summaries: dict[str, ModuleSummary]
    conventions: CodeConventions
    entry_points: list[str]
    indexed_at: datetime
    version: str = "1.0"
```

### Tech Stack Detection (`project/stack_detector.py`)

Deterministic detection, no LLM:

| Item | Method |
|------|--------|
| Language distribution | Scan file extensions |
| Python frameworks | Parse pyproject.toml/requirements.txt dependencies |
| JS/TS frameworks | Parse package.json dependencies/devDependencies |
| Go frameworks | Parse go.mod |
| Rust frameworks | Parse Cargo.toml |
| Test frameworks | Find pytest.ini/jest.config/go test patterns |
| Linters | Find .flake8/.eslintrc/ruff.toml |
| Entry points | Find main.py/app.py/manage.py/src/index.ts/main.go/main.rs |

### CLI Commands

```bash
python main.py project-analyze --project ./my-project          # Analyze project
python main.py project-index --project ./my-project             # View index
python main.py project-analyze --project ./my-project --force   # Force rebuild
```

### Auto-trigger Logic

In `control_plane/service.py` `_plan_and_execute`:
1. Check `.weave/index/` existence and freshness (mtime vs file changes)
2. Missing or stale → auto-run `ProjectIndexer.index()`
3. Present and fresh → load directly

### Staleness Detection

```python
def is_index_stale(root: Path, index_path: Path) -> bool:
    """Index is stale if any source file is newer than the index."""
    index_mtime = index_path.stat().st_mtime
    for path in root.rglob("*"):
        if path.is_file() and path.stat().st_mtime > index_mtime:
            if not _is_skip_path(path):  # __pycache__, .git, etc.
                return True
    return False
```

---

## M4.1 — Enhanced Dependency Graph

**Goal**: Upgrade Python-only `dependency_graph.py` to multi-language, multi-edge-type graph engine.

### Edge Types (`project/graph/models.py`)

```python
class EdgeType(str, Enum):
    IMPORTS = "imports"           # A imports B
    INHERITS = "inherits"         # A inherits from B
    CALLS = "calls"               # A calls B's function
    IMPLEMENTS = "implements"     # A implements B interface
    DECORATES = "decorates"       # A decorates B (Python)
    TESTS = "tests"               # A tests B
    REFERENCES = "references"     # A references B (generic)

class GraphNode(BaseModel):
    id: str                       # File path
    language: str
    node_type: str                # "module" | "class" | "function"
    name: str
    signature: str | None         # For SIGNATURES output mode

class GraphEdge(BaseModel):
    source: str
    target: str
    edge_type: EdgeType
    weight: float = 1.0
    metadata: dict = {}
```

### Parser Architecture

```python
class BaseParser(ABC):
    language: str

    @abstractmethod
    def parse_file(self, path: Path) -> tuple[list[GraphNode], list[GraphEdge]]: ...

    @abstractmethod
    def can_parse(self, path: Path) -> bool: ...
```

Language parsers use tree-sitter:
- **Python**: Existing AST + tree-sitter for CALLS/INHERITS/DECORATES
- **JS/TS**: tree-sitter for import/export/call/extends/implements
- **Go**: tree-sitter for import/call/interface/struct
- **Rust**: tree-sitter for use/call/trait/impl

### Graph Engine API (`project/graph/engine.py`)

```python
class ProjectGraph:
    def build(self, root: Path) -> None: ...
    def load(self, path: Path) -> None: ...
    def save(self, path: Path) -> None: ...
    def query(self, node_id: str, edge_types: list[EdgeType],
              direction: str, max_depth: int) -> list[GraphNode]: ...
    def subgraph(self, node_ids: list[str]) -> ProjectGraph: ...
    def to_text(self, max_nodes: int = 50) -> str: ...
```

### Migration from M3.5

- `analysis/dependency_graph.py` becomes thin adapter, delegates to `project/graph/engine.py`
- `analysis/impact_predictor.py` updated to use `ProjectGraph`
- Old tests continue passing; new tests cover extended capabilities

### CLI Commands

```bash
python main.py project-graph --project ./my-project              # View graph
python main.py project-graph --project . --query auth.py         # Query node
```

---

## M4.2 — Context Router

**Goal**: Select the most relevant project context for each task within a token budget.

### Routing Algorithm (`project/context/router.py`)

```
User task "Fix auth bug"
        |
Step 1: Keyword extraction
Step 2: BM25 file matching (score files by keyword overlap)
Step 3: Graph traversal from matched nodes (expand via graph edges)
Step 4: Fusion ranking: score = 0.3 * BM25 + 0.7 * graph_decay(d)
Step 5: Token budget pruning (budget = 4000 tokens)
        |
  [auth.py:FULL, user.py:SIGNATURES, test_auth.py:COMPRESSED]
```

**Graph distance decay** (from graphsift research):

```
decay(d) = 1 / (1 + alpha * d)    # d = graph distance, alpha = decay factor
```

### Output Modes (`project/context/selector.py`)

```python
class OutputMode(str, Enum):
    FULL = "full"              # Complete file content
    SIGNATURES = "signatures"  # Class/function signatures + docstrings
    COMPRESSED = "compressed"  # Path + export symbol list only

def select_output_mode(score: float, remaining_budget: int,
                       file_size: int) -> OutputMode:
    if score > 0.8 and file_size < remaining_budget:
        return OutputMode.FULL
    if score > 0.5:
        return OutputMode.SIGNATURES
    return OutputMode.COMPRESSED
```

### Injection Points

| Consumer | Token Budget | Content |
|----------|-------------|---------|
| Orchestrator planning | 4000 | Structure + relevant module summaries + graph subset |
| Agent system prompt | 2000 | Conventions + task-relevant file SIGNATURES |
| Evaluator | 1000 | Conventions + test patterns |

### Injection Format

```python
project_context = {
    "project_path": ...,
    "existing_files": ...,
    # M4 additions:
    "project_structure": index.structure,
    "tech_stack": index.tech_stack.model_dump(),
    "relevant_context": router.route(requirement, index, budget=3000),
    "conventions": index.conventions.model_dump(),
}
```

### CLI Command

```bash
python main.py project-context "Fix auth bug" --project .   # Preview routing
```

---

## M4.3 — Project-Aware Agents

**Goal**: Agents work on existing projects like senior engineers who understand the codebase.

### Viewer/Editor Agent Separation

```
Current:  generator -> (read + understand + write, all in one context window)
M4.3:     viewer -> (read-only, locate + understand)
          editor -> (write-only, execute modifications from viewer's plan)
```

**Viewer agent** (`project/agents/viewer.py`):
- Tools: read, grep, glob, graph_query (read-only)
- Output: Structured modification plan (file + location + change description)

**Editor agent**:
- Uses viewer's compact output as context
- Focused context window for code modifications

### Prompt Enhancements (`project/agents/prompts.py`)

**Orchestrator planning prompt additions**:

```markdown
## Project Context

### Structure
{project_structure}

### Tech Stack
{tech_stack}

### Conventions
- Naming: {naming_style}
- Indent: {indent_style} {indent_size}
- Types: {type_hints_usage}
- Tests: {test_patterns}

### Modify Existing Code Rules
- PREFER editing existing files over creating new ones
- Read and understand existing patterns before writing
- Run existing tests FIRST to establish baseline
- Minimize diff — surgical changes, not full rewrites
- Follow existing naming and import conventions
- When adding to a module, match its existing style
```

**Agent system prompt additions**:

```markdown
## Project Navigation Guide
{relevant_modules_signatures}

## Key Entry Points
{entry_points}

## Module Dependencies (relevant subgraph)
{relevant_dependency_graph}
```

### Planning Rules Extension (`orchestrator/prompts/planning.md`)

```
20. Project-aware planning: Use dependency graph and module summaries to:
    a. Assign file ownership based on actual module boundaries
    b. Determine which existing files need modification
    c. Include relevant module context in task descriptions
    d. Use Viewer agent type for read-only analysis tasks
    e. Estimate complexity from affected module count

21. Minimal diff principle for existing projects:
    a. First task identifies exactly which files need changes
    b. Each change is the smallest possible to achieve the goal
    c. Never rewrite entire files when targeted edits suffice
    d. Include original code context in task descriptions
```

### DAG Template Updates

New `templates/fix_bug_v2.yaml`:

```yaml
name: fix_bug_v2
nodes:
  - id: analyze
    agent_type: viewer
    task: "Analyze: {bug_description}. Query dependency graph to locate
           affected files. Trace call chain. Output modification plan."
  - id: fix
    agent_type: editor
    task: "Apply modification plan. Minimal surgical changes.
           Follow project conventions."
  - id: verify
    agent_type: evaluator
    task: "Verify: run tests, check convention compliance"
edges:
  - {from: analyze, to: fix}
  - {from: fix, to: verify}
```

---

## Timeline

```
M4.0 Project Onboarder     Weeks 1-3
M4.1 Enhanced Dep Graph    Weeks 4-7
M4.2 Context Router        Weeks 8-10
M4.3 Project-Aware Agents  Weeks 11-13
```

**Total estimated duration**: 10-13 weeks.

### Dependency Chain

```
M4.0 -> M4.1 -> M4.2 -> M4.3
```

Each milestone is independently usable:
- M4.0 alone gives tech stack detection and module summaries
- M4.0 + M4.1 adds multi-language dependency graph
- M4.0 + M4.1 + M4.2 adds intelligent context routing
- Full M4 adds project-aware agent workflows

---

## New Dependencies

```
tree-sitter                # Multi-language parsing foundation
tree-sitter-python         # Python grammar
tree-sitter-javascript     # JavaScript grammar
tree-sitter-typescript     # TypeScript grammar
tree-sitter-go             # Go grammar
tree-sitter-rust           # Rust grammar
```

## New Environment Variables

```
WEAVE_PROJECT_INDEX_ENABLED    # Enable/disable project indexing (default: true)
WEAVE_PROJECT_INDEX_PATH       # Index storage path (default: .weave/index/)
WEAVE_PROJECT_INDEX_AUTO       # Auto-detect and rebuild (default: true)
WEAVE_PROJECT_GRAPH_MAX_DEPTH  # Max graph query depth (default: 5)
WEAVE_CONTEXT_BUDGET           # Router token budget (default: 4000)
```

## New CLI Commands

```bash
# M4.0
python main.py project-analyze --project <path>       # Analyze project
python main.py project-index --project <path>          # View index

# M4.1
python main.py project-graph --project <path>          # View graph
python main.py project-graph --project . --query X     # Query node

# M4.2
python main.py project-context "task" --project <path> # Preview routing
```

---

## Key Design Decisions

1. **Deterministic first, LLM second** — Structure layer (AST, file scanning, package.json parsing) before semantic layer (LLM-generated architecture docs). Cheaper, deterministic, self-refreshing.

2. **Meta's compass principle** — Each context file ~1000 tokens, always loadable without consuming significant context window.

3. **Graph navigation over retrieval** — CodeCompass benchmark: graph navigation (99.4%) far exceeds BM25 retrieval (78.2%) and pure agent search (76.2%) for hidden dependencies.

4. **Viewer/Editor separation** — SWE-Edit's insight: separating read-only analysis from code generation reduces context pollution and improves accuracy.

5. **Backward compatibility** — `analysis/dependency_graph.py` remains functional as thin adapter. All M3.5 tests continue passing.

---

## Sources

See `docs/research-project-understanding.md` for the full research bibliography (30+ sources).
