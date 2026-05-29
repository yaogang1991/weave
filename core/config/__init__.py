"""Configuration management for the Weave project.

Split into sub-modules for maintainability (#917).
All symbols are re-exported here so that
``from core.config import WeaveConfig``
continues to work unchanged.
"""
from core.config.env import (
    _get_non_interactive_env,
    _load_claude_settings,
    _CLAUDE_ENV,
    infer_provider,
)
from core.config.llm import LLMConfig
from core.config.sandbox import SandboxConfig
from core.config.mcp import MCPServerConfig, MCPConfig
from core.config.watchdog import AgentWatchdogOverride, WatchdogConfig
from core.config.routing import ModelRoute, ModelRoutingConfig
from core.config.memory import MemoryConfig
from core.config.learning import LearningConfig
from core.config.analysis import ImpactConfig
from core.config.timeout import (
    EvalTimeoutScaleConfig,
    EvaluatorStallScaleConfig,
    GeneratorStallScaleConfig,
    NodeTimeoutConfig,
)
from core.config.budget import BudgetConfig, TokenEstimationConfig
from core.config.backends import CodexBackendConfig, ClaudeCodeConfig
from core.config.observability import ObservabilityConfig
from core.config.root import WeaveConfig

__all__ = [
    "WeaveConfig",
    "LLMConfig",
    "SandboxConfig",
    "MCPServerConfig",
    "MCPConfig",
    "AgentWatchdogOverride",
    "WatchdogConfig",
    "ModelRoute",
    "ModelRoutingConfig",
    "MemoryConfig",
    "LearningConfig",
    "ImpactConfig",
    "EvalTimeoutScaleConfig",
    "EvaluatorStallScaleConfig",
    "GeneratorStallScaleConfig",
    "NodeTimeoutConfig",
    "BudgetConfig",
    "TokenEstimationConfig",
    "CodexBackendConfig",
    "ClaudeCodeConfig",
    "ObservabilityConfig",
    "infer_provider",
]
