"""
Comprehensive tests for control_plane/execution_factory.py.

ExecutionFactory builds the object graph for DAG execution:
IntelligentOrchestrator, DAGExecutionEngine, AgentPool, Guardrails,
ToolRegistry, EvaluatorEngine, and BackendRegistry.

All external dependencies are mocked. Tests cover:
- Factory construction (defaults and custom parameters)
- create_orchestrator (learning optimizer extraction, token estimator injection)
- create_execution_engine (full wiring, event handler, guardrails selection)
- _register_external_backends (codex registration, import failure)
- load_project_guardrails (static method, yaml parsing, edge cases)
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import (
    MagicMock,
    Mock,
    patch,
)

import pytest

# Ensure project root on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import LLMConfig
from core.config.timeout import WatchdogConfig
from core.guardrail_models import (
    GuardrailPolicy,
    PermissionMode,
    PersonalGuardrailPolicy,
)
from session.store import SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_llm_config(**overrides) -> LLMConfig:
    defaults = dict(api_key="test-key", model="test-model")
    defaults.update(overrides)
    return LLMConfig(**defaults)


def _make_watchdog_config(**overrides) -> WatchdogConfig:
    defaults = dict(enabled=True, heartbeat_interval_sec=30.0, heartbeat_miss_threshold=12)
    defaults.update(overrides)
    return WatchdogConfig(**defaults)


def _make_factory(**overrides):
    """Create an ExecutionFactory with sensible test defaults."""
    from control_plane.execution_factory import ExecutionFactory

    defaults = dict(
        llm_config=_make_llm_config(),
        max_parallel=4,
        agent_timeout=300,
        max_context_tokens=100_000,
        max_iterations=50,
        artifact_path="/tmp/artifacts",
        non_interactive=False,
        watchdog_config=_make_watchdog_config(),
        hooks=[],
        approval_repo=None,
        policy=None,
        budget_manager=None,
    )
    defaults.update(overrides)
    return ExecutionFactory(**defaults)


# ---------------------------------------------------------------------------
# 1. Factory construction tests
# ---------------------------------------------------------------------------


class TestFactoryConstruction:
    """Test ExecutionFactory.__init__ stores all parameters."""

    def test_stores_llm_config(self):
        cfg = _make_llm_config(model="custom-model")
        factory = _make_factory(llm_config=cfg)
        assert factory._llm_config.model == "custom-model"

    def test_stores_max_parallel(self):
        factory = _make_factory(max_parallel=8)
        assert factory._max_parallel == 8

    def test_stores_agent_timeout(self):
        factory = _make_factory(agent_timeout=600)
        assert factory._agent_timeout == 600

    def test_stores_max_context_tokens(self):
        factory = _make_factory(max_context_tokens=200_000)
        assert factory._max_context_tokens == 200_000

    def test_stores_max_iterations(self):
        factory = _make_factory(max_iterations=100)
        assert factory._max_iterations == 100

    def test_stores_artifact_path(self):
        factory = _make_factory(artifact_path="/data/art")
        assert factory._artifact_path == "/data/art"

    def test_stores_non_interactive(self):
        factory = _make_factory(non_interactive=True)
        assert factory._non_interactive is True

    def test_stores_watchdog_config(self):
        wc = _make_watchdog_config(enabled=False)
        factory = _make_factory(watchdog_config=wc)
        assert factory._watchdog_config.enabled is False

    def test_stores_hooks(self):
        hook1, hook2 = MagicMock(), MagicMock()
        factory = _make_factory(hooks=[hook1, hook2])
        assert factory._hooks == [hook1, hook2]

    def test_stores_optional_approval_repo(self):
        repo = MagicMock()
        factory = _make_factory(approval_repo=repo)
        assert factory._approval_repo is repo

    def test_stores_optional_policy(self):
        policy = GuardrailPolicy(mode=PermissionMode.AUTO)
        factory = _make_factory(policy=policy)
        assert factory._policy is policy

    def test_stores_optional_budget_manager(self):
        bm = MagicMock()
        factory = _make_factory(budget_manager=bm)
        assert factory._budget_manager is bm

    def test_defaults_none_for_optional_params(self):
        factory = _make_factory()
        assert factory._approval_repo is None
        assert factory._policy is None
        assert factory._budget_manager is None


# ---------------------------------------------------------------------------
# 2. create_orchestrator tests
# ---------------------------------------------------------------------------


class TestCreateOrchestrator:
    """Test ExecutionFactory.create_orchestrator."""

    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_returns_orchestrator_instance(self, mock_inject, MockOrch):
        """create_orchestrator returns an IntelligentOrchestrator."""
        mock_instance = MagicMock()
        MockOrch.return_value = mock_instance
        # Patch the import inside _inject_token_estimator
        factory = _make_factory()
        with patch.dict("sys.modules", {}):
            store = MagicMock(spec=SessionStore)
            result = factory.create_orchestrator(store)
        assert result is mock_instance

    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_passes_llm_config_to_orchestrator(self, mock_inject, MockOrch):
        """Orchestrator receives the factory's LLM config."""
        MockOrch.return_value = MagicMock()
        cfg = _make_llm_config(model="my-model")
        factory = _make_factory(llm_config=cfg)
        store = MagicMock(spec=SessionStore)
        factory.create_orchestrator(store)
        call_kwargs = MockOrch.call_args
        assert call_kwargs[1]["llm_config"] is cfg

    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_passes_session_store_to_orchestrator(self, mock_inject, MockOrch):
        """Orchestrator receives the provided session store."""
        MockOrch.return_value = MagicMock()
        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        factory.create_orchestrator(store)
        call_kwargs = MockOrch.call_args
        assert call_kwargs[1]["session_store"] is store

    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_extracts_learning_optimizer_from_hooks(self, mock_inject, MockOrch):
        """If a hook has an .optimizer attribute, it is passed to the orchestrator."""
        MockOrch.return_value = MagicMock()
        hook_with_optimizer = MagicMock()
        hook_with_optimizer.optimizer = "fake_optimizer"
        factory = _make_factory(hooks=[hook_with_optimizer])
        store = MagicMock(spec=SessionStore)
        factory.create_orchestrator(store)
        call_kwargs = MockOrch.call_args
        assert call_kwargs[1]["learning_optimizer"] == "fake_optimizer"

    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_no_learning_optimizer_when_hooks_empty(self, mock_inject, MockOrch):
        """No learning_optimizer when hooks list is empty."""
        MockOrch.return_value = MagicMock()
        factory = _make_factory(hooks=[])
        store = MagicMock(spec=SessionStore)
        factory.create_orchestrator(store)
        call_kwargs = MockOrch.call_args
        assert call_kwargs[1]["learning_optimizer"] is None

    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_uses_first_hook_with_optimizer(self, mock_inject, MockOrch):
        """Only the first hook with an .optimizer attribute is used."""
        MockOrch.return_value = MagicMock()
        hook1 = MagicMock()
        hook1.optimizer = "first_optimizer"
        hook2 = MagicMock()
        hook2.optimizer = "second_optimizer"
        factory = _make_factory(hooks=[hook1, hook2])
        store = MagicMock(spec=SessionStore)
        factory.create_orchestrator(store)
        call_kwargs = MockOrch.call_args
        assert call_kwargs[1]["learning_optimizer"] == "first_optimizer"

    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_creates_fresh_agent_registry(self, mock_inject, MockOrch):
        """Each orchestrator gets a new AgentRegistry instance."""
        MockOrch.return_value = MagicMock()
        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        factory.create_orchestrator(store)
        call_kwargs = MockOrch.call_args
        from core.agent_registry import AgentRegistry
        assert isinstance(call_kwargs[1]["agent_registry"], AgentRegistry)


