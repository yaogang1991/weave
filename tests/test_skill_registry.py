"""Tests for the Skills registry (M3.6)."""

import pytest
import yaml
from pathlib import Path

from core.models import Skill, SkillVariable
from skills.registry import SkillRegistry


class TestSkillVariable:
    def test_defaults(self):
        var = SkillVariable()
        assert var.default == ""
        assert var.description == ""
        assert var.required is False

    def test_custom_values(self):
        var = SkillVariable(default="test", description="desc", required=True)
        assert var.default == "test"
        assert var.required is True


class TestSkill:
    def test_minimal_skill(self):
        skill = Skill(name="test", description="desc", prompt="hello")
        assert skill.name == "test"
        assert skill.agent_types == []
        assert skill.version == "1.0"

    def test_skill_with_variables(self):
        skill = Skill(
            name="review",
            description="Code review",
            prompt="Review {target_files}",
            variables={"target_files": SkillVariable(required=True)},
        )
        assert "target_files" in skill.variables
        assert skill.variables["target_files"].required is True


class TestSkillRegistry:
    def test_list_skills_empty_dir(self, tmp_path):
        registry = SkillRegistry(skills_dir=tmp_path / "skills")
        assert registry.list_skills() == []

    def test_list_skills_no_dir(self):
        registry = SkillRegistry(skills_dir="/nonexistent/path")
        assert registry.list_skills() == []

    def test_load_single_skill(self, tmp_path):
        skills_dir = tmp_path / ".harness" / "skills"
        skills_dir.mkdir(parents=True)
        skill_file = skills_dir / "review_code.yaml"
        skill_file.write_text(yaml.dump({
            "name": "review_code",
            "description": "Review code for quality",
            "prompt": "Review these files: {target_files}",
            "variables": {
                "target_files": {"default": "", "required": True},
                "focus": {"default": "all", "description": "Focus area"},
            },
            "agent_types": ["evaluator"],
        }))

        registry = SkillRegistry(skills_dir=skills_dir)
        skills = registry.list_skills()
        assert len(skills) == 1
        assert skills[0].name == "review_code"
        assert skills[0].agent_types == ["evaluator"]
        assert "target_files" in skills[0].variables
        assert skills[0].variables["focus"].default == "all"

    def test_get_skill_by_name(self, tmp_path):
        skills_dir = tmp_path / ".harness" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "my_skill.yml").write_text(yaml.dump({
            "name": "my_skill",
            "description": "test",
            "prompt": "do stuff",
        }))

        registry = SkillRegistry(skills_dir=skills_dir)
        skill = registry.get_skill("my_skill")
        assert skill is not None
        assert skill.name == "my_skill"

    def test_get_skill_not_found(self, tmp_path):
        registry = SkillRegistry(skills_dir=tmp_path)
        assert registry.get_skill("nonexistent") is None

    def test_instantiate_substitutes_variables(self, tmp_path):
        skills_dir = tmp_path / ".harness" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "greet.yaml").write_text(yaml.dump({
            "name": "greet",
            "description": "Greeting",
            "prompt": "Hello {name}, welcome to {place}!",
            "variables": {
                "name": {"default": "World"},
                "place": {"default": "harness"},
            },
        }))

        registry = SkillRegistry(skills_dir=skills_dir)
        result = registry.instantiate("greet", {"name": "Alice"})
        assert "Hello Alice" in result
        assert "welcome to harness!" in result

    def test_instantiate_uses_defaults(self, tmp_path):
        skills_dir = tmp_path / ".harness" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "greet.yaml").write_text(yaml.dump({
            "name": "greet",
            "description": "Greeting",
            "prompt": "Hello {name}!",
            "variables": {"name": {"default": "World"}},
        }))

        registry = SkillRegistry(skills_dir=skills_dir)
        result = registry.instantiate("greet")
        assert "Hello World!" in result

    def test_instantiate_required_variable_missing(self, tmp_path):
        skills_dir = tmp_path / ".harness" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "strict.yaml").write_text(yaml.dump({
            "name": "strict",
            "description": "Requires input",
            "prompt": "Process {data}",
            "variables": {"data": {"required": True}},
        }))

        registry = SkillRegistry(skills_dir=skills_dir)
        with pytest.raises(ValueError, match="requires variable"):
            registry.instantiate("strict")

    def test_instantiate_not_found(self, tmp_path):
        registry = SkillRegistry(skills_dir=tmp_path)
        with pytest.raises(ValueError, match="Skill not found"):
            registry.instantiate("missing")

    def test_skills_for_agent_filter(self, tmp_path):
        skills_dir = tmp_path / ".harness" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "eval_skill.yaml").write_text(yaml.dump({
            "name": "eval_skill",
            "description": "for evaluators",
            "prompt": "evaluate",
            "agent_types": ["evaluator"],
        }))
        (skills_dir / "all_skill.yaml").write_text(yaml.dump({
            "name": "all_skill",
            "description": "for everyone",
            "prompt": "anything",
        }))

        registry = SkillRegistry(skills_dir=skills_dir)
        planner_skills = registry.skills_for_agent("planner")
        assert len(planner_skills) == 1
        assert planner_skills[0].name == "all_skill"

        evaluator_skills = registry.skills_for_agent("evaluator")
        assert len(evaluator_skills) == 2

    def test_to_prompt_description(self, tmp_path):
        skills_dir = tmp_path / ".harness" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "skill.yaml").write_text(yaml.dump({
            "name": "review",
            "description": "Review code",
            "prompt": "review {files}",
            "variables": {"files": {"required": True}},
            "agent_types": ["evaluator"],
        }))

        registry = SkillRegistry(skills_dir=skills_dir)
        desc = registry.to_prompt_description()
        assert "review" in desc
        assert "files" in desc
        assert "evaluator" in desc

    def test_to_prompt_description_empty(self, tmp_path):
        registry = SkillRegistry(skills_dir=tmp_path)
        assert registry.to_prompt_description() == ""

    def test_context_files_injection(self, tmp_path):
        ctx_file = tmp_path / "style_guide.md"
        ctx_file.write_text("# Style Guide\nUse 4 spaces.")

        skills_dir = tmp_path / ".harness" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "lint.yaml").write_text(yaml.dump({
            "name": "lint",
            "description": "Lint with style",
            "prompt": "Lint the code",
            "context_files": [str(ctx_file)],
        }))

        registry = SkillRegistry(skills_dir=skills_dir)
        result = registry.instantiate("lint")
        assert "Style Guide" in result
        assert "4 spaces" in result

    def test_multiple_skills_loading(self, tmp_path):
        skills_dir = tmp_path / ".harness" / "skills"
        skills_dir.mkdir(parents=True)
        for i in range(3):
            (skills_dir / f"skill_{i}.yaml").write_text(yaml.dump({
                "name": f"skill_{i}",
                "description": f"Skill {i}",
                "prompt": f"Do task {i}",
            }))

        registry = SkillRegistry(skills_dir=skills_dir)
        skills = registry.list_skills()
        assert len(skills) == 3
        names = {s.name for s in skills}
        assert names == {"skill_0", "skill_1", "skill_2"}

    def test_invalid_yaml_skipped(self, tmp_path):
        skills_dir = tmp_path / ".harness" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "good.yaml").write_text(yaml.dump({
            "name": "good",
            "description": "valid",
            "prompt": "ok",
        }))
        (skills_dir / "bad.yaml").write_text("not: valid: yaml: [")

        registry = SkillRegistry(skills_dir=skills_dir)
        skills = registry.list_skills()
        assert len(skills) == 1
        assert skills[0].name == "good"
