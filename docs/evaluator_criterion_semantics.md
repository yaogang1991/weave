# Evaluator Criterion Semantics

This document defines the exact semantics of each evaluator criterion type.
Every criterion returns a 3-tuple `(passed: bool, message: str, was_auto: bool)`.

## Result States

| Label | `passed` | `was_auto` | Meaning |
|-------|----------|------------|---------|
| **PASS** | `True` | `True` | Automatically verified and passed |
| **FAIL** | `False` | `True` | Automatically verified and failed |
| **WARN** | `True` or `False` | `False` | Could not auto-verify; requires manual review |

WARN criteria:
- Are added to `result.suggestions` for downstream consumers.
- Do not contribute to `score` when `passed=False`.
- Do not trigger `overall_passed = False` on their own (unless `passed=False`).

## Evaluation Flow

```
1. Normalize criteria (strings → SuccessCriterion)
2. For each criterion: _check_criterion() → (passed, msg, was_auto)
3. Compute all_auto_passed, apply pass_threshold if set
4. HARD_CRITERIA veto: if any hard criterion fails, overall = FAIL
5. Artifact verification gate: phantom output_artifacts → overall = FAIL
6. Emit result
```

## Criterion Reference

### TESTS_PASS

Runs `python -m pytest -v --tb=short` with shell=False (security).

| Condition | Result |
|-----------|--------|
| `test_path` set | Run that path only → PASS/FAIL |
| `output_artifacts` contains test files | Run only those test files (scoped) → PASS/FAIL |
| Neither | WARN: "not verified" — no test files found |

**Hard criterion** — cannot be overridden by pass_threshold.

### TEST_FILE_EXISTS

Checks that at least one test file exists on disk.

| Condition | Result |
|-----------|--------|
| Test file exists (any size, including 0-byte) | PASS |
| No test file found | FAIL |

Uses standard test naming conventions: `test_*.py`, `*_test.py`, `*_spec.py`, files in `tests/` or `test/` directories.

### COVERAGE

Runs `pytest --cov-report=term-missing` scoped to output_artifacts packages.

| Condition | Result |
|-----------|--------|
| Scoped test files found + TOTAL ≥ target | PASS |
| Scoped test files found + TOTAL < target | FAIL |
| `output_artifacts` present but no test files in them | WARN: "cannot verify coverage without scoped tests" |
| `output_artifacts` is None | Runs pytest without --cov (avoids scanning historical files) |
| TOTAL line unparseable but tests pass | WARN: "coverage could not be parsed" |

Coverage scope is inferred from `output_artifacts` paths: `src/module.py` → `--cov=src/module`.