# ---------------------------------------------------------------------------
# 3. _inject_token_estimator tests
# ---------------------------------------------------------------------------


class TestInjectTokenEstimator:
    """Test ExecutionFactory._inject_token_estimator."""

    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_calls_inject_with_correct_params(self, mock_inject):
        """_inject_token_estimator delegates to inject_token_estimator."""
        mock_inject.return_value = None
        cfg = _make_llm_config(api_key="k", model="m")
        factory = _make_factory(llm_config=cfg)
        orchestrator = MagicMock()

        # The actual import is inside the method, so we patch the import target
        with patch.dict("sys.modules", {"core.token_estimator": MagicMock(inject_token_estimator=mock_inject)}):
            factory._inject_token_estimator(orchestrator)

        mock_inject.assert_called_once_with(
            orchestrator=orchestrator,
            api_key="k",
            model="m",
            provider="anthropic",
            base_url=cfg.base_url,
        )

    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_passes_provider_from_llm_config(self, mock_inject):
        """Provider attribute is forwarded from LLM config."""
        mock_inject.return_value = None
        cfg = _make_llm_config(provider="openai")
        factory = _make_factory(llm_config=cfg)
        orchestrator = MagicMock()

        with patch.dict("sys.modules", {"core.token_estimator": MagicMock(inject_token_estimator=mock_inject)}):
            factory._inject_token_estimator(orchestrator)

        call_kwargs = mock_inject.call_args[1]
        assert call_kwargs["provider"] == "openai"


# ---------------------------------------------------------------------------
# 4. load_project_guardrails (static method) tests
# ---------------------------------------------------------------------------


