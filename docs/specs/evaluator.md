# Module SPEC: evaluator/engine.py

## Purpose

Automated evaluation engine that verifies code artifacts against predefined success criteria.
Supports test execution (pytest), code coverage checks, lint validation (flake8/ruff), file
existence verification, and critical marker detection. Integrates with the session store to
emit evaluation lifecycle events.

## Public Interfaces

### Class `EvaluatorEngine`

```python
class EvaluatorEngine:
    def __init__(self, session_store: SessionStore) -> None
```

**Attributes:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `session_store` | `SessionStore` | Used to emit `EVAL_START` and `EVAL_RESULT` events |

**Public Methods:**

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `evaluate_stage` | `(session_id: str, stage_name: str, criteria: list[str], artifact_path: str) -> EvaluationResult` | `EvaluationResult` | Evaluates a stage against its success criteria, emits events, returns result |

**Private Methods:**

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `_check_criterion` | `(criterion: str, artifact_path: str) -> tuple[bool, str, bool]` | `(passed, message, was_auto_checked)` | Dispatches to the appropriate checker based on keyword matching |
| `_run_tests` | `(path: Path) -> tuple[bool, str]` | `(passed, message)` | Runs `python -m pytest {path} -v --tb=short` with 120s timeout |
| `_check_coverage` | `(path: Path, target: int) -> tuple[bool, str]` | `(passed, message)` | Runs pytest with `--cov=.` and parses TOTAL line for percentage |
| `_run_lint` | `(path: Path) -> tuple[bool, str]` | `(passed, message)` | Runs flake8 (falls back to ruff) with `--max-line-length=100` |
| `_check_files_exist` | `(criterion: str, path: Path) -> tuple[bool, str]` | `(passed, message)` | Parses filenames from criterion text, checks existence |
| `_check_no_critical_issues` | `(path: Path) -> tuple[bool, str]` | `(passed, message)` | Scans files for TODO/FIXME/XXX/HACK markers |
| `_extract_percentage` | `(text: str) -> int \| None` | `int \| None` | Extracts first `NN%` pattern from text |

## Data Flow

```
evaluate_stage(session_id, stage_name, criteria, artifact_path)
    |
    v
emit_event(session_id, EVAL_START, {stage, criteria, artifact})
    |
    v
for each criterion in criteria:
    |
    v
    _check_criterion(criterion, artifact_path)
        |
        +-- "test" + "pass" in criterion ---> _run_tests(path)
        |       Runs: python -m pytest {path} -v --tb=short
        |
        +-- "coverage" in criterion --------> _check_coverage(path, target)
        |       Runs: python -m pytest {path} --cov=. --cov-report=term-missing
        |       Parses TOTAL line for coverage percentage
        |       Default target: 80%
        |
        +-- "lint"/"clean" in criterion ----> _run_lint(path)
        |       Runs: python -m flake8 {path} --max-line-length=100
        |       Fallback: ruff check {path}
        |
        +-- "file" + "exist" in criterion --> _check_files_exist(criterion, path)
        |       Parses filenames after colon/space from criterion text
        |
        +-- "no_critical"/"no bug" ---------> _check_no_critical_issues(path)
        |       Scans for TODO, FIXME, XXX, HACK markers
        |
        +-- (unrecognized) -----------------> (False, "not automatically checkable", False)
    |
    v
    Accumulate: results dict, score, feedback, uncheckable list
    |
    v
overall_passed = all(results) AND no uncheckable criteria
score = (passed_count / total_count) * 10.0, rounded to 1 decimal
    |
    v
EvaluationResult(passed, score, criteria_results, feedback, suggestions)
    |
    v
emit_event(session_id, EVAL_RESULT, result.model_dump())
    |
    v
return EvaluationResult
```

## Error Codes

No custom error codes. All errors are captured and returned as `(False, message)` tuples:

