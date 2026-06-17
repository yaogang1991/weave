"""Tests for #1136: ``submit --non-interactive`` propagates end-to-end to the
claude_code backend's ``--permission-mode bypassPermissions``.

Background
----------
The core permission_mode promotion (default -> bypassPermissions when
non-interactive) was fixed in #1125 via ``ClaudeCodeRuntimeConfig.from_core_config``
(covered by ``test_1125_1136_noninteractive_permission.py``). ``run``/``execute``
already expose ``--non-interactive`` through ``cli/args.py::add_execution_args``
(#497/#528) and wire it straight into ``from_core_config``.

The remaining gap was the **submit** command: submit only enqueues, the worker
executes in a separate process, so the intent had to persist into
``job.metadata["non_interactive"]`` and be merged by the worker. These tests
cover that propagation chain:

  cmd_submit -> submit_job(job.metadata) -> worker run_job/_execute_plan_and_run
      -> create_execution_engine(non_interactive_override) -> from_core_config
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core.guardrail_models import PermissionMode
from session.store import SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job(metadata=None, job_id="job_1", status="queued"):
    """Build a Job with an explicit metadata dict (default empty)."""
    from control_plane.models import Job, JobStatus
    now = datetime.now(timezone.utc)
    return Job(
        id=job_id,
        requirement="x",
        status=JobStatus(status),
        project_path="/tmp/project",
        attempt=0,
        last_error="",
        error_category="",
        created_at=now,
        updated_at=now,
        metadata=metadata if metadata is not None else {},
    )


def _make_run(status="succeeded"):
    from control_plane.models import Run, RunStatus
    now = datetime.now(timezone.utc)
    return Run(
        id="run_1",
        job_id="job_1",
        session_id="sess_1",
        status=RunStatus(status),
        started_at=now,
        completed_at=now,
        created_at=now,
        updated_at=now,
    )


def _make_factory(non_interactive=False, policy=None):
    """ExecutionFactory with sensible test defaults (mirrors
    test_execution_factory._make_factory without importing test helpers)."""
    from control_plane.execution_factory import ExecutionFactory
    from core.config import LLMConfig, WatchdogConfig
    return ExecutionFactory(
        llm_config=LLMConfig(api_key="test-key", model="test-model"),
        max_parallel=4,
        agent_timeout=300,
        max_context_tokens=100_000,
        max_iterations=50,
        artifact_path="/tmp/artifacts",
        non_interactive=non_interactive,
        watchdog_config=WatchdogConfig(
            enabled=True, heartbeat_interval_sec=30.0, heartbeat_miss_threshold=12,
        ),
        hooks=[],
        approval_repo=None,
        policy=policy,
        budget_manager=None,
    )


def _patch_factory_internals(stack, claude_code_enabled=False):
    """Mock the external dependencies create_execution_engine pulls in.

    Mirrors the @patch stack used in test_execution_factory.py.
    """
    MockConfig = stack.enter_context(patch("control_plane.execution_factory.WeaveConfig"))
    MockConfig.from_env.return_value = MagicMock(
        pass_threshold=7.0,
        auto_format_before_eval=False,
        node_timeout=MagicMock(),
        default_agent_backend="builtin",
        claude_code=MagicMock(enabled=claude_code_enabled),
        codex=MagicMock(enabled=False),
    )
    stack.enter_context(patch("control_plane.execution_factory.IntelligentOrchestrator"))
    stack.enter_context(patch("control_plane.execution_factory.AgentPool"))
    stack.enter_context(patch("control_plane.execution_factory.EvaluatorEngine"))
    stack.enter_context(patch("control_plane.execution_factory.DAGExecutionEngine"))
    stack.enter_context(patch("control_plane.execution_factory.LightweightLLMCaller"))
    stack.enter_context(patch("control_plane.execution_factory.BuiltinBackend"))
    stack.enter_context(patch("control_plane.execution_factory.BackendRegistry"))
    stack.enter_context(patch("control_plane.execution_factory.ToolRegistry"))
    stack.enter_context(
        patch("control_plane.execution_factory.inject_token_estimator", create=True)
    )


# ===========================================================================
# 1. cmd_submit passes non_interactive through to submit_job
# ===========================================================================

class TestCmdSubmitNonInteractive:
    """cmd_submit reads args.non_interactive and forwards it to submit_job."""

    @patch("cli.jobs._make_run_service")
    @patch("cli.jobs._make_repository")
    @patch("cli.jobs._resolve_project_path")
    def test_forwards_true(self, mock_resolve, mock_make_repo, mock_make_svc):
        from cli.jobs import cmd_submit

        mock_resolve.return_value = "/tmp/project"
        mock_make_repo.return_value = MagicMock()
        svc = MagicMock()
        svc.submit_job = AsyncMock(return_value=_make_job())
        mock_make_svc.return_value = svc

        args = argparse.Namespace(
            project="/tmp/project", requirement="x", timeout=1800,
            max_attempts=3, allow_self_modify=False, non_interactive=True,
        )
        asyncio.run(cmd_submit(args))

        assert svc.submit_job.await_args.kwargs["non_interactive"] is True

    @patch("cli.jobs._make_run_service")
    @patch("cli.jobs._make_repository")
    @patch("cli.jobs._resolve_project_path")
    def test_defaults_false_when_attr_missing(self, mock_resolve, mock_make_repo, mock_make_svc):
        from cli.jobs import cmd_submit

        mock_resolve.return_value = "/tmp/project"
        mock_make_repo.return_value = MagicMock()
        svc = MagicMock()
        svc.submit_job = AsyncMock(return_value=_make_job())
        mock_make_svc.return_value = svc

        # non_interactive deliberately omitted — getattr must default to False.
        args = argparse.Namespace(
            project="/tmp/project", requirement="x", timeout=1800,
            max_attempts=3, allow_self_modify=False,
        )
        asyncio.run(cmd_submit(args))

        assert svc.submit_job.await_args.kwargs["non_interactive"] is False


# ===========================================================================
# 2. RunService.submit_job persists the intent into job.metadata
# ===========================================================================

class TestSubmitJobPersistsNonInteractive:
    """submit_job stores non_interactive in job.metadata so the worker
    (a separate process) can read it later."""

    def _service(self):
        from control_plane.service import RunService
        # Bypass __init__ (which wires hooks/factory); submit_job only needs
        # self.repository.
        svc = RunService.__new__(RunService)
        repo = MagicMock()
        repo.create_job.return_value = _make_job()
        svc.repository = repo
        return svc, repo

    def test_metadata_set_when_non_interactive(self):
        svc, repo = self._service()
        result = asyncio.run(svc.submit_job("x", non_interactive=True))
        assert result.metadata["non_interactive"] is True
        repo.update_job.assert_called_once()

    def test_metadata_absent_when_interactive_default(self):
        svc, repo = self._service()
        result = asyncio.run(svc.submit_job("x"))
        assert "non_interactive" not in result.metadata
        repo.update_job.assert_called_once()


# ===========================================================================
# 3. ExecutionFactory.create_execution_engine honours non_interactive_override
# ===========================================================================

class TestExecutionEngineNonInteractiveOverride:
    """The per-job override takes precedence over the factory's
    constructor-level _non_interactive (#1136 worker merge path)."""

    def test_override_promotes_to_dont_ask(self):
        """Factory built interactive, but override=True -> DONT_ASK."""
        with ExitStack() as stack:
            _patch_factory_internals(stack)
            factory = _make_factory(non_interactive=False, policy=None)
            with patch("control_plane.execution_factory.Guardrails") as MockGuardrails:
                MockGuardrails.return_value = MagicMock()
                factory.create_execution_engine(
                    session_id="s",
                    store=MagicMock(spec=SessionStore),
                    work_dir=Path("/tmp/work"),
                    non_interactive_override=True,
                )
            policy_arg = MockGuardrails.call_args[0][0]
            assert policy_arg.mode == PermissionMode.DONT_ASK

    def test_no_override_keeps_interactive_default(self):
        """Factory built interactive, no override -> ACCEPT_EDITS."""
        with ExitStack() as stack:
            _patch_factory_internals(stack)
            factory = _make_factory(non_interactive=False, policy=None)
            with patch("control_plane.execution_factory.Guardrails") as MockGuardrails:
                MockGuardrails.return_value = MagicMock()
                factory.create_execution_engine(
                    session_id="s",
                    store=MagicMock(spec=SessionStore),
                    work_dir=Path("/tmp/work"),
                )
            policy_arg = MockGuardrails.call_args[0][0]
            assert policy_arg.mode == PermissionMode.ACCEPT_EDITS

    def test_override_none_falls_back_to_constructor_value(self):
        """Factory built non-interactive, override=None -> still DONT_ASK
        (constructor value wins when override is absent; backward compat)."""
        with ExitStack() as stack:
            _patch_factory_internals(stack)
            factory = _make_factory(non_interactive=True, policy=None)
            with patch("control_plane.execution_factory.Guardrails") as MockGuardrails:
                MockGuardrails.return_value = MagicMock()
                factory.create_execution_engine(
                    session_id="s",
                    store=MagicMock(spec=SessionStore),
                    work_dir=Path("/tmp/work"),
                )
            policy_arg = MockGuardrails.call_args[0][0]
            assert policy_arg.mode == PermissionMode.DONT_ASK

    def test_override_reaches_claude_code_backend(self):
        """override=True makes from_core_config receive non_interactive=True,
        i.e. the backend is promoted to bypassPermissions."""
        with ExitStack() as stack:
            _patch_factory_internals(stack, claude_code_enabled=True)
            MockCC = stack.enter_context(
                patch("agent.backends.claude_code.ClaudeCodeRuntimeConfig")
            )
            MockCC.from_core_config.return_value = MagicMock()
            stack.enter_context(patch("agent.backends.claude_code.ClaudeCodeBackend"))
            factory = _make_factory(non_interactive=False, policy=None)
            factory.create_execution_engine(
                session_id="s",
                store=MagicMock(spec=SessionStore),
                work_dir=Path("/tmp/work"),
                non_interactive_override=True,
            )
            MockCC.from_core_config.assert_called_once()
            assert MockCC.from_core_config.call_args.kwargs["non_interactive"] is True


# ===========================================================================
# 4. worker_executor.execute_job_core OR-merges job.metadata
# ===========================================================================

class TestExecuteJobCoreMerge:
    """A job submitted with --non-interactive gets its approval tickets
    expired even if the worker wasn't started with --non-interactive."""

    async def test_job_metadata_triggers_expire(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        repo.get_job.return_value = _make_job(metadata={"non_interactive": True})
        approval_repo = MagicMock()
        run_service = MagicMock()
        run_service.approval_repo = approval_repo
        run_service.run_job = AsyncMock(return_value=_make_run("succeeded"))

        await execute_job_core(repo, run_service, "job_1", non_interactive=False)

        approval_repo.expire_tickets.assert_called_once()

    async def test_no_intent_no_flag_skips_expire(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        repo.get_job.return_value = _make_job(metadata={})
        approval_repo = MagicMock()
        run_service = MagicMock()
        run_service.approval_repo = approval_repo
        run_service.run_job = AsyncMock(return_value=_make_run("succeeded"))

        await execute_job_core(repo, run_service, "job_1", non_interactive=False)

        approval_repo.expire_tickets.assert_not_called()

    async def test_worker_flag_alone_still_expires(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        repo.get_job.return_value = _make_job(metadata={})
        approval_repo = MagicMock()
        run_service = MagicMock()
        run_service.approval_repo = approval_repo
        run_service.run_job = AsyncMock(return_value=_make_run("succeeded"))

        await execute_job_core(repo, run_service, "job_1", non_interactive=True)

        approval_repo.expire_tickets.assert_called_once()
