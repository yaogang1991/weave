"""Tests for MCP client helpers (M3.6)."""


from mcp.client import make_prefixed_name, parse_prefixed_name


class TestPrefixedNames:
    def test_make_prefixed_name(self):
        result = make_prefixed_name("github", "create_issue")
        assert result == "mcp__github__create_issue"

    def test_parse_prefixed_name_valid(self):
        result = parse_prefixed_name("mcp__github__create_issue")
        assert result == ("github", "create_issue")

    def test_parse_prefixed_name_with_underscores(self):
        result = parse_prefixed_name("mcp__my_server__my_tool_name")
        assert result == ("my_server", "my_tool_name")

    def test_parse_prefixed_name_not_mcp(self):
        assert parse_prefixed_name("read") is None
        assert parse_prefixed_name("bash") is None

    def test_parse_prefixed_name_malformed(self):
        assert parse_prefixed_name("mcp__onlyonepart") is None
        assert parse_prefixed_name("mcp___") is None

    def test_roundtrip(self):
        original_server = "test_server"
        original_tool = "some_tool"
        prefixed = make_prefixed_name(original_server, original_tool)
        parsed = parse_prefixed_name(prefixed)
        assert parsed == (original_server, original_tool)
