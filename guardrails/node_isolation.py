"""
Node isolation guard for DAG blast radius limiting (#511 isolation layer).

Scans artifact handoffs between DAG nodes for injection patterns, preventing
a compromised node from propagating injection payloads to downstream nodes.

This is the third defense layer:
1. Input layer: detect injection in user input (guardrails/injection.py)
2. Output layer: detect injection in tool output (guardrails/output_monitor.py)
3. Isolation layer: detect injection in inter-node handoffs (this module)

When injection is detected in a handoff artifact:
- A warning is logged with node and pattern details
- The artifact content is wrapped with an isolation warning banner
- An event is emitted for monitoring/alerting

The isolation guard does NOT block handoffs — it marks them as suspicious
so downstream agents and operators can make informed decisions.

Usage in ArtifactHandoffService::

    guard = NodeIsolationGuard()
    safe_artifacts = guard.scan_handoffs(artifacts, from_node_id, to_node_id)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.models import HandoffArtifact
from guardrails.injection import detect_injection

logger = logging.getLogger(__name__)


@dataclass
class IsolationScanResult:
    """Result of scanning handoff artifacts for injection patterns."""

    injected: bool = False
    injected_artifact_count: int = 0
    total_artifact_count: int = 0
    risk_level: str = "none"
    patterns_matched: list[str] = field(default_factory=list)
    sanitized_artifacts: list[HandoffArtifact] = field(default_factory=list)


class NodeIsolationGuard:
    """Scans DAG node handoff artifacts for injection payloads (#511 isolation).

    Prevents blast radius expansion: if one DAG node reads a malicious file
    (indirect injection), the injection payload should not propagate through
    artifact handoffs to downstream nodes.

    Each handoff artifact's content and metadata are scanned using the shared
    injection pattern database. Suspicious artifacts are wrapped with a warning
    banner so the receiving agent knows the content may be compromised.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    def scan_handoffs(
        self,
        artifacts: list[HandoffArtifact],
        from_node_id: str,
        to_node_id: str,
    ) -> IsolationScanResult:
        """Scan handoff artifacts for injection patterns.

        Args:
            artifacts: List of HandoffArtifact objects from upstream nodes.
            from_node_id: Source node ID (for logging).
            to_node_id: Destination node ID (for logging).

        Returns:
            IsolationScanResult with detection status and sanitized artifacts.
        """
        if not self._enabled or not artifacts:
            return IsolationScanResult(
                total_artifact_count=len(artifacts),
                sanitized_artifacts=list(artifacts),
            )

        all_patterns: list[str] = []
        max_risk = "none"
        injected_count = 0
        sanitized: list[HandoffArtifact] = []

        for artifact in artifacts:
            scan_result = self._scan_artifact(artifact)

            if scan_result.injected:
                injected_count += 1
                all_patterns = _merge_patterns(all_patterns, scan_result.patterns_matched)
                max_risk = _higher_risk(max_risk, scan_result.risk_level)

                logger.warning(
                    "Isolation guard: injection detected in handoff artifact "
                    "(#511 isolation layer): from_node=%s to_node=%s "
                    "from_agent=%s risk=%s patterns=%s",
                    from_node_id, to_node_id,
                    artifact.from_agent,
                    scan_result.risk_level,
                    scan_result.patterns_matched,
                )

                sanitized.append(
                    artifact.model_copy(
                        update=_wrap_artifact_with_warning(
                            artifact, scan_result.patterns_matched,
                        ),
                    ),
                )
            else:
                sanitized.append(artifact)

        return IsolationScanResult(
            injected=injected_count > 0,
            injected_artifact_count=injected_count,
            total_artifact_count=len(artifacts),
            risk_level=max_risk,
            patterns_matched=all_patterns,
            sanitized_artifacts=sanitized,
        )

    @staticmethod
    def _scan_artifact(artifact: HandoffArtifact) -> IsolationScanResult:
        """Scan a single artifact's content and metadata for injection."""
        # Scan content
        content_detection = detect_injection(artifact.content)

        # Scan string values in metadata
        meta_text = " ".join(
            str(v) for v in artifact.metadata.values()
            if isinstance(v, str)
        )
        meta_detection = detect_injection(meta_text)

        # Merge results
        all_patterns = _merge_patterns(
            content_detection.patterns_matched,
            meta_detection.patterns_matched,
        )

        if not all_patterns:
            return IsolationScanResult()

        risk_level = _higher_risk(
            content_detection.risk_level,
            meta_detection.risk_level,
        )

        return IsolationScanResult(
            injected=True,
            risk_level=risk_level,
            patterns_matched=all_patterns,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled


def _merge_patterns(
    existing: list[str], new: list[str],
) -> list[str]:
    """Merge pattern lists without duplicates, preserving order."""
    seen = set(existing)
    for p in new:
        if p not in seen:
            existing.append(p)
            seen.add(p)
    return existing


_RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _higher_risk(a: str, b: str) -> str:
    """Return the higher of two risk levels."""
    if _RISK_ORDER.get(a, 0) >= _RISK_ORDER.get(b, 0):
        return a
    return b


def _wrap_artifact_with_warning(
    artifact: HandoffArtifact,
    patterns: list[str],
) -> dict:
    """Create update dict that wraps artifact content with isolation warning."""
    warning = (
        "[WEAVE ISOLATION GUARD: Potential injection patterns detected in "
        f"upstream artifact ({', '.join(patterns)}). Review carefully before "
        "trusting this content.]"
    )
    updated_content = f"{warning}\n\n{artifact.content}"
    updated_metadata = {
        **artifact.metadata,
        "isolation_warning": True,
        "isolation_patterns": patterns,
    }
    return {
        "content": updated_content,
        "metadata": updated_metadata,
    }