class TestLoadProjectGuardrails:
    """Test ExecutionFactory.load_project_guardrails static method."""

    def test_returns_empty_dict_when_work_dir_is_none(self):
        """No work_dir returns empty dict."""
        result = ExecutionFactory.load_project_guardrails(None)
        assert result == {}

    def test_returns_empty_dict_when_no_config_file(self, tmp_path):
        """Missing .weave/config.yaml returns empty dict."""
        result = ExecutionFactory.load_project_guardrails(tmp_path)
        assert result == {}

    def test_loads_permission_mode_from_yaml(self, tmp_path):
        """permission_mode is parsed and returned as PermissionMode enum."""
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text(
            "guardrails:\n  permission_mode: dont_ask\n",
            encoding="utf-8",
        )
        result = ExecutionFactory.load_project_guardrails(tmp_path)
        assert result["permission_mode"] == PermissionMode.DONT_ASK

    def test_loads_auto_approve_read_from_yaml(self, tmp_path):
        """auto_approve_read boolean is loaded."""
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text(
            "guardrails:\n  auto_approve_read: false\n",
            encoding="utf-8",
        )
        result = ExecutionFactory.load_project_guardrails(tmp_path)
        assert result["auto_approve_read"] is False

    def test_loads_denied_commands_from_yaml(self, tmp_path):
        """denied_commands list is loaded."""
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text(
            "guardrails:\n  denied_commands:\n    - rm -rf /\n    - format\n",
            encoding="utf-8",
        )
        result = ExecutionFactory.load_project_guardrails(tmp_path)
        assert result["denied_commands"] == ["rm -rf /", "format"]

    def test_loads_allowed_tools_from_yaml(self, tmp_path):
        """allowed_tools list is loaded."""
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text(
            "guardrails:\n  allowed_tools:\n    - read\n    - write\n",
            encoding="utf-8",
        )
        result = ExecutionFactory.load_project_guardrails(tmp_path)
        assert result["allowed_tools"] == ["read", "write"]

    def test_returns_empty_for_empty_yaml(self, tmp_path):
        """Empty config.yaml returns empty dict."""
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text("", encoding="utf-8")
        result = ExecutionFactory.load_project_guardrails(tmp_path)
        assert result == {}

    def test_ignores_unrelated_guardrail_keys(self, tmp_path):
        """Unknown keys under guardrails are silently ignored."""
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text(
            "guardrails:\n  unknown_key: value\n  permission_mode: auto\n",
            encoding="utf-8",
        )
        result = ExecutionFactory.load_project_guardrails(tmp_path)
        assert "unknown_key" not in result
        assert result["permission_mode"] == PermissionMode.AUTO

    def test_handles_malformed_yaml_gracefully(self, tmp_path):
        """Malformed YAML does not raise, returns empty dict."""
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text(": : invalid: yaml: [", encoding="utf-8")
        # Should not raise; logger.debug is called instead
        result = ExecutionFactory.load_project_guardrails(tmp_path)
        assert isinstance(result, dict)

    def test_handles_guardrails_section_without_recognized_keys(self, tmp_path):
        """Guardrails section present but no recognized keys returns empty dict."""
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text(
            "guardrails:\n  some_other_setting: true\n",
            encoding="utf-8",
        )
        result = ExecutionFactory.load_project_guardrails(tmp_path)
        assert result == {}

    def test_loads_all_guardrail_fields_together(self, tmp_path):
        """All four recognized guardrail keys are loaded simultaneously."""
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text(
            "guardrails:\n"
            "  permission_mode: plan\n"
            "  auto_approve_read: true\n"
            "  denied_commands:\n"
            "    - shutdown\n"
            "  allowed_tools:\n"
            "    - read\n"
            "    - glob\n",
            encoding="utf-8",
        )
        result = ExecutionFactory.load_project_guardrails(tmp_path)
        assert result["permission_mode"] == PermissionMode.PLAN
        assert result["auto_approve_read"] is True
        assert result["denied_commands"] == ["shutdown"]
        assert result["allowed_tools"] == ["read", "glob"]


# ---------------------------------------------------------------------------
# 5. _register_external_backends tests
# ---------------------------------------------------------------------------


