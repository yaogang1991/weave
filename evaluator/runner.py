"""Test, lint, coverage, and import execution helpers.

Extracted from EvaluatorEngine for maintainability (#440).
These functions are stateless — all context is passed as arguments.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from evaluator.lint.parser import LintIssue, parse_flake8_output, get_changed_lines

logger = logging.getLogger(__name__)


def safe_eval_id(eval_id: str) -> str:
    """Sanitize eval_id for use as a filename component."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", eval_id)


def isolated_env(eval_id: str = "", work_dir: Path | None = None) -> dict[str, str]:
    """Build env with a unique COVERAGE_FILE to prevent parallel node contention (#260).

    Uses an absolute path so that coverage data is written to the work
    directory regardless of the subprocess cwd.
    """
    env = os.environ.copy()
    if eval_id:
        sid = safe_eval_id(eval_id)
        if work_dir:
            env["COVERAGE_FILE"] = str(work_dir / f".coverage.{sid}")
        else:
            env["COVERAGE_FILE"] = f".coverage.{sid}"
    return env


def find_test_files(
    output_artifacts: list[str],
    work_dir: Path,
) -> list[str]:
    """Find test files relevant to the current artifacts.

    1. Test files already in output_artifacts.
    2. Test files matching source artifact names (e.g. parser.py -> test_parser.py).
    This prevents pytest from collecting leftover test files from previous runs (#249).
    """
    test_files: list[str] = []

    # Direct test files from artifacts
    for a in output_artifacts:
        name = Path(a).name.lower()
        if "test" in name and name.endswith(".py"):
            full = work_dir / a if not Path(a).is_absolute() else Path(a)
            if full.exists():
                test_files.append(a)

    # Infer test files from source artifact stems
    source_stems: list[str] = []
    for a in output_artifacts:
        name = Path(a).name
        lower = name.lower()
        if lower.endswith(".py") and "test" not in lower:
            source_stems.append(Path(a).stem)

    if source_stems:
        # Search for test files in common locations
        search_dirs = [work_dir, work_dir / "tests"]
        for search_dir in search_dirs:
            if not search_dir.is_dir():
                continue
            for tf in search_dir.glob("test_*.py"):
                stem = tf.stem  # e.g. "test_parser"
                module_name = stem[5:]  # strip "test_"
                if module_name in source_stems:
                    rel = str(tf.relative_to(work_dir)) if tf.is_relative_to(work_dir) else str(tf)
                    if rel not in test_files:
                        test_files.append(rel)

    # Fallback: when no test files found from artifacts, discover all test
    # files in the project to avoid false PASS (#598, #599).
    # Guard: only fallback when artifacts were actually provided (#605).
    if not test_files and output_artifacts:
        for search_dir in [work_dir / "tests", work_dir]:
            if not search_dir.is_dir():
                continue
            for tf in search_dir.glob("test_*.py"):
                rel = (
                    str(tf.relative_to(work_dir))
                    if tf.is_relative_to(work_dir)
                    else str(tf)
                )
                if rel not in test_files:
                    test_files.append(rel)
            if test_files:
                break  # Found tests in first available directory

    return test_files


def detect_shadowing_test_inits(work_dir: Path) -> list[str]:
    """Detect __init__.py in tests/<pkg>/ that shadows root <pkg>/ (#221).

    Returns a list of diagnostic messages instead of deleting files.
    Generator agents sometimes create ``tests/configlib/__init__.py``
    which shadows the root ``configlib/`` package, causing
    ModuleNotFoundError when pytest tries to import the package.
    """
    warnings: list[str] = []
    tests_dir = work_dir / "tests"
    if not tests_dir.is_dir():
        return warnings
    for init_file in tests_dir.glob("*/__init__.py"):
        pkg_name = init_file.parent.name
        root_pkg = work_dir / pkg_name / "__init__.py"
        if root_pkg.is_file():
            warnings.append(
                f"tests/{pkg_name}/__init__.py shadows root {pkg_name}/ package"
            )
            logger.warning(
                "Shadowing detected: tests/%s/__init__.py shadows %s/ package",
                pkg_name, pkg_name,
            )
    return warnings


