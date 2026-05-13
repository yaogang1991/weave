# Evaluator Criterion Semantics

This document defines the exact semantics of every evaluator criterion type —
what each checks, what constitutes pass/fail/warn, and how results combine into
an overall evaluation. This is the normative reference for all evaluator
behavior.

## Criterion Types

| Type | Hard/Soft | Auto-checkable | Summary |
|------|-----------|----------------|---------|
| `FILE_EXISTS` | Hard | Yes | Required files exist on disk |
| `FILE_PATTERN` | Hard | Yes | Files matching a glob pattern exist |
| `TESTS_PASS` | Hard | Yes | Pytest execution succeeds |
| `PATTERN_PRESENT` | Hard | Yes | Regex pattern found in file |
| `PATTERN_ABSENT` | Hard | Yes | Regex pattern absent from file |
| `TEST_FILE_EXISTS` | Hard | Yes | Agent produced test files |
| `COVERAGE` | Soft | Yes | Test coverage meets target % |
| `LINT` | Soft | Yes | No new lint issues on changed lines |
| `NO_CRITICAL` | Soft | Yes | No TODO/FIXME/HACK markers |
| `FILE_CHANGED` | Soft | Yes | Agent modified specified files |
| `CUSTOM` | Soft | No | Manual review required |

## Hard vs Soft Criteria

**Hard criteria** represent fundamental correctness checks that **must always
pass**. A failed hard criterion cannot be overridden by `pass_threshold` — the
overall evaluation result is always FAIL.

**Soft criteria** represent quality gates that can be relaxed via
`pass_threshold`. When threshold is set and the score meets the threshold,
failed soft criteria are reported as WARN instead of FAIL.

Default: when no `pass_threshold` is set (strict mode), all criteria must pass.

## Per-Criterion Semantics

### `FILE_EXISTS` (Hard)

**Checks:** Specified files exist on disk in the evaluation directory.

- **PASS:** Every candidate file found (exact path, loose glob by stem, or
  alternative test file path).
- **FAIL:** Any required file not found after all fallback searches.
- **Fallback 1:** Loose glob match by filename stem (≥3 chars).
- **Fallback 2:** Alternative test file locations (`tests/test_x.py` for
  `module/test_x.py`).
- **No candidates:** PASS with "No specific files listed" (vacuously true).

### `FILE_PATTERN` (Hard)

**Checks:** At least one file matching a glob pattern exists.

- **PASS:** One or more files match the pattern.
- **FAIL:** No files match.
- **Recursive fallback:** Single-level patterns (e.g. `dir/*.py`) automatically
  search recursively (`dir/**/*.py`) to handle nested directories.
- **No pattern specified:** PASS (skipped).

### `TESTS_PASS` (Hard)

**Checks:** Pytest execution against scoped test files.

- **PASS:** All tests pass (returncode 0).
- **FAIL:** Any test failure or execution error.
- **No test files found:** PASS with warning "tests not verified" (uncheckable
  — returned as `was_auto=False`).
- **Scoped test discovery:** Only runs tests related to `output_artifacts`
  (direct test files + inferred test files by stem matching). Prevents
  collection of leftover test files.
- **Timeout:** 60s — reports "background thread or process leak" guidance.
- **Coverage isolation:** Each evaluation gets a unique `COVERAGE_FILE` env var
  to prevent parallel node corruption.

### `PATTERN_PRESENT` (Hard)

**Checks:** A regex pattern exists in a specified file.

- **PASS:** At least one match found.
- **FAIL:** Pattern not found, or file does not exist, or invalid regex.
- **No path/pattern specified:** PASS (skipped).

### `PATTERN_ABSENT` (Hard)

**Checks:** A regex pattern is absent from a specified file.

- **PASS:** No matches found, or file does not exist (trivially absent).
- **FAIL:** Pattern still found in file, or invalid regex.
- **No path/pattern specified:** PASS (skipped).

### `TEST_FILE_EXISTS` (Hard)

**Checks:** Agent's `output_artifacts` contain at least one test file.

- **PASS:** At least one artifact matches test naming conventions
  (`test_*.py`, `*_test.py`, `*_spec.py`, or `.py` under `tests/`/`test/`).
- **FAIL:** No test files in output artifacts, or no output artifacts at all.
- **Purpose:** Prevents "no tests fake pass" — ensures agents create test files.

### `COVERAGE` (Soft)

**Checks:** Test coverage percentage against a target.

- **PASS:** Coverage ≥ target (default 80%).
- **FAIL:** Coverage < target, or tests failed and coverage report unparseable.
- **WARN (unverifiable):** Tests passed but coverage report could not be parsed
  (no TOTAL line found). Returned as `was_auto=False`.
- **No test files found:** FAIL — cannot verify coverage without scoped tests.
- **No output_artifacts:** PASS with warning — coverage target not verified.
- **Scoped coverage:** Coverage scope limited to packages inferred from
  `output_artifacts`.

### `LINT` (Soft)

**Checks:** No new lint issues on lines changed by the agent (delta lint).

- **PASS:** No new issues on changed lines, or no lint issues at all.
- **FAIL:** New lint issues found on changed lines.
- **WARN (no linter):** If neither flake8 nor ruff is installed, treated as
  uncheckable (WARN, not hard FAIL). Missing tool ≠ bad code.
