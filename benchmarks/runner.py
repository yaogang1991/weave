"""SWE-bench runner — orchestrate evaluation of task instances.

Loads a SWE-bench dataset, converts each task instance to a Weave DAG,
executes it, and collects results. Supports filtering by instance IDs,
max count, and parallel execution.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from benchmarks.converter import task_to_dag
from benchmarks.models import (
    SWEBenchResult,
    SWEBenchRunConfig,
    SWEBenchTaskInstance,
)

logger = logging.getLogger(__name__)


def load_dataset(path: str) -> list[SWEBenchTaskInstance]:
    """Load SWE-bench task instances from a JSONL file.

    Each line in the file is a JSON object matching
    ``SWEBenchTaskInstance`` schema.
    """
    instances: list[SWEBenchTaskInstance] = []
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Dataset not found: {path}")

    with open(file_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                instances.append(SWEBenchTaskInstance(**data))
            except Exception as e:
                logger.warning(
                    "Failed to parse line %d in %s: %s", line_num, path, e
                )

    logger.info("Loaded %d instances from %s", len(instances), path)
    return instances


def filter_instances(
    instances: list[SWEBenchTaskInstance],
    config: SWEBenchRunConfig,
) -> list[SWEBenchTaskInstance]:
    """Filter instances based on run config."""
    filtered = instances

    # Filter by specific instance IDs
    if config.instance_ids:
        id_set = set(config.instance_ids)
        filtered = [i for i in filtered if i.instance_id in id_set]

    # Limit count
    if config.max_instances > 0:
        filtered = filtered[:config.max_instances]

    return filtered


class SWEBenchRunner:
    """Orchestrates SWE-bench evaluation runs.

    Usage::

        config = SWEBenchRunConfig(dataset_path="swebench.jsonl")
        runner = SWEBenchRunner(config)
        results = runner.run()
    """

    def __init__(self, config: SWEBenchRunConfig) -> None:
        self.config = config
        self._results: dict[str, SWEBenchResult] = {}

    def run(
        self,
        executor: Any | None = None,
    ) -> list[SWEBenchResult]:
        """Run the SWE-bench evaluation.

        Parameters
        ----------
        executor:
            Optional callable that takes (instance, dag) and returns
            a dict with at least "status" and optionally "patch".
            When None, only generates DAGs without execution.

        Returns
        -------
        List of results for each task instance.
        """
        instances = load_dataset(self.config.dataset_path)
        instances = filter_instances(instances, self.config)

        logger.info(
            "Running SWE-bench evaluation: %d instances", len(instances)
        )

        results: list[SWEBenchResult] = []
        for instance in instances:
            result = self._run_instance(instance, executor)
            results.append(result)
            self._results[instance.instance_id] = result

        self._save_results(results)
        return results

    def _run_instance(
        self,
        instance: SWEBenchTaskInstance,
        executor: Any | None,
    ) -> SWEBenchResult:
        """Run a single task instance."""
        start_time = time.time()
        result = SWEBenchResult(
            instance_id=instance.instance_id,
            status="running",
        )

        try:
            dag = task_to_dag(
                instance, timeout=self.config.timeout_per_instance
            )
            result.metadata["dag_nodes"] = list(dag.nodes.keys())

            if executor is not None:
                exec_result = executor(instance, dag)
                result.generated_patch = exec_result.get("patch", "")
                result.model_patch = result.generated_patch
                result.test_result = exec_result.get("test_result")
                result.status = exec_result.get("status", "completed")
            else:
                result.status = "completed"
                result.metadata["note"] = "DAG generated, no executor"

        except Exception as e:
            result.status = "failed"
            result.error = str(e)
            logger.error(
                "Failed instance %s: %s", instance.instance_id, e
            )

        result.execution_time_sec = time.time() - start_time
        return result

    def _save_results(self, results: list[SWEBenchResult]) -> None:
        """Save results to output directory."""
        output_dir = Path(self.config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / "results.json"
        data = [r.model_dump(mode="json") for r in results]
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

        logger.info("Saved %d results to %s", len(results), output_path)

    @property
    def results(self) -> dict[str, SWEBenchResult]:
        """Access results by instance ID."""
        return dict(self._results)

    def get_summary(self) -> dict[str, Any]:
        """Get aggregate statistics."""
        all_results = list(self._results.values())
        if not all_results:
            return {"total": 0}

        completed = [r for r in all_results if r.status == "completed"]
        failed = [r for r in all_results if r.status == "failed"]

        return {
            "total": len(all_results),
            "completed": len(completed),
            "failed": len(failed),
            "avg_time_sec": (
                sum(r.execution_time_sec for r in all_results)
                / len(all_results)
            ),
        }
