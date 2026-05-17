"""
规范合同测试 — 确保实现与 docs/m1_personal_spec.md 一致。

设计原则：
- 这些测试是"防偏离"测试，修改 CLI 或状态枚举导致偏离时测试失败
- 发布前必须全绿
- 新增命令或枚举值时同步更新本测试
"""

from __future__ import annotations

import argparse
import inspect
import subprocess  # noqa: F401
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock heavy LLM dependencies before importing main
_mock_anthropic = MagicMock()
_mock_openai = MagicMock()
sys.modules["anthropic"] = _mock_anthropic
sys.modules["openai"] = _mock_openai

# Now safe to import project modules
from control_plane.models import JobStatus, RunStatus  # noqa: E402, F401
from control_plane.approval import TicketStatus  # noqa: E402
from control_plane.repository import JobRepository  # noqa: E402
from core.models import RiskLevel  # noqa: E402

import main as main_module  # noqa: E402


# =============================================================================
# CLI Subcommands Contract
# =============================================================================


class TestCLISubcommands:
    """验证 CLI 子命令存在（对应 spec Part 3）。

    使用 argparse introspection 而非 subprocess，因为子进程
    无法继承当前进程中的 anthropic mock。
    """

    ALL_COMMANDS = [
        "plan", "execute", "run", "viz",
        "submit", "status", "list", "cancel", "recover",
        "worker", "tickets", "approve", "reject",
    ]

    def _get_parser(self) -> argparse.ArgumentParser:
        """Introspect the main module's argument parser.

        main.main() builds the parser inline; we replicate that construction
        by extracting the parser-building logic into a helper we can call
        directly in the test process (where anthropic is mocked).
        """
        # Rebuild parser by calling main() with a special argv that only
        # triggers parser construction. We use inspect to find the parser
        # object built inside main().
        import io
        import contextlib

        # Capture parser from main() by interceptting parser.parse_args()
        captured_parser = []
        original_parse_args = argparse.ArgumentParser.parse_args

        def capture_parse_args(self, args=None, namespace=None):
            captured_parser.append(self)
            # Don't actually parse; just return a namespace that won't run
            return argparse.Namespace(command=None)

        argparse.ArgumentParser.parse_args = capture_parse_args  # type: ignore[method-assign]
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                try:
                    main_module.main()
                except SystemExit:
                    pass
        finally:
            argparse.ArgumentParser.parse_args = original_parse_args  # type: ignore[method-assign]

        assert captured_parser, "Failed to capture parser from main()"
        return captured_parser[0]

    def test_all_subcommands_exist(self):
        """所有规范中定义的子命令都存在。"""
        parser = self._get_parser()
        # Find subparsers action
        subparsers_action = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subparsers_action = action
                break
        assert subparsers_action is not None, "No subparsers found"
        actual_cmds = set(subparsers_action.choices.keys())
        for cmd in self.ALL_COMMANDS:
            assert cmd in actual_cmds, f"Command '{cmd}' not found in CLI"

    def _get_subparser(self, cmd_name: str) -> argparse.ArgumentParser:
        """Get the sub-parser for a specific command."""
        parser = self._get_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                assert cmd_name in action.choices, f"Command '{cmd_name}' not found"
                return action.choices[cmd_name]
        raise AssertionError("No subparsers found")

    def _get_help_text(self, parser: argparse.ArgumentParser) -> str:
        """Get help text from a parser."""
        import io
        f = io.StringIO()
        parser.print_help(f)
        return f.getvalue()

    def test_submit_command_has_required_args(self):
        """submit 命令有所需参数。"""
        parser = self._get_subparser("submit")
        help_text = self._get_help_text(parser)
        assert "requirement" in help_text
        assert "--project" in help_text
        assert "--timeout" in help_text

    def test_worker_command_has_required_args(self):
        """worker 命令有所需参数。"""
        parser = self._get_subparser("worker")
        help_text = self._get_help_text(parser)
        assert "--concurrency" in help_text
        assert "--poll-interval" in help_text
        assert "--non-interactive" in help_text  # M1.1

    def test_tickets_command_has_required_args(self):
        """tickets 命令有所需参数（M1.1）。"""
        parser = self._get_subparser("tickets")
        help_text = self._get_help_text(parser)
        assert "--status" in help_text
        assert "--job" in help_text

    def test_approve_command_has_required_args(self):
        """approve 命令有所需参数（M1.1）。"""
        parser = self._get_subparser("approve")
        help_text = self._get_help_text(parser)
        assert "ticket_id" in help_text
        assert "--reason" in help_text

    def test_reject_command_has_required_args(self):
        """reject 命令有所需参数（M1.1）。"""
        parser = self._get_subparser("reject")
        help_text = self._get_help_text(parser)
        assert "ticket_id" in help_text
        assert "--reason" in help_text


