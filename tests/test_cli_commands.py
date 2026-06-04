"""Comprehensive tests for CLI command modules: approval, impact, learning,
memory, skills, github.

Focus: command dispatch logic, argument handling, JSON output format,
and error paths. Underlying business logic is mocked.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))


def _ns(**kwargs):
    """Build an argparse.Namespace from kwargs."""
    return argparse.Namespace(**kwargs)


# ===========================================================================
# 1. cli/approval.py -- cmd_tickets, cmd_approve, cmd_reject
# ===========================================================================


class TestCmdTickets:
    """Tests for cmd_tickets."""

    def test_happy_path_lists_tickets(self, capsys):
        """cmd_tickets prints JSON with tickets list and count."""
        mock_ticket = MagicMock()
        mock_ticket.id = "ticket_001"
        mock_ticket.job_id = "job_001"
        mock_ticket.tool_name = "bash"
        mock_ticket.status.value = "pending"
        mock_ticket.risk_level = "high"
        mock_ticket.args_preview = "rm -rf /"
        mock_ticket.requested_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        mock_ticket.expires_at = datetime(2025, 1, 2, tzinfo=timezone.utc)

        mock_repo = MagicMock()
        mock_repo.expire_tickets = MagicMock()
        mock_repo.list_tickets = MagicMock(return_value=[mock_ticket])
        mock_repo.get_stats = MagicMock(return_value={"pending": 1})

        args = _ns(status=None, job_id=None)
        with patch("cli.approval.ApprovalRepository", return_value=mock_repo):
            import asyncio
            asyncio.run(__import__("cli.approval", fromlist=["cmd_tickets"]).cmd_tickets(args))

        output = json.loads(capsys.readouterr().out)
        assert output["count"] == 1
        assert output["tickets"][0]["id"] == "ticket_001"
        assert output["tickets"][0]["tool_name"] == "bash"
        assert output["stats"]["pending"] == 1

    def test_empty_tickets(self, capsys):
        """cmd_tickets returns empty list when no tickets exist."""
        mock_repo = MagicMock()
        mock_repo.expire_tickets = MagicMock()
        mock_repo.list_tickets = MagicMock(return_value=[])
        mock_repo.get_stats = MagicMock(return_value={})

        args = _ns(status=None, job_id=None)
        with patch("cli.approval.ApprovalRepository", return_value=mock_repo):
            import asyncio
            asyncio.run(__import__("cli.approval", fromlist=["cmd_tickets"]).cmd_tickets(args))

        output = json.loads(capsys.readouterr().out)
        assert output["count"] == 0
        assert output["tickets"] == []

    def test_status_filter_passed(self, capsys):
        """cmd_tickets passes status filter to repo.list_tickets."""
        mock_repo = MagicMock()
        mock_repo.expire_tickets = MagicMock()
        mock_repo.list_tickets = MagicMock(return_value=[])
        mock_repo.get_stats = MagicMock(return_value={})

        args = _ns(status="pending", job_id=None)
        with patch("cli.approval.ApprovalRepository", return_value=mock_repo):
            import asyncio
            asyncio.run(__import__("cli.approval", fromlist=["cmd_tickets"]).cmd_tickets(args))

        mock_repo.list_tickets.assert_called_once()
        call_kwargs = mock_repo.list_tickets.call_args
        assert call_kwargs[1]["status"].value == "pending" or call_kwargs[0][0].value == "pending"

    def test_job_id_filter_passed(self, capsys):
        """cmd_tickets passes job_id filter to repo.list_tickets."""
        mock_repo = MagicMock()
        mock_repo.expire_tickets = MagicMock()
        mock_repo.list_tickets = MagicMock(return_value=[])
        mock_repo.get_stats = MagicMock(return_value={})

        args = _ns(status=None, job_id="job_abc")
        with patch("cli.approval.ApprovalRepository", return_value=mock_repo):
            import asyncio
            asyncio.run(__import__("cli.approval", fromlist=["cmd_tickets"]).cmd_tickets(args))

        call_kwargs = mock_repo.list_tickets.call_args
        assert call_kwargs[1].get("job_id") == "job_abc" or call_kwargs[0][1] == "job_abc"


class TestCmdApprove:
    """Tests for cmd_approve."""

    def test_ticket_not_found_exits(self):
        """cmd_approve exits with code 1 when ticket not found."""
        mock_repo = MagicMock()
        mock_repo.get_ticket = MagicMock(return_value=None)

        args = _ns(ticket_id="ticket_missing", reason=None)
        with patch("cli.approval.ApprovalRepository", return_value=mock_repo):
            import asyncio
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(__import__("cli.approval", fromlist=["cmd_approve"]).cmd_approve(args))
            assert exc_info.value.code == 1

    def test_happy_path_approves_ticket(self, capsys):
        """cmd_approve approves a ticket and prints JSON result."""
        mock_ticket = MagicMock()
        mock_ticket.id = "ticket_001"
        mock_ticket.job_id = "job_001"
        mock_ticket.status.value = "pending"
        mock_ticket.decided_by = "user"
        mock_ticket.reason = "looks good"
        mock_ticket.decided_at = datetime(2025, 1, 1, tzinfo=timezone.utc)

        mock_repo = MagicMock()
        mock_repo.get_ticket = MagicMock(return_value=mock_ticket)
        mock_repo.approve_ticket = MagicMock(return_value=mock_ticket)

        mock_service = MagicMock()
        mock_service.resume_after_approval = AsyncMock()

        args = _ns(ticket_id="ticket_001", reason="looks good")
        with patch("cli.approval.ApprovalRepository", return_value=mock_repo), \
             patch("cli.approval._make_repository", return_value=MagicMock()), \
             patch("cli.approval._make_run_service", return_value=mock_service):
            import asyncio
            asyncio.run(__import__("cli.approval", fromlist=["cmd_approve"]).cmd_approve(args))

        output = json.loads(capsys.readouterr().out)
        assert output["ticket_id"] == "ticket_001"
        assert output["message"] == "Ticket approved"

    def test_approve_value_error_exits(self):
        """cmd_approve exits when repo raises ValueError (already decided)."""
        mock_ticket = MagicMock()
        mock_ticket.id = "ticket_001"
        mock_ticket.job_id = "job_001"
        mock_ticket.status.value = "approved"

        mock_repo = MagicMock()
        mock_repo.get_ticket = MagicMock(return_value=mock_ticket)
        mock_repo.approve_ticket = MagicMock(side_effect=ValueError("Already decided"))

        args = _ns(ticket_id="ticket_001", reason=None)
        with patch("cli.approval.ApprovalRepository", return_value=mock_repo), \
             patch("cli.approval._make_repository", return_value=MagicMock()), \
             patch("cli.approval._make_run_service", return_value=MagicMock()):
            import asyncio
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(__import__("cli.approval", fromlist=["cmd_approve"]).cmd_approve(args))
            assert exc_info.value.code == 1


class TestCmdReject:
    """Tests for cmd_reject."""

    def test_ticket_not_found_exits(self):
        """cmd_reject exits with code 1 when ticket not found."""
        mock_repo = MagicMock()
        mock_repo.get_ticket = MagicMock(return_value=None)

        args = _ns(ticket_id="ticket_missing", reason=None)
        with patch("cli.approval.ApprovalRepository", return_value=mock_repo):
            import asyncio
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(__import__("cli.approval", fromlist=["cmd_reject"]).cmd_reject(args))
            assert exc_info.value.code == 1

    def test_happy_path_rejects_ticket(self, capsys):
        """cmd_reject rejects a ticket and prints JSON result."""
        mock_ticket = MagicMock()
        mock_ticket.id = "ticket_001"
        mock_ticket.job_id = "job_001"
        mock_ticket.status.value = "rejected"
        mock_ticket.decided_by = "user"
        mock_ticket.reason = "too risky"
        mock_ticket.decided_at = datetime(2025, 1, 1, tzinfo=timezone.utc)

        mock_repo = MagicMock()
        mock_repo.get_ticket = MagicMock(return_value=mock_ticket)
        mock_repo.reject_ticket = MagicMock(return_value=mock_ticket)

        mock_service = MagicMock()
        mock_service.abort_after_rejection = AsyncMock()

        args = _ns(ticket_id="ticket_001", reason="too risky")
        with patch("cli.approval.ApprovalRepository", return_value=mock_repo), \
             patch("cli.approval._make_repository", return_value=MagicMock()), \
             patch("cli.approval._make_run_service", return_value=mock_service):
            import asyncio
            asyncio.run(__import__("cli.approval", fromlist=["cmd_reject"]).cmd_reject(args))

        output = json.loads(capsys.readouterr().out)
        assert output["ticket_id"] == "ticket_001"
        assert output["message"] == "Ticket rejected"

    def test_reject_value_error_exits(self):
        """cmd_reject exits when repo raises ValueError."""
        mock_ticket = MagicMock()
        mock_ticket.id = "ticket_001"
        mock_ticket.job_id = "job_001"
        mock_ticket.status.value = "approved"

        mock_repo = MagicMock()
        mock_repo.get_ticket = MagicMock(return_value=mock_ticket)
        mock_repo.reject_ticket = MagicMock(side_effect=ValueError("Cannot reject"))

        args = _ns(ticket_id="ticket_001", reason=None)
        with patch("cli.approval.ApprovalRepository", return_value=mock_repo), \
             patch("cli.approval._make_repository", return_value=MagicMock()), \
             patch("cli.approval._make_run_service", return_value=MagicMock()):
            import asyncio
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(__import__("cli.approval", fromlist=["cmd_reject"]).cmd_reject(args))
            assert exc_info.value.code == 1


# ===========================================================================
# 2. cli/impact.py -- cmd_impact_predict, cmd_impact_graph, cmd_impact_history
# ===========================================================================


class TestCmdImpactPredict:
    """Tests for cmd_impact_predict."""

    def test_happy_path(self, capsys):
        """cmd_impact_predict prints JSON with prediction result."""
        mock_result = MagicMock()
        mock_result.model_dump = MagicMock(return_value={
            "predicted_files": ["src/main.py"],
            "confidence": 0.85,
        })

        mock_predictor = MagicMock()
        mock_predictor.predict_static = MagicMock(return_value=mock_result)

        mock_config = MagicMock()
        mock_config.impact.max_predicted_files = 20
        mock_config.impact.confidence_threshold = 0.5

        args = _ns(requirement="Fix bug in DAG engine", project=".")
        with patch("cli.impact.WeaveConfig.from_env", return_value=mock_config), \
             patch("analysis.impact_predictor.ImpactPredictor", return_value=mock_predictor):
            import asyncio
            asyncio.run(__import__("cli.impact", fromlist=["cmd_impact_predict"]).cmd_impact_predict(args))

        output = json.loads(capsys.readouterr().out)
        assert "predicted_files" in output
        assert output["confidence"] == 0.85

    def test_project_defaults_to_cwd(self, capsys):
        """cmd_impact_predict defaults project to '.' when not provided."""
        mock_result = MagicMock()
        mock_result.model_dump = MagicMock(return_value={"predicted_files": []})
        mock_predictor = MagicMock()
        mock_predictor.predict_static = MagicMock(return_value=mock_result)

        mock_config = MagicMock()
        mock_config.impact.max_predicted_files = 20
        mock_config.impact.confidence_threshold = 0.5

        args = _ns(requirement="Fix something", project=None)
        with patch("cli.impact.WeaveConfig.from_env", return_value=mock_config), \
             patch("analysis.impact_predictor.ImpactPredictor", return_value=mock_predictor):
            import asyncio
            asyncio.run(__import__("cli.impact", fromlist=["cmd_impact_predict"]).cmd_impact_predict(args))

        # Verify predict_static was called with "." as project path
        mock_predictor.predict_static.assert_called_once_with("Fix something", ".")


class TestCmdImpactGraph:
    """Tests for cmd_impact_graph."""

    def test_happy_path(self, capsys):
        """cmd_impact_graph prints JSON dependency graph."""
        mock_graph = MagicMock()
        mock_graph.build = MagicMock()
        mock_graph.to_dict = MagicMock(return_value={"nodes": {}, "edges": []})

        args = _ns(project=".")
        with patch("analysis.dependency_graph.DependencyGraph", return_value=mock_graph):
            import asyncio
            asyncio.run(__import__("cli.impact", fromlist=["cmd_impact_graph"]).cmd_impact_graph(args))

        output = json.loads(capsys.readouterr().out)
        assert "nodes" in output
        assert "edges" in output
        mock_graph.build.assert_called_once()

    def test_project_defaults_to_cwd(self, capsys):
        """cmd_impact_graph defaults project to '.' when not provided."""
        mock_graph = MagicMock()
        mock_graph.build = MagicMock()
        mock_graph.to_dict = MagicMock(return_value={})

        args = _ns(project=None)
        with patch("analysis.dependency_graph.DependencyGraph", return_value=mock_graph) as MockCls:
            import asyncio
            asyncio.run(__import__("cli.impact", fromlist=["cmd_impact_graph"]).cmd_impact_graph(args))

        MockCls.assert_called_once_with(".")


class TestCmdImpactHistory:
    """Tests for cmd_impact_history."""

    def test_no_history_dir(self, capsys):
        """cmd_impact_history returns empty list when dir doesn't exist."""
        mock_config = MagicMock()
        mock_config.impact.base_path = "/nonexistent/path"

        with patch("cli.impact.WeaveConfig.from_env", return_value=mock_config):
            import asyncio
            asyncio.run(__import__("cli.impact", fromlist=["cmd_impact_history"]).cmd_impact_history(
                _ns()
            ))

        output = json.loads(capsys.readouterr().out)
        assert output["history"] == []
        assert output["count"] == 0

    def test_with_records(self, capsys, tmp_path):
        """cmd_impact_history reads and returns stored records."""
        impact_dir = tmp_path / "impact"
        impact_dir.mkdir()
        record_file = impact_dir / "record_001.json"
        record_file.write_text(json.dumps({"requirement": "fix bug", "files": ["a.py"]}))

        mock_config = MagicMock()
        mock_config.impact.base_path = str(impact_dir)

        with patch("cli.impact.WeaveConfig.from_env", return_value=mock_config):
            import asyncio
            asyncio.run(__import__("cli.impact", fromlist=["cmd_impact_history"]).cmd_impact_history(
                _ns()
            ))

        output = json.loads(capsys.readouterr().out)
        assert output["count"] == 1
        assert output["history"][0]["requirement"] == "fix bug"


