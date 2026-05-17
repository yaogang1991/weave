"""SWE-bench task instance models.

Models the data schema for a single SWE-bench task instance,
which represents a real-world GitHub issue + ground-truth patch
from a Python repository.

Reference: https://www.swebench.com/
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SWEBenchTaskInstance(BaseModel):
    """A single SWE-bench task instance.

    Each instance represents a real GitHub issue with an associated
    gold-standard patch. The goal is for the system to generate a
    patch that resolves the issue and passes relevant tests.
    """

    instance_id: str
    repo: str                           # e.g. "django/django"
    version: str = ""                   # Base version/tag to checkout
    base_commit: str = ""               # Specific commit SHA
    problem_statement: str              # The issue text / PR description
    hints_text: str = ""                # Optional developer hints
    created_at: str = ""
    patch: str = ""                     # Gold-standard patch (ground truth)
    test_patch: str = ""                # Test changes for validation
    fail_to_pass: list[str] = Field(default_factory=list)
    pass_to_pass: list[str] = Field(default_factory=list)
    environment_setup_commit: str = ""


class SWEBenchResult(BaseModel):
    """Result of running a single SWE-bench task instance through Weave."""

    instance_id: str
    status: str = "pending"             # pending, running, completed, failed
    generated_patch: str = ""           # The patch Weave produced
    model_patch: str = ""               # Alias for compatibility
    test_result: dict | None = None     # Test execution results
    error: str = ""
    execution_time_sec: float = 0.0
    metadata: dict = Field(default_factory=dict)


class SWEBenchRunConfig(BaseModel):
    """Configuration for a SWE-bench evaluation run."""

    dataset_path: str = ""              # Path to SWE-bench JSONL dataset
    max_instances: int = 0              # 0 = all instances
    instance_ids: list[str] = Field(default_factory=list)
    timeout_per_instance: int = 600     # seconds
    model: str = ""                     # LLM model override
    output_dir: str = "./data/swebench"
    parallel: int = 1                   # Max concurrent instances
