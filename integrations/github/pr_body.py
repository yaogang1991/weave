"""PR body generation -- diff stat + LLM code review + execution summary."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.subprocess_runner import run_with_progress
from integrations.models import NormalizedIssue

logger = logging.getLogger(__name__)


def _format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


async def get_diff_stat(work_dir: str) -> str:
    """Run git diff --stat, trying HEAD first then origin/main...HEAD."""
    result = await asyncio.to_thread(
        run_with_progress,
        ["git", "diff", "--stat", "HEAD"],
        timeout=30,
        cwd=work_dir,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    result = await asyncio.to_thread(
        run_with_progress,
        ["git", "diff", "--stat", "origin/main...HEAD"],
        timeout=30,
        cwd=work_dir,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


async def get_full_diff(work_dir: str, max_chars: int = 8000) -> str:
    """Run git diff for LLM review, trying HEAD first then origin/main...HEAD."""
    result = await asyncio.to_thread(
        run_with_progress,
        ["git", "diff", "HEAD"],
        timeout=60,
        cwd=work_dir,
    )
    if result.returncode != 0 or not result.stdout.strip():
        result = await asyncio.to_thread(
            run_with_progress,
            ["git", "diff", "origin/main...HEAD"],
            timeout=60,
            cwd=work_dir,
        )
    if result.returncode != 0:
        return ""
    text = result.stdout.strip()
    return text[:max_chars] if len(text) > max_chars else text


async def generate_llm_review(diff_text: str, llm_config: Any) -> str:
    """Generate code review from diff via LLM. Returns empty string on failure."""
    if not diff_text:
        return ""
    try:
        from core.config import LLMConfig
        from core.llm_client import LLMClient

        config = LLMConfig(
            api_key=llm_config.api_key,
            model=getattr(llm_config, "model", "claude-sonnet-4-6"),
            provider=getattr(llm_config, "provider", "anthropic"),
            base_url=getattr(llm_config, "base_url", None),
        )
        client = LLMClient(config)
        prompt = (
            "Review this code diff. Provide:\n"
            "1. Brief summary of changes (2-3 sentences)\n"
            "2. Potential risks or issues\n"
            "3. Test coverage suggestions\n\n"
            f"```\n{diff_text}\n```"
        )
        response = await asyncio.to_thread(
            client.call,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            max_tokens_override=500,
        )
        content = response.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        return content.strip()[:4000]
    except Exception:
        logger.exception("LLM review generation failed")
        return ""


def _pr_prefix(labels: list[str]) -> str:
    """Derive summary prefix from issue labels."""
    if "enhancement" in labels:
        return "Feat"
    return "Fix"


async def generate_pr_body(
    work_dir: str,
    issue: NormalizedIssue,
    llm_config: Any = None,
    execution_summary: dict[str, Any] | None = None,
) -> str:
    """Generate complete PR body with diff stat, execution summary, and optional LLM review."""
    diff_stat = await get_diff_stat(work_dir)
    prefix = _pr_prefix(issue.labels)

    review_section = ""
    if llm_config:
        full_diff = await get_full_diff(work_dir)
        review = await generate_llm_review(full_diff, llm_config)
        if review:
            review_section = f"\n## Code Review\n{review}\n"

    execution_section = ""
    if execution_summary:
        total = execution_summary.get("nodes_total", 0)
        completed = execution_summary.get("nodes_completed", 0)
        tokens_in = execution_summary.get("tokens_in", 0)
        tokens_out = execution_summary.get("tokens_out", 0)
        duration = execution_summary.get("duration_sec", 0)
        execution_section = (
            f"\n## Execution\n"
            f"- Nodes: {completed}/{total} completed\n"
            f"- Tokens: {tokens_in}in / {tokens_out}out\n"
            f"- Duration: {_format_duration(duration)}\n"
        )

    results_section = ""
    if execution_summary:
        test_summary = execution_summary.get("test_summary")
        lint_summary = execution_summary.get("lint_summary")
        if test_summary or lint_summary:
            parts = []
            if test_summary:
                parts.append(f"- Tests: {test_summary}")
            if lint_summary:
                parts.append(f"- Lint: {lint_summary}")
            results_section = f"\n## Results\n" + "\n".join(parts) + "\n"

    return (
        f"## Summary\n{prefix} #{issue.number}: {issue.title}\n\n"
        f"## Changes\n```\n{diff_stat}\n```\n"
        f"{execution_section}"
        f"{results_section}"
        f"{review_section}"
        f"\n## Test plan\n- [ ] python -m pytest -v --tb=short\n\n"
        f"Fixes #{issue.number}\n"
    )