# ===========================================================================
# 3. cli/learning.py -- cmd_learning_analyze, cmd_learning_insights,
#                        cmd_learning_status
# ===========================================================================


class TestCmdLearningAnalyze:
    """Tests for cmd_learning_analyze."""

    def test_happy_path(self, capsys):
        """cmd_learning_analyze prints JSON analysis result."""
        mock_scheduler = MagicMock()
        mock_scheduler.run_analysis = MagicMock(return_value={
            "total_insights": 3,
            "stored_memories": 2,
        })

        with patch("cli.learning._make_learning_scheduler", return_value=mock_scheduler):
            import asyncio
            asyncio.run(__import__("cli.learning", fromlist=["cmd_learning_analyze"]).cmd_learning_analyze(
                _ns()
            ))

        output = json.loads(capsys.readouterr().out)
        assert output["total_insights"] == 3
        assert output["stored_memories"] == 2

    def test_scheduler_error_propagates(self):
        """cmd_learning_analyze lets exceptions from scheduler bubble up."""
        mock_scheduler = MagicMock()
        mock_scheduler.run_analysis = MagicMock(side_effect=RuntimeError("DB error"))

        with patch("cli.learning._make_learning_scheduler", return_value=mock_scheduler):
            import asyncio
            with pytest.raises(RuntimeError, match="DB error"):
                asyncio.run(
                    __import__("cli.learning", fromlist=["cmd_learning_analyze"]).cmd_learning_analyze(
                        _ns()
                    )
                )


