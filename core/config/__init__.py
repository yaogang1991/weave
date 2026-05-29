"""Configuration management for the Weave.

Re-exports all public symbols so that ``from core.config import X`` continues
to work unchanged after the split from a single ``core/config.py`` into a
``core/config/`` package.
"""

from core.config.env import (
    _CLAUDE_ENV,
    _get_non_interactive_env,
    infer_provider,
)
from core.config.llm import (
    LLMConfig,
    ModelRoute,
    ModelRoutingConfig,
)
from core.config.timeout import (
    AgentWatchdogOverride,
    EvalTimeoutScaleConfig,
    EvaluatorStallScaleConfig,
    GeneratorStallScaleConfig,
    NodeTimeoutConfig,
    WatchdogConfig,
    _DEFAULT_AGENT_WATCHDOG_OVERRIDES,
)
from core.config.domains import (
    BudgetConfig,
    ClaudeCodeConfig,
    CodexBackendConfig,
    ImpactConfig,
    LearningConfig,
    MCPConfig,
    MCPServerConfig,
    MemoryConfig,
    ObservabilityConfig,
    SandboxConfig,
    TokenEstimationConfig,
)
from core.config.root import WeaveConfig

__all__ = [
    # env
    "_CLAUDE_ENV",
    "_get_non_interactive_env",
    "infer_provider",
    # llm
    "LLMConfig",
    "ModelRoute",
    "ModelRoutingConfig",
    # timeout
    "AgentWatchdogOverride",
    "EvalTimeoutScaleConfig",
    "EvaluatorStallScaleConfig",
    "GeneratorStallScaleConfig",
    "NodeTimeoutConfig",
    "WatchdogConfig",
    "_DEFAULT_AGENT_WATCHDOG_OVERRIDES",
    # domains
    "BudgetConfig",
    "ClaudeCodeConfig",
    "CodexBackendConfig",
    "ImpactConfig",
    "LearningConfig",
    "MCPConfig",
    "MCPServerConfig",
    "MemoryConfig",
    "ObservabilityConfig",
    "SandboxConfig",
    "TokenEstimationConfig",
    # root
    "WeaveConfig",
]
