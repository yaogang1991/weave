"""IntegrationRegistry -- registers IssueTracker and CodeHost implementations."""
from __future__ import annotations

import logging

from integrations.base import CodeHost, IssueTracker

logger = logging.getLogger(__name__)


class IntegrationRegistry:
    """Registry of IssueTracker and CodeHost implementations."""

    def __init__(self) -> None:
        self._trackers: dict[str, IssueTracker] = {}
        self._hosts: dict[str, CodeHost] = {}

    def register_tracker(self, name: str, tracker: IssueTracker) -> None:
        self._trackers[name] = tracker

    def register_host(self, name: str, host: CodeHost) -> None:
        self._hosts[name] = host

    def get_tracker(self, name: str) -> IssueTracker | None:
        return self._trackers.get(name)

    def get_host(self, name: str) -> CodeHost | None:
        return self._hosts.get(name)

    def list_trackers(self) -> list[str]:
        return list(self._trackers.keys())

    def list_hosts(self) -> list[str]:
        return list(self._hosts.keys())
