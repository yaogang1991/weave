"""Tests for core/project_config.py"""

import pytest  # noqa: F401
import yaml

from core.project_config import (  # noqa: F401
    HookConfig,
    RuntimeConfig,
    GuardrailsConfig,
    ProjectContext,
    ProjectConfig,
)


class TestHookConfig:
    def test_defaults(self):
        cfg = HookConfig()
        assert cfg.after_create == ""
        assert cfg.before_run == ""
        assert cfg.after_run == ""
        assert cfg.before_remove == ""
        assert cfg.timeout_sec == 60

    def test_custom_values(self):
        cfg = HookConfig(
            after_create="npm install",
            before_run="npm test",
            after_run="npm run lint",
            before_remove="cp -r ./artifacts /tmp/backup",
            timeout_sec=120,
        )
        assert cfg.after_create == "npm install"
        assert cfg.timeout_sec == 120


class TestRuntimeConfig:
    def test_defaults(self):
        cfg = RuntimeConfig()
        assert cfg.max_turns == 50
        assert cfg.max_parallel == 3
        assert cfg.turn_timeout_sec == 600
        assert cfg.max_retries == 3
        assert cfg.base_backoff_sec == 1.0
        assert cfg.max_backoff_sec == 300
        assert cfg.backoff_multiplier == 2.0


class TestProjectConfig:
    def test_defaults(self):
        cfg = ProjectConfig()
        assert cfg.runtime.max_parallel == 3
        assert cfg.hooks.timeout_sec == 60
        assert cfg.guardrails.approval_policy == "accept_edits"
        assert cfg.project_context.language == ""

    def test_load_missing_file(self, tmp_path):
        cfg = ProjectConfig.load(tmp_path)
        # Should return defaults
        assert cfg.runtime.max_parallel == 3

    def test_load_none_path(self):
        cfg = ProjectConfig.load(None)
        assert cfg.runtime.max_parallel == 3

    def test_load_from_yaml(self, tmp_path):
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text(yaml.dump({
            "runtime": {
                "max_parallel": 5,
                "max_turns": 100,
            },
            "hooks": {
                "before_run": "pytest --co -q",
                "after_run": "ruff check .",
            },
            "project_context": {
                "language": "python",
                "framework": "fastapi",
            },
        }))

        cfg = ProjectConfig.load(tmp_path)
        assert cfg.runtime.max_parallel == 5
        assert cfg.runtime.max_turns == 100
        assert cfg.hooks.before_run == "pytest --co -q"
        assert cfg.hooks.after_run == "ruff check ."
        assert cfg.project_context.language == "python"
        assert cfg.project_context.framework == "fastapi"

    def test_load_partial_yaml(self, tmp_path):
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text(yaml.dump({
            "runtime": {"max_parallel": 7},
        }))

        cfg = ProjectConfig.load(tmp_path)
        assert cfg.runtime.max_parallel == 7
        # Other fields should keep defaults
        assert cfg.runtime.max_turns == 50
        assert cfg.hooks.timeout_sec == 60

    def test_load_empty_yaml(self, tmp_path):
        weave_dir = tmp_path / ".weave"
        weave_dir.mkdir()
        config_file = weave_dir / "config.yaml"
        config_file.write_text("")

        cfg = ProjectConfig.load(tmp_path)
        assert cfg.runtime.max_parallel == 3  # default

    def test_effective_runtime_no_overrides(self):
        cfg = ProjectConfig()
        effective = cfg.effective_runtime()
        assert effective.max_parallel == 3

    def test_effective_runtime_with_overrides(self):
        cfg = ProjectConfig()
        effective = cfg.effective_runtime({"max_parallel": 10, "max_turns": None})
        assert effective.max_parallel == 10
        assert effective.max_turns == 50  # None override ignored

    def test_effective_runtime_ignores_unknown_keys(self):
        cfg = ProjectConfig()
        effective = cfg.effective_runtime({"unknown_key": 999})
        assert effective.max_parallel == 3  # unchanged