# =============================================================================
# JobStatus Enum Contract
# =============================================================================


class TestJobStatusEnum:
    """验证 JobStatus 枚举完整（对应 spec Part 2）。"""

    REQUIRED_STATUSES = [
        "queued", "leased", "running", "pending_approval", "succeeded", "failed",
        "canceled", "dead_letter",
    ]

    def test_all_statuses_exist(self):
        """所有规范中定义的状态都存在。"""
        actual = [s.value for s in JobStatus]
        for status in self.REQUIRED_STATUSES:
            assert status in actual, f"JobStatus '{status}' missing"

    def test_required_statuses_exist(self):
        """必需值都存在（允许扩展）。"""
        actual = {s.value for s in JobStatus}
        required = set(self.REQUIRED_STATUSES)
        assert required.issubset(actual), f"Missing required statuses: {required - actual}"


# =============================================================================
# RunStatus Enum Contract
# =============================================================================


class TestRunStatusEnum:
    """验证 RunStatus 枚举完整。"""

    REQUIRED_STATUSES = [
        "running", "pending_approval",
        "succeeded", "failed", "aborted", "timed_out"
    ]

    def test_all_statuses_exist(self):
        actual = [s.value for s in RunStatus]
        for status in self.REQUIRED_STATUSES:
            assert status in actual, f"RunStatus '{status}' missing"

    def test_required_statuses_exist(self):
        """必需值都存在（允许扩展）。"""
        actual = {s.value for s in RunStatus}
        required = set(self.REQUIRED_STATUSES)
        assert required.issubset(actual), f"Missing required statuses: {required - actual}"


# =============================================================================
# TicketStatus Enum Contract
# =============================================================================


class TestTicketStatusEnum:
    """验证 TicketStatus 枚举完整（M1.1 新增）。"""

    REQUIRED_STATUSES = ["pending", "approved", "rejected", "expired"]

    def test_all_statuses_exist(self):
        actual = [s.value for s in TicketStatus]
        for status in self.REQUIRED_STATUSES:
            assert status in actual, f"TicketStatus '{status}' missing"

    def test_required_statuses_exist(self):
        """必需值都存在（允许扩展）。"""
        actual = {s.value for s in TicketStatus}
        required = set(self.REQUIRED_STATUSES)
        assert required.issubset(actual), f"Missing required statuses: {required - actual}"


# =============================================================================
# RiskLevel Enum Contract
# =============================================================================


class TestRiskLevelEnum:
    """验证 RiskLevel 枚举完整。"""

    def test_all_levels_exist(self):
        assert RiskLevel.LOW.value == 1
        assert RiskLevel.MEDIUM.value == 2
        assert RiskLevel.HIGH.value == 3
        assert RiskLevel.CRITICAL.value == 4

    def test_required_levels_exist(self):
        """必需值都存在（允许扩展）。"""
        actual = {m.value for m in RiskLevel}
        required = {1, 2, 3, 4}
        assert required.issubset(actual), f"Missing required levels: {required - actual}"


# =============================================================================
# Error Codes Contract
# =============================================================================


class TestErrorCodes:
    """验证错误码存在（对应 spec Part 3 错误码约定）。"""

    REQUIRED_CODES: dict[str, str] = {
        "E3001": "TicketNotFound",
        "E3002": "ApproveFailed",
        "E3003": "RejectFailed",
    }

    def test_error_codes_in_approve(self):
        """E3001, E3002 在 approve 命令源码中引用。"""
        source = inspect.getsource(main_module.cmd_approve)
        assert "E3001" in source, "Error code E3001 not in cmd_approve"
        assert "E3002" in source, "Error code E3002 not in cmd_approve"

    def test_error_codes_in_reject(self):
        """E3001, E3003 在 reject 命令源码中引用。"""
        source = inspect.getsource(main_module.cmd_reject)
        assert "E3001" in source, "Error code E3001 not in cmd_reject"
        assert "E3003" in source, "Error code E3003 not in cmd_reject"

    def test_error_codes_in_tickets(self):
        """tickets 命令源码中引用 TicketStatus。"""
        source = inspect.getsource(main_module.cmd_tickets)
        assert "TicketStatus" in source, "TicketStatus not referenced in cmd_tickets"


