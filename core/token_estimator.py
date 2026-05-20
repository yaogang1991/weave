"""
Token estimation layer for M4.6 pre-execution planning.

Wraps Anthropic's count_tokens() API with heuristic fallback.
Provides single-node and batch estimation with caching.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import anthropic
from pydantic import BaseModel, Field

from core.config import TokenEstimationConfig
from orchestrator.llm_utils import estimate_tokens as heuristic_estimate

if TYPE_CHECKING:
    from core.dag_models import DAGNode

logger = logging.getLogger(__name__)


class NodeTokenContext(BaseModel):
    """Full context that will be sent to the LLM for a node."""

    system_prompt: str = ""
    task_description: str = ""
    dependency_artifacts: list[str] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    agent_type: str = ""


class TokenEstimateResult(BaseModel):
    """Result of token estimation for a single node."""

    node_id: str
    estimated_tokens: int
    estimation_method: str  # "api" | "heuristic"
    breakdown: dict[str, int] = Field(default_factory=dict)
    cached: bool = False


class TokenEstimator:
    """Estimates token counts for planned DAG nodes.

    Uses Anthropic count_tokens() API when available,
    falls back to char/3.5 heuristic on failure.
    """

    def __init__(
        self,
        config: TokenEstimationConfig,
        client: anthropic.Anthropic | None = None,
        model: str = "claude-sonnet-4-6",
    ):
        self._config = config
        self._client = client
        self._model = model
        self._cache: dict[str, tuple[int, float]] = {}

    async def estimate_node_tokens(
        self,
        node_id: str,
        context: NodeTokenContext,
    ) -> TokenEstimateResult:
        cache_key = self._cache_key(context)
        if cache_key in self._cache:
            tokens, ts = self._cache[cache_key]
            if time.time() - ts < self._config.cache_ttl_seconds:
                return TokenEstimateResult(
                    node_id=node_id,
                    estimated_tokens=tokens,
                    estimation_method="api",
                    breakdown=self._heuristic_breakdown(context),
                    cached=True,
                )

        if self._config.enabled and self._client:
            try:
                tokens = await self._count_tokens_api(context)
                self._cache[cache_key] = (tokens, time.time())
                return TokenEstimateResult(
                    node_id=node_id,
                    estimated_tokens=tokens,
                    estimation_method="api",
                    breakdown=self._heuristic_breakdown(context),
                )
            except Exception as e:
                logger.warning(
                    "count_tokens API failed for node %s: %s", node_id, e,
                )
                if not self._config.fallback_to_heuristic:
                    raise

        tokens = self._heuristic_estimate(context)
        return TokenEstimateResult(
            node_id=node_id,
            estimated_tokens=tokens,
            estimation_method="heuristic",
            breakdown=self._heuristic_breakdown(context),
        )

    async def estimate_nodes_batch(
        self,
        nodes: list[tuple[str, NodeTokenContext]],
    ) -> list[TokenEstimateResult]:
        semaphore = asyncio.Semaphore(self._config.max_estimation_concurrency)

        async def _limited(nid: str, ctx: NodeTokenContext) -> TokenEstimateResult:
            async with semaphore:
                return await self.estimate_node_tokens(nid, ctx)

        results = await asyncio.gather(
            *[_limited(nid, ctx) for nid, ctx in nodes],
            return_exceptions=True,
        )

        final: list[TokenEstimateResult] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                nid, ctx = nodes[i]
                logger.warning("Token estimation failed for %s: %s", nid, r)
                final.append(TokenEstimateResult(
                    node_id=nid,
                    estimated_tokens=self._heuristic_estimate(ctx),
                    estimation_method="heuristic",
                ))
            else:
                final.append(r)
        return final

    async def _count_tokens_api(self, context: NodeTokenContext) -> int:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": context.task_description},
        ]
        if context.dependency_artifacts:
            dep_text = (
                "Dependency artifacts (file paths):\n"
                + "\n".join(f"- {p}" for p in context.dependency_artifacts)
            )
            messages.append({
                "role": "assistant",
                "content": "Understood, I will reference these files.",
            })
            messages.append({"role": "user", "content": dep_text})

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if context.system_prompt:
            kwargs["system"] = context.system_prompt
        if context.tools:
            kwargs["tools"] = context.tools

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: self._client.messages.count_tokens(**kwargs),
        )
        return result.input_tokens

    def _heuristic_estimate(self, context: NodeTokenContext) -> int:
        total = heuristic_estimate(context.system_prompt)
        total += heuristic_estimate(context.task_description)
        for dep in context.dependency_artifacts:
            total += heuristic_estimate(dep)
        for tool in context.tools:
            total += heuristic_estimate(str(tool))
        return total

    def _heuristic_breakdown(self, context: NodeTokenContext) -> dict[str, int]:
        return {
            "system": heuristic_estimate(context.system_prompt),
            "task": heuristic_estimate(context.task_description),
            "deps": sum(
                heuristic_estimate(d) for d in context.dependency_artifacts
            ),
            "tools": sum(
                heuristic_estimate(str(t)) for t in context.tools
            ),
        }

    @staticmethod
    def _cache_key(context: NodeTokenContext) -> str:
        tool_content = json.dumps(context.tools, sort_keys=True)
        content = (
            f"{context.system_prompt}|{context.task_description}"
            f"|{'|'.join(context.dependency_artifacts)}"
            f"|{tool_content}"
        )
        return hashlib.md5(content.encode()).hexdigest()

    def clear_cache(self) -> None:
        self._cache.clear()


def build_node_context(
    node: DAGNode,
    agent_prompts: dict[str, str],
    tool_definitions: list[dict] | None = None,
    dependency_file_paths: list[str] | None = None,
) -> NodeTokenContext:
    """Construct NodeTokenContext from a planned DAGNode."""
    system_prompt = agent_prompts.get(node.agent_type, "")
    return NodeTokenContext(
        system_prompt=system_prompt,
        task_description=node.task_description,
        dependency_artifacts=dependency_file_paths or [],
        tools=tool_definitions or [],
        agent_type=node.agent_type,
    )
