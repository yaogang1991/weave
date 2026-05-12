"""Tests for E501 auto-fix in lint pipeline and write tool warning (issue #112)."""
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from evaluator.engine import EvaluatorEngine
from tools.registry import ToolRegistry


class TestE501AutofixInLint:
    def test_autopep8_fixes_long_lines(self, tmp_path):
        """_run_lint runs autopep8 to fix E501 before flake8 verification."""
        long_line = "x = " + "'" + "a" * 120 + "'"  # 128 chars
        f = tmp_path / "target.py"
        f.write_text(f"{long_line}\n")

        engine = EvaluatorEngine(MagicMock())

        def fake_run(cmd, **kwargs):
            if "autopep8" in cmd:
                # Simulate autopep8 wrapping the long line
                for arg in cmd:
                    p = Path(arg)
                    if p.suffix == ".py" and p.exists():
                        content = p.read_text()
                        if len(content.split("\n")[0]) > 100:
                            p.write_text("x = ('aaaaaaaaaa'\n     'aaaa')\n")
                return MagicMock(returncode=0, stdout="", stderr="")
            if "autoflake" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.engine.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint(["target.py"], tmp_path)

        assert passed
        assert "Lint clean" in msg

    def test_autopep8_not_installed_graceful(self, tmp_path):
        """autopep8 missing → lint still proceeds (graceful degradation)."""
        f = tmp_path / "target.py"
        f.write_text("print('ok')\n")

        engine = EvaluatorEngine(MagicMock())
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if "autopep8" in cmd:
                raise FileNotFoundError("autopep8 not found")
            if "autoflake" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.engine.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint(["target.py"], tmp_path)

        assert passed  # Should still pass via flake8

    def test_autopep8_failure_graceful(self, tmp_path):
        """autopep8 crashes → lint still proceeds."""
        f = tmp_path / "target.py"
        f.write_text("print('ok')\n")

        engine = EvaluatorEngine(MagicMock())

        def fake_run(cmd, **kwargs):
            if "autopep8" in cmd:
                raise RuntimeError("autopep8 crashed")
            if "autoflake" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.engine.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint(["target.py"], tmp_path)

        assert passed

    def test_autopep8_runs_after_autoflake(self, tmp_path):
        """autopep8 should run AFTER autoflake (both execute on same files)."""
        f = tmp_path / "target.py"
        f.write_text("import os\nprint('ok')\n")

        engine = EvaluatorEngine(MagicMock())
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

        with patch("evaluator.engine.subprocess.run", side_effect=fake_run):
            engine._run_lint(["target.py"], tmp_path)

        assert order == ["autoflake", "autopep8", "flake8"]


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
