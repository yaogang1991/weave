"""
SkillRegistry: Load, validate, and instantiate YAML-based skill definitions.

Skills are reusable prompt templates with variable substitution,
analogous to DAG templates but for single-agent invocations.

Follows the same pattern as templates/library.py.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from core.models import Skill, SkillVariable

logger = logging.getLogger(__name__)

_VAR_PATTERN = re.compile(r"\{(\w+)\}")


class SkillRegistry:
    """Discover, load, and instantiate skill definitions."""

    def __init__(self, skills_dir: str | Path | None = None) -> None:
        self.skills_dir = Path(skills_dir) if skills_dir else Path(".harness/skills")
        self._cache: dict[str, Skill] = {}

    def list_skills(self) -> list[Skill]:
        """Discover and return all .yaml/.yml skill definitions."""
        if not self.skills_dir.is_dir():
            return []

        skills: list[Skill] = []
        for path in sorted(self.skills_dir.glob("*.y*ml")):
            try:
                skill = self._load_yaml(path)
                skills.append(skill)
            except Exception as e:
                logger.warning("Failed to load skill %s: %s", path, e)
        return skills

    def get_skill(self, name: str) -> Skill | None:
        """Load a skill by name (filename stem or declared YAML name)."""
        if name in self._cache:
            return self._cache[name]

        for skill in self.list_skills():
            if skill.name == name:
                self._cache[name] = skill
                return skill
        return None

    def instantiate(
        self,
        skill_name: str,
        variables: dict[str, str] | None = None,
    ) -> str:
        """Load a skill, substitute variables, return the rendered prompt."""
        skill = self.get_skill(skill_name)
        if skill is None:
            raise ValueError(f"Skill not found: {skill_name}")

        # Merge defaults with provided variables
        merged: dict[str, str] = {}
        for var_name, var_def in skill.variables.items():
            merged[var_name] = var_def.default
        if variables:
            merged.update(variables)

        # Validate required variables
        for var_name, var_def in skill.variables.items():
            if var_def.required and not merged.get(var_name):
                raise ValueError(
                    f"Skill '{skill_name}' requires variable '{var_name}'"
                )

        prompt = self._substitute(skill.prompt, merged)

        # Append context file contents
        if skill.context_files:
            context = self._load_context_files(skill.context_files)
            if context:
                prompt = f"{prompt}\n\n## Context Files\n{context}"

        return prompt

    def skills_for_agent(self, agent_type: str) -> list[Skill]:
        """Return skills applicable to a given agent type."""
        return [
            s for s in self.list_skills()
            if not s.agent_types or agent_type in s.agent_types
        ]

    def to_prompt_description(self) -> str:
        """Format skill list for injection into LLM prompts."""
        skills = self.list_skills()
        if not skills:
            return ""
        lines = ["Available skills that can assist with tasks:"]
        for s in skills:
            agent_note = (
                f" (agents: {', '.join(s.agent_types)})"
                if s.agent_types
                else ""
            )
            var_note = ""
            if s.variables:
                var_names = ", ".join(s.variables.keys())
                var_note = f" Variables: {var_names}."
            lines.append(
                f"- {s.name}: {s.description}{agent_note}{var_note}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _substitute(self, text: str, variables: dict[str, str]) -> str:
        """Replace {var} placeholders with values."""
        def _replacer(match: re.Match) -> str:
            key = match.group(1)
            return variables.get(key, match.group(0))
        return _VAR_PATTERN.sub(_replacer, text)

    def _load_yaml(self, path: Path) -> Skill:
        """Parse and validate a YAML skill file."""
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"Skill file must be a YAML mapping: {path}")

        # Parse variables
        variables: dict[str, SkillVariable] = {}
        for key, val in data.get("variables", {}).items():
            if isinstance(val, dict):
                variables[key] = SkillVariable(**val)
            elif isinstance(val, str):
                variables[key] = SkillVariable(default=val)
            else:
                variables[key] = SkillVariable(default=str(val))

        return Skill(
            name=data.get("name", path.stem),
            description=data.get("description", ""),
            prompt=data.get("prompt", ""),
            variables=variables,
            agent_types=data.get("agent_types", []),
            tool_allowlist=data.get("tool_allowlist", []),
            context_files=data.get("context_files", []),
            version=data.get("version", "1.0"),
        )

    def _load_context_files(self, context_files: list[str]) -> str:
        """Read and concatenate context file contents for skill injection."""
        parts: list[str] = []
        for file_path in context_files:
            path = Path(file_path)
            if not path.is_absolute():
                path = self.skills_dir.parent.parent / path
            if path.exists() and path.is_file():
                try:
                    content = path.read_text(encoding="utf-8")
                    parts.append(f"### {path.name}\n{content}")
                except Exception as e:
                    logger.warning(
                        "Failed to read context file %s: %s", file_path, e
                    )
            else:
                logger.warning("Context file not found: %s", file_path)
        return "\n\n".join(parts)
