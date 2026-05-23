"""Integration layer configuration."""
from __future__ import annotations

import os

from integrations.models import LabelConfig

from pydantic import BaseModel


class IntegrationConfig(BaseModel):
    """Configuration for the integration layer."""
    label: LabelConfig = LabelConfig()
    github_repo: str = ""
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> IntegrationConfig:
        return cls(
            label=LabelConfig(
                trigger_label=os.environ.get("WEAVE_INTEGRATION_LABEL", "weave"),
            ),
            github_repo=os.environ.get("WEAVE_GITHUB_REPO", ""),
            dry_run=os.environ.get("WEAVE_INTEGRATION_DRY_RUN", "").lower()
            in ("true", "1", "yes"),
        )
