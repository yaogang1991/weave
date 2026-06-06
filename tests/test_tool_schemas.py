"""Tests for tools/schemas.py — tool JSON schema definitions."""
from __future__ import annotations

import pytest

from tools.schemas import TOOL_SCHEMAS


EXPECTED_TOOLS = {"read", "write", "edit", "bash", "glob", "grep", "git"}


class TestSchemaCompleteness:
    def test_all_expected_tools_present(self):
        assert set(TOOL_SCHEMAS.keys()) == EXPECTED_TOOLS

    @pytest.mark.parametrize("name", EXPECTED_TOOLS)
    def test_schema_has_name(self, name):
        assert TOOL_SCHEMAS[name]["name"] == name

    @pytest.mark.parametrize("name", EXPECTED_TOOLS)
    def test_schema_has_description(self, name):
        assert isinstance(TOOL_SCHEMAS[name]["description"], str)
        assert len(TOOL_SCHEMAS[name]["description"]) > 0

    @pytest.mark.parametrize("name", EXPECTED_TOOLS)
    def test_schema_has_input_schema(self, name):
        schema = TOOL_SCHEMAS[name]["input_schema"]
        assert schema["type"] == "object"
        assert "properties" in schema


class TestRequiredFields:
    def test_read_requires_file_path(self):
        assert "file_path" in TOOL_SCHEMAS["read"]["input_schema"]["required"]

    def test_write_requires_file_path_and_content(self):
        required = TOOL_SCHEMAS["write"]["input_schema"]["required"]
        assert "file_path" in required
        assert "content" in required

    def test_edit_requires_three_fields(self):
        required = TOOL_SCHEMAS["edit"]["input_schema"]["required"]
        assert set(required) == {"file_path", "old_string", "new_string"}

    def test_bash_requires_command(self):
        assert "command" in TOOL_SCHEMAS["bash"]["input_schema"]["required"]

    def test_glob_requires_pattern(self):
        assert "pattern" in TOOL_SCHEMAS["glob"]["input_schema"]["required"]

    def test_grep_requires_pattern(self):
        assert "pattern" in TOOL_SCHEMAS["grep"]["input_schema"]["required"]

    def test_git_requires_command(self):
        assert "command" in TOOL_SCHEMAS["git"]["input_schema"]["required"]


class TestPropertyTypes:
    def test_file_path_is_string(self):
        for tool in ["read", "write", "edit"]:
            assert TOOL_SCHEMAS[tool]["input_schema"]["properties"]["file_path"]["type"] == "string"

    def test_content_is_string(self):
        assert TOOL_SCHEMAS["write"]["input_schema"]["properties"]["content"]["type"] == "string"

    def test_command_is_string(self):
        assert TOOL_SCHEMAS["bash"]["input_schema"]["properties"]["command"]["type"] == "string"

    def test_timeout_is_integer(self):
        assert TOOL_SCHEMAS["bash"]["input_schema"]["properties"]["timeout"]["type"] == "integer"

    def test_git_args_is_array(self):
        props = TOOL_SCHEMAS["git"]["input_schema"]["properties"]
        assert props["args"]["type"] == "array"
        assert props["args"]["items"]["type"] == "string"

    def test_max_results_is_integer(self):
        assert TOOL_SCHEMAS["glob"]["input_schema"]["properties"]["max_results"]["type"] == "integer"
