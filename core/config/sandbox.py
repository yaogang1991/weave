"""Sandbox configuration."""
from __future__ import annotations

from pydantic import BaseModel


class SandboxConfig(BaseModel):
    enabled: bool = True
    runtime: str = "local"
    image: str = "python:3.11-slim"
    network_mode: str = "none"
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    timeout: int = 300
    credential_proxy: bool = True
