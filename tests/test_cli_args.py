"""Tests for cli/args.py shared argument helpers (#497)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # noqa: E402

from cli.args import (  # noqa: E402
    add_project_arg,
    add_display_args,
    add_template_args,
    add_execution_args,
    add_self_modify_arg,
    add_requirement_arg,
)


def _make_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser()


class TestSharedArgs:
    def test_project_arg_default_suppress(self):
        p = _make_parser()
        add_project_arg(p)
        args = p.parse_args([])
        assert "project" not in args  # SUPPRESS means absent when not provided

    def test_project_arg_with_value(self):
        p = _make_parser()
        add_project_arg(p)
        args = p.parse_args(["--project", "/tmp/target"])
        assert args.project == "/tmp/target"

    def test_project_arg_custom_default(self):
        p = _make_parser()
        add_project_arg(p, default=".")
        args = p.parse_args([])
        assert args.project == "."

    def test_display_args(self):
        p = _make_parser()
        add_display_args(p)
        args = p.parse_args(["--viz", "--no-browser"])
        assert args.viz is True
        assert args.no_browser is True
        assert args.visualize is False

    def test_template_args(self):
        p = _make_parser()
        add_template_args(p)
        args = p.parse_args(["--template", "build_api", "--var", "x=1", "--var", "y=2"])
        assert args.template == "build_api"
        assert args.var == ["x=1", "y=2"]

    def test_execution_args(self):
        p = _make_parser()
        add_execution_args(p)
        args = p.parse_args([
            "--max-parallel", "5",
            "--max-iterations", "100",
            "--non-interactive",
            "--pass-threshold", "8.5",
        ])
        assert args.max_parallel == 5
        assert args.max_iterations == 100
        assert args.non_interactive is True
        assert args.pass_threshold == 8.5

    def test_execution_args_defaults(self):
        p = _make_parser()
        add_execution_args(p)
        args = p.parse_args([])
        assert args.max_parallel == 3
        assert args.max_iterations == 50
        assert args.non_interactive is False
        assert args.pass_threshold is None

    def test_self_modify_arg(self):
        p = _make_parser()
        add_self_modify_arg(p)
        args = p.parse_args(["--allow-self-modify"])
        assert args.allow_self_modify is True

    def test_requirement_arg(self):
        p = _make_parser()
        add_requirement_arg(p)
        args = p.parse_args(["Build API"])
        assert args.requirement == "Build API"
        assert args.file is None

    def test_requirement_arg_from_file(self):
        p = _make_parser()
        add_requirement_arg(p)
        args = p.parse_args(["-f", "req.txt"])
        assert args.file == "req.txt"
        assert args.requirement is None

    def test_requirement_arg_optional(self):
        p = _make_parser()
        add_requirement_arg(p)
        args = p.parse_args([])
        assert args.requirement is None
        assert args.file is None

    def test_main_help_works(self):
        """Verify main.py --help doesn't crash after refactor."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "main.py", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "Weave" in result.stdout

    def test_run_help_has_all_args(self):
        """Verify run subcommand retains all expected arguments."""
        import subprocess
        result = subprocess.run(
            [sys.executable, "main.py", "run", "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        for arg in [
            "--project", "--template", "--var", "--viz",
            "--no-browser", "--max-parallel", "--non-interactive",
            "--pass-threshold", "--timeout",
        ]:
            assert arg in result.stdout, f"Missing {arg} in run --help"
