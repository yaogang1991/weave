"""Tests for autoflake change tracking in evaluator lint (issue #133)."""
import subprocess  # noqa: F401
from pathlib import Path
from unittest.mock import MagicMock, patch

from evaluator.engine import EvaluatorEngine
from core.models import EventType


def _make_engine() -> tuple[EvaluatorEngine, MagicMock]:
    store = MagicMock()
    return EvaluatorEngine(store), store


class TestAutoflakeTracking:
    def test_tracks_changed_file(self, tmp_path):
        """autoflake modifies a file → _run_lint returns autofix info."""
        f = tmp_path / "mod.py"
        f.write_text("import os\nprint('hello')\n")

        engine, _ = _make_engine()

        def fake_run(cmd, **kwargs):
            if "autoflake" in cmd:
                # Simulate autoflake removing unused import
                for arg in cmd:
                    p = Path(arg)
                    if p.suffix == ".py" and p.exists():
                        content = p.read_text()
                        p.write_text(content.replace("import os\n", ""))
                r = MagicMock()
                r.returncode = 0
                r.stdout = ""
                r.stderr = ""
                return r
            if "autopep8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                r = MagicMock()
                r.returncode = 0
                r.stdout = ""
                r.stderr = ""
                return r
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint([str(f)], tmp_path)

        assert engine._last_autofixed == ["mod.py"]
        assert "Autoflake auto-fixed" in msg
        assert "mod.py" in msg

    def test_no_changes_clean_file(self, tmp_path):
        """Clean file → no autofix info in feedback."""
        f = tmp_path / "clean.py"
        f.write_text("print('hello')\n")

        engine, _ = _make_engine()

        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint([str(f)], tmp_path)

        assert engine._last_autofixed == []
        assert "Autoflake" not in msg

    def test_autoflake_not_installed(self, tmp_path):
        """autoflake missing → lint still runs, no crash, no autofix list."""
        f = tmp_path / "test.py"
        f.write_text("import os\nprint('hello')\n")

        engine, _ = _make_engine()
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if "autoflake" in cmd:
                raise FileNotFoundError("autoflake not found")
            if "autopep8" in cmd:
                raise FileNotFoundError("autopep8 not found")
            if "flake8" in cmd:
                r = MagicMock()
                r.returncode = 0
                r.stdout = ""
                return r
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint([str(f)], tmp_path)

        assert engine._last_autofixed == []
        assert "Autoflake" not in msg

    def test_autofix_included_in_failed_lint(self, tmp_path):
        """Autofix info prepended even when lint fails."""
        f = tmp_path / "bad.py"
        f.write_text("import os\nx = \n")

        engine, _ = _make_engine()

        def fake_run(cmd, **kwargs):
            if "autoflake" in cmd:
                for arg in cmd:
                    p = Path(arg)
                    if p.suffix == ".py" and p.exists():
                        content = p.read_text()
                        p.write_text(content.replace("import os\n", ""))
                r = MagicMock()
                r.returncode = 0
                r.stdout = ""
                r.stderr = ""
                return r
            if "autopep8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                r = MagicMock()
                r.returncode = 1
                r.stdout = "bad.py:1:1 E901 SyntaxError"
                r.stderr = ""
                return r
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            passed, msg = engine._run_lint([str(f)], tmp_path)

        assert not passed
        assert "Autoflake auto-fixed" in msg
        assert "Lint issues" in msg

    def test_autofixed_resets_between_calls(self, tmp_path):
        """_last_autofixed resets at the start of each _run_lint call."""
        engine, _ = _make_engine()

        # First call: simulate a change
        f1 = tmp_path / "a.py"
        f1.write_text("import os\nprint('a')\n")

        def fake_run_change(cmd, **kwargs):
            if "autoflake" in cmd:
                for arg in cmd:
                    p = Path(arg)
                    if p.suffix == ".py" and p.exists():
                        p.write_text(p.read_text().replace("import os\n", ""))
                return MagicMock(returncode=0, stdout="", stderr="")
            if "autopep8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run_change):
            engine._run_lint([str(f1)], tmp_path)
        assert engine._last_autofixed == ["a.py"]

        # Second call: no changes
        f2 = tmp_path / "b.py"
        f2.write_text("print('b')\n")

        def fake_run_noop(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run_noop):
            engine._run_lint([str(f2)], tmp_path)
        assert engine._last_autofixed == []


class TestAutofixEventEmission:
    def test_event_emitted_on_changes(self, tmp_path):
        """evaluate_stage emits EVAL_AUTOFIX_APPLIED when autoflake changes files."""
        engine, store = _make_engine()

        f = tmp_path / "target.py"
        f.write_text("import os\nprint('hello')\n")

        def fake_run(cmd, **kwargs):
            if "autoflake" in cmd:
                for arg in cmd:
                    p = Path(arg)
                    if p.suffix == ".py" and p.exists():
                        p.write_text(p.read_text().replace("import os\n", ""))
                return MagicMock(returncode=0, stdout="", stderr="")
            if "autopep8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if "flake8" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise RuntimeError(f"unexpected cmd: {cmd}")

        from core.models import SuccessCriterion, CriterionType
        crit = SuccessCriterion(type=CriterionType.LINT, description="lint check")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            engine.evaluate_stage(
                session_id="test-session",
                stage_name="test-stage",
                criteria=[crit],
                artifact_path=str(tmp_path),
                work_dir=str(tmp_path),
                output_artifacts=["target.py"],
            )

        event_calls = store.emit_event.call_args_list
        event_types = [c[0][1] for c in event_calls]
        assert EventType.EVAL_AUTOFIX_APPLIED in event_types

        autofix_call = [c for c in event_calls if c[0][1] == EventType.EVAL_AUTOFIX_APPLIED][0]
        payload = autofix_call[0][2]
        assert payload["tool"] == "autoflake"
        assert "target.py" in payload["files"]
        assert payload["stage"] == "test-stage"

    def test_no_event_when_no_changes(self, tmp_path):
        """evaluate_stage does NOT emit EVAL_AUTOFIX_APPLIED when no files changed."""
        engine, store = _make_engine()

        f = tmp_path / "clean.py"
        f.write_text("print('hello')\n")

        def fake_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        from core.models import SuccessCriterion, CriterionType
        crit = SuccessCriterion(type=CriterionType.LINT, description="lint check")

        with patch("evaluator.runner.subprocess.run", side_effect=fake_run):
            engine.evaluate_stage(
                session_id="test-session",
                stage_name="test-stage",
                criteria=[crit],
                artifact_path=str(tmp_path),
                work_dir=str(tmp_path),
                output_artifacts=["clean.py"],
            )

        event_calls = store.emit_event.call_args_list
        event_types = [c[0][1] for c in event_calls]
        assert EventType.EVAL_AUTOFIX_APPLIED not in event_types