class TestCmdLearningInsights:
    """Tests for cmd_learning_insights."""

    def test_happy_path(self, capsys):
        """cmd_learning_insights prints JSON list of insight entries."""
        mock_entry = MagicMock()
        mock_entry.id = "mem_001"
        mock_entry.agent_type = "planner"
        mock_entry.scope.value = "global"
        mock_entry.memory_type.value = "pattern"
        mock_entry.content = "Prefer small DAGs"
        mock_entry.keywords = ["dag", "small"]
        mock_entry.relevance_score = 0.9
        mock_entry.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)

        mock_manager = MagicMock()
        mock_manager.store.search = MagicMock(return_value=[mock_entry])

        with patch("cli.learning._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(
                __import__("cli.learning", fromlist=["cmd_learning_insights"]).cmd_learning_insights(
                    _ns(limit=10)
                )
            )

        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        assert output[0]["id"] == "mem_001"
        assert output[0]["scope"] == "global"

    def test_limit_passed_to_search(self, capsys):
        """cmd_learning_insights passes args.limit to store.search."""
        mock_manager = MagicMock()
        mock_manager.store.search = MagicMock(return_value=[])

        args = _ns(limit=5)
        with patch("cli.learning._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(
                __import__("cli.learning", fromlist=["cmd_learning_insights"]).cmd_learning_insights(args)
            )

        mock_manager.store.search.assert_called_once()
        assert mock_manager.store.search.call_args[1]["limit"] == 5

    def test_empty_insights(self, capsys):
        """cmd_learning_insights returns empty list when no insights found."""
        mock_manager = MagicMock()
        mock_manager.store.search = MagicMock(return_value=[])

        with patch("cli.learning._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(
                __import__("cli.learning", fromlist=["cmd_learning_insights"]).cmd_learning_insights(
                    _ns(limit=10)
                )
            )

        output = json.loads(capsys.readouterr().out)
        assert output == []


