"""Tests for SWE-bench runner framework."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from benchmarks.models import (
    SWEBenchResult,
    SWEBenchRunConfig,
    SWEBenchTaskInstance,
)
from benchmarks.converter import dag_to_task_context, task_to_dag
from benchmarks.runner import (
    SWEBenchRunner,
    filter_instances,
    load_dataset,
)


def _make_instance(**overrides) -> SWEBenchTaskInstance:
    defaults = {
        "instance_id": "test-org__test-repo-12345",
        "repo": "test-org/test-repo",
        "base_commit": "abc123def456",
        "problem_statement": "Fix the bug where users cannot login.",
        "patch": "diff --git a/auth.py b/auth.py\n...",
        "test_patch": "diff --git a/test_auth.py b/test_auth.py\n...",
        "fail_to_pass": [
            "test_auth.py::test_login",
            "test_auth.py::test_logout",
        ],
        "pass_to_pass": ["test_auth.py::test_signup"],
    }
    defaults.update(overrides)
    return SWEBenchTaskInstance(**defaults)


def _make_dataset_jsonl(path: Path, count: int = 3) -> None:
    with open(path, "w") as f:
        for i in range(count):
            inst = _make_instance(
                instance_id=f"org__repo-{i}",
                problem_statement=f"Fix bug #{i}",
            )
            f.write(inst.model_dump_json() + "\n")


# -- Model tests --


class TestSWEBenchTaskInstance:
    def test_minimal_instance(self):
        inst = SWEBenchTaskInstance(
            instance_id="test-1",
            repo="org/repo",
            problem_statement="Fix X",
        )
        assert inst.instance_id == "test-1"
        assert inst.fail_to_pass == []
        assert inst.patch == ""

    def test_full_instance(self):
        inst = _make_instance()
        assert inst.repo == "test-org/test-repo"
        assert len(inst.fail_to_pass) == 2

    def test_json_round_trip(self):
        inst = _make_instance()
        data = json.loads(inst.model_dump_json())
        restored = SWEBenchTaskInstance(**data)
        assert restored.instance_id == inst.instance_id


class TestSWEBenchResult:
    def test_default_status(self):
        result = SWEBenchResult(instance_id="test-1")
        assert result.status == "pending"
        assert result.generated_patch == ""

    def test_completed_result(self):
        result = SWEBenchResult(
            instance_id="test-1",
            status="completed",
            generated_patch="diff --git ...",
        )
        assert result.status == "completed"


class TestSWEBenchRunConfig:
    def test_defaults(self):
        config = SWEBenchRunConfig()
        assert config.max_instances == 0
        assert config.parallel == 1
        assert config.timeout_per_instance == 600


# -- Converter tests --


class TestTaskToDAG:
    def test_creates_dag_with_four_nodes(self):
        inst = _make_instance()
        dag = task_to_dag(inst)
        assert len(dag.nodes) == 4

    def test_linear_dependency_chain(self):
        inst = _make_instance()
        dag = task_to_dag(inst)
        assert len(dag.edges) == 3

        # Verify chain: setup → analyze → generate → validate
        edge_pairs = [(e.from_node, e.to_node) for e in dag.edges]
        node_ids = list(dag.nodes.keys())
        assert (node_ids[0], node_ids[1]) in edge_pairs
        assert (node_ids[1], node_ids[2]) in edge_pairs
        assert (node_ids[2], node_ids[3]) in edge_pairs

    def test_node_agent_types(self):
        inst = _make_instance()
        dag = task_to_dag(inst)
        agents = [n.agent_type for n in dag.nodes.values()]
        assert "generator" in agents
        assert "planner" in agents
        assert "evaluator" in agents

    def test_problem_statement_in_descriptions(self):
        inst = _make_instance()
        dag = task_to_dag(inst)
        all_descs = " ".join(n.task_description for n in dag.nodes.values())
        assert "Fix the bug" in all_descs

    def test_hints_included_when_present(self):
        inst = _make_instance(hints_text="Look at auth.py line 42")
        dag = task_to_dag(inst)
        all_descs = " ".join(n.task_description for n in dag.nodes.values())
        assert "Look at auth.py line 42" in all_descs

    def test_fail_to_pass_in_generate_node(self):
        inst = _make_instance()
        dag = task_to_dag(inst)
        gen_node = [n for n in dag.nodes.values()
                    if n.agent_type == "generator"][1]  # second generator
        assert "test_login" in gen_node.task_description

    def test_dag_reasoning(self):
        inst = _make_instance()
        dag = task_to_dag(inst)
        assert inst.instance_id in dag.reasoning


class TestDagToTaskContext:
    def test_basic_context(self):
        inst = _make_instance()
        ctx = dag_to_task_context(inst)
        assert ctx["source"] == "swebench"
        assert ctx["instance_id"] == inst.instance_id
        assert ctx["repo"] == inst.repo

    def test_includes_target_tests(self):
        inst = _make_instance()
        ctx = dag_to_task_context(inst)
        assert len(ctx["target_tests"]) == 2

    def test_hints_when_present(self):
        inst = _make_instance(hints_text="Check auth.py")
        ctx = dag_to_task_context(inst)
        assert ctx["hints"] == "Check auth.py"

    def test_no_hints_key_when_absent(self):
        inst = _make_instance(hints_text="")
        ctx = dag_to_task_context(inst)
        assert "hints" not in ctx


# -- Runner tests --


class TestLoadDataset:
    def test_loads_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            _make_dataset_jsonl(path, count=3)
            instances = load_dataset(str(path))
            assert len(instances) == 3

    def test_raises_on_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_dataset("/nonexistent/path.jsonl")

    def test_skips_empty_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            inst = _make_instance()
            with open(path, "w") as f:
                f.write(inst.model_dump_json() + "\n")
                f.write("\n")
                f.write(inst.model_dump_json() + "\n")
            instances = load_dataset(str(path))
            assert len(instances) == 2


class TestFilterInstances:
    def test_filter_by_ids(self):
        instances = [_make_instance(instance_id=f"i-{i}") for i in range(5)]
        config = SWEBenchRunConfig(instance_ids=["i-1", "i-3"])
        filtered = filter_instances(instances, config)
        assert len(filtered) == 2
        assert filtered[0].instance_id == "i-1"

    def test_filter_by_max_count(self):
        instances = [_make_instance(instance_id=f"i-{i}") for i in range(5)]
        config = SWEBenchRunConfig(max_instances=2)
        filtered = filter_instances(instances, config)
        assert len(filtered) == 2

    def test_no_filters(self):
        instances = [_make_instance(instance_id=f"i-{i}") for i in range(3)]
        config = SWEBenchRunConfig()
        filtered = filter_instances(instances, config)
        assert len(filtered) == 3


class TestSWEBenchRunner:
    def test_run_without_executor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "test.jsonl"
            _make_dataset_jsonl(dataset_path, count=2)
            config = SWEBenchRunConfig(
                dataset_path=str(dataset_path),
                output_dir=str(Path(tmpdir) / "output"),
            )
            runner = SWEBenchRunner(config)
            results = runner.run()

            assert len(results) == 2
            assert all(r.status == "completed" for r in results)
            assert all(r.generated_patch == "" for r in results)

    def test_run_with_executor(self):
        def mock_executor(instance, dag):
            return {
                "patch": "diff --git a/fix.py",
                "test_result": {"passed": 2, "failed": 0},
                "status": "completed",
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "test.jsonl"
            _make_dataset_jsonl(dataset_path, count=1)
            config = SWEBenchRunConfig(
                dataset_path=str(dataset_path),
                output_dir=str(Path(tmpdir) / "output"),
            )
            runner = SWEBenchRunner(config)
            results = runner.run(executor=mock_executor)

            assert len(results) == 1
            assert results[0].generated_patch == "diff --git a/fix.py"
            assert results[0].test_result["passed"] == 2

    def test_saves_results_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "test.jsonl"
            _make_dataset_jsonl(dataset_path, count=1)
            output_dir = Path(tmpdir) / "output"
            config = SWEBenchRunConfig(
                dataset_path=str(dataset_path),
                output_dir=str(output_dir),
            )
            runner = SWEBenchRunner(config)
            runner.run()

            results_file = output_dir / "results.json"
            assert results_file.exists()
            data = json.loads(results_file.read_text())
            assert len(data) == 1

    def test_results_property(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "test.jsonl"
            _make_dataset_jsonl(dataset_path, count=2)
            config = SWEBenchRunConfig(
                dataset_path=str(dataset_path),
                output_dir=str(Path(tmpdir) / "output"),
            )
            runner = SWEBenchRunner(config)
            runner.run()
            assert len(runner.results) == 2

    def test_get_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "test.jsonl"
            _make_dataset_jsonl(dataset_path, count=3)
            config = SWEBenchRunConfig(
                dataset_path=str(dataset_path),
                output_dir=str(Path(tmpdir) / "output"),
            )
            runner = SWEBenchRunner(config)
            runner.run()
            summary = runner.get_summary()
            assert summary["total"] == 3
            assert summary["completed"] == 3
            assert summary["failed"] == 0

    def test_max_instances_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dataset_path = Path(tmpdir) / "test.jsonl"
            _make_dataset_jsonl(dataset_path, count=5)
            config = SWEBenchRunConfig(
                dataset_path=str(dataset_path),
                max_instances=2,
                output_dir=str(Path(tmpdir) / "output"),
            )
            runner = SWEBenchRunner(config)
            results = runner.run()
            assert len(results) == 2