class TestRegisterExternalBackends:
    """Test ExecutionFactory._register_external_backends."""

    def test_skips_when_codex_disabled(self):
        """No registration when codex.enabled is False."""
        factory = _make_factory()
        registry = MagicMock()
        config = MagicMock()
        config.codex.enabled = False
        factory._register_external_backends(registry, config)
        registry.register.assert_not_called()

    @patch.dict("sys.modules", {
        "agent.backends.codex": MagicMock(),
    })
    def test_registers_codex_when_enabled(self):
        """CodexBackend is registered when codex.enabled is True."""
        factory = _make_factory()
        registry = MagicMock()
        config = MagicMock()
        config.codex.enabled = True
        mock_codex_cls = MagicMock()
        mock_codex_instance = MagicMock()
        mock_codex_cls.return_value = mock_codex_instance

        with patch.dict("sys.modules", {"agent.backends.codex": MagicMock(CodexBackend=mock_codex_cls)}):
            factory._register_external_backends(registry, config)

        registry.register.assert_called_once_with("codex", mock_codex_instance)

    def test_handles_import_error_gracefully(self):
        """Import failure for CodexBackend is silently caught."""
        factory = _make_factory()
        registry = MagicMock()
        config = MagicMock()
        config.codex.enabled = True

        # Force ImportError by making the import fail
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "agent.backends.codex":
                raise ImportError("no codex")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            factory._register_external_backends(registry, config)

        registry.register.assert_not_called()


# ---------------------------------------------------------------------------
# 6. create_execution_engine integration tests (heavily mocked)
# ---------------------------------------------------------------------------