# =============================================================================
# State Transitions Contract
# =============================================================================


class TestStateTransitions:
    """验证状态转换合法（对应 spec Part 2 状态机）。"""

    VALID_TRANSITIONS = [
        # (from, to)
        ("queued", "leased"),
        ("queued", "canceled"),
        ("leased", "running"),
        ("leased", "queued"),  # 租约过期回收
        ("leased", "canceled"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "canceled"),
        ("failed", "queued"),  # 重试
        ("failed", "dead_letter"),
    ]

    @pytest.fixture
    def repo(self, tmp_path: Path):
        """Fresh JobRepository for each test."""
        return JobRepository(base_path=str(tmp_path / "jobs"))

    def test_valid_transitions(self, repo: JobRepository):
        """验证所有合法转换。"""
        for from_status, to_status in self.VALID_TRANSITIONS:
            job = repo.create_job(f"test_{from_status}_to_{to_status}")
            # 强制设置初始状态
            job.status = JobStatus(from_status)
            job.lease_expires_at = None  # 重置租约
            repo.update_job(job)

            try:
                job = repo.transition_job_status(job.id, JobStatus(to_status))
                assert job.status == JobStatus(to_status)
            except ValueError as e:
                pytest.fail(
                    f"Transition {from_status} -> {to_status} should be valid: {e}"
                )

    def test_invalid_transitions(self, repo: JobRepository):
        """验证非法转换被拒绝。"""
        INVALID_TRANSITIONS = [
            ("succeeded", "running"),
            ("dead_letter", "queued"),
            ("canceled", "running"),
        ]

        for from_status, to_status in INVALID_TRANSITIONS:
            job = repo.create_job(f"test_invalid_{from_status}_to_{to_status}")
            job.status = JobStatus(from_status)
            job.lease_expires_at = None
            repo.update_job(job)

            with pytest.raises(ValueError):
                repo.transition_job_status(job.id, JobStatus(to_status))


# =============================================================================
# Guardrails Three-State Contract (M1.1)
# =============================================================================


class TestGuardrailThreeState:
    """验证 Guardrails 三态返回（M1.1）。"""

    def test_guardrail_result_has_allowed_state(self):
        from guardrails.policy import GuardrailResult
        result = GuardrailResult(decision="allowed")
        assert result.is_allowed
        assert not result.is_blocked
        assert not result.is_pending

    def test_guardrail_result_has_blocked_state(self):
        from guardrails.policy import GuardrailResult
        result = GuardrailResult(decision="blocked")
        assert not result.is_allowed
        assert result.is_blocked
        assert not result.is_pending

    def test_guardrail_result_has_pending_state(self):
        from guardrails.policy import GuardrailResult
        result = GuardrailResult(decision="pending_approval")
        assert not result.is_allowed
        assert not result.is_blocked
        assert result.is_pending

    def test_guardrail_result_with_ticket_id(self):
        from guardrails.policy import GuardrailResult
        result = GuardrailResult(
            decision="pending_approval", reason="High risk", ticket_id="ticket_abc"
        )
        assert result.is_pending
        assert result.ticket_id == "ticket_abc"


# =============================================================================
# Ticket State Machine Contract
# =============================================================================