def run_tests(
    work_dir: Path,
    test_path: str | list[str] | None = None,
    eval_id: str = "",
) -> tuple[bool, str]:
    """Run pytest with a fixed command. Never executes arbitrary commands."""
    # Detect __init__.py in test subdirs that shadow project packages (#221).
    # Report as diagnostic warning rather than deleting files.
    shadow_warnings = detect_shadowing_test_inits(work_dir)
    try:
        cmd = [sys.executable, "-m", "pytest", "-v", "--tb=short"]
        if test_path:
            if isinstance(test_path, list):
                cmd.extend(str(work_dir / t) if not Path(t).is_absolute() else t for t in test_path)
            else:
                cmd.append(test_path)
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=180,
            cwd=str(work_dir) if work_dir.is_dir() else None,
            env=isolated_env(eval_id, work_dir),
        )
        passed = result.returncode == 0
        if passed:
            if shadow_warnings:
                return passed, (
                    "Tests passed\n\nWARNING: Shadowing test __init__.py files detected "
                    "(may cause ModuleNotFoundError):\n"
                    + "\n".join(f"  - {w}" for w in shadow_warnings)
                )
            return passed, "Tests passed"
        # Extract specific failure lines for actionable feedback
        failure_lines = []
        for line in result.stdout.split("\n"):
            if any(kw in line for kw in ("FAILED", "AssertionError", "Error:", "error")):
                failure_lines.append(line)
        detail = "\n".join(failure_lines[-20:]) if failure_lines else result.stdout[-500:]
        msg = f"Tests failed:\n{detail}"
        if shadow_warnings:
            msg += (
                "\n\nWARNING: Shadowing test __init__.py files detected "
                "(may cause ModuleNotFoundError):\n"
                + "\n".join(f"  - {w}" for w in shadow_warnings)
            )
        return passed, msg
    except subprocess.TimeoutExpired:
        return False, (
            "Tests timed out after 180s — likely a background thread or process leak. "
            "Ensure all threads use daemon=True and add proper cleanup in test teardown "
            "(e.g. @pytest.fixture with yield + stop() call)."
        )
    except FileNotFoundError:
        return False, "pytest not installed"
    except Exception as e:
        return False, f"Test execution error: {e}"


