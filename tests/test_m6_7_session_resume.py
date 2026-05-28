"""Tests for M6.7: Session Resume + BackendResult extension + bidirectional comms protocol."""
from core.backend_models import BackendContext, BackendResult, BackendStatus
from core.dag_models import DAGNode


# -- Helpers --


def _make_node(agent_type: str = "generator", task: str = "test task") -> DAGNode:
    return DAGNode(
        id="node_1",
        agent_type=agent_type,
        task_description=task,
    )


def _make_context(
    node: DAGNode | None = None,
    workspace_path: str | None = "/tmp/test",
    session_id: str = "sess_1",
    resume_session_id: str = "",
) -> BackendContext:
    return BackendContext(
        node=node or _make_node(),
        session_id=session_id,
        workspace_path=workspace_path,
        job_id="job_1",
        resume_session_id=resume_session_id,
    )


# -- BackendResult field tests --


class TestBackendResultSessionFields:
    def test_session_id_default_empty(self):
        r = BackendResult(status=BackendStatus.COMPLETED)
        assert r.session_id == ""

    def test_can_resume_default_false(self):
        r = BackendResult(status=BackendStatus.COMPLETED)
        assert r.can_resume is False

    def test_session_id_set(self):
        r = BackendResult(
            status=BackendStatus.COMPLETED,
            session_id="sess_abc123",
        )
        assert r.session_id == "sess_abc123"

    def test_can_resume_set_true(self):
        r = BackendResult(
            status=BackendStatus.COMPLETED,
            session_id="sess_abc123",
            can_resume=True,
        )
        assert r.can_resume is True

    def test_to_dict_promotes_can_resume(self):
        r = BackendResult(
            status=BackendStatus.COMPLETED,
            summary="done",
            session_id="sess_abc",
            can_resume=True,
        )
        d = r.to_dict()
        assert d["can_resume"] is True
        assert d["session_id"] == "sess_abc"

    def test_to_dict_omits_can_resume_when_false(self):
        r = BackendResult(
            status=BackendStatus.COMPLETED,
            summary="done",
        )
        d = r.to_dict()
        assert "can_resume" not in d

    def test_to_dict_prefers_first_class_over_metadata(self):
        r = BackendResult(
            status=BackendStatus.COMPLETED,
            summary="done",
            session_id="first_class_id",
            metadata={"session_id": "metadata_id"},
        )
        d = r.to_dict()
        assert d["session_id"] == "first_class_id"

    def test_to_dict_falls_back_to_metadata_when_default(self):
        r = BackendResult(
            status=BackendStatus.COMPLETED,
            summary="done",
            metadata={"session_id": "from_metadata"},
        )
        d = r.to_dict()
        assert d["session_id"] == "from_metadata"


# -- BackendContext field tests --


class TestBackendContextResumeSessionId:
    def test_resume_session_id_default_empty(self):
        ctx = BackendContext(node=_make_node())
        assert ctx.resume_session_id == ""

    def test_resume_session_id_set(self):
        ctx = BackendContext(
            node=_make_node(),
            resume_session_id="sess_previous",
        )
        assert ctx.resume_session_id == "sess_previous"


# -- Bidirectional protocol tests --


class TestBidirectionalConfig:
    def test_default_values(self):
        from agent.backends.bidirectional import BidirectionalConfig
        cfg = BidirectionalConfig()
        assert cfg.enabled is False
        assert cfg.input_format == "stream-json"
        assert cfg.supports_tool_result is False
        assert cfg.supports_initialize is False

    def test_enabled_config(self):
        from agent.backends.bidirectional import BidirectionalConfig
        cfg = BidirectionalConfig(enabled=True, supports_tool_result=True)
        assert cfg.enabled is True
        assert cfg.supports_tool_result is True


class TestInitializeRequest:
    def test_default_type(self):
        from agent.backends.bidirectional import InitializeRequest
        req = InitializeRequest()
        assert req.type == "initialize"
        assert req.agents == []
        assert req.hooks == {}
        assert req.mcp_servers == {}

    def test_with_agents(self):
        from agent.backends.bidirectional import InitializeRequest
        req = InitializeRequest(agents=[{"name": "planner"}])
        assert len(req.agents) == 1
        assert req.agents[0]["name"] == "planner"


class TestToolResultMessage:
    def test_default_type(self):
        from agent.backends.bidirectional import ToolResultMessage
        msg = ToolResultMessage()
        assert msg.type == "tool_result"
        assert msg.tool_use_id == ""
        assert msg.content == ""

    def test_with_values(self):
        from agent.backends.bidirectional import ToolResultMessage
        msg = ToolResultMessage(tool_use_id="tu_123", content="file contents")
        assert msg.tool_use_id == "tu_123"
        assert msg.content == "file contents"


