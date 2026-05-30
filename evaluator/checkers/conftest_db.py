"""
Conftest database initialization checker: CONFTEST_DB_INIT.

Validates that conftest.py correctly imports all model classes and calls
create_all() so that Base.metadata contains all required tables.

Addresses #1001: generator-produced conftest.py consistently fails to
initialize database tables (100% failure rate across 3 E2E test rounds).
"""
from __future__ import annotations

import re
from pathlib import Path

from core.models import CriterionType, SuccessCriterion
from evaluator.models import CheckResult, CheckSeverity, EvaluationContext


class ConftestDbChecker:
    """Validates conftest.py database initialization patterns."""

    # Patterns that indicate correct model importing
    _MODEL_IMPORT_RE = re.compile(
        r"from\s+[\w.]*models[\w.]*\s+import\s+.*\w",
    )
    _STAR_IMPORT_RE = re.compile(
        r"from\s+[\w.]*models[\w.]*\s+import\s+\*",
    )
    _CREATE_ALL_RE = re.compile(
        r"(?:Base\.metadata|metadata)\.create_all\s*\(",
    )
    # Patterns that indicate incorrect setup
    _BARE_BASE_IMPORT_RE = re.compile(
        r"from\s+sqlalchemy\.orm\s+import\s+.*Base",
    )
    _DECLARATIVE_BASE_RE = re.compile(
        r"declarative_base\s*\(\s*\)",
    )

    def check(
        self,
        criterion: SuccessCriterion,
        context: EvaluationContext,
    ) -> CheckResult:
        ct = criterion.type
        if ct != CriterionType.CONFTEST_DB_INIT:
            return CheckResult(
                passed=False,
                message=f"Unhandled criterion type: {ct}",
            )
        return self._check_conftest(criterion, context)

    def _check_conftest(
        self,
        crit: SuccessCriterion,
        context: EvaluationContext,
    ) -> CheckResult:
        work_dir = context.work_dir
        conftest_path = self._find_conftest(work_dir)

        if conftest_path is None:
            return CheckResult(
                passed=True,
                message="No conftest.py found — check skipped",
                severity=CheckSeverity.WARNING,
            )

        content = conftest_path.read_text(encoding="utf-8", errors="replace")
        issues: list[str] = []

        # Check 1: create_all() must be called
        if not self._CREATE_ALL_RE.search(content):
            issues.append("create_all() not called in conftest.py")

        # Check 2: models must be imported (not just bare Base)
        has_model_import = bool(
            self._MODEL_IMPORT_RE.search(content)
            or self._STAR_IMPORT_RE.search(content),
        )
        has_bare_base = bool(self._BARE_BASE_IMPORT_RE.search(content))
        has_declarative_base = bool(self._DECLARATIVE_BASE_RE.search(content))

        if not has_model_import:
            if has_bare_base or has_declarative_base:
                issues.append(
                    "conftest.py imports Base from sqlalchemy.orm instead of "
                    "from the project's models module — Base.metadata will be empty",
                )
            elif not self._has_any_db_import(content):
                issues.append(
                    "No model imports found in conftest.py — "
                    "Base.metadata.create_all() will create zero tables",
                )

        # Check 3: verify models module exists and exports Base + models
        models_file = self._find_models_module(work_dir)
        if models_file is not None:
            models_content = models_file.read_text(
                encoding="utf-8", errors="replace",
            )
            model_classes = [
                name for name in re.findall(
                    r"class\s+(\w+)\s*\([^)]*Base[^)]*\)",
                    models_content,
                )
                if name != "Base"
            ]
            if model_classes:
                imported_symbols = self._extract_imported_symbols(content)
                if has_model_import:
                    imported_models = [
                        m for m in model_classes
                        if m in imported_symbols
                    ]
                    if (
                        not imported_models
                        and not self._STAR_IMPORT_RE.search(content)
                    ):
                        issues.append(
                            f"conftest.py imports from models but none of the "
                            f"actual model classes ({model_classes}) — "
                            f"Base.metadata will be empty",
                        )

        if issues:
            return CheckResult(
                passed=False,
                message="; ".join(issues),
            )

        return CheckResult(
            passed=True,
            message="conftest.py database initialization looks correct",
        )

    @staticmethod
    def _has_any_db_import(content: str) -> bool:
        """Check if conftest imports from a non-sqlalchemy DB module."""
        return bool(re.search(
            r"from\s+(?!sqlalchemy)[\w.]+\s+import\s+.*(?:Base|engine|session)",
            content,
        ))

    def _find_conftest(self, work_dir: Path) -> Path | None:
        candidates = [
            work_dir / "tests" / "conftest.py",
            work_dir / "conftest.py",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    def _find_models_module(self, work_dir: Path) -> Path | None:
        candidates = [
            work_dir / "models.py",
            work_dir / "models" / "__init__.py",
            work_dir / "app" / "models.py",
            work_dir / "app" / "models" / "__init__.py",
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    def _extract_imported_symbols(self, content: str) -> set[str]:
        symbols: set[str] = set()
        for match in re.finditer(
            r"from\s+[\w.]+\s+import\s+(.+)",
            content,
        ):
            import_list = match.group(1)
            for part in import_list.split(","):
                name = part.strip().split(" as ")[0].strip()
                if name and name != "*":
                    symbols.add(name)
        return symbols