class TestCreateExecutionEngine:
    """Test ExecutionFactory.create_execution_engine with mocked internals."""

    @patch("control_plane.execution_factory.WeaveConfig")
    @patch("control_plane.execution_factory.EvaluatorEngine")
    @patch("control_plane.execution_factory.DAGExecutionEngine")
    @patch("control_plane.execution_factory.LightweightLLMCaller")
    @patch("control_plane.execution_factory.BuiltinBackend")
    @patch("control_plane.execution_factory.BackendRegistry")
    @patch("control_plane.execution_factory.AgentPool")
    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.ToolRegistry")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_returns_engine_instance(
        self, mock_inject, MockToolReg, MockOrch, MockPool,
        MockBackendReg, MockBuiltin, MockLWCaller, MockEngine,
        MockEval, MockConfig,
    ):
        """create_execution_engine returns a DAGExecutionEngine."""
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="builtin",
            claude_code=MagicMock(enabled=False),
            codex=MagicMock(enabled=False),
        )
        MockOrch.return_value = MagicMock()
        MockPool.return_value.get_executor.return_value = MagicMock()
        mock_engine = MagicMock()
        MockEngine.return_value = mock_engine

        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        result = factory.create_execution_engine(
            session_id="sess-1",
            store=store,
            work_dir=Path("/tmp/work"),
        )
        assert result is mock_engine

    @patch("control_plane.execution_factory.WeaveConfig")
    @patch("control_plane.execution_factory.EvaluatorEngine")
    @patch("control_plane.execution_factory.DAGExecutionEngine")
    @patch("control_plane.execution_factory.LightweightLLMCaller")
    @patch("control_plane.execution_factory.BuiltinBackend")
    @patch("control_plane.execution_factory.BackendRegistry")
    @patch("control_plane.execution_factory.AgentPool")
    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.ToolRegistry")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_non_interactive_uses_dont_ask_mode(
        self, mock_inject, MockToolReg, MockOrch, MockPool,
        MockBackendReg, MockBuiltin, MockLWCaller, MockEngine,
        MockEval, MockConfig,
    ):
        """In non-interactive mode, default permission mode is DONT_ASK."""
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="builtin",
            claude_code=MagicMock(enabled=False),
            codex=MagicMock(enabled=False),
        )
        MockOrch.return_value = MagicMock()
        MockPool.return_value.get_executor.return_value = MagicMock()
        MockEngine.return_value = MagicMock()

        factory = _make_factory(non_interactive=True, policy=None)
        store = MagicMock(spec=SessionStore)

        # Capture the Guardrails/PersonalGuardrails constructor args
        with patch("control_plane.execution_factory.Guardrails") as MockGuardrails:
            MockGuardrails.return_value = MagicMock()
            with patch("control_plane.execution_factory.PersonalGuardrails"):
                factory.create_execution_engine(
                    session_id="sess-2",
                    store=store,
                    work_dir=Path("/tmp/work"),
                )
            # Find the GuardrailPolicy arg in Guardrails call
            guardrails_call = MockGuardrails.call_args
            policy_arg = guardrails_call[0][0]
            assert policy_arg.mode == PermissionMode.DONT_ASK

    @patch("control_plane.execution_factory.WeaveConfig")
    @patch("control_plane.execution_factory.EvaluatorEngine")
    @patch("control_plane.execution_factory.DAGExecutionEngine")
    @patch("control_plane.execution_factory.LightweightLLMCaller")
    @patch("control_plane.execution_factory.BuiltinBackend")
    @patch("control_plane.execution_factory.BackendRegistry")
    @patch("control_plane.execution_factory.AgentPool")
    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.ToolRegistry")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_interactive_uses_accept_edits_mode(
        self, mock_inject, MockToolReg, MockOrch, MockPool,
        MockBackendReg, MockBuiltin, MockLWCaller, MockEngine,
        MockEval, MockConfig,
    ):
        """In interactive mode, default permission mode is ACCEPT_EDITS."""
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="builtin",
            claude_code=MagicMock(enabled=False),
            codex=MagicMock(enabled=False),
        )
        MockOrch.return_value = MagicMock()
        MockPool.return_value.get_executor.return_value = MagicMock()
        MockEngine.return_value = MagicMock()

        factory = _make_factory(non_interactive=False, policy=None)
        store = MagicMock(spec=SessionStore)

        with patch("control_plane.execution_factory.Guardrails") as MockGuardrails:
            MockGuardrails.return_value = MagicMock()
            with patch("control_plane.execution_factory.PersonalGuardrails"):
                factory.create_execution_engine(
                    session_id="sess-3",
                    store=store,
                    work_dir=Path("/tmp/work"),
                )
            policy_arg = MockGuardrails.call_args[0][0]
            assert policy_arg.mode == PermissionMode.ACCEPT_EDITS

    @patch("control_plane.execution_factory.WeaveConfig")
    @patch("control_plane.execution_factory.EvaluatorEngine")
    @patch("control_plane.execution_factory.DAGExecutionEngine")
    @patch("control_plane.execution_factory.LightweightLLMCaller")
    @patch("control_plane.execution_factory.BuiltinBackend")
    @patch("control_plane.execution_factory.BackendRegistry")
    @patch("control_plane.execution_factory.AgentPool")
    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.ToolRegistry")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_custom_policy_overrides_default(
        self, mock_inject, MockToolReg, MockOrch, MockPool,
        MockBackendReg, MockBuiltin, MockLWCaller, MockEngine,
        MockEval, MockConfig,
    ):
        """Explicitly provided policy takes precedence over defaults."""
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="builtin",
            claude_code=MagicMock(enabled=False),
            codex=MagicMock(enabled=False),
        )
        MockOrch.return_value = MagicMock()
        MockPool.return_value.get_executor.return_value = MagicMock()
        MockEngine.return_value = MagicMock()

        custom_policy = GuardrailPolicy(mode=PermissionMode.AUTO, max_iterations=99)
        factory = _make_factory(policy=custom_policy)
        store = MagicMock(spec=SessionStore)

        with patch("control_plane.execution_factory.Guardrails") as MockGuardrails:
            MockGuardrails.return_value = MagicMock()
            with patch("control_plane.execution_factory.PersonalGuardrails"):
                factory.create_execution_engine(
                    session_id="sess-4",
                    store=store,
                    work_dir=Path("/tmp/work"),
                )
            policy_arg = MockGuardrails.call_args[0][0]
            assert policy_arg is custom_policy
            assert policy_arg.mode == PermissionMode.AUTO

    @patch("control_plane.execution_factory.WeaveConfig")
    @patch("control_plane.execution_factory.EvaluatorEngine")
    @patch("control_plane.execution_factory.DAGExecutionEngine")
    @patch("control_plane.execution_factory.LightweightLLMCaller")
    @patch("control_plane.execution_factory.BuiltinBackend")
    @patch("control_plane.execution_factory.BackendRegistry")
    @patch("control_plane.execution_factory.AgentPool")
    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.ToolRegistry")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_personal_guardrail_policy_uses_personal_guardrails(
        self, mock_inject, MockToolReg, MockOrch, MockPool,
        MockBackendReg, MockBuiltin, MockLWCaller, MockEngine,
        MockEval, MockConfig,
    ):
        """PersonalGuardrailPolicy triggers PersonalGuardrails instead of Guardrails."""
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="builtin",
            claude_code=MagicMock(enabled=False),
            codex=MagicMock(enabled=False),
        )
        MockOrch.return_value = MagicMock()
        MockPool.return_value.get_executor.return_value = MagicMock()
        MockEngine.return_value = MagicMock()

        personal_policy = PersonalGuardrailPolicy(mode=PermissionMode.DEFAULT)
        factory = _make_factory(policy=personal_policy, approval_repo=MagicMock())
        store = MagicMock(spec=SessionStore)

        with patch("control_plane.execution_factory.Guardrails") as MockGuardrails:
            with patch("control_plane.execution_factory.PersonalGuardrails") as MockPersonal:
                MockPersonal.return_value = MagicMock()
                factory.create_execution_engine(
                    session_id="sess-5",
                    store=store,
                    work_dir=Path("/tmp/work"),
                    project_dir="/tmp/project",
                )
                MockGuardrails.assert_not_called()
                MockPersonal.assert_called_once()
                call_kwargs = MockPersonal.call_args
                assert call_kwargs[0][0] is personal_policy
                assert call_kwargs[1]["non_interactive"] is False
                assert call_kwargs[1]["project_dir"] == "/tmp/project"

    @patch("control_plane.execution_factory.WeaveConfig")
    @patch("control_plane.execution_factory.EvaluatorEngine")
    @patch("control_plane.execution_factory.DAGExecutionEngine")
    @patch("control_plane.execution_factory.LightweightLLMCaller")
    @patch("control_plane.execution_factory.BuiltinBackend")
    @patch("control_plane.execution_factory.BackendRegistry")
    @patch("control_plane.execution_factory.AgentPool")
    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.ToolRegistry")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_event_handler_registered_on_engine(
        self, mock_inject, MockToolReg, MockOrch, MockPool,
        MockBackendReg, MockBuiltin, MockLWCaller, MockEngine,
        MockEval, MockConfig,
    ):
        """Engine.on_event is called with the session event handler."""
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="builtin",
            claude_code=MagicMock(enabled=False),
            codex=MagicMock(enabled=False),
        )
        MockOrch.return_value = MagicMock()
        MockPool.return_value.get_executor.return_value = MagicMock()
        mock_engine = MagicMock()
        MockEngine.return_value = mock_engine

        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        factory.create_execution_engine(
            session_id="sess-6",
            store=store,
        )
        mock_engine.on_event.assert_called_once()
        handler = mock_engine.on_event.call_args[0][0]
        assert callable(handler)

    @patch("control_plane.execution_factory.WeaveConfig")
    @patch("control_plane.execution_factory.EvaluatorEngine")
    @patch("control_plane.execution_factory.DAGExecutionEngine")
    @patch("control_plane.execution_factory.LightweightLLMCaller")
    @patch("control_plane.execution_factory.BuiltinBackend")
    @patch("control_plane.execution_factory.BackendRegistry")
    @patch("control_plane.execution_factory.AgentPool")
    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.ToolRegistry")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_work_dir_passed_as_string_to_engine(
        self, mock_inject, MockToolReg, MockOrch, MockPool,
        MockBackendReg, MockBuiltin, MockLWCaller, MockEngine,
        MockEval, MockConfig,
    ):
        """work_dir Path is converted to string in engine config."""
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="builtin",
            claude_code=MagicMock(enabled=False),
            codex=MagicMock(enabled=False),
        )
        MockOrch.return_value = MagicMock()
        MockPool.return_value.get_executor.return_value = MagicMock()
        MockEngine.return_value = MagicMock()

        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        factory.create_execution_engine(
            session_id="sess-7",
            store=store,
            work_dir=Path("/some/work/dir"),
        )
        engine_call_kwargs = MockEngine.call_args[1]
        # Path conversion uses OS-native separator on Windows
        assert engine_call_kwargs["work_dir"] == str(Path("/some/work/dir"))

    @patch("control_plane.execution_factory.WeaveConfig")
    @patch("control_plane.execution_factory.EvaluatorEngine")
    @patch("control_plane.execution_factory.DAGExecutionEngine")
    @patch("control_plane.execution_factory.LightweightLLMCaller")
    @patch("control_plane.execution_factory.BuiltinBackend")
    @patch("control_plane.execution_factory.BackendRegistry")
    @patch("control_plane.execution_factory.AgentPool")
    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.ToolRegistry")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_no_work_dir_passes_none(
        self, mock_inject, MockToolReg, MockOrch, MockPool,
        MockBackendReg, MockBuiltin, MockLWCaller, MockEngine,
        MockEval, MockConfig,
    ):
        """No work_dir passes None for work_dir to engine."""
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="builtin",
            claude_code=MagicMock(enabled=False),
            codex=MagicMock(enabled=False),
        )
        MockOrch.return_value = MagicMock()
        MockPool.return_value.get_executor.return_value = MagicMock()
        MockEngine.return_value = MagicMock()

        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        factory.create_execution_engine(
            session_id="sess-8",
            store=store,
            work_dir=None,
        )
        engine_call_kwargs = MockEngine.call_args[1]
        assert engine_call_kwargs["work_dir"] is None

    @patch("control_plane.execution_factory.WeaveConfig")
    @patch("control_plane.execution_factory.EvaluatorEngine")
    @patch("control_plane.execution_factory.DAGExecutionEngine")
    @patch("control_plane.execution_factory.LightweightLLMCaller")
    @patch("control_plane.execution_factory.BuiltinBackend")
    @patch("control_plane.execution_factory.BackendRegistry")
    @patch("control_plane.execution_factory.AgentPool")
    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.ToolRegistry")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_budget_manager_forwarded_to_engine(
        self, mock_inject, MockToolReg, MockOrch, MockPool,
        MockBackendReg, MockBuiltin, MockLWCaller, MockEngine,
        MockEval, MockConfig,
    ):
        """budget_manager from factory is forwarded to engine."""
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="builtin",
            claude_code=MagicMock(enabled=False),
            codex=MagicMock(enabled=False),
        )
        MockOrch.return_value = MagicMock()
        MockPool.return_value.get_executor.return_value = MagicMock()
        MockEngine.return_value = MagicMock()

        bm = MagicMock()
        factory = _make_factory(budget_manager=bm)
        store = MagicMock(spec=SessionStore)
        factory.create_execution_engine(
            session_id="sess-9",
            store=store,
        )
        engine_call_kwargs = MockEngine.call_args[1]
        assert engine_call_kwargs["budget_manager"] is bm

    @patch("control_plane.execution_factory.WeaveConfig")
    @patch("control_plane.execution_factory.EvaluatorEngine")
    @patch("control_plane.execution_factory.DAGExecutionEngine")
    @patch("control_plane.execution_factory.LightweightLLMCaller")
    @patch("control_plane.execution_factory.BuiltinBackend")
    @patch("control_plane.execution_factory.BackendRegistry")
    @patch("control_plane.execution_factory.AgentPool")
    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.ToolRegistry")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_watchdog_config_reflected_in_engine_config(
        self, mock_inject, MockToolReg, MockOrch, MockPool,
        MockBackendReg, MockBuiltin, MockLWCaller, MockEngine,
        MockEval, MockConfig,
    ):
        """Watchdog config parameters flow into DAGEngineConfig."""
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="builtin",
            claude_code=MagicMock(enabled=False),
            codex=MagicMock(enabled=False),
        )
        MockOrch.return_value = MagicMock()
        MockPool.return_value.get_executor.return_value = MagicMock()
        MockEngine.return_value = MagicMock()

        wc = _make_watchdog_config(
            enabled=True,
            heartbeat_interval_sec=45.0,
            heartbeat_miss_threshold=10,
        )
        factory = _make_factory(watchdog_config=wc)
        store = MagicMock(spec=SessionStore)
        factory.create_execution_engine(
            session_id="sess-10",
            store=store,
        )
        engine_call_kwargs = MockEngine.call_args[1]
        dag_config = engine_call_kwargs["config"]
        assert dag_config.heartbeat_interval_sec == 45.0
        assert dag_config.heartbeat_miss_threshold == 10
        assert dag_config.enable_watchdog is True

    @patch("control_plane.execution_factory.WeaveConfig")
    @patch("control_plane.execution_factory.EvaluatorEngine")
    @patch("control_plane.execution_factory.DAGExecutionEngine")
    @patch("control_plane.execution_factory.LightweightLLMCaller")
    @patch("control_plane.execution_factory.BuiltinBackend")
    @patch("control_plane.execution_factory.BackendRegistry")
    @patch("control_plane.execution_factory.AgentPool")
    @patch("control_plane.execution_factory.IntelligentOrchestrator")
    @patch("control_plane.execution_factory.ToolRegistry")
    @patch("control_plane.execution_factory.inject_token_estimator", create=True)
    def test_claude_code_backend_registered_when_enabled(
        self, mock_inject, MockToolReg, MockOrch, MockPool,
        MockBackendReg, MockBuiltin, MockLWCaller, MockEngine,
        MockEval, MockConfig,
    ):
        """ClaudeCodeBackend is registered when config.claude_code.enabled is True."""
        # Build a claude_code config with valid permission_mode string
        mock_cc_config = MagicMock(
            enabled=True,
            cli_path="claude",
            model="",
            max_turns=0,
            permission_mode="default",
            allowed_tools=None,
            system_prompt_append="",
            max_budget_usd=0.0,
            timeout_override=0,
        )
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="claude_code",
            claude_code=mock_cc_config,
            codex=MagicMock(enabled=False),
        )
        MockOrch.return_value = MagicMock()
        MockPool.return_value.get_executor.return_value = MagicMock()
        mock_backend_reg = MagicMock()
        MockBackendReg.return_value = mock_backend_reg
        MockEngine.return_value = MagicMock()

        factory = _make_factory()
        store = MagicMock(spec=SessionStore)

        mock_cc_instance = MagicMock()

        with patch("agent.backends.claude_code.ClaudeCodeBackend", return_value=mock_cc_instance):
            factory.create_execution_engine(
                session_id="sess-11",
                store=store,
            )

        # claude_code should be registered on the backend registry
        mock_backend_reg.register.assert_any_call("claude_code", mock_cc_instance)


