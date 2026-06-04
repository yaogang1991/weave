"""Tests for CLI modules: approval, impact, learning, memory, skills, github."""
from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


class TestCmdTickets:
    @pytest.mark.asyncio
    @patch("cli.approval.ApprovalRepository")
    async def test_list_tickets_happy_path(self, MockRepo):
        from cli.approval import cmd_tickets
        mock_ticket = MagicMock()
        mock_ticket.id = "t1"
        mock_ticket.job_id = "j1"
        mock_ticket.tool_name = "bash"
        mock_ticket.status.value = "pending"
        mock_ticket.risk_level = "high"
        mock_ticket.args_preview = "rm -rf"
        mock_ticket.requested_at = MagicMock()
        mock_ticket.requested_at.isoformat.return_value = "2024-01-01T00:00:00"
        mock_ticket.expires_at = MagicMock()
        mock_ticket.expires_at.isoformat.return_value = "2024-01-01T01:00:00"

        repo = MockRepo.return_value
        repo.list_tickets.return_value = [mock_ticket]
        repo.get_stats.return_value = {"total": 1}

        args = argparse.Namespace(status=None, job_id=None)
        await cmd_tickets(args)

    @pytest.mark.asyncio
    @patch("cli.approval.ApprovalRepository")
    async def test_list_tickets_with_status_filter(self, MockRepo):
        from cli.approval import cmd_tickets
        repo = MockRepo.return_value
        repo.list_tickets.return_value = []
        repo.get_stats.return_value = {"total": 0}

        args = argparse.Namespace(status="pending", job_id=None)
        await cmd_tickets(args)
        repo.list_tickets.assert_called_once()


class TestCmdApprove:
    @pytest.mark.asyncio
    @patch("cli.approval._make_run_service")
    @patch("cli.approval._make_repository")
    @patch("cli.approval.ApprovalRepository")
    async def test_approve_happy_path(self, MockRepo, MockJobRepo, MockService):
        from cli.approval import cmd_approve
        repo = MockRepo.return_value
        ticket = MagicMock()
        ticket.id = "t1"
        ticket.job_id = "j1"
        ticket.status.value = "pending"
        repo.get_ticket.return_value = ticket
        repo.approve_ticket.return_value = ticket

        service = AsyncMock()
        MockService.return_value = service

        args = argparse.Namespace(ticket_id="t1", reason="ok")
        await cmd_approve(args)
        repo.approve_ticket.assert_called_once_with("t1", reason="ok")


class TestCmdImpactPredict:
    @pytest.mark.asyncio
    async def test_predict_happy_path(self):
        from cli.impact import cmd_impact_predict
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"predicted_files": []}
        with patch("core.config.WeaveConfig") as MockConfig, \
             patch("analysis.impact_predictor.ImpactPredictor") as MockPredictor:
            mock_config = MockConfig.from_env.return_value
            mock_config.impact.max_predicted_files = 50
            mock_config.impact.confidence_threshold = 0.5
            MockPredictor.return_value.predict_static.return_value = mock_result

            args = argparse.Namespace(requirement="fix bug", project=".")
            await cmd_impact_predict(args)


class TestCmdImpactGraph:
    @pytest.mark.asyncio
    async def test_graph_happy_path(self):
        from cli.impact import cmd_impact_graph
        with patch("analysis.dependency_graph.DependencyGraph") as MockGraph:
            mock_graph = MockGraph.return_value
            mock_graph.to_dict.return_value = {"nodes": [], "edges": []}

            args = argparse.Namespace(project=".")
            await cmd_impact_graph(args)
            mock_graph.build.assert_called_once()


class TestCmdImpactHistory:
    @pytest.mark.asyncio
    @patch("cli.impact.WeaveConfig")
    async def test_history_no_data(self, MockConfig, tmp_path):
        from cli.impact import cmd_impact_history
        mock_config = MockConfig.from_env.return_value
        mock_config.impact.base_path = str(tmp_path / "nonexistent")

        args = argparse.Namespace()
        await cmd_impact_history(args)


class TestCmdLearningStatus:
    @pytest.mark.asyncio
    @patch("cli.learning._make_learning_scheduler")
    async def test_status_happy_path(self, MockScheduler):
        from cli.learning import cmd_learning_status
        scheduler = MockScheduler.return_value
        scheduler.get_status.return_value = {"enabled": True}

        args = argparse.Namespace()
        await cmd_learning_status(args)