def run_lint(
    targets: list[str],
    work_dir: Path,
    auto_format_before_eval: bool = False,
) -> tuple[bool, str, list[str], list[str], list[str], list[str]]:
    """Dry-run autofix then delta-lint resolved target files.

    Returns (passed, message, autofixed_files, auto_formatted_files,
             lint_new_issues, lint_all_issues).
    """
    autofixed_files: list[str] = []
    auto_formatted_files: list[str] = []
    lint_new_issues: list[str] = []
    lint_all_issues: list[str] = []

    resolved = []
    for t in targets:
        p = work_dir / t
        if p.is_file() and p.suffix == ".py":
            resolved.append(str(p))
        elif p.is_dir():
            for f in p.glob("*.py"):
                resolved.append(str(f))
        elif Path(t).is_file() and Path(t).suffix == ".py":
            resolved.append(str(Path(t)))
    if not resolved:
        return (
            True, "No targets to lint", autofixed_files,
            auto_formatted_files, lint_new_issues, lint_all_issues,
        )

    # Auto-fix unused imports/variables via autoflake --in-place (#283).
    autofixed_files = auto_fix_unused(resolved, work_dir)
    if autofixed_files:
        logger.info(
            "autoflake removed unused imports from %d file(s): %s",
            len(autofixed_files), autofixed_files,
        )

    # Apply autopep8 in-place formatting to fix whitespace issues (#206).
    formatted_files = auto_format_apply(resolved, work_dir, auto_format_before_eval)
    auto_formatted_files = formatted_files
    if formatted_files:
        logger.info(
            "autopep8 formatted %d file(s): %s",
            len(formatted_files), formatted_files,
        )

    # Run flake8 (or ruff fallback)
    lint_stdout = ""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "flake8"] + resolved
            + ["--max-line-length=100"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        lint_stdout = result.stdout
    except FileNotFoundError:
        try:
            result = subprocess.run(
                ["ruff", "check"] + resolved,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
            lint_stdout = result.stdout
        except FileNotFoundError:
            return (
                False, "No linter available (install flake8 or ruff)",
                autofixed_files, auto_formatted_files, lint_new_issues, lint_all_issues,
            )
    except Exception as e:
        return (
            False, f"Lint error: {e}",
            autofixed_files, auto_formatted_files, lint_new_issues, lint_all_issues,
        )

    if result.returncode == 0:
        msg = "Lint clean"
        if autofixed_files:
            msg = (
                f"Autoflake auto-fixed: {', '.join(autofixed_files)}"
                f"\n{msg}"
            )
        return True, msg, autofixed_files, auto_formatted_files, lint_new_issues, lint_all_issues

    # Parse all lint issues
    all_issues = parse_flake8_output(lint_stdout)
    if not all_issues:
        # Could not parse (unexpected format) — treat as before
        msg = f"Lint issues:\n{lint_stdout[:500]}"
        if autofixed_files:
            msg = (
                f"Autoflake auto-fixed: {', '.join(autofixed_files)}"
                f"\n{msg}"
            )
        return False, msg, autofixed_files, auto_formatted_files, lint_new_issues, lint_all_issues

    # Delta lint: use git diff to find changed lines (#150)
    rel_targets = []
    for r in resolved:
        try:
            rel_targets.append(str(Path(r).relative_to(work_dir)))
        except ValueError:
            rel_targets.append(Path(r).name)

    changed = get_changed_lines(rel_targets, work_dir)

    # Store issues for regression tracking (#151)
    lint_all_issues = [
        f"{i.path}:{i.line}:{i.code}" for i in all_issues
    ]

    new_issues: list[LintIssue] = []

    if changed:
        # Only issues on changed lines are "new"
        existing_issues: list[LintIssue] = []
        for issue in all_issues:
            issue_path = issue.path
            # Normalize to relative for comparison
            try:
                issue_path = str(
                    Path(issue.path).relative_to(work_dir),
                )
            except ValueError:
                pass
            changed_lines = changed.get(issue_path, set())
            if issue.line in changed_lines:
                new_issues.append(issue)
            else:
                existing_issues.append(issue)

        lint_new_issues = [
            f"{i.path}:{i.line}:{i.code}" for i in new_issues
        ]

        if not new_issues:
            msg = "Lint clean (all issues are pre-existing)"
        else:
            new_lines = [
                f"  - {i.path}:{i.line} {i.code} {i.message}"
                for i in new_issues
            ]
            msg = (
                f"Lint failed: {len(new_issues)} new issue(s)"
            )
            if existing_issues:
                msg += (
                    f", {len(existing_issues)} existing ignored"
                )
            msg += "\nNEW:\n" + "\n".join(new_lines)
            if existing_issues:
                existing_lines = [
                    f"  - {i.path}:{i.line} {i.code} {i.message}"
                    for i in existing_issues[:10]
                ]
                msg += (
                    "\nIGNORED_EXISTING:\n"
                    + "\n".join(existing_lines)
                )
                if len(existing_issues) > 10:
                    msg += (
                        f"\n  ... and {len(existing_issues) - 10} more"
                    )
    else:
        # No git diff available — all issues are potential failures
        new_issues = all_issues
        lines = [
            f"  - {i.path}:{i.line} {i.code} {i.message}"
            for i in all_issues
        ]
        msg = "Lint issues (delta unavailable):\n" + "\n".join(lines)
        lint_new_issues = lint_all_issues

    if autofixed_files:
        msg = (
            f"Autoflake auto-fixed: {', '.join(autofixed_files)}"
            f"\n{msg}"
        )

    return (
        len(new_issues) == 0, msg, autofixed_files,
        auto_formatted_files, lint_new_issues, lint_all_issues,
    )


def auto_fix_unused(resolved: list[str], work_dir: Path) -> list[str]:
    """Remove unused imports and variables via autoflake --in-place (#283).

    Returns list of relative paths of files that were actually modified.
    Silently skips if autoflake is not installed or times out.
    """
    if not resolved:
        return []

    before: dict[str, str] = {}
    for fpath in resolved:
        try:
            before[fpath] = Path(fpath).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    try:
        subprocess.run(
            [
                sys.executable, "-m", "autoflake",
                "--in-place",
                "--remove-all-unused-imports",
                "--remove-unused-variables",
            ] + resolved,
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        logger.debug("autoflake not installed, skipping unused import fix")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("autoflake timed out, skipping unused import fix")
        return []
    except Exception as exc:
        logger.warning("autoflake error: %s", exc)
        return []

    changed: list[str] = []
    for fpath, content_before in before.items():
        try:
            content_after = Path(fpath).read_text(encoding="utf-8", errors="replace")
            if content_after != content_before:
                try:
                    rel = str(Path(fpath).relative_to(work_dir))
                except ValueError:
                    rel = Path(fpath).name
                changed.append(rel)
        except OSError:
            pass
    return changed


def auto_format_apply(resolved: list[str], work_dir: Path, enabled: bool = False) -> list[str]:
    """Apply autopep8 in-place formatting to fix whitespace issues (#206).

    Returns list of relative paths of files that were actually modified.
    Silently skips if autopep8 is not installed or times out.
    Disabled by default; requires enabled=True.
    """
    if not enabled or not resolved:
        return []

    before: dict[str, str] = {}
    for fpath in resolved:
        try:
            before[fpath] = Path(fpath).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "autopep8",
                "--in-place",
                "--select=E203,E303,W291,W293,W605,E302",
            ] + resolved,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.debug(
                "autopep8 skipped (rc=%d): %s",
                result.returncode, result.stderr[-500:],
            )
            return []
    except FileNotFoundError:
        logger.debug("autopep8 not installed, skipping auto-format")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("autopep8 timed out, skipping auto-format")
        return []
    except Exception as exc:
        logger.warning("autopep8 error: %s", exc)
        return []

    changed: list[str] = []
    for fpath, content_before in before.items():
        try:
            content_after = Path(fpath).read_text(encoding="utf-8", errors="replace")
            if content_after != content_before:
                try:
                    rel = str(Path(fpath).relative_to(work_dir))
                except ValueError:
                    rel = Path(fpath).name
                changed.append(rel)
        except OSError:
            pass
    return changed


def import_smoke_test(
    artifacts: list[str],
    eval_dir: Path,
) -> list[tuple[str, str]]:
    """Try importing each generated .py source file.

    Returns a list of (file_path, error_message) for files that fail
    to import.  Only checks source files (skips test files and
    non-Python files).  Catches ImportError, NameError, SyntaxError,
    and other module-load-time failures that flake8 cannot detect.
    """
    errors: list[tuple[str, str]] = []
    for art in artifacts:
        p = Path(art)
        if p.suffix != ".py":
            continue
        # Skip test files — they may have test-specific imports.
        parts = p.parts
        if (
            any(part in ("tests", "test") for part in parts[:-1])
            or p.name.startswith("test_")
        ):
            continue
        full = p if p.is_absolute() else eval_dir / p
        if not full.is_file():
            continue
        # Convert file path to module path: a/b/c.py -> a.b.c
        rel = p if not p.is_absolute() else p.relative_to(eval_dir)
        module = str(rel.with_suffix("")).replace("/", ".").replace(
            "\\", ".",
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", f"import {module}"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(eval_dir),
            )
            if result.returncode != 0:
                err = result.stderr.strip().split("\n")[-1]
                errors.append((art, err))
        except subprocess.TimeoutExpired:
            errors.append((art, "import timed out (10s)"))
        except Exception as exc:
            errors.append((art, str(exc)))
    return errors


def check_coverage(
    work_dir: Path,
    target: int,
    output_artifacts: list[str] | None = None,
    eval_id: str = "",
) -> tuple[bool, str, bool]:
    """Check test coverage against target percentage.

    Returns (passed, message, was_auto_verified).
    When coverage output cannot be parsed, returns was_auto_verified=False
    so the caller emits WARN instead of PASS (#152).
    """
    try:
        cmd = [
            sys.executable, "-m", "pytest", "-v",
            "--tb=short", "--cov-report=term-missing",
        ]

        # Scope test collection to relevant test files only (#249).
        test_targets: list[str] | None = None
        if output_artifacts:
            test_targets = find_test_files(output_artifacts, work_dir)

        if not test_targets and output_artifacts:
            return (
                False,
                "No test files found for coverage check — "
                "cannot verify coverage without scoped tests.",
                False,
            )

        if test_targets:
            for t in test_targets:
                p = Path(t)
                cmd.append(str(work_dir / p) if not p.is_absolute() else str(p))

        # Limit coverage scope to packages inferred from output artifacts
        if output_artifacts:
            cov_targets = set()
            for a in output_artifacts:
                parts = Path(a).parts
                if len(parts) > 1:
                    cov_targets.add(str(Path(*parts[:2])))
                if cov_targets:
                    for t in cov_targets:
                        cmd.append(f"--cov={t}")
                else:
                    # No package-level targets found; scope to work_dir
                    cmd.append(f"--cov={work_dir}")
        else:
            # output_artifacts empty: run tests without coverage to avoid
            # scanning historical files that may have import errors (#165).
            cmd = [sys.executable, "-m", "pytest", "-v", "--tb=short"]

        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
            cwd=str(work_dir) if work_dir.is_dir() else None,
            env=isolated_env(eval_id, work_dir),
        )

        # Parse TOTAL line via regex — handles both compact and wide formats:
        #   TOTAL  123  4  97%
        #   TOTAL  123  4  5  97.5%
        for line in result.stdout.split("\n"):
            stripped = line.strip()
            if stripped.startswith("TOTAL"):
                m = re.search(r"(\d+(?:\.\d+)?)%", stripped)
                if m:
                    cov = float(m.group(1))
                    return (
                        cov >= target,
                        f"Coverage: {cov}% (target: {target}%)",
                        True,
                    )

        # Could not parse TOTAL line — coverage target is unverifiable.
        stdout_tail = result.stdout[-500:] if result.stdout else ""
        stderr_tail = result.stderr[-500:] if result.stderr else ""
        if result.returncode == 0:
            if not output_artifacts:
                return True, (
                    f"Coverage could not be verified: no output_artifacts "
                    f"to scope coverage. Tests passed but coverage target "
                    f"{target}% was not verified."
                ), False
            return True, (
                f"Coverage could not be parsed; tests passed but coverage "
                f"target {target}% was not verified. "
                f"stdout_tail=...{stdout_tail} "
                f"stderr_tail=...{stderr_tail}"
            ), False
        return False, (
            f"Tests failed and coverage report could not be parsed. "
            f"stdout_tail=...{stdout_tail} "
            f"stderr_tail=...{stderr_tail}"
        ), True
    except subprocess.TimeoutExpired:
        return False, (
            "Coverage check timed out after 60s — likely a background thread leak. "
            "Use daemon threads and proper test teardown."
        ), True
    except Exception as e:
        return False, f"Coverage check error: {e}", True


def check_no_critical(path: Path, artifacts: list[str] | None = None) -> tuple[bool, str]:
    """Check files for TODO/FIXME/XXX/HACK markers."""
    targets = artifacts or []
    if not targets:
        return True, "No artifacts to check"
    issues = []
    for fname in targets:
        fpath = path / fname
        if not fpath.exists():
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            for marker in ["TODO", "FIXME", "XXX", "HACK"]:
                if marker in content:
                    issues.append(f"{fname}: {marker}")
        except Exception:
            pass
    passed = len(issues) == 0
    return passed, f"Found markers: {issues}" if issues else "No critical markers found"


def check_files_exist(files: list[str], base: Path) -> tuple[bool, str]:
    """Check that all files exist under base directory."""
    missing = [f for f in files if not (base / f).exists()]
    passed = len(missing) == 0
    return passed, f"Missing: {missing}" if missing else "All required files present"


def check_files_exist_loose(patterns: list[str], base: Path) -> tuple[bool, str]:
    """Loose file matching: exact, glob by name, or substring match."""
    missing = []
    for pattern in patterns:
        # 1. Exact match
        if (base / pattern).exists():
            continue
        # 2. Glob by filename
        name = Path(pattern).name
        if list(base.glob(f"**/{name}")):
            continue
        # 3. Substring match (without extension)
        stem = Path(pattern).stem
        if len(stem) >= 3 and list(base.glob(f"**/*{stem}*")):
            continue
        missing.append(pattern)
    passed = len(missing) == 0
    return passed, f"Missing: {missing}" if missing else "Required files found (loose match)"


def extract_percentage(text: str) -> int | None:
    """Extract first percentage value from text."""
    match = re.search(r'(\d+)%', text)
    return int(match.group(1)) if match else None