# ---------------------------------------------------------------------------
# 7. Session event handler mapping tests
# ---------------------------------------------------------------------------


def _extract_event_handler(factory, store):
    """Helper: call create_execution_engine with all deps mocked, return the event handler."""
    with patch("control_plane.execution_factory.WeaveConfig") as MockConfig:
        MockConfig.from_env.return_value = MagicMock(
            pass_threshold=7.0,
            auto_format_before_eval=False,
            node_timeout=MagicMock(),
            default_agent_backend="builtin",
            claude_code=MagicMock(enabled=False),
            codex=MagicMock(enabled=False),
        )
        with patch("control_plane.execution_factory.EvaluatorEngine", return_value=MagicMock()):
            with patch("control_plane.execution_factory.DAGExecutionEngine", return_value=MagicMock()) as MockEngine:
                with patch("control_plane.execution_factory.LightweightLLMCaller", return_value=MagicMock()):
                    with patch("control_plane.execution_factory.BuiltinBackend", return_value=MagicMock()):
                        with patch("control_plane.execution_factory.BackendRegistry", return_value=MagicMock()):
                            with patch("control_plane.execution_factory.AgentPool") as MockPool:
                                MockPool.return_value.get_executor.return_value = MagicMock()
                                with patch("control_plane.execution_factory.IntelligentOrchestrator", return_value=MagicMock()):
                                    with patch("control_plane.execution_factory.ToolRegistry", return_value=MagicMock()):
                                        factory.create_execution_engine(
                                            session_id="sess-ev",
                                            store=store,
                                        )
                                        handler = MockEngine.return_value.on_event.call_args[0][0]
                                        return handler


