"""PR body generation -- diff stat + LLM code review + template."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.subprocess_runner import run_with_progress
from integrations.models import NormalizedIssue

logger = logging.getLogger(__name__)


async def get_diff_stat(work_dir: str) -> str:
    """Run git diff --stat HEAD in work_dir."""
    result = await asyncio.to_thread(
        run_with_progress,
        ["git", "diff", "--stat", "HEAD"],
        timeout=30,
        cwd=work_dir,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


async def get_full_diff(work_dir: str, max_chars: int = 15000) -> str:
    """Run git diff HEAD for LLM review, truncated."""
    result = await asyncio.to_thread(
        run_with_progress,
        ["git", "diff", "HEAD"],
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


async def generate_pr_body(
    work_dir: str,
    issue: NormalizedIssue,
    llm_config: Any = None,
) -> str:
    """Generate complete PR body with diff stat, optional LLM review, and template."""
    diff_stat = await get_diff_stat(work_dir)

    review_section = ""
    if llm_config:
        full_diff = await get_full_diff(work_dir)
        review = await generate_llm_review(full_diff, llm_config)
        if review:
            review_section = f"\n## Code Review\n{review}\n"

    return (
        f"## Summary\nFix #{issue.number}: {issue.title}\n\n"
        f"## Changes\n```\n{diff_stat}\n```\n"
        f"{review_section}"
        f"\n## Test plan\n- [ ] python -m pytest -v --tb=short\n\n"
        f"Fixes #{issue.number}\n"
    )