# -- CLI command building tests --


class TestBuildCLICommandResume:
    def _backend(self, **kwargs):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        return ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig(**kwargs))

    def test_includes_resume_when_resume_session_id_set(self):
        backend = self._backend()
        ctx = _make_context(resume_session_id="sess_previous")
        cmd = backend._build_cli_command(ctx, "continue work")
        assert "--resume" in cmd

    def test_no_resume_when_resume_session_id_empty(self):
        backend = self._backend()
        ctx = _make_context(resume_session_id="")
        cmd = backend._build_cli_command(ctx, "test")
        assert "--resume" not in cmd

    def test_resume_adds_session_id_if_not_present(self):
        backend = self._backend()
        ctx = BackendContext(
            node=_make_node(),
            session_id="",
            resume_session_id="sess_old",
        )
        cmd = backend._build_cli_command(ctx, "test")
        assert "--resume" in cmd
        assert "--session-id" in cmd
        idx = cmd.index("--session-id")
        assert cmd[idx + 1] == "sess_old"

    def test_resume_keeps_existing_session_id(self):
        backend = self._backend()
        ctx = _make_context(session_id="sess_new", resume_session_id="sess_old")
        cmd = backend._build_cli_command(ctx, "test")
        assert "--resume" in cmd
        idx = cmd.index("--session-id")
        assert cmd[idx + 1] == "sess_new"
        assert cmd.count("--session-id") == 1


# -- _build_stream_result tests --


class TestBuildStreamResult:
    def test_sets_session_id_and_can_resume(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        from agent.backends.stream_parser import StreamParser

        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        parser = StreamParser()
        usage = {"input_tokens": 100, "output_tokens": 50}
        state = {"session_id": "sess_abc", "result": "Done", "error": ""}
        artifacts = ["file.py"]

        result = backend._build_stream_result(parser, usage, state, artifacts)
        assert result.session_id == "sess_abc"
        assert result.can_resume is True

    def test_can_resume_false_when_no_session_id(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        from agent.backends.stream_parser import StreamParser

        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        parser = StreamParser()
        usage = {"input_tokens": 0, "output_tokens": 0}
        state = {"session_id": "", "result": "", "error": ""}
        artifacts = []

        result = backend._build_stream_result(parser, usage, state, artifacts)
        assert result.session_id == ""
        assert result.can_resume is False

    def test_error_result_still_sets_session_id(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig
        from agent.backends.stream_parser import StreamParser

        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        parser = StreamParser()
        usage = {"input_tokens": 50, "output_tokens": 25}
        state = {"session_id": "sess_err", "result": "", "error": "Something failed"}
        artifacts = []

        result = backend._build_stream_result(parser, usage, state, artifacts)
        assert result.status == BackendStatus.FAILED
        assert result.session_id == "sess_err"
        assert result.can_resume is True


# -- _parse_sdk_result tests --


class TestParseSDKResult:
    def test_sets_session_id_and_can_resume(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig

        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        ctx = _make_context()

        raw_result = {
            "result": "SDK completed",
            "session_id": "sdk_sess_123",
            "usage": {"input_tokens": 200, "output_tokens": 100},
        }

        result = backend._parse_sdk_result(raw_result, ctx)
        assert result.session_id == "sdk_sess_123"
        assert result.can_resume is True
        assert result.status == BackendStatus.COMPLETED

    def test_no_session_id_means_cannot_resume(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig

        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        ctx = _make_context()

        raw_result = {
            "result": "Done without session",
        }

        result = backend._parse_sdk_result(raw_result, ctx)
        assert result.session_id == ""
        assert result.can_resume is False

    def test_sdk_error_with_session_id_still_resumable(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig

        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        ctx = _make_context()

        raw_result = {
            "result": "bad output",
            "is_error": True,
            "errors": ["something broke"],
            "session_id": "sdk_err_sess",
        }

        result = backend._parse_sdk_result(raw_result, ctx)
        assert result.status == BackendStatus.FAILED
        assert result.session_id == "sdk_err_sess"
        assert result.can_resume is True

    def test_sdk_error_no_session(self):
        from agent.backends.claude_code import ClaudeCodeBackend, ClaudeCodeRuntimeConfig

        backend = ClaudeCodeBackend(config=ClaudeCodeRuntimeConfig())
        ctx = _make_context()

        raw_result = {
            "result": "bad output",
            "is_error": True,
            "errors": ["something broke"],
        }

        result = backend._parse_sdk_result(raw_result, ctx)
        assert result.status == BackendStatus.FAILED
        assert result.session_id == ""
        assert result.can_resume is False