class TestCmdMemoryStats:
    @pytest.mark.asyncio
    @patch("cli.memory._make_memory_manager")
    async def test_stats_happy_path(self, MockMM):
        from cli.memory import cmd_memory_stats
        mm = MockMM.return_value
        mm.get_stats.return_value = {"total_entries": 0}

        args = argparse.Namespace()
        await cmd_memory_stats(args)


class TestCmdMemorySearch:
    @pytest.mark.asyncio
    @patch("cli.memory._make_memory_manager")
    async def test_search_happy_path(self, MockMM):
        from cli.memory import cmd_memory_search
        mm = MockMM.return_value
        mm.store.search.return_value = []

        args = argparse.Namespace(query="test", scope=None, type=None, agent=None, limit=10)
        await cmd_memory_search(args)
        mm.store.search.assert_called_once()


class TestCmdMemoryCleanup:
    @pytest.mark.asyncio
    @patch("cli.memory._make_memory_manager")
    async def test_cleanup_happy_path(self, MockMM):
        from cli.memory import cmd_memory_cleanup
        mm = MockMM.return_value
        mm.run_maintenance.return_value = {"cleaned": 0}

        args = argparse.Namespace()
        await cmd_memory_cleanup(args)


class TestCmdSkills:
    @pytest.mark.asyncio
    async def test_list_skills_empty(self):
        from cli.skills import cmd_skills
        with patch("skills.registry.SkillRegistry") as MockRegistry:
            registry = MockRegistry.return_value
            registry.list_skills.return_value = []

            args = argparse.Namespace(project=".", agent=None)
            await cmd_skills(args)
            registry.list_skills.assert_called_once()


class TestCmdTemplates:
    @pytest.mark.asyncio
    async def test_list_templates(self):
        from cli.skills import cmd_templates
        with patch("templates.library.TemplateRegistry") as MockRegistry:
            registry = MockRegistry.return_value
            registry.list_templates.return_value = []

            args = argparse.Namespace(name=None)
            await cmd_templates(args)
            registry.list_templates.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_template_by_name(self):
        from cli.skills import cmd_templates
        with patch("templates.library.TemplateRegistry") as MockRegistry:
            mock_tpl = MagicMock()
            mock_tpl.name = "build_api"
            mock_tpl.description = "Build an API"
            mock_tpl.version = "1.0"
            mock_tpl.category = "build"
            mock_tpl.variables = {}
            mock_tpl.nodes = []
            mock_tpl.edges = []
            mock_tpl.reasoning_template = ""
            registry = MockRegistry.return_value
            registry.get_template.return_value = mock_tpl
            registry.list_templates.return_value = []

            args = argparse.Namespace(name="build_api")
            await cmd_templates(args)
            registry.get_template.assert_called_once_with("build_api")

    @pytest.mark.asyncio
    async def test_template_not_found(self):
        from cli.skills import cmd_templates
        with patch("templates.library.TemplateRegistry") as MockRegistry:
            registry = MockRegistry.return_value
            registry.get_template.return_value = None
            registry.list_templates.return_value = []

            args = argparse.Namespace(name="nonexistent")
            with pytest.raises(SystemExit):
                await cmd_templates(args)


class TestCmdIssuePoll:
    @pytest.mark.asyncio
    @patch("cli.github.IntegrationConfig")
    async def test_poll_no_repo(self, MockConfig):
        from cli.github import cmd_issue_poll
        mock_config = MockConfig.from_env.return_value
        mock_config.github_repo = None
        mock_config.dry_run = False

        args = argparse.Namespace(repo=None, dry_run=False, limit=1)
        with pytest.raises(SystemExit):
            await cmd_issue_poll(args)


class TestCmdIssueRun:
    @pytest.mark.asyncio
    @patch("cli.github.IntegrationConfig")
    async def test_run_no_repo(self, MockConfig):
        from cli.github import cmd_issue_run
        mock_config = MockConfig.from_env.return_value
        mock_config.github_repo = None

        args = argparse.Namespace(repo=None, number=42)
        with pytest.raises(SystemExit):
            await cmd_issue_run(args)


class TestCmdIssueStatus:
    @pytest.mark.asyncio
    @patch("cli.github.IntegrationConfig")
    async def test_status_no_repo(self, MockConfig):
        from cli.github import cmd_issue_status
        mock_config = MockConfig.from_env.return_value
        mock_config.github_repo = None

        args = argparse.Namespace(repo=None)
        with pytest.raises(SystemExit):
            await cmd_issue_status(args)