| Condition | Error Message | Method |
|-----------|---------------|--------|
| pytest not installed | `"pytest not installed"` | `_run_tests` |
| Test execution failure | `"Test execution error: {e}"` | `_run_tests` |
| Tests failed | `"Tests failed:\n{last 500 chars of stdout}"` | `_run_tests` |
| Coverage parse failure | `"Could not parse coverage report"` | `_check_coverage` |
| Coverage check error | `"Coverage check error: {e}"` | `_check_coverage` |
| No linter available | `"No linter available (install flake8 or ruff)"` | `_run_lint` |
| Lint error | `"Lint error: {e}"` | `_run_lint` |
| Unrecognized criterion | `"Criterion '{criterion}' is not automatically checkable..."` | `_check_criterion` |

## Dependencies

| Dependency | Type | Usage |
|------------|------|-------|
| `core.models` | Internal | `EvaluationResult`, `EventType` |
| `session.store` | Internal | `SessionStore` for event emission |
| `subprocess` | Stdlib | Running pytest, flake8, ruff as child processes |
| `pathlib.Path` | Stdlib | File path handling |
| `re` | Stdlib | Parsing filenames from criteria, extracting percentages |

## Configuration

| Parameter | Default | Scope | Description |
|-----------|---------|-------|-------------|
| Test timeout | `120` seconds | `_run_tests` | `subprocess.run` timeout for pytest |
| Coverage timeout | `120` seconds | `_check_coverage` | `subprocess.run` timeout for coverage run |
| Lint timeout | `60` seconds | `_run_lint` | `subprocess.run` timeout for flake8/ruff |
| Max line length | `100` | `_run_lint` | `--max-line-length=100` flag for flake8 |
| Default coverage target | `80`% | `_check_criterion` | Used when no percentage is found in the criterion text |
| Max score | `10.0` | `evaluate_stage` | Divided equally among criteria |
| Test stdout truncation | Last 500 chars | `_run_tests` | Limit on failure output |
| Lint stdout truncation | First 500 chars | `_run_lint` | Limit on failure output |

## Extension Points

1. **New criterion types**: Add a new keyword check in `_check_criterion()` and implement a
   corresponding `_{check_name}(path) -> tuple[bool, str]` method.
2. **Custom test runners**: Replace the `subprocess.run(["python", "-m", pytest, ...])` call
   in `_run_tests()` to support other test frameworks.
3. **Scoring strategy**: The current scoring divides 10.0 equally among all criteria. A weighted
   scoring system could be introduced by accepting a `dict[str, float]` of criterion-to-weight
   mappings.
4. **External linters**: The fallback chain (flake8 -> ruff) in `_run_lint()` can be extended
   with additional tools.
5. **Critical markers**: The list `["TODO", "FIXME", "XXX", "HACK"]` in `_check_no_critical_issues()`
   is hardcoded and could be made configurable.

## Invariants

1. **Uncheckable criteria are never treated as passed**: If `_check_criterion` returns
   `was_auto_checked=False`, the overall `passed` is always `False` regardless of other results.
2. **Every evaluation emits exactly two events**: One `EVAL_START` at the beginning and one
   `EVAL_RESULT` at the end.
3. **Score is bounded**: `score` is computed as `(passed_count / total_count) * 10.0`, rounded
   to 1 decimal. It reflects only auto-checked criteria that passed, not overall pass/fail.
4. **Subprocess isolation**: All external tool invocations use `subprocess.run()` with timeouts
   and `capture_output=True`. Failures return `(False, message)` and never propagate exceptions.
5. **Output truncation**: Test failure output is truncated to the last 500 characters; lint
   output is truncated to the first 500 characters. This prevents unbounded log growth.
6. **Criterion matching is case-insensitive**: `criterion.lower()` is used for all keyword
   matching in `_check_criterion()`.
7. **No mutation of inputs**: `evaluate_stage()` does not modify `criteria`, `artifact_path`,
   or any `SessionStore` state beyond emitting events.