- **Delta lint mechanism:**
  1. Run flake8 (or ruff fallback) on target files.
  2. Parse output into structured `LintIssue` list.
  3. Use `git diff --unified=0` to find changed line numbers.
  4. Only issues on changed lines count as failures.
  5. Pre-existing issues are reported as "IGNORED_EXISTING" but do not fail.
- **No git available:** All issues treated as failures (conservative fallback).
- **No output_artifacts:** PASS (nothing to lint).
- **Dry-run autofix:** Detects auto-fixable issues (F401/F841) without
  modifying files in-place, preventing cross-node corruption in parallel DAGs.

### `NO_CRITICAL` (Soft)

**Checks:** No `TODO`, `FIXME`, `XXX`, `HACK` markers in output artifacts.

- **PASS:** No markers found in any checked file.
- **FAIL:** Markers found in one or more artifacts.
- **No artifacts:** PASS (nothing to check).
- **Non-existent files:** Skipped silently.

### `FILE_CHANGED` (Soft)

**Checks:** Agent actually modified the specified file(s).

- **PASS:** All target files appear in `output_artifacts` (by filename match).
- **FAIL:** Target files not found in `output_artifacts`, or no artifacts at
  all.
- **No path specified:** PASS if any `output_artifacts` exist, FAIL otherwise.

### `CUSTOM` (Soft, not auto-checkable)

**Checks:** None — cannot be automatically verified.

- **Always passes** with a warning that manual review is recommended.
- **Returned as `was_auto=False`** so evaluate_stage emits WARN.

## Scoring

Each criterion contributes `10.0 / N` points when passed (where N is the total
number of criteria). Maximum score is 10.0.

```
score = sum(10.0 / N for each passed criterion)
```

## Overall Result Determination

```
if pass_threshold is None (strict mode):
    overall = all criteria passed
elif any hard criterion failed:
    overall = FAIL  (hard criteria veto threshold override)
elif score >= pass_threshold:
    overall = PASS  (soft failures downgraded to WARN)
else:
    overall = FAIL  (score below threshold)
```

## Artifact Verification (Mandatory)

After evaluation, all `output_artifacts` are verified against disk. If any
reported artifact does not exist as a file, the overall result is forced to
FAIL with score 0. This prevents false positives when a generator claims to
have created files that were never written.

## False Pass / False Fail Prevention

| Scenario | Prevention |
|----------|------------|
| No test files → auto pass | `TEST_FILE_EXISTS` criterion forces test creation |
| Leftover tests collected | Scoped test discovery from `output_artifacts` |
| Coverage file collision | Per-evaluation `COVERAGE_FILE` env var |
| Phantom artifacts | Mandatory on-disk artifact verification |
| No scoped tests → coverage fake pass | Coverage requires scoped test files |
| Lint: pre-existing issues fail | Delta lint via git diff (only new issues fail) |
| No linter → hard fail | Treated as uncheckable WARN |
| Missing files not detected | `FILE_EXISTS` checks disk, not just artifact list |
| Nested files missed by glob | Recursive glob fallback for single-level patterns |
| Pytest hangs forever | 60s timeout with actionable feedback |

## Evaluation Status

Each evaluation produces an `eval_status` that distinguishes the quality of the
pass:

| Status | Meaning |
|--------|---------|
| `CLEAN_PASS` | All criteria passed, no warnings |
| `PARTIAL_PASS` | Passed via `pass_threshold` override — soft failures downgraded to WARN |
| `WARNED` | All checked criteria passed, but some criteria are uncheckable (manual review needed) |
| `FAILED` | Overall evaluation failed |

### Threshold-Assisted Pass ≠ Clean Pass

A threshold-assisted pass (`PARTIAL_PASS`) is **not** equivalent to a clean
pass. When `pass_threshold` is set and `score >= pass_threshold` but some soft
criteria failed:

1. Failed soft criteria are reported as `WARN` instead of `FAIL` in feedback.
2. The evaluation result metadata sets `has_warnings=True`.
3. The downstream evaluator must still investigate the WARN criteria — they
   cannot be treated as fully verified.

The auto-eval handoff to the downstream evaluator reflects this distinction:

- **Clean pass** (`has_warnings=False`): header reads
  `AUTOMATED EVALUATION RESULTS (already verified)` — downstream evaluator may
  reuse these results without re-running checks.
- **Threshold-assisted pass** (`has_warnings=True`): header reads
  `AUTOMATED EVALUATION RESULTS (passed via threshold — some criteria have WARNINGS)`
  — downstream evaluator **must** investigate the specific WARN criteria further.

This prevents the auto-eval handoff from presenting a threshold pass as a fully
verified result, ensuring downstream evaluators do not skip investigating soft
criteria that failed auto-checking.

## Feedback Format

Each criterion result produces one feedback line:

- `PASS <label>: <message>` — auto-checked, passed
- `FAIL <label>: <message>` — auto-checked, failed
- `WARN <label>: <message>` — not auto-checked or downgraded by threshold

Additionally, uncheckable criteria are listed in a summary:
`WARNING: N criterion/criteria could not be automatically verified...`