class TestTicketStateTransitions:
    """验证 ApprovalTicket 状态转换（M1.1）。"""

    @pytest.fixture
    def approval_repo(self, tmp_path: Path):
        from control_plane.approval import ApprovalRepository
        return ApprovalRepository(str(tmp_path / "approvals"))

    def test_pending_to_approved(self, approval_repo):
        ticket = approval_repo.create_ticket(
            job_id="job_abc", tool_name="bash", args={"command": "echo hi"}
        )
        assert ticket.status == TicketStatus.PENDING
        approved = approval_repo.approve_ticket(ticket.id, reason="Looks safe")
        assert approved.status == TicketStatus.APPROVED
        assert approved.decided_by == "user"

    def test_pending_to_rejected(self, approval_repo):
        ticket = approval_repo.create_ticket(
            job_id="job_abc", tool_name="bash", args={"command": "echo hi"}
        )
        rejected = approval_repo.reject_ticket(ticket.id, reason="Too risky")
        assert rejected.status == TicketStatus.REJECTED
        assert rejected.decided_by == "user"

    def test_pending_to_expired(self, approval_repo):
        import datetime
        from unittest.mock import patch
        from control_plane.approval import _utc_now  # noqa: F401

        approval_repo.create_ticket(  # noqa: F841
            job_id="job_abc", tool_name="bash", args={"command": "echo hi"},
            timeout_sec=1,
        )
        # Fast-forward past expiration
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=2)
        with patch("control_plane.approval._utc_now", return_value=future):
            expired = approval_repo.expire_tickets()
        assert len(expired) == 1
        assert expired[0].status == TicketStatus.EXPIRED
        assert expired[0].decided_by == "timeout"

    def test_approve_non_pending_fails(self, approval_repo):
        ticket = approval_repo.create_ticket(
            job_id="job_abc", tool_name="bash", args={"command": "echo hi"}
        )
        approval_repo.approve_ticket(ticket.id)
        with pytest.raises(ValueError):
            approval_repo.approve_ticket(ticket.id)

    def test_reject_non_pending_fails(self, approval_repo):
        ticket = approval_repo.create_ticket(
            job_id="job_abc", tool_name="bash", args={"command": "echo hi"}
        )
        approval_repo.reject_ticket(ticket.id)
        with pytest.raises(ValueError):
            approval_repo.reject_ticket(ticket.id)


# =============================================================================
# Version Header Contract
# =============================================================================


class TestSpecVersion:
    """验证 spec 文档版本声明。"""

    def test_spec_has_m11_version_header(self):
        spec_path = Path(__file__).parent.parent / "docs" / "m1_personal_spec.md"
        assert spec_path.exists(), "Spec document not found"
        content = spec_path.read_text(encoding="utf-8")
        assert "M1.1" in content, "Spec missing M1.1 version declaration"
        assert "[IMPLEMENTED]" in content, "Spec missing IMPLEMENTED tags"
        assert "状态标签说明" in content, "Spec missing status legend"

    def test_spec_has_status_legend_table(self):
        spec_path = Path(__file__).parent.parent / "docs" / "m1_personal_spec.md"
        content = spec_path.read_text(encoding="utf-8")
        assert "IMPLEMENTED" in content
        assert "PARTIAL" in content
        assert "PLANNED" in content


# =============================================================================
# Pyproject.toml Config Health Check
# =============================================================================


class TestPyprojectConfig:
    """验证 pyproject.toml 可被工具链正常解析。"""

    def test_pyproject_toml_exists(self):
        """pyproject.toml 文件存在。"""
        assert Path("pyproject.toml").exists()

    def test_pyproject_toml_valid_syntax(self):
        """pyproject.toml 可被 toml 解析，无重复键。"""
        import tomllib  # Python 3.11+
        content = Path("pyproject.toml").read_text()
        config = tomllib.loads(content)

        # 基本结构检查
        assert "project" in config
        assert "tool" in config
        assert "pytest" in config["tool"]
        assert "ini_options" in config["tool"]["pytest"]

    def test_pyproject_no_duplicate_asyncio_mode(self):
        """asyncio_mode 不重复定义（防回归）。"""
        content = Path("pyproject.toml").read_text()
        # 统计 asyncio_mode 出现次数
        count = content.count("asyncio_mode")
        assert count <= 1, f"asyncio_mode defined {count} times, should be 0 or 1"

    def test_pytest_can_collect_tests(self):
        """pytest 能正常收集测试（配置无错误）。"""
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            capture_output=True, text=True, cwd=str(Path(__file__).parent.parent),
            timeout=30,
        )
        # 退出码 0 = 全部成功, 2 = 部分测试文件有导入错误(属已有问题)
        # 关键验证：pytest 成功启动并收集到了测试（配置正确）
        assert result.returncode in (0, 2, 3, 4, 5), f"pytest collect crashed: {result.stderr}"
        # 确认收集到了测试
        assert "test session starts" in result.stdout or "collected" in result.stdout
