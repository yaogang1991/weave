"""Tests for leftover generated files cleanup (#240)."""
import os
import time

import pytest

from backend.lifecycle import BackendManager
from backend.local import LocalBackend


class TestLeftoverCleanup:
    """Verify cleanup_node_artifacts removes unexpected files."""

    @pytest.fixture
    def workspace(self, tmp_path):
        """Create a temp workspace."""
        ws = tmp_path / "project"
        ws.mkdir()
        return ws

    @pytest.fixture
    def manager(self, tmp_path):
        """Create a BackendManager with local workspace."""
        return BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
        )

    def _setup_run(self, manager, workspace, job_id="j1", run_id="r1"):
        """Set up a fake active run for testing."""
        # Use repo_root so get_work_dir returns our workspace path
        backend = LocalBackend(repo_root=str(workspace), base_path=str(workspace))
        manager._active_runs[run_id] = backend
        return workspace

    def test_no_leftover_files(self, manager, workspace):
        """Node with all artifacts in owned_files produces no cleanup."""
        self._setup_run(manager, workspace)
        # Create expected files
        (workspace / "src").mkdir()
        (workspace / "src" / "a.py").write_text("# a")
        (workspace / "src" / "b.py").write_text("# b")

        cleaned = manager.cleanup_node_artifacts(
            "j1", "r1", "n1",
            expected_artifacts=["src/a.py", "src/b.py"],
        )
        assert cleaned == []
        assert (workspace / "src" / "a.py").exists()
        assert (workspace / "src" / "b.py").exists()

    def test_leftover_py_files_quarantined(self, manager, workspace):
        """Extra .py files not in owned_files are moved to .leftovers."""
        self._setup_run(manager, workspace)
        (workspace / "src").mkdir()
        (workspace / "src" / "a.py").write_text("# expected")
        (workspace / "src" / "extra.py").write_text("# unexpected")

        cleaned = manager.cleanup_node_artifacts(
            "j1", "r1", "n1",
            expected_artifacts=["src/a.py"],
        )
        assert "src/extra.py" in cleaned
        assert (workspace / "src" / "a.py").exists()
        assert not (workspace / "src" / "extra.py").exists()
        assert (workspace / ".leftovers" / "src" / "extra.py").exists()

    def test_non_py_files_not_cleaned(self, manager, workspace):
        """Non-.py files are not touched by cleanup."""
        self._setup_run(manager, workspace)
        (workspace / "data").mkdir()
        (workspace / "data" / "config.json").write_text("{}")
        (workspace / "data" / "notes.txt").write_text("notes")

        cleaned = manager.cleanup_node_artifacts(
            "j1", "r1", "n1",
            expected_artifacts=[],
        )
        assert cleaned == []
        assert (workspace / "data" / "config.json").exists()

    def test_cleanup_respects_timestamps(self, manager, workspace):
        """Only files newer than started_at are candidates."""
        self._setup_run(manager, workspace)
        now = time.time()
        (workspace / "src").mkdir()

        # Create an "old" file
        old_file = workspace / "src" / "old.py"
        old_file.write_text("# old")

        # Simulate started_at in the future — no files should be cleaned
        cleaned = manager.cleanup_node_artifacts(
            "j1", "r1", "n1",
            expected_artifacts=[],
            started_at=now + 1000,  # Far future
        )
        assert cleaned == []
        assert old_file.exists()

    def test_cleanup_with_no_owned_files_is_noop(self, manager, workspace):
        """When owned_files is empty, cleanup is skipped."""
        self._setup_run(manager, workspace)
        (workspace / "src").mkdir()
        (workspace / "src" / "random.py").write_text("# random")

        cleaned = manager.cleanup_node_artifacts(
            "j1", "r1", "n1",
            expected_artifacts=[],
        )
        assert cleaned == []
        assert (workspace / "src" / "random.py").exists()

    def test_ignored_dirs_not_scanned(self, manager, workspace):
        """Files in __pycache__, .git etc are not scanned."""
        self._setup_run(manager, workspace)
        (workspace / "__pycache__").mkdir()
        (workspace / "__pycache__" / "module.pyc").write_text("bytecode")

        cleaned = manager.cleanup_node_artifacts(
            "j1", "r1", "n1",
            expected_artifacts=["src/a.py"],
        )
        assert cleaned == []
