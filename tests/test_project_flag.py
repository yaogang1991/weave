"""Tests for #193: --project flag works on subcommands, not just top-level.

Covers:
- `run --project X` works (subcommand-level flag)
- `--project X run` works (top-level flag, backward compat)
- Subcommand --project overrides top-level --project
- `plan --project X` and `execute --project X` also work
"""
import argparse
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))


def _parse_args(argv):
    """Parse args using a replica of the main.py parser structure."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=None)
    parser.add_argument("--max-parallel", type=int, default=3)
    parser.add_argument("--max-iterations", type=int, default=50)

    subparsers = parser.add_subparsers(dest="command")

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--project", default=argparse.SUPPRESS)
    plan_parser.add_argument("requirement")

    exec_parser = subparsers.add_parser("execute")
    exec_parser.add_argument("--project", default=argparse.SUPPRESS)
    exec_parser.add_argument("plan_file")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--project", default=argparse.SUPPRESS)
    run_parser.add_argument("requirement")

    return parser.parse_args(argv[1:])


class TestProjectFlagPlacement:
    """--project works at both top-level and subcommand level."""

    def test_run_with_subcommand_project(self):
        """`main.py run --project /tmp/test "task"` → project set."""
        args = _parse_args(["main.py", "run", "--project", "/tmp/test", "task"])
        assert args.project == "/tmp/test"
        assert args.command == "run"

    def test_run_with_toplevel_project(self):
        """`main.py --project /tmp/test run "task"` → project set."""
        args = _parse_args(["main.py", "--project", "/tmp/test", "run", "task"])
        assert args.project == "/tmp/test"

    def test_subcommand_overrides_toplevel(self):
        """`main.py --project /foo run --project /bar "task"` → /bar wins."""
        args = _parse_args([
            "main.py", "--project", "/foo",
            "run", "--project", "/bar", "task",
        ])
        assert args.project == "/bar"

    def test_no_project_defaults_none(self):
        """`main.py run "task"` → project is None."""
        args = _parse_args(["main.py", "run", "task"])
        assert args.project is None

    def test_plan_subcommand_project(self):
        """`main.py plan --project /tmp "task"` → project set."""
        args = _parse_args(["main.py", "plan", "--project", "/tmp", "task"])
        assert args.project == "/tmp"

    def test_execute_subcommand_project(self):
        """`main.py execute --project /tmp plan.json` → project set."""
        args = _parse_args([
            "main.py", "execute", "--project", "/tmp", "plan.json",
        ])
        assert args.project == "/tmp"

    def test_toplevel_project_preserved_without_subcommand_flag(self):
        """Top-level --project not overwritten when subcommand has no --project."""
        args = _parse_args(["main.py", "--project", "/top", "run", "task"])
        assert args.project == "/top"
