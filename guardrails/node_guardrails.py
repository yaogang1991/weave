"""Node-level guardrails for DAG execution boundary control (M6.2).

Provides deterministic pre-check (agent_type gate + workspace boundary +
denied commands) and post-check (protected paths) for external backend
execution paths. Blocked nodes do not consume retry budget.

Usage::

    guard = NodeGuardrails(config, project_dir="/project")
    result = guard.pre_check(node, workspace_path)
    if result.is_blocked:
        raise GuardrailBlockedException(result.reason, phase="pre")
"""
from __future__ import annotations

import logging
import os
from fnmatch import fnmatch

from core.dag_models import DAGNode
from core.project_config import GuardrailsConfig
from guardrails.policy import GuardrailResult

logger = logging.getLogger(__name__)

# Agent types that bypass pre-check (read-only / analysis roles).
_TRUSTED_AGENT_TYPES = frozenset({"planner", "evaluator"})


def _normalize_path(path: str) -> str:
    """Normalize path: resolve '..' segments then convert to forward-slash."""
    return os.path.normpath(path).replace("\\", "/")


class NodeGuardrails:
    """Deterministic safety checks at the DAG node boundary for external backends.

    Pre-check gates execution; post-check validates outputs against
    protected paths. Both return GuardrailResult for consistency.
    """

    def __init__(
        self,
        config: GuardrailsConfig,
        project_dir: str | None = None,
    ) -> None:
        self._config = config
        self._project_dir = project_dir

    def pre_check(
        self,
        node: DAGNode,
        workspace_path: str | None = None,
    ) -> GuardrailResult:
        """Check whether a node should be allowed to execute.

        Rules:
        1. Trusted agent types (planner, evaluator) -> allowed
        2. workspace_path outside project_dir -> blocked
        3. denied_commands substring match in task_description -> blocked
        4. Otherwise -> allowed
        """
        if node.agent_type in _TRUSTED_AGENT_TYPES:
            return GuardrailResult(decision="allowed", reason="Trusted agent type")

        if self._project_dir and workspace_path:
            norm_project = _normalize_path(self._project_dir.rstrip("/\\"))
            norm_workspace = _normalize_path(workspace_path.rstrip("/\\"))
            if not norm_workspace.startswith(norm_project + "/") and norm_workspace != norm_project:
                return GuardrailResult(
                    decision="blocked",
                    reason=(
                        f"Workspace path '{workspace_path}' is outside "
                        f"project directory '{self._project_dir}'"
                    ),
                )

        if self._config.denied_commands and node.task_description:
            desc_lower = node.task_description.lower()
            for denied in self._config.denied_commands:
                if denied.lower() in desc_lower:
                    return GuardrailResult(
                        decision="blocked",
                        reason=f"Task description matches denied command: '{denied}'",
                    )

        return GuardrailResult(decision="allowed", reason="All pre-checks passed")

    def post_check(
        self,
        artifacts: list[str],
        workspace_path: str | None = None,
    ) -> GuardrailResult:
        """Check whether any artifact touches a protected path.

        Uses fnmatch glob matching on forward-slash normalized paths.
        Empty artifacts list returns allowed (no false positives).
        """
        if not artifacts:
            return GuardrailResult(decision="allowed", reason="No artifacts to check")

        for artifact_path in artifacts:
            if not isinstance(artifact_path, str):
                continue
            norm_artifact = _normalize_path(artifact_path)
            # Extract relative portion for matching
            rel = norm_artifact.split("/")[-1] if "/" in norm_artifact else norm_artifact

            for pattern in self._config.protected_paths:
                norm_pattern = _normalize_path(pattern)
                if fnmatch(rel, norm_pattern):
                    return GuardrailResult(
                        decision="blocked",
                        reason=(
                            f"Artifact '{artifact_path}' matches "
                            f"protected path pattern '{pattern}'"
                        ),
                    )
                # Also check the full normalized path for prefix patterns
                if fnmatch(norm_artifact, norm_pattern):
                    return GuardrailResult(
                        decision="blocked",
                        reason=(
                            f"Artifact '{artifact_path}' matches "
                            f"protected path pattern '{pattern}'"
                        ),
                    )

        return GuardrailResult(decision="allowed", reason="All artifacts safe")
