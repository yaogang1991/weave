"""
Tests for #327: PlanValidator stdlib shadowing false positives.

The validator should only warn when the task explicitly creates/names a
module that shadows stdlib, not when the task merely uses or imports from
stdlib modules.
"""
import pytest

from orchestrator.plan_validator import PlanValidator


def _plan_with_task(task: str) -> dict:
    return {"nodes": [{"id": "impl", "task": task}], "edges": []}


class TestStdlibShadowingFalsePositives:
    def test_using_json_no_warning(self):
        """Task that uses json module should NOT trigger warning."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Use json.dumps to serialize data"
        ))
        assert not v.warnings

    def test_using_collections_no_warning(self):
        """Task that uses collections module should NOT trigger warning."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Use collections.OrderedDict for ordering"
        ))
        assert not v.warnings

    def test_using_http_no_warning(self):
        """Task that uses http module should NOT trigger warning."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Use http.client for HTTP requests"
        ))
        assert not v.warnings

    def test_import_stdlib_no_warning(self):
        """Task that imports from stdlib should NOT trigger warning."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Import from asyncio and logging modules"
        ))
        assert not v.warnings

    def test_use_json_library_no_warning(self):
        """'use the json library' is usage, not creation — no warning."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Use the json library for serialization"
        ))
        assert not v.warnings

    def test_create_module_named_stdlib_warns(self):
        """'create a module named json' SHOULD trigger warning."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Create a module named 'json' for data handling"
        ))
        assert len(v.warnings) == 1
        assert "json" in v.warnings[0]

    def test_create_package_named_collections_warns(self):
        """'create a package named collections' SHOULD trigger warning."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Create a package named collections"
        ))
        assert len(v.warnings) == 1
        assert "collections" in v.warnings[0]

    def test_file_named_stdlib_py_warns(self):
        """'json.py file' SHOULD trigger warning."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Create the json.py file with serialization logic"
        ))
        assert len(v.warnings) == 1
        assert "json" in v.warnings[0]

    def test_build_library_named_stdlib_warns(self):
        """'build a library named urllib' SHOULD trigger warning."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Build a library named 'urllib' for HTTP"
        ))
        assert len(v.warnings) == 1
        assert "urllib" in v.warnings[0]

    def test_mixed_usage_and_creation(self):
        """Using stdlib AND creating a different module — no false positive."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Use json and http modules, create a module named 'myclient'"
        ))
        assert not v.warnings

    def test_non_stdlib_name_no_warning(self):
        """Creating a module with a non-stdlib name — no warning."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Create a module named 'myauth' for authentication"
        ))
        assert not v.warnings

    def test_quoted_stdlib_in_import_context_no_warning(self):
        """Quoted stdlib name in import context should NOT warn."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Import 'asyncio' and use it for async operations"
        ))
        assert not v.warnings

    def test_create_stdlib_module_unquoted_warns(self):
        """'create an asyncio module' SHOULD trigger warning."""
        v = PlanValidator()
        v.validate(_plan_with_task(
            "Create an asyncio module for scheduling"
        ))
        assert len(v.warnings) == 1
        assert "asyncio" in v.warnings[0]
