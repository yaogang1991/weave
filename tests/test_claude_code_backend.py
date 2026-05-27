"""Tests for M4.1 ClaudeCodeBackend implementation."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backend_models import BackendContext, BackendResult, BackendStatus
from core.dag_models import DAGNode
from core.exceptions import BudgetExhaustedError, NodeTimeoutError, RateLimitError


def _make_node(agent_type: str = "generator", task: str = "test task") -> DAGNode:
    return DAGNode(
        id="node_1",
        agent_type=agent_type,
        task_description=task,
    )


def _make_context(
    node: DAGNode | None = None,
    workspace_path: str | None = "/tmp/test",
) -> BackendContext:
    return BackendContext(
        node=node or _make_node(),
        session_id="sess_1",
        workspace_path=workspace_path,
        job_id="job_1",
    )


# -- Import tests --


class TestClaudeCodeImport:
    def test_import_does_not_require_sdk(self):
        from agent.backends.claude_code import ClaudeCodeBackend
        assert ClaudeCodeBackend is not None


class TestClaudeCodeRuntimeConfig:
    def test_default_values(self):
        from agent.backends.claude_code import ClaudeCodeRuntimeConfig
        cfg = ClaudeCodeRuntimeConfig()
        assert cfg.cli_path == "claude"
        assert cfg.model == ""
        assert cfg.max_turns == 0
        assert cfg.permission_mode == "default"
        assert cfg.allowed_tools == []
        assert cfg.max_budget_usd == 0.0
        assert cfg.timeout_override == 0

    def test_from_core_config(self):
        from agent.backends.claude_code import ClaudeCodeRuntimeConfig
        from core.config import ClaudeCodeConfig as CoreConfig
        core = CoreConfig(cli_path="/usr/local/bin/claude", model="opus-4")
        cfg = ClaudeCodeRuntimeConfig.from_core_config(core)
        assert cfg.cli_path == "/usr/local/bin/claude"
        assert cfg.model == "opus-4"

    def test_allowed_tools_returns_copy(self):
        from agent.backends.claude_code import ClaudeCodeRuntimeConfig
        cfg = ClaudeCodeRuntimeConfig(allowed_tools=["read", "write"])
        tools1 = cfg.allowed_tools
        tools2 = cfg.allowed_tools
        assert tools1 == tools2
        assert tools1 is not tools2


class TestClaudeCodeBackendConstruction:
    def test_backend_name(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        assert backend.name == "claude_code"

    def test_get_capabilities(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        assert "generator" in backend.get_capabilities()
        assert "planner" in backend.get_capabilities()
        assert "evaluator" in backend.get_capabilities()


class TestClaudeCodeBackendHealthCheck:
    def test_healthy_when_cli_available(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
            
            assert asyncio.run(backend.health_check())

    def test_unhealthy_when_nothing_available(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        with patch("shutil.which", return_value=None):
            backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
            backend._sdk_available = False
            
            assert not asyncio.run(backend.health_check())


class TestClaudeCodeBackendBuildPrompt:
    def test_generator_task(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        ctx = _make_context(node=_make_node("generator", "Build a REST API"))
        prompt = backend._build_prompt(ctx)
        assert "code generation" in prompt.lower()
        assert "Build a REST API" in prompt

    def test_with_input_artifacts(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        from core.dag_models import HandoffArtifact
        from datetime import datetime, timezone
        artifact = HandoffArtifact(
            from_agent="planner",
            to_agent="generator",
            content="Plan: create models.py",
            file_paths=["models.py"],
            created_at=datetime.now(timezone.utc),
        )
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        ctx = BackendContext(
            node=_make_node(),
            session_id="s1",
            artifacts=[artifact],
        )
        prompt = backend._build_prompt(ctx)
        assert "models.py" in prompt
        assert "Plan: create models.py" in prompt


class TestClaudeCodeBackendExecuteCLI:
    def _make_stream_lines(self, result="Done", is_error=False,
                           usage=None, session_id="sess_abc"):
        """Build NDJSON lines for stream-json output."""
        lines = []
        if session_id:
            lines.append(json.dumps({
                "type": "system", "session_id": session_id,
            }).encode() + b"\n")
        lines.append(json.dumps({
            "type": "result",
            "result": result,
            "is_error": is_error,
            "usage": usage or {"input_tokens": 100, "output_tokens": 50},
            "session_id": session_id,
        }).encode() + b"\n")
        return lines

    def _mock_process(self, lines, returncode=0, stderr=b""):
        """Create a mock asyncio subprocess."""
        process = MagicMock()
        process.returncode = returncode
        line_iter = iter(lines)

        async def readline():
            return next(line_iter, b"")

        stdout_mock = MagicMock()
        stdout_mock.readline = readline
        process.stdout = stdout_mock

        stderr_mock = MagicMock()

        async def read_stderr():
            return stderr

        stderr_mock.read = read_stderr
        process.stderr = stderr_mock
        process.terminate = MagicMock()
        process.kill = MagicMock()
        return process

    def test_cli_success(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        backend._sdk_available = False
        ctx = _make_context()

        lines = self._make_stream_lines()
        mock_proc = self._mock_process(lines, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = asyncio.run(
                backend._execute_via_cli(ctx, "test prompt"),
            )

        assert result.status == BackendStatus.COMPLETED
        assert result.metadata["token_usage"]["input_tokens"] == 100
        assert result.metadata["token_usage"]["output_tokens"] == 50

    def test_cli_nonzero_exit(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        backend._sdk_available = False
        ctx = _make_context()

        mock_proc = self._mock_process(
            [], returncode=1, stderr=b"Something went wrong",
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = asyncio.run(
                backend._execute_via_cli(ctx, "test prompt"),
            )

        assert result.status == BackendStatus.FAILED
        assert "Something went wrong" in result.error

    def test_cli_json_parse_error(self):
        """Non-JSON lines are skipped; if no result event, output is empty."""
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        ctx = _make_context()

        lines = [b"not valid json {{{\n"]
        mock_proc = self._mock_process(lines, returncode=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = asyncio.run(
                backend._execute_via_cli(ctx, "test prompt"),
            )

        assert result.status == BackendStatus.COMPLETED
        assert result.output == ""

    def test_cli_timeout_raises_node_timeout_error(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        cfg = ClaudeCodeRuntimeConfig(timeout_override=1)
        backend = ClaudeCodeBackend(config=cfg)
        ctx = _make_context()

        # Mock process that hangs forever.
        process = MagicMock()
        process.returncode = None

        async def readline_hang():
            await asyncio.sleep(100)
            return b""

        stdout_mock = MagicMock()
        stdout_mock.readline = readline_hang
        process.stdout = stdout_mock
        process.stderr = MagicMock()
        process.stderr.read = AsyncMock(return_value=b"")
        process.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=process):
            with pytest.raises(NodeTimeoutError):
                asyncio.run(
                    backend._execute_via_cli(ctx, "test prompt"),
                )

    def test_cli_rate_limit_error(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        backend._sdk_available = False
        ctx = _make_context()

        mock_proc = self._mock_process(
            [], returncode=1,
            stderr=b"Error: rate limit exceeded",
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RateLimitError):
                asyncio.run(
                    backend._execute_via_cli(ctx, "test prompt"),
                )

    def test_cli_budget_error(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        backend._sdk_available = False
        ctx = _make_context()

        mock_proc = self._mock_process(
            [], returncode=1,
            stderr=b"Budget exhausted",
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(BudgetExhaustedError):
                asyncio.run(
                    backend._execute_via_cli(ctx, "test prompt"),
                )

    def test_cli_file_not_found(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        cfg = ClaudeCodeRuntimeConfig(cli_path="/nonexistent/claude")
        backend = ClaudeCodeBackend(config=cfg)
        ctx = _make_context()

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError,
        ):
            result = asyncio.run(
                backend._execute_via_cli(ctx, "test prompt"),
            )

        assert result.status == BackendStatus.FAILED
        assert "not found" in result.error.lower()


class TestClaudeCodeBackendExecuteDispatch:
    def test_fallback_to_cli_when_sdk_fails(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        backend._sdk_available = True
        ctx = _make_context()

        lines = [
            json.dumps({
                "type": "result",
                "result": "CLI fallback worked",
                "is_error": False,
                "usage": {"input_tokens": 50, "output_tokens": 25},
            }).encode() + b"\n",
        ]
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        line_iter = iter(lines)

        async def readline():
            return next(line_iter, b"")

        stdout_mock = MagicMock()
        stdout_mock.readline = readline
        mock_proc.stdout = stdout_mock
        stderr_mock = MagicMock()
        stderr_mock.read = AsyncMock(return_value=b"")
        mock_proc.stderr = stderr_mock
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()

        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                with patch.object(
                    backend, "_execute_via_sdk",
                    side_effect=RuntimeError("SDK crashed"),
                ):
                    result = asyncio.run(
                        backend.execute(ctx),
                    )

        assert result.status == BackendStatus.COMPLETED
        assert "CLI fallback" in result.output

    def test_returns_failed_when_nothing_available(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        backend._sdk_available = False
        ctx = _make_context()

        with patch("shutil.which", return_value=None):
            
            result = asyncio.run(
                backend.execute(ctx),
            )

        assert result.status == BackendStatus.FAILED


class TestClaudeCodeBackendBuildCLICommand:
    def _backend(self, **kwargs):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        return ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig(**kwargs))

    def test_basic_command(self):
        backend = self._backend()
        ctx = _make_context()
        cmd = backend._build_cli_command(ctx, "test prompt")
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert cmd[-1] == "test prompt"

    def test_includes_model(self):
        backend = self._backend(model="opus-4")
        ctx = _make_context()
        cmd = backend._build_cli_command(ctx, "test")
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "opus-4"

    def test_includes_max_turns(self):
        backend = self._backend(max_turns=10)
        ctx = _make_context()
        cmd = backend._build_cli_command(ctx, "test")
        assert "--max-turns" in cmd
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "10"

    def test_includes_session_id(self):
        backend = self._backend()
        ctx = _make_context()
        cmd = backend._build_cli_command(ctx, "test")
        assert "--session-id" in cmd
        idx = cmd.index("--session-id")
        assert cmd[idx + 1] == "sess_1"

    def test_no_session_id_when_empty(self):
        backend = self._backend()
        ctx = BackendContext(node=_make_node(), session_id="")
        cmd = backend._build_cli_command(ctx, "test")
        assert "--session-id" not in cmd


class TestClaudeCodeBackendTokenUsage:
    def test_extract_from_full_response(self):
        from agent.backends.claude_code import ClaudeCodeBackend
        usage = {"input_tokens": 1500, "output_tokens": 800, "cache_read": 500}
        result = ClaudeCodeBackend._extract_token_usage(usage)
        assert result == {"input_tokens": 1500, "output_tokens": 800}

    def test_extract_from_empty_dict(self):
        from agent.backends.claude_code import ClaudeCodeBackend
        result = ClaudeCodeBackend._extract_token_usage({})
        assert result == {"input_tokens": 0, "output_tokens": 0}

    def test_extract_from_none(self):
        from agent.backends.claude_code import ClaudeCodeBackend
        result = ClaudeCodeBackend._extract_token_usage(None)
        assert result == {"input_tokens": 0, "output_tokens": 0}


class TestClaudeCodeBackendArtifactDiscovery:
    def test_discovers_via_git_diff(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        ctx = _make_context(workspace_path="/tmp/project")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "src/main.py\nsrc/utils.py\n"

        with patch("agent.backends.claude_code.run_with_progress", return_value=mock_result):
            artifacts = backend._discover_artifacts(ctx)

        assert artifacts == ["src/main.py", "src/utils.py"]

    def test_returns_empty_no_workspace(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        ctx = _make_context(workspace_path=None)
        assert backend._discover_artifacts(ctx) == []

    def test_returns_empty_on_git_failure(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        ctx = _make_context(workspace_path="/tmp")

        with patch("agent.backends.claude_code.run_with_progress", side_effect=FileNotFoundError()):
            assert backend._discover_artifacts(ctx) == []


class TestClaudeCodeBackendErrorClassification:
    def _backend(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        return ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())

    def test_rate_limit(self):
        backend = self._backend()
        ctx = _make_context()
        result = backend._classify_error("rate limit exceeded", ctx)
        assert isinstance(result, RateLimitError)

    def test_timeout(self):
        backend = self._backend()
        ctx = _make_context()
        result = backend._classify_error("operation timed out", ctx)
        assert isinstance(result, NodeTimeoutError)

    def test_budget(self):
        backend = self._backend()
        ctx = _make_context()
        result = backend._classify_error("budget exhausted", ctx)
        assert isinstance(result, BudgetExhaustedError)

    def test_budget_subtype(self):
        backend = self._backend()
        ctx = _make_context()
        result = backend._classify_error("error", ctx, subtype="error_max_budget_usd")
        assert isinstance(result, BudgetExhaustedError)

    def test_auth_returns_none(self):
        backend = self._backend()
        ctx = _make_context()
        result = backend._classify_error("invalid api key", ctx)
        assert result is None

    def test_generic_returns_none(self):
        backend = self._backend()
        ctx = _make_context()
        result = backend._classify_error("something unexpected", ctx)
        assert result is None


class TestBackendResultTokenUsage:
    def test_to_dict_includes_token_usage(self):
        r = BackendResult(
            status=BackendStatus.COMPLETED,
            summary="done",
            metadata={
                "token_usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )
        d = r.to_dict()
        assert d["token_usage"] == {"input_tokens": 100, "output_tokens": 50}

    def test_to_dict_without_token_usage(self):
        r = BackendResult(status=BackendStatus.COMPLETED, summary="done")
        d = r.to_dict()
        assert "token_usage" not in d