class TestSessionEventHandler:
    """Test the internal _session_event_handler closure behavior."""

    def test_started_event_maps_to_stage_start(self):
        """Event with event_type='started' emits WORKFLOW_STAGE_START."""
        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        handler = _extract_event_handler(factory, store)

        from core.models import EventType
        event = MagicMock()
        event.event_type = "started"
        event.node_id = "node-1"
        event.details = {"key": "val"}

        import asyncio
        asyncio.run(handler(event))

        store.emit_event.assert_called_once()
        call_args = store.emit_event.call_args
        assert call_args[0][0] == "sess-ev"
        assert call_args[0][1] == EventType.WORKFLOW_STAGE_START

    def test_completed_event_maps_to_stage_end(self):
        """Event with event_type='completed' emits WORKFLOW_STAGE_END."""
        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        handler = _extract_event_handler(factory, store)

        from core.models import EventType
        event = MagicMock()
        event.event_type = "completed"
        event.node_id = "node-2"
        event.details = {}

        import asyncio
        asyncio.run(handler(event))

        call_args = store.emit_event.call_args
        assert call_args[0][1] == EventType.WORKFLOW_STAGE_END

    def test_failed_event_maps_to_stage_error(self):
        """Event with event_type='failed' emits WORKFLOW_STAGE_ERROR."""
        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        handler = _extract_event_handler(factory, store)

        from core.models import EventType
        event = MagicMock()
        event.event_type = "failed"
        event.node_id = "node-3"
        event.details = {"reason": "timeout"}

        import asyncio
        asyncio.run(handler(event))

        call_args = store.emit_event.call_args
        assert call_args[0][1] == EventType.WORKFLOW_STAGE_ERROR

    def test_unknown_event_type_is_dropped(self):
        """Event with unrecognized event_type is silently dropped."""
        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        handler = _extract_event_handler(factory, store)

        event = MagicMock()
        event.event_type = "unknown_type"
        event.node_id = "node-x"
        event.details = {}

        import asyncio
        asyncio.run(handler(event))

        store.emit_event.assert_not_called()

    def test_trace_event_maps_correctly(self):
        """Event with event_type='trace' and trace_type='run_start' emits TRACE_RUN_START."""
        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        handler = _extract_event_handler(factory, store)

        from core.models import EventType
        event = MagicMock()
        event.event_type = "trace"
        event.node_id = "node-t"
        event.details = {"trace_type": "run_start"}

        import asyncio
        asyncio.run(handler(event))

        call_args = store.emit_event.call_args
        assert call_args[0][1] == EventType.TRACE_RUN_START

    def test_unknown_trace_type_is_dropped(self):
        """Trace event with unrecognized trace_type is dropped."""
        factory = _make_factory()
        store = MagicMock(spec=SessionStore)
        handler = _extract_event_handler(factory, store)

        event = MagicMock()
        event.event_type = "trace"
        event.node_id = "node-t"
        event.details = {"trace_type": "bogus"}

        import asyncio
        asyncio.run(handler(event))

        store.emit_event.assert_not_called()


# Import needed for direct reference in tests above
from control_plane.execution_factory import ExecutionFactory
