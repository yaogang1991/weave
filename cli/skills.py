"""CLI skills and template commands — skills, skill, templates (M3.4, M3.6)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cli.utils import _write_error, _parse_template_vars


async def cmd_skills(args):
    """List available skills."""
    from skills.registry import SkillRegistry
    project_path = getattr(args, "project", None) or "."
    registry = SkillRegistry(skills_dir=Path(project_path) / ".weave" / "skills")
    skills = registry.list_skills()

    agent_filter = getattr(args, "agent", None)
    if agent_filter:
        skills = [s for s in skills if not s.agent_types or agent_filter in s.agent_types]

    result = {
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "agent_types": s.agent_types or ["all"],
                "variables": {k: v.default for k, v in s.variables.items()},
                "tool_allowlist": s.tool_allowlist,
            }
            for s in skills
        ],
        "count": len(skills),
    }
    print(json.dumps(result, indent=2))


async def cmd_skill(args):
    """Invoke a skill (creates a plan+run with skill context)."""
    from skills.registry import SkillRegistry
    project_path = getattr(args, "project", None) or "."
    registry = SkillRegistry(skills_dir=Path(project_path) / ".weave" / "skills")

    variables = _parse_template_vars(getattr(args, "var", []) or [])
    prompt = registry.instantiate(args.name, variables)

    run_args = argparse.Namespace(
        requirement=prompt,
        project=project_path,
        file=None,
        template=None,
        var=[],
        viz=False,
        visualize=False,
        no_browser=True,
        allow_self_modify=getattr(args, "allow_self_modify", False),
        max_parallel=getattr(args, "max_parallel", 3),
        max_iterations=getattr(args, "max_iterations", 50),
        pass_threshold=None,
        non_interactive=False,
        timeout=None,
        cleanup_stdlib_shadowing=False,
    )
    from cli.execution import cmd_run
    return await cmd_run(run_args)


async def cmd_templates(args):
    """List available DAG templates."""
    from templates.library import TemplateRegistry
    registry = TemplateRegistry()
    templates = registry.list_templates()

    if args.name:
        tpl = registry.get_template(args.name)
        if tpl is None:
            _write_error("E_TEMPLATE_NOT_FOUND", f"Template not found: {args.name}")
            return
        result = {
            "name": tpl.name,
            "description": tpl.description,
            "version": tpl.version,
            "category": tpl.category,
            "variables": tpl.variables,
            "nodes": tpl.nodes,
            "edges": tpl.edges,
            "reasoning_template": tpl.reasoning_template,
        }
    else:
        result = {
            "templates": [
                {
                    "name": t.name,
                    "description": t.description,
                    "version": t.version,
                    "category": t.category,
                    "nodes": len(t.nodes),
                    "edges": len(t.edges),
                    "variables": list(t.variables.keys()),
                }
                for t in templates
            ],
            "count": len(templates),
        }
    print(json.dumps(result, indent=2, default=str))
