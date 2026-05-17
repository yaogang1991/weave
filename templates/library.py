"""
TemplateRegistry — Load, validate, and instantiate DAG templates.

Templates are YAML files describing reusable DAG structures with
variable substitution support ({var_name} placeholders).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from core.models import DAG, DAGEdge, DAGNode, DAGTemplate, DependencyType, SuccessCriterion

logger = logging.getLogger(__name__)

BUILTIN_TEMPLATES_DIR = Path(__file__).parent

_VAR_PATTERN = re.compile(r"\{(\w+)\}")
_SAFE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


class TemplateRegistry:
    """Discover, load, and instantiate DAG templates."""

    def __init__(self, templates_dir: str | Path | None = None) -> None:
        self.templates_dir = Path(templates_dir) if templates_dir else BUILTIN_TEMPLATES_DIR
        self._cache: dict[str, DAGTemplate] = {}

    def list_templates(self) -> list[DAGTemplate]:
        """Discover and return all .yaml/.yml templates."""
        templates: list[DAGTemplate] = []
        if not self.templates_dir.exists():
            return templates
        for path in sorted(self.templates_dir.glob("*.yaml")):
            try:
                tpl = self._load_yaml(path)
                templates.append(tpl)
            except Exception as e:
                logger.warning("Failed to load template %s: %s", path.name, e)
        for path in sorted(self.templates_dir.glob("*.yml")):
            try:
                tpl = self._load_yaml(path)
                templates.append(tpl)
            except Exception as e:
                logger.warning("Failed to load template %s: %s", path.name, e)
        return templates

    def get_template(self, name: str) -> DAGTemplate | None:
        """Load a template by name (filename stem or declared YAML name)."""
        # Prevent path traversal
        if not _SAFE_NAME_PATTERN.match(name):
            logger.warning("Invalid template name rejected: %s", name)
            return None
        if name in self._cache:
            return self._cache[name]
        # Try by filename stem first
        for ext in (".yaml", ".yml"):
            path = self.templates_dir / f"{name}{ext}"
            if path.exists():
                try:
                    tpl = self._load_yaml(path)
                    self._cache[name] = tpl
                    return tpl
                except Exception as e:
                    logger.warning("Failed to load template %s: %s", name, e)
        # Fallback: scan all templates and match by declared name
        matches = [t for t in self.list_templates() if t.name == name]
        if len(matches) > 1:
            raise ValueError(
                f"Duplicate template name '{name}' found in {len(matches)} files"
            )
        if matches:
            self._cache[name] = matches[0]
            return matches[0]
        return None

    def instantiate(
        self,
        template_name: str,
        variables: dict[str, str] | None = None,
    ) -> DAG:
        """Load a template, substitute variables, and return a DAG."""
        tpl = self.get_template(template_name)
        if tpl is None:
            raise ValueError(f"Template not found: {template_name}")

        merged_vars: dict[str, str] = dict(tpl.variables)
        if variables:
            merged_vars.update(variables)

        nodes_dict: dict[str, DAGNode] = {}
        for node_def in tpl.nodes:
            node_dict = self._substitute_deep(node_def, merged_vars)
            node = DAGNode(**node_dict)
            if node.id in nodes_dict:
                raise ValueError(
                    f"Duplicate node ID '{node.id}' in template '{template_name}'"
                )
            nodes_dict[node.id] = node

        edges: list[DAGEdge] = []
        for edge_def in tpl.edges:
            from_node = self._substitute(str(edge_def.get("from", "")), merged_vars)
            to_node = self._substitute(str(edge_def.get("to", "")), merged_vars)
            if not from_node or not to_node:
                raise ValueError(
                    f"Edge in template '{template_name}' has empty "
                    f"from/to node: from='{from_node}', to='{to_node}'"
                )
            if from_node not in nodes_dict:
                raise ValueError(
                    f"Edge references unknown node '{from_node}' in template '{template_name}'"
                )
            if to_node not in nodes_dict:
                raise ValueError(
                    f"Edge references unknown node '{to_node}' in template '{template_name}'"
                )
            dep_type_str = edge_def.get("dependency_type", "hard")
            dep_type = DependencyType.SOFT if dep_type_str == "soft" else DependencyType.HARD
            edges.append(DAGEdge(
                from_node=from_node, to_node=to_node,
                dependency_type=dep_type,
            ))

        dag = DAG(
            nodes=nodes_dict,
            edges=edges,
            reasoning=self._substitute(tpl.reasoning_template, merged_vars),
        )

        # Warn on unresolved placeholders
        warnings = self.validate_substitution(template_name, dag, merged_vars)
        if warnings:
            logger.warning(
                "Template '%s' has %d unresolved placeholder(s): %s",
                template_name, len(warnings), "; ".join(warnings),
            )

        return dag

    def validate_substitution(
        self,
        template_name: str,
        dag: DAG,
        variables: dict[str, str] | None = None,
    ) -> list[str]:
        """Check for unresolved {var} placeholders in the DAG. Returns warnings."""
        warnings_list: list[str] = []
        text_fields: list[str] = []

        for node in dag.nodes.values():
            text_fields.append(node.task_description)
            if node.agent_type:
                text_fields.append(node.agent_type)
            # Scan success_criteria and other list fields
            for sc in (node.success_criteria or []):
                if isinstance(sc, SuccessCriterion):
                    if sc.description:
                        text_fields.append(sc.description)
                    if sc.test_path:
                        text_fields.append(sc.test_path)
                    if sc.path:
                        text_fields.append(sc.path)
                elif isinstance(sc, str):
                    text_fields.append(sc)
        text_fields.append(dag.reasoning or "")

        for text in text_fields:
            for match in _VAR_PATTERN.finditer(text):
                var = match.group(1)
                if variables is None or var not in variables:
                    msg = f"Unresolved variable '{{{var}}}' in template '{template_name}'"
                    logger.warning(msg)
                    warnings_list.append(msg)

        return warnings_list

    def _substitute_deep(self, obj: dict | list, variables: dict[str, str]) -> dict | list:
        """Recursively substitute {var} placeholders in dicts and lists."""
        if isinstance(obj, dict):
            return {k: self._substitute_deep(v, variables) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._substitute_deep(item, variables) for item in obj]
        if isinstance(obj, str):
            return self._substitute(obj, variables)
        return obj

    def _substitute(self, text: str, variables: dict[str, str]) -> str:
        """Replace {var} placeholders in text."""
        def _replacer(match: re.Match) -> str:
            key = match.group(1)
            return variables.get(key, match.group(0))
        return _VAR_PATTERN.sub(_replacer, text)

    def _load_yaml(self, path: Path) -> DAGTemplate:
        """Parse and validate a YAML template file."""
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Template {path.name} must be a YAML mapping")
        if "name" not in data or "nodes" not in data:
            raise ValueError(
                f"Template {path.name} must have 'name' and 'nodes' fields"
            )
        return DAGTemplate(**data)
