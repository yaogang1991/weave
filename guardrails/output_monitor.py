"""
Agent output injection monitor (#511 output layer).

Detects injection instructions embedded in tool execution results
(read file content, bash output, etc.). This is the second defense layer:
an attacker may hide injection payloads in files or external data that
the agent reads, causing indirect prompt injection.

Usage in worker::

    monitor = OutputMonitor()
    result = monitor.scan_tool_output("read", {"file_path": "/tmp/data.txt"}, tool_output)
    if result.injected:
        logger.warning("Injection in tool output: %s", result.patterns_matched)

The monitor reuses the pattern database from guardrails/injection.py
but applies different risk thresholds (tool output is less trusted than
user input, so single matches trigger warnings).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from guardrails.injection import detect_injection

logger = logging.getLogger(__name__)


@dataclass
class OutputScanResult:
    """Result of scanning tool output for injection patterns."""

    injected: bool = False
    risk_level: str = "none"  # none, low, medium, high
    patterns_matched: list[str] = field(default_factory=list)
    details: str = ""
    sanitized_output: str = ""


class OutputMonitor:
    """Monitors agent tool execution outputs for injection payloads (#511 output layer).

    Scans tool results (file contents, bash output, etc.) for injection patterns.
    Unlike input-layer detection (which blocks suspicious inputs), output monitoring
    logs warnings and can sanitize the output before it reaches the LLM context.

    This prevents indirect injection where an attacker hides payloads in files
    or external data that the agent processes.
    """

    # Tool types whose output should be scanned (read external data)
    SCAN_TOOLS: frozenset[str] = frozenset({
        "read",       # File contents may contain injected instructions
        "bash",       # Command output may be crafted
        "glob",       # File names could be crafted (lower risk)
        "grep",       # Search results may contain injected content
    })

    # Tools that produce trusted output (no scanning needed)
    SKIP_TOOLS: frozenset[str] = frozenset({
        "write",      # Output is just success/failure
        "edit",       # Output is just success/failure
        "git",        # Git output is structured
    })

    def __init__(
        self,
        enabled: bool = True,
        sanitize: bool = False,
        log_level: str = "warning",
    ) -> None:
        self._enabled = enabled
        self._sanitize = sanitize
        self._log_level = log_level

    def scan_tool_output(
        self,
        tool_name: str,
        tool_args: dict,
        output: str,
    ) -> OutputScanResult:
        """Scan a tool execution result for injection patterns.

        Args:
            tool_name: Name of the tool that produced the output.
            tool_args: Arguments passed to the tool.
            output: The tool's output string.

        Returns:
            OutputScanResult with detection status and optional sanitized output.
        """
        if not self._enabled:
            return OutputScanResult(sanitized_output=output)

        if not output or not output.strip():
            return OutputScanResult(sanitized_output=output)

        # Skip trusted tools
        if tool_name in self.SKIP_TOOLS:
            return OutputScanResult(sanitized_output=output)

        # Only scan tools that read external data
        if tool_name not in self.SCAN_TOOLS:
            return OutputScanResult(sanitized_output=output)

        detection = detect_injection(output)

        if not detection.detected:
            return OutputScanResult(sanitized_output=output)

        # Log the detection
        log_msg = (
            "Injection detected in tool output (#511 output layer): "
            "tool=%s risk=%s patterns=%s output_preview=%.100s",
            tool_name,
            detection.risk_level,
            detection.patterns_matched,
            output[:100],
        )
        if self._log_level == "error":
            logger.error(*log_msg)
        else:
            logger.warning(*log_msg)

        result = OutputScanResult(
            injected=True,
            risk_level=detection.risk_level,
            patterns_matched=detection.patterns_matched,
            details=detection.details,
            sanitized_output=output,
        )

        # Optionally sanitize high-risk outputs
        if self._sanitize and detection.risk_level in ("high", "medium"):
            result.sanitized_output = self._sanitize_output(
                output, detection.patterns_matched,
            )

        return result

    def scan_llm_response(self, content: str) -> OutputScanResult:
        """Scan an LLM response for injection-like patterns.

        Catches cases where the LLM produces suspicious content that
        could propagate injection to downstream agents or tools.

        Args:
            content: The LLM's text response.

        Returns:
            OutputScanResult with detection status.
        """
        if not self._enabled or not content or not content.strip():
            return OutputScanResult(sanitized_output=content)

        detection = detect_injection(content)

        if detection.detected:
            logger.info(
                "LLM response contains injection-like patterns (#511): "
                "risk=%s patterns=%s",
                detection.risk_level,
                detection.patterns_matched,
            )
            return OutputScanResult(
                injected=True,
                risk_level=detection.risk_level,
                patterns_matched=detection.patterns_matched,
                details=detection.details,
                sanitized_output=content,
            )

        return OutputScanResult(sanitized_output=content)

    @staticmethod
    def _sanitize_output(output: str, patterns: list[str]) -> str:
        """Inject a warning banner into output containing injection patterns.

        Does not redact content — adds a visible marker so the LLM
        and operator know the output may contain injection payloads.
        """
        warning = (
            "[WEAVE OUTPUT MONITOR: Potential injection patterns detected "
            f"({', '.join(patterns)}). Review carefully.]"
        )
        # Prepend warning to the output
        return f"{warning}\n\n{output}"

    @property
    def enabled(self) -> bool:
        return self._enabled