Each evaluation gets an isolated `COVERAGE_FILE` env variable (`.coverage.{session_id}_{stage_name}`) to prevent parallel node contention (#260).

### LINT

Runs autoflake dry-run (detect unused imports/vars) then flake8/ruff with delta linting.

| Condition | Result |
|-----------|--------|
| 0 new issues on changed lines | PASS |
| ≥1 new issues on changed lines | FAIL |
| No linter available | WARN (not FAIL) |
| No git diff available | All issues count as new (fallback) |

Delta linting: uses `git diff --unified=0` to find changed lines. Only issues on changed lines are failures (#150). Pre-existing issues are reported but ignored.

### FILE_EXISTS

Verifies files exist on disk (not just agent-reported).

| Condition | Result |
|-----------|--------|
| File exists (any size, including 0-byte like `__init__.py`) | PASS |
| File missing, but loose glob by stem finds it | PASS |
| File missing entirely | FAIL with actionable feedback |

Supports exact match, loose match by stem (≥3 chars), and fallback to test file path conventions.

**Hard criterion** — cannot be overridden by pass_threshold.

### FILE_PATTERN

Glob pattern matching for files.

| Condition | Result |
|-----------|--------|
| ≥1 file matches pattern (any size) | PASS |
| 0 files match | FAIL |

Automatically falls back to recursive glob for nested directories (e.g. `src/**/*.py`).

### NO_CRITICAL

Checks for TODO/FIXME/XXX/HACK markers.

| Condition | Result |
|-----------|--------|
| 0 markers found | PASS |
| ≥1 markers found | FAIL |
| No artifacts to check | PASS (nothing to scan) |

### FILE_CHANGED

Verifies agent actually modified specified files.

| Condition | Result |
|-----------|--------|
| Target file in `output_artifacts` | PASS |
| Target file not in `output_artifacts` | FAIL |
| No path specified + has output_artifacts | PASS |

### PATTERN_ABSENT / PATTERN_PRESENT

Regex pattern verification for bug fixes.

| Condition | Result |
|-----------|--------|
| PATTERN_ABSENT: pattern not found | PASS |
| PATTERN_ABSENT: pattern found | FAIL |
| PATTERN_PRESENT: pattern found | PASS |
| PATTERN_PRESENT: pattern not found | FAIL |
| File doesn't exist | PASS (absent) / FAIL (present) |

### CUSTOM

Fallback for unrecognized criteria.

| Condition | Result |
|-----------|--------|
| Always | WARN: "Cannot auto-verify" — manual review recommended |

Never contributes to PASS/FAIL determination.

## Semantic Rules

### Rule 1: No scope, no guess

If a criterion has no explicit scope (no `test_path`, no relevant `output_artifacts`), it emits WARN rather than assuming PASS. This prevents false confidence from vacuously passing criteria.

Examples:
- TESTS_PASS with no test files → WARN
- COVERAGE with no output_artifacts → skips --cov
- LINT with no targets → PASS (nothing to check)

### Rule 2: WARN is not PASS

`was_auto=False` criteria:
- Do not contribute to `score`
- Are listed in `result.suggestions`
- Do not affect `overall_passed` on their own

But when `passed=False` AND `was_auto=False`, the criterion IS counted as failed for `all_auto_passed`. This means a WARN with `passed=False` (e.g., COVERAGE with no scoped tests) does cause `overall_passed = False`.

### Rule 3: Each criterion is independent

No cross-criterion inference. TESTS_PASS does not imply FILE_EXISTS. FILE_PATTERN does not imply LINT. Each criterion is checked in isolation.

### Rule 4: Hard criteria cannot be overridden

The following are hard criteria — if any fails, `overall_passed = False` regardless of pass_threshold:
- `FILE_EXISTS`
- `FILE_PATTERN`
- `TESTS_PASS`
- `PATTERN_PRESENT`
- `PATTERN_ABSENT`

### Rule 5: Artifact verification is a hard gate

After all criteria are evaluated, `output_artifacts` are verified on disk. If any reported artifact file does not exist, `overall_passed = False` and `score = 0` regardless of individual criterion results.

This prevents the fundamental false positive: generator reports creating files that were never written (#234).

### Rule 6: pass_threshold interaction

When `pass_threshold` is set (0 < threshold ≤ 10):
- `score >= threshold` allows overall pass even if some soft criteria fail
- Failed soft criteria are downgraded from FAIL to WARN in feedback
- Hard criteria failures veto the threshold override

Without pass_threshold (default): all criteria must pass (strict mode).

### Rule 7: Threshold-assisted pass is not a clean pass

When `pass_threshold` lets a node pass despite having `was_auto=False` (WARN) criteria:

- The node `overall_passed = True`, but the evaluation result still contains soft criteria with `passed=False`.
- **Auto-eval handoff** must NOT report the node as fully `already verified`. The handoff metadata includes `has_warnings=True` to signal unverified criteria.
- **Evaluator prompts** should instruct downstream evaluators to investigate these WARN criteria rather than skipping them.
- Threshold-assisted pass means "good enough to proceed", not "all criteria confirmed".

This distinction matters for the orchestrator's retry/adapt decisions: a threshold-assisted pass may still warrant a follow-up evaluation if the WARN criteria involve correctness-sensitive checks (e.g., test coverage, pattern compliance).
