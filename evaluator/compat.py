"""
Legacy string criteria compatibility adapter.

Isolates Chinese keyword mapping and plain-text criterion parsing
so the evaluator core only operates on structured SuccessCriterion.

Part of #178 PR 4: formalize evaluation contract format.
"""
from __future__ import annotations

import json
import re

from core.models import CriterionType, SuccessCriterion


# Chinese → English keyword mapping
_CN_KEYWORD_MAP = {
    "测试": "test", "覆盖率": "coverage", "代码": "code",
    "文件": "file", "存在": "exist", "无严重": "no_critical",
    "无 bug": "no bug", "检查": "check", "通过": "pass",
    "清理": "clean",
}


def normalize_criteria(
    criteria: list[str | SuccessCriterion],
) -> list[SuccessCriterion]:
    """Parse mixed list[str | SuccessCriterion] into list[SuccessCriterion].

    SuccessCriterion instances pass through unchanged.
    Strings that are valid JSON with a 'type' key are deserialized.
    Plain strings go through legacy keyword matching.
    """
    result: list[SuccessCriterion] = []
    for c in criteria:
        if isinstance(c, SuccessCriterion):
            result.append(c)
            continue
        if isinstance(c, str) and c.startswith("{"):
            try:
                data = json.loads(c)
                if isinstance(data, dict) and "type" in data:
                    result.append(SuccessCriterion(**data))
                    continue
            except (json.JSONDecodeError, Exception):
                pass
        result.append(parse_string_criterion(c))
    return result


def parse_string_criterion(criterion: str) -> SuccessCriterion:
    """Convert a plain-text criterion string into a SuccessCriterion.

    Supports English keywords and Chinese equivalents.
    Falls back to CUSTOM for unrecognized strings.
    """
    lower = criterion.lower()
    for cn, en in _CN_KEYWORD_MAP.items():
        lower = lower.replace(cn, en)
    if "test" in lower and "pass" in lower:
        return SuccessCriterion(type=CriterionType.TESTS_PASS, description=criterion)
    if "test_file_exist" in lower or "test file exist" in lower:
        return SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS, description=criterion)
    if "coverage" in lower:
        pct = _extract_percentage(lower)
        return SuccessCriterion(
            type=CriterionType.COVERAGE,
            target=float(pct or 80),
            description=criterion,
        )
    if "lint" in lower or "clean" in lower:
        return SuccessCriterion(type=CriterionType.LINT, description=criterion)
    if "file" in lower and "exist" in lower:
        match = re.search(r"[:\s]+(.+)", lower)
        return SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path=match.group(1) if match else "",
            description=criterion,
        )
    if "no_critical" in lower or "no bug" in lower:
        return SuccessCriterion(type=CriterionType.NO_CRITICAL, description=criterion)
    return SuccessCriterion(type=CriterionType.CUSTOM, description=criterion)


def _extract_percentage(text: str) -> int | None:
    """Extract first percentage number from text (e.g., '80%')."""
    m = re.search(r"(\d+)%", text)
    return int(m.group(1)) if m else None
