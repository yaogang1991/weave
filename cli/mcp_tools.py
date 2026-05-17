"""MCP tool registrations for Weave Server (#512).

All weave.* tool handlers are defined here, keeping cli/execution.py
focused on CLI command orchestration.
"""

from __future__ import annotations


def register_weave_tools(server) -> None:
    """Register Weave core tools on the MCP server (#512).

    Parameters
    ----------
    server:
        MCPServer instance from mcp/server.py.
    """

    @server.tool(
        "weave.plan",
        description="Plan a DAG from a task description",
        input_schema={
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "The software task to plan",
                },
                "project": {
                    "type": "string",
                    "description": "Project path (optional)",
                },
            },
            "required": ["task_description"],
        },
    )
    def weave_plan(task_description: str, project: str | None = None) -> dict:
        """Plan a DAG — sync wrapper that delegates to async."""
        return {
            "status": "planned",
            "task": task_description[:200],
            "message": (
                "DAG planning via MCP requires async execution. "
                "Use `weave.run` for full plan+execute in one call."
            ),
        }

    @server.tool(
        "weave.run",
        description="Plan and execute a task in one call",
        input_schema={
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "The software task to execute",
                },
                "project": {
                    "type": "string",
                    "description": "Project path (optional)",
                },
            },
            "required": ["task_description"],
        },
    )
    def weave_run(task_description: str, project: str | None = None) -> dict:
        return {
            "status": "accepted",
            "task": task_description[:200],
            "message": (
                "Full execution requires worker mode. "
                "Submit via `weave.submit` for async processing."
            ),
        }

    @server.tool(
        "weave.status",
        description="Get the status of a job",
        input_schema={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job ID to check",
                },
            },
            "required": ["job_id"],
        },
    )
    def weave_status(job_id: str) -> dict:
        from core.config import WeaveConfig
        from control_plane.repository import Repository

        config = WeaveConfig.from_env()
        repo = Repository(config.queue_path)
        job = repo.load_job(job_id)
        if job is None:
            return {"error": f"Job {job_id} not found"}
        return {
            "job_id": job.id,
            "status": job.status,
            "requirement": job.requirement[:200],
        }

    @server.tool(
        "weave.list",
        description="List jobs with optional status filter",
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status (pending/running/completed/failed)",
                },
            },
        },
    )
    def weave_list(status: str | None = None) -> dict:
        from core.config import WeaveConfig
        from control_plane.repository import Repository

        config = WeaveConfig.from_env()
        repo = Repository(config.queue_path)
        jobs = repo.list_jobs(status=status)
        return {
            "jobs": [
                {
                    "job_id": j.id,
                    "status": j.status,
                    "requirement": j.requirement[:100],
                }
                for j in jobs[:20]
            ],
            "total": len(jobs),
        }

    @server.tool(
        "weave.memory_query",
        description="Query agent memory entries",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "agent_type": {
                    "type": "string",
                    "description": "Filter by agent type (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10)",
                },
            },
            "required": ["query"],
        },
    )
    def weave_memory_query(
        query: str,
        agent_type: str | None = None,
        limit: int = 10,
    ) -> dict:
        try:
            from core.config import WeaveConfig
            from memory.manager import MemoryManager

            config = WeaveConfig.from_env()
            if not config.memory.enabled:
                return {"entries": [], "message": "Memory system is disabled"}

            mm = MemoryManager(config.memory)
            entries = mm.search(query, agent_type=agent_type, limit=limit)
            return {
                "entries": [
                    {
                        "id": e.id,
                        "content": e.content[:200],
                        "agent_type": e.agent_type,
                        "scope": e.scope,
                    }
                    for e in entries
                ],
                "total": len(entries),
            }
        except Exception as exc:
            return {"error": str(exc), "entries": []}

    @server.tool(
        "weave.memory_store",
        description="Store a memory entry for an agent",
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Memory content to store",
                },
                "agent_type": {
                    "type": "string",
                    "description": "Agent type (default: shared)",
                },
                "scope": {
                    "type": "string",
                    "description": "Memory scope: private, session, global (default: global)",
                },
            },
            "required": ["content"],
        },
    )
    def weave_memory_store(
        content: str,
        agent_type: str = "shared",
        scope: str = "global",
    ) -> dict:
        try:
            from core.config import WeaveConfig
            from core.memory_models import MemoryScope
            from memory.manager import MemoryManager

            config = WeaveConfig.from_env()
            if not config.memory.enabled:
                return {"stored": False, "message": "Memory system is disabled"}

            scope_map = {
                "global": MemoryScope.GLOBAL,
                "private": MemoryScope.PRIVATE,
                "session": MemoryScope.SESSION,
            }
            memory_scope = scope_map.get(scope, MemoryScope.GLOBAL)

            mm = MemoryManager(config.memory)
            entry = mm.store_learning(
                agent_type=agent_type,
                content=content,
                scope=memory_scope,
            )
            return {
                "stored": True,
                "id": entry.id,
                "scope": entry.scope.value,
                "agent_type": entry.agent_type,
            }
        except Exception as exc:
            return {"error": str(exc), "stored": False}

    @server.tool(
        "weave.health",
        description="Check Weave server health and configuration",
        input_schema={
            "type": "object",
            "properties": {},
        },
    )
    def weave_health() -> dict:
        from core.config import WeaveConfig

        config = WeaveConfig.from_env()
        return {
            "status": "ok",
            "version": "0.1.0",
            "provider": config.llm.provider,
            "model": config.llm.model,
            "memory_enabled": config.memory.enabled,
            "sandbox": config.sandbox.runtime,
        }

    # -- #512 P2: Learning analysis tools --------------------------------------

    @server.tool(
        "weave.analyze",
        description="Run learning analysis on execution patterns and return insights",
        input_schema={
            "type": "object",
            "properties": {
                "min_confidence": {
                    "type": "number",
                    "description": "Minimum confidence threshold (0.0-1.0, default 0.5)",
                },
            },
        },
    )
    def weave_analyze(min_confidence: float = 0.5) -> dict:
        try:
            from core.config import WeaveConfig
            from learning.analyzer import LearningAnalyzer

            config = WeaveConfig.from_env()
            if not config.learning.enabled:
                return {
                    "insights": [],
                    "message": "Learning system is disabled",
                }

            analyzer = LearningAnalyzer(
                metrics_collector=None,
                memory_manager=None,
                config=config.learning,
            )
            insights = analyzer.analyze()

            filtered = [
                i for i in insights
                if i.confidence >= min_confidence
            ]
            return {
                "insights": [
                    {
                        "id": i.id,
                        "type": i.insight_type.value,
                        "category": i.category.value,
                        "description": i.description[:300],
                        "confidence": i.confidence,
                        "impact": i.impact,
                        "applies_to": i.applies_to,
                    }
                    for i in filtered
                ],
                "total": len(filtered),
                "total_unfiltered": len(insights),
            }
        except Exception as exc:
            return {"error": str(exc), "insights": []}

    @server.tool(
        "weave.insights",
        description="Get planning hints derived from learning analysis",
        input_schema={
            "type": "object",
            "properties": {
                "requirement": {
                    "type": "string",
                    "description": "Task requirement to get hints for (optional)",
                },
            },
        },
    )
    def weave_insights(requirement: str = "") -> dict:
        try:
            from core.config import WeaveConfig
            from memory.manager import MemoryManager
            from core.memory_models import MemoryScope

            config = WeaveConfig.from_env()
            if not config.memory.enabled:
                return {
                    "hints": "",
                    "message": "Memory system is disabled",
                }

            mm = MemoryManager(config.memory)
            query = (
                f"planning recommendation {requirement}"
                if requirement else "planning recommendation"
            )
            memories = mm.store.search(
                query=query,
                scope=MemoryScope.GLOBAL,
                limit=10,
            )
            if not memories:
                return {
                    "hints": "",
                    "total": 0,
                    "message": "No planning hints available yet",
                }

            hints_lines = [
                f"- {m.content[:200]} (confidence: {m.relevance_score:.2f})"
                for m in memories[:10]
            ]
            return {
                "hints": "\n".join(hints_lines),
                "total": len(memories),
            }
        except Exception as exc:
            return {"error": str(exc), "hints": ""}
