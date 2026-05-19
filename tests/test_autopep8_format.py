"""Tests for autopep8 in-place formatting before lint evaluation (#206).

Covers:
- autopep8 fixes whitespace issues (W291 trailing whitespace, E303 blank lines)
- Graceful degradation when autopep8 unavailable/crashes
- Call order: autoflake → autopep8 → flake8
- Disabled by default (auto_format_before_eval=False)
- Relative path tracking based on work_dir
- returncode != 0 handling
- Different-directory same-name files produce distinct paths
"""
import os
import subprocess  # noqa: F401
from pathlib import Path
from unittest.mock import MagicMock, patch


from evaluator.engine import EvaluatorEngine
from tools.registry import ToolRegistry


class TestAutopep8Format:
    def test_autopep8_fixes_trailing_whitespace(self, tmp_path):
        """autopep8 removes W291 trailing whitespace before flake8."""
        f = tmp_path / "target.py"
        f.write_text("x = 1   \ny = 2\n")

        engine = EvaluatorEngine(MagicMock(), auto_format_before_eval=True)

        def fake_run(cmd, **kwargs):
            if "autopep8" in cmd:
                for arg in cmd:
                    p = Path(arg)
                    if p.suffix == ".py" and p.exists():
                        content = p.read_text()
                        p.write_text(content.replace("   \n", "\n"))
                return MagicMock(returncode=0, stdout="", stderr="")
            if "autoflake" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint(["target.py"], tmp_path)

        assert passed
        assert "Lint clean" in msg

    def test_autopep8_not_installed_graceful(self, tmp_path):
        """autopep8 missing → lint still proceeds."""
        f = tmp_path / "target.py"
        f.write_text("print('ok')\n")

        engine = EvaluatorEngine(MagicMock(), auto_format_before_eval=True)

        def fake_run(cmd, **kwargs):
            if "autopep8" in cmd:
                raise FileNotFoundError("autopep8 not found")
            if "autoflake" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint(["target.py"], tmp_path)

        assert passed

    def test_autopep8_failure_graceful(self, tmp_path):
        """autopep8 crashes → lint still proceeds."""
        f = tmp_path / "target.py"
        f.write_text("print('ok')\n")

        engine = EvaluatorEngine(MagicMock(), auto_format_before_eval=True)

        def fake_run(cmd, **kwargs):
            if "autopep8" in cmd:
                raise RuntimeError("autopep8 crashed")
            if "autoflake" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint(["target.py"], tmp_path)

        assert passed

    def test_autopep8_returncode_nonzero_skipped(self, tmp_path):
        """autopep8 returns non-zero → skipped, lint still proceeds."""
        f = tmp_path / "target.py"
        f.write_text("print('ok')\n")

        engine = EvaluatorEngine(MagicMock(), auto_format_before_eval=True)

        def fake_run(cmd, **kwargs):
            if "autopep8" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="parse error")
            if "autoflake" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint(["target.py"], tmp_path)

        assert passed
        assert engine._last_auto_formatted == []

    def test_autopep8_runs_after_autoflake(self, tmp_path):
        """autopep8 should run AFTER autoflake (both execute on same files)."""
        f = tmp_path / "target.py"
        f.write_text("import os\nprint('ok')\n")

        engine = EvaluatorEngine(MagicMock(), auto_format_before_eval=True)
        order = []

        def fake_run(cmd, **kwargs):
            if "autoflake" in cmd:
                order.append("autoflake")
                return MagicMock(returncode=0, stdout="", stderr="")
            if "autopep8" in cmd:
                order.append("autopep8")
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                order.append("flake8")
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            engine._run_lint(["target.py"], tmp_path)

        assert order == ["autoflake", "autopep8", "flake8"]

    def test_autopep8_disabled_by_default(self, tmp_path):
        """When auto_format_before_eval=False (default), autopep8 is never called."""
        f = tmp_path / "target.py"
        f.write_text("print('ok')\n")

        engine = EvaluatorEngine(MagicMock())  # default: auto_format_before_eval=False
        order = []

        def fake_run(cmd, **kwargs):
            if "autoflake" in cmd:
                order.append("autoflake")
                return MagicMock(returncode=0, stdout="", stderr="")
            if "autopep8" in cmd:
                order.append("autopep8")
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                order.append("flake8")
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint(["target.py"], tmp_path)

        assert passed
        assert "autopep8" not in order
        assert order == ["autoflake", "flake8"]

    def test_relative_path_uses_work_dir(self, tmp_path):
        """_last_auto_formatted uses relative path based on work_dir."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        f1 = pkg / "a.py"
        f1.write_text("x = 1   \n")
        f2 = tmp_path / "a.py"
        f2.write_text("y = 2   \n")

        engine = EvaluatorEngine(MagicMock(), auto_format_before_eval=True)

        def fake_run(cmd, **kwargs):
            if "autopep8" in cmd:
                for arg in cmd:
                    p = Path(arg)
                    if p.suffix == ".py" and p.exists():
                        p.write_text(p.read_text().replace("   \n", "\n"))
                return MagicMock(returncode=0, stdout="", stderr="")
            if "autoflake" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            engine._run_lint(["pkg/a.py", "a.py"], tmp_path)

        assert sorted(p.replace("\\", "/") for p in engine._last_auto_formatted) == ["a.py", "pkg/a.py"]

    def test_select_does_not_include_e501(self, tmp_path):
        """autopep8 --select should not include E501 (first version: low-risk only)."""
        f = tmp_path / "target.py"
        f.write_text("print('ok')\n")

        engine = EvaluatorEngine(MagicMock(), auto_format_before_eval=True)

        def fake_run(cmd, **kwargs):
            if "autopep8" in cmd:
                select_arg = next(a for a in cmd if a.startswith("--select="))
                assert "E501" not in select_arg
                assert "E203" in select_arg
                assert "W291" in select_arg
                return MagicMock(returncode=0, stdout="", stderr="")
            if "autoflake" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            engine._run_lint(["target.py"], tmp_path)


class TestWriteToolLineWarning:
    def test_warns_on_long_lines_py(self):
        """write tool warns about long lines in Python files."""
        registry = ToolRegistry()
        long_line = "x = " + "'" + "a" * 120 + "'"
        result = registry.execute("write", {
            "file_path": "/tmp/test_e501.py",
            "content": f"{long_line}\n",
        })
        assert result.success
        assert "WARNING" in result.output
        assert "over 100 chars" in result.output

    def test_no_warning_short_lines(self, tmp_path):
        """write tool does not warn for files with short lines."""
        registry = ToolRegistry()
        result = registry.execute("write", {
            "file_path": str(tmp_path / "ok.py"),
            "content": "print('hello')\n",
        })
        assert result.success
        assert "WARNING" not in result.output

    def test_no_warning_non_py(self):
        """write tool does not warn for non-Python files."""
        registry = ToolRegistry()
        long_line = "x = " + "'" + "a" * 120 + "'"
        result = registry.execute("write", {
            "file_path": "/tmp/test.md",
            "content": f"{long_line}\n",
        })
        assert result.success
        assert "WARNING" not in result.output

    def test_warning_limits_preview(self):
        """warning previews at most 5 long lines."""
        registry = ToolRegistry()
        lines = "\n".join(
            f"x_{i} = " + "'" + "a" * 120 + "'" for i in range(8)
        )
        result = registry.execute("write", {
            "file_path": "/tmp/test_many.py",
            "content": f"{lines}\n",
        })
        assert result.success
        assert "+3 more" in result.output