class TestCmdLearningStatus:
    """Tests for cmd_learning_status."""

    def test_happy_path(self, capsys):
        """cmd_learning_status prints JSON status."""
        mock_scheduler = MagicMock()
        mock_scheduler.get_status = MagicMock(return_value={
            "enabled": True,
            "analysis_interval_hours": 24,
            "confidence_threshold": 0.7,
            "last_analysis": None,
            "last_insight_count": 0,
        })

        with patch("cli.learning._make_learning_scheduler", return_value=mock_scheduler):
            import asyncio
            asyncio.run(
                __import__("cli.learning", fromlist=["cmd_learning_status"]).cmd_learning_status(_ns())
            )

        output = json.loads(capsys.readouterr().out)
        assert output["enabled"] is True
        assert output["analysis_interval_hours"] == 24

    def test_status_with_last_analysis(self, capsys):
        """cmd_learning_status shows last analysis data when present."""
        mock_scheduler = MagicMock()
        mock_scheduler.get_status = MagicMock(return_value={
            "enabled": True,
            "last_analysis": "2025-01-01T00:00:00",
            "last_insight_count": 5,
            "last_memory_count": 3,
        })

        with patch("cli.learning._make_learning_scheduler", return_value=mock_scheduler):
            import asyncio
            asyncio.run(
                __import__("cli.learning", fromlist=["cmd_learning_status"]).cmd_learning_status(_ns())
            )

        output = json.loads(capsys.readouterr().out)
        assert output["last_insight_count"] == 5
        assert output["last_memory_count"] == 3


# ===========================================================================
# 4. cli/memory.py -- cmd_memory_search, cmd_memory_list, cmd_memory_stats,
#                      cmd_memory_add, cmd_memory_cleanup
# ===========================================================================


