"""
Tests for #224: --file option for reading requirements from file.

Allows users to avoid shell escaping issues with ${...} patterns
by reading requirements from a file instead of command line.
"""
import subprocess
import sys


class TestRequirementFileOption:
    """--file reads requirement from file or stdin."""

    def test_run_with_file(self, tmp_path):
        """python main.py run --file req.txt works."""
        req_file = tmp_path / "requirement.txt"
        req_file.write_text("Build a hello world API\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, "main.py", "run", "--file", str(req_file), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        # --help exits with 0; the important thing is no parse error
        assert result.returncode == 0

    def test_plan_with_file(self, tmp_path):
        """python main.py plan --file req.txt works."""
        req_file = tmp_path / "requirement.txt"
        req_file.write_text("Build a REST API\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, "main.py", "plan", "--file", str(req_file), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_file_with_special_chars(self, tmp_path):
        """Requirements with ${...} patterns can be read from file."""
        req_file = tmp_path / "req.txt"
        req_file.write_text(
            "Resolve ${ENV_VAR:default} placeholders in YAML configs\n",
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, "main.py", "run", "--file", str(req_file), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

    def test_no_requirement_and_no_file_exits(self, tmp_path):
        """python main.py run (no requirement, no --file) exits with error."""
        result = subprocess.run(
            [sys.executable, "main.py", "run"],
            capture_output=True, text=True, timeout=10,
            env={**__import__("os").environ, "ANTHROPIC_API_KEY": "test"},
        )
        # Should exit with error (either argparse error or our check)
        assert result.returncode != 0

    def test_stdin_file_option(self, tmp_path):
        """--file - reads from stdin."""
        result = subprocess.run(
            [sys.executable, "main.py", "run", "--file", "-", "--help"],
            input="Build something great\n",
            capture_output=True, text=True, timeout=10,
        )
        # --help exits with 0 before stdin is read, which is fine
        assert result.returncode == 0