class TestCmdMemorySearch:
    """Tests for cmd_memory_search."""

    def test_happy_path(self, capsys):
        """cmd_memory_search prints JSON search results."""
        mock_entry = MagicMock()
        mock_entry.id = "mem_001"
        mock_entry.agent_type = "generator"
        mock_entry.scope.value = "session"
        mock_entry.memory_type.value = "fact"
        mock_entry.content = "Use async/await"
        mock_entry.keywords = ["async"]
        mock_entry.relevance_score = 0.95
        mock_entry.access_count = 3
        mock_entry.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)

        mock_manager = MagicMock()
        mock_manager.store.search = MagicMock(return_value=[mock_entry])

        args = _ns(query="async patterns", scope=None, agent=None, type=None, limit=10)
        with patch("cli.memory._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(__import__("cli.memory", fromlist=["cmd_memory_search"]).cmd_memory_search(args))

        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        assert output[0]["id"] == "mem_001"
        assert output[0]["access_count"] == 3

    def test_scope_and_type_filters(self, capsys):
        """cmd_memory_search passes scope and type filters to store.search."""
        mock_manager = MagicMock()
        mock_manager.store.search = MagicMock(return_value=[])

        args = _ns(query="test", scope="global", agent="planner", type="experience", limit=5)
        with patch("cli.memory._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(__import__("cli.memory", fromlist=["cmd_memory_search"]).cmd_memory_search(args))

        call_kwargs = mock_manager.store.search.call_args[1]
        assert call_kwargs["scope"].value == "global"
        assert call_kwargs["memory_type"].value == "experience"
        assert call_kwargs["agent_type"] == "planner"
        assert call_kwargs["limit"] == 5

    def test_empty_results(self, capsys):
        """cmd_memory_search returns empty list when no matches."""
        mock_manager = MagicMock()
        mock_manager.store.search = MagicMock(return_value=[])

        args = _ns(query="nonexistent", scope=None, agent=None, type=None, limit=10)
        with patch("cli.memory._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(__import__("cli.memory", fromlist=["cmd_memory_search"]).cmd_memory_search(args))

        output = json.loads(capsys.readouterr().out)
        assert output == []


class TestCmdMemoryList:
    """Tests for cmd_memory_list."""

    def test_happy_path(self, capsys):
        """cmd_memory_list prints JSON list of entries."""
        mock_entry = MagicMock()
        mock_entry.id = "mem_002"
        mock_entry.agent_type = "evaluator"
        mock_entry.scope.value = "global"
        mock_entry.memory_type.value = "anti_pattern"
        mock_entry.content = "Do not use sync calls in async code" + "x" * 250
        mock_entry.keywords = ["sync", "anti-pattern"]
        mock_entry.relevance_score = 0.8
        mock_entry.access_count = 1
        mock_entry.created_at = datetime(2025, 6, 1, tzinfo=timezone.utc)

        mock_manager = MagicMock()
        mock_manager.store.list_entries = MagicMock(return_value=[mock_entry])

        args = _ns(scope=None, agent=None, type=None)
        with patch("cli.memory._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(__import__("cli.memory", fromlist=["cmd_memory_list"]).cmd_memory_list(args))

        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        # content is truncated to 200 chars
        assert len(output[0]["content"]) == 200

    def test_scope_filter(self, capsys):
        """cmd_memory_list passes scope filter to list_entries."""
        mock_manager = MagicMock()
        mock_manager.store.list_entries = MagicMock(return_value=[])

        args = _ns(scope="session", agent=None, type=None)
        with patch("cli.memory._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(__import__("cli.memory", fromlist=["cmd_memory_list"]).cmd_memory_list(args))

        call_kwargs = mock_manager.store.list_entries.call_args[1]
        assert call_kwargs["scope"].value == "session"


class TestCmdMemoryStats:
    """Tests for cmd_memory_stats."""

    def test_happy_path(self, capsys):
        """cmd_memory_stats prints JSON stats."""
        mock_manager = MagicMock()
        mock_manager.get_stats = MagicMock(return_value={
            "total_entries": 42,
            "by_scope": {"global": 30, "session": 12},
            "by_type": {"fact": 20, "pattern": 22},
        })

        with patch("cli.memory._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(__import__("cli.memory", fromlist=["cmd_memory_stats"]).cmd_memory_stats(_ns()))

        output = json.loads(capsys.readouterr().out)
        assert output["total_entries"] == 42
        assert output["by_scope"]["global"] == 30


class TestCmdMemoryAdd:
    """Tests for cmd_memory_add."""

    def test_happy_path(self, capsys):
        """cmd_memory_add stores an entry and prints JSON result."""
        mock_entry = MagicMock()
        mock_entry.id = "mem_new"
        mock_entry.agent_type = "planner"
        mock_entry.scope.value = "global"
        mock_entry.memory_type.value = "fact"

        mock_manager = MagicMock()
        mock_manager.store_learning = MagicMock(return_value=mock_entry)

        args = _ns(agent="planner", content="Always use type hints", type="fact", scope="global", keywords=None)
        with patch("cli.memory._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(__import__("cli.memory", fromlist=["cmd_memory_add"]).cmd_memory_add(args))

        output = json.loads(capsys.readouterr().out)
        assert output["id"] == "mem_new"
        assert output["message"] == "Memory entry added"

    def test_keywords_passed(self, capsys):
        """cmd_memory_add passes keywords to store_learning."""
        mock_entry = MagicMock()
        mock_entry.id = "mem_kw"
        mock_entry.agent_type = "generator"
        mock_entry.scope.value = "private"
        mock_entry.memory_type.value = "experience"

        mock_manager = MagicMock()
        mock_manager.store_learning = MagicMock(return_value=mock_entry)

        args = _ns(agent="generator", content="test", type="experience", scope="private",
                    keywords=["k1", "k2"])
        with patch("cli.memory._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(__import__("cli.memory", fromlist=["cmd_memory_add"]).cmd_memory_add(args))

        call_kwargs = mock_manager.store_learning.call_args[1]
        assert call_kwargs["keywords"] == ["k1", "k2"]


class TestCmdMemoryCleanup:
    """Tests for cmd_memory_cleanup."""

    def test_happy_path(self, capsys):
        """cmd_memory_cleanup runs maintenance and prints JSON result."""
        mock_manager = MagicMock()
        mock_manager.run_maintenance = MagicMock(return_value={
            "deleted": 5,
            "archived": 2,
            "errors": 0,
        })

        with patch("cli.memory._make_memory_manager", return_value=mock_manager):
            import asyncio
            asyncio.run(
                __import__("cli.memory", fromlist=["cmd_memory_cleanup"]).cmd_memory_cleanup(_ns())
            )

        output = json.loads(capsys.readouterr().out)
        assert output["deleted"] == 5
        assert output["errors"] == 0


# ===========================================================================
# 5. cli/skills.py -- cmd_skills, cmd_skill, cmd_templates
# ===========================================================================


class TestCmdSkills:
    """Tests for cmd_skills."""

    def test_happy_path(self, capsys):
        """cmd_skills prints JSON with skills list."""
        mock_skill = MagicMock()
        mock_skill.name = "review_code"
        mock_skill.description = "Review code for issues"
        mock_skill.agent_types = ["evaluator"]
        mock_skill.variables = {"file": MagicMock(default="")}
        mock_skill.tool_allowlist = ["read", "bash"]

        mock_registry = MagicMock()
        mock_registry.list_skills = MagicMock(return_value=[mock_skill])

        args = _ns(project=None, agent=None)
        with patch("skills.registry.SkillRegistry", return_value=mock_registry):
            import asyncio
            asyncio.run(__import__("cli.skills", fromlist=["cmd_skills"]).cmd_skills(args))

        output = json.loads(capsys.readouterr().out)
        assert output["count"] == 1
        assert output["skills"][0]["name"] == "review_code"
        assert output["skills"][0]["agent_types"] == ["evaluator"]

    def test_agent_filter(self, capsys):
        """cmd_skills filters skills by agent type."""
        skill_a = MagicMock()
        skill_a.name = "review"
        skill_a.description = "Review"
        skill_a.agent_types = ["evaluator"]
        skill_a.variables = {}
        skill_a.tool_allowlist = []

        skill_b = MagicMock()
        skill_b.name = "generate"
        skill_b.description = "Generate"
        skill_b.agent_types = ["generator"]
        skill_b.variables = {}
        skill_b.tool_allowlist = []

        mock_registry = MagicMock()
        mock_registry.list_skills = MagicMock(return_value=[skill_a, skill_b])

        args = _ns(project=None, agent="evaluator")
        with patch("skills.registry.SkillRegistry", return_value=mock_registry):
            import asyncio
            asyncio.run(__import__("cli.skills", fromlist=["cmd_skills"]).cmd_skills(args))

        output = json.loads(capsys.readouterr().out)
        assert output["count"] == 1
        assert output["skills"][0]["name"] == "review"

    def test_empty_skills(self, capsys):
        """cmd_skills returns empty list when no skills found."""
        mock_registry = MagicMock()
        mock_registry.list_skills = MagicMock(return_value=[])

        args = _ns(project=None, agent=None)
        with patch("skills.registry.SkillRegistry", return_value=mock_registry):
            import asyncio
            asyncio.run(__import__("cli.skills", fromlist=["cmd_skills"]).cmd_skills(args))

        output = json.loads(capsys.readouterr().out)
        assert output["count"] == 0

    def test_skill_with_no_agent_types_shows_all(self, capsys):
        """Skills with empty agent_types list should show 'all'."""
        mock_skill = MagicMock()
        mock_skill.name = "generic"
        mock_skill.description = "Generic skill"
        mock_skill.agent_types = []  # empty means all agents
        mock_skill.variables = {}
        mock_skill.tool_allowlist = []

        mock_registry = MagicMock()
        mock_registry.list_skills = MagicMock(return_value=[mock_skill])

        args = _ns(project=None, agent=None)
        with patch("skills.registry.SkillRegistry", return_value=mock_registry):
            import asyncio
            asyncio.run(__import__("cli.skills", fromlist=["cmd_skills"]).cmd_skills(args))

        output = json.loads(capsys.readouterr().out)
        assert output["skills"][0]["agent_types"] == ["all"]


class TestCmdTemplates:
    """Tests for cmd_templates."""

    def test_list_all_templates(self, capsys):
        """cmd_templates lists all templates when no name given."""
        mock_tpl = MagicMock()
        mock_tpl.name = "build_api"
        mock_tpl.description = "Build an API"
        mock_tpl.version = "1.0"
        mock_tpl.category = "build"
        mock_tpl.nodes = ["node1", "node2"]
        mock_tpl.edges = ["edge1"]
        mock_tpl.variables = {"feature": "string"}

        mock_registry = MagicMock()
        mock_registry.list_templates = MagicMock(return_value=[mock_tpl])

        args = _ns(name=None)
        with patch("templates.library.TemplateRegistry", return_value=mock_registry):
            import asyncio
            asyncio.run(
                __import__("cli.skills", fromlist=["cmd_templates"]).cmd_templates(args)
            )

        output = json.loads(capsys.readouterr().out)
        assert output["count"] == 1
        assert output["templates"][0]["name"] == "build_api"
        assert output["templates"][0]["nodes"] == 2
        assert output["templates"][0]["edges"] == 1

    def test_get_named_template(self, capsys):
        """cmd_templates shows single template details when name given."""
        mock_tpl = MagicMock()
        mock_tpl.name = "fix_bug"
        mock_tpl.description = "Fix a bug"
        mock_tpl.version = "2.0"
        mock_tpl.category = "fix"
        mock_tpl.nodes = []
        mock_tpl.edges = []
        mock_tpl.variables = {"bug": "string"}
        mock_tpl.reasoning_template = "Fix {bug}"

        mock_registry = MagicMock()
        mock_registry.get_template = MagicMock(return_value=mock_tpl)

        args = _ns(name="fix_bug")
        with patch("templates.library.TemplateRegistry", return_value=mock_registry):
            import asyncio
            asyncio.run(
                __import__("cli.skills", fromlist=["cmd_templates"]).cmd_templates(args)
            )

        output = json.loads(capsys.readouterr().out)
        assert output["name"] == "fix_bug"
        assert output["version"] == "2.0"
        assert output["reasoning_template"] == "Fix {bug}"

    def test_template_not_found_exits(self):
        """cmd_templates exits when named template not found."""
        mock_registry = MagicMock()
        mock_registry.get_template = MagicMock(return_value=None)

        args = _ns(name="nonexistent")
        with patch("templates.library.TemplateRegistry", return_value=mock_registry):
            import asyncio
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    __import__("cli.skills", fromlist=["cmd_templates"]).cmd_templates(args)
                )
            assert exc_info.value.code == 1


# ===========================================================================
# 6. cli/github.py -- cmd_issue_poll, cmd_issue_run, cmd_issue_status
# ===========================================================================


class TestCmdIssuePoll:
    """Tests for cmd_issue_poll."""

    def test_no_repo_exits(self):
        """cmd_issue_poll exits when no repo configured."""
        mock_config = MagicMock()
        mock_config.github_repo = ""
        mock_config.label.trigger_label = "weave"
        mock_config.dry_run = False

        args = _ns(repo=None, dry_run=False, limit=1)
        with patch("cli.github.IntegrationConfig.from_env", return_value=mock_config):
            import asyncio
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    __import__("cli.github", fromlist=["cmd_issue_poll"]).cmd_issue_poll(args)
                )
            assert exc_info.value.code == 1

    def test_health_check_failure_exits(self):
        """cmd_issue_poll exits when gh not authenticated."""
        mock_config = MagicMock()
        mock_config.github_repo = "org/repo"
        mock_config.label.trigger_label = "weave"
        mock_config.dry_run = False

        mock_tracker = MagicMock()
        mock_tracker.health_check = AsyncMock(return_value=False)

        args = _ns(repo=None, dry_run=False, limit=1)
        with patch("cli.github.IntegrationConfig.from_env", return_value=mock_config), \
             patch("cli.github._make_tracker", return_value=mock_tracker):
            import asyncio
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    __import__("cli.github", fromlist=["cmd_issue_poll"]).cmd_issue_poll(args)
                )
            assert exc_info.value.code == 1

    def test_no_issues_found(self, capsys):
        """cmd_issue_poll prints message when no issues found."""
        mock_config = MagicMock()
        mock_config.github_repo = "org/repo"
        mock_config.label.trigger_label = "weave"
        mock_config.dry_run = False

        mock_tracker = MagicMock()
        mock_tracker.health_check = AsyncMock(return_value=True)
        mock_tracker.fetch = AsyncMock(return_value=[])

        args = _ns(repo=None, dry_run=False, limit=1)
        with patch("cli.github.IntegrationConfig.from_env", return_value=mock_config), \
             patch("cli.github._make_tracker", return_value=mock_tracker):
            import asyncio
            asyncio.run(
                __import__("cli.github", fromlist=["cmd_issue_poll"]).cmd_issue_poll(args)
            )

        assert "No issues found" in capsys.readouterr().out

    def test_dry_run_lists_issues(self, capsys):
        """cmd_issue_poll in dry-run mode lists issues without executing."""
        mock_config = MagicMock()
        mock_config.github_repo = "org/repo"
        mock_config.label.trigger_label = "weave"
        mock_config.dry_run = True

        mock_issue = MagicMock()
        mock_issue.number = 42
        mock_issue.title = "Fix login bug"

        raw_issue = MagicMock()
        mock_tracker = MagicMock()
        mock_tracker.health_check = AsyncMock(return_value=True)
        mock_tracker.fetch = AsyncMock(return_value=[raw_issue])
        mock_tracker.normalize = MagicMock(return_value=mock_issue)

        mock_ranker = MagicMock()
        mock_ranker.rank = AsyncMock(return_value=[mock_issue])

        args = _ns(repo=None, dry_run=True, limit=1)
        with patch("cli.github.IntegrationConfig.from_env", return_value=mock_config), \
             patch("cli.github._make_tracker", return_value=mock_tracker), \
             patch("cli.github._make_ranker", return_value=mock_ranker):
            import asyncio
            asyncio.run(
                __import__("cli.github", fromlist=["cmd_issue_poll"]).cmd_issue_poll(args)
            )

        output = capsys.readouterr().out
        assert "Found 1 issue(s)" in output
        assert "#42" in output
        assert "Fix login bug" in output


class TestCmdIssueRun:
    """Tests for cmd_issue_run."""

    def test_no_repo_exits(self):
        """cmd_issue_run exits when no repo configured."""
        mock_config = MagicMock()
        mock_config.github_repo = ""

        args = _ns(repo=None, number=42)
        with patch("cli.github.IntegrationConfig.from_env", return_value=mock_config):
            import asyncio
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    __import__("cli.github", fromlist=["cmd_issue_run"]).cmd_issue_run(args)
                )
            assert exc_info.value.code == 1

    def test_health_check_failure_exits(self):
        """cmd_issue_run exits when gh not authenticated."""
        mock_config = MagicMock()
        mock_config.github_repo = "org/repo"

        mock_tracker = MagicMock()
        mock_tracker.health_check = AsyncMock(return_value=False)

        args = _ns(repo=None, number=42)
        with patch("cli.github.IntegrationConfig.from_env", return_value=mock_config), \
             patch("cli.github._make_tracker", return_value=mock_tracker):
            import asyncio
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    __import__("cli.github", fromlist=["cmd_issue_run"]).cmd_issue_run(args)
                )
            assert exc_info.value.code == 1


class TestCmdIssueStatus:
    """Tests for cmd_issue_status."""

    def test_no_repo_exits(self):
        """cmd_issue_status exits when no repo configured."""
        mock_config = MagicMock()
        mock_config.github_repo = ""
        mock_config.label = MagicMock()

        args = _ns(repo=None)
        with patch("cli.github.IntegrationConfig.from_env", return_value=mock_config):
            import asyncio
            with pytest.raises(SystemExit) as exc_info:
                asyncio.run(
                    __import__("cli.github", fromlist=["cmd_issue_status"]).cmd_issue_status(args)
                )
            assert exc_info.value.code == 1

    def test_no_issues_found(self, capsys):
        """cmd_issue_status prints message when no issues found."""
        mock_config = MagicMock()
        mock_config.github_repo = "org/repo"
        mock_config.label = MagicMock()

        mock_tracker = MagicMock()
        mock_tracker.fetch = AsyncMock(return_value=[])

        args = _ns(repo=None)
        with patch("cli.github.IntegrationConfig.from_env", return_value=mock_config), \
             patch("cli.github._make_tracker", return_value=mock_tracker):
            import asyncio
            asyncio.run(
                __import__("cli.github", fromlist=["cmd_issue_status"]).cmd_issue_status(args)
            )

        assert "No Weave-managed issues found" in capsys.readouterr().out

    def test_lists_issues_with_status(self, capsys):
        """cmd_issue_status lists issues with correct status labels."""
        mock_config = MagicMock()
        mock_config.github_repo = "org/repo"
        mock_config.label.running_label = "weave-running"
        mock_config.label.pr_label = "weave-pr"
        mock_config.label.failed_label = "weave-failed"
        mock_config.label.trigger_label = "weave"

        issue1 = MagicMock()
        issue1.number = 1
        issue1.title = "Running issue"
        issue1.labels = ["weave-running", "weave"]

        issue2 = MagicMock()
        issue2.number = 2
        issue2.title = "PR issue"
        issue2.labels = ["weave-pr", "weave"]

        issue3 = MagicMock()
        issue3.number = 3
        issue3.title = "Failed issue"
        issue3.labels = ["weave-failed", "weave"]

        issue4 = MagicMock()
        issue4.number = 4
        issue4.title = "Queued issue"
        issue4.labels = ["weave"]

        raw_issues = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        mock_tracker = MagicMock()
        mock_tracker.fetch = AsyncMock(return_value=raw_issues)
        mock_tracker.normalize = MagicMock(side_effect=[issue1, issue2, issue3, issue4])

        args = _ns(repo=None)
        with patch("cli.github.IntegrationConfig.from_env", return_value=mock_config), \
             patch("cli.github._make_tracker", return_value=mock_tracker):
            import asyncio
            asyncio.run(
                __import__("cli.github", fromlist=["cmd_issue_status"]).cmd_issue_status(args)
            )

        output = capsys.readouterr().out
        assert "running" in output
        assert "PR created" in output
        assert "failed" in output
        assert "queued" in output
