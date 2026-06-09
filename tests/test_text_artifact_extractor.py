"""Tests for text_artifact_extractor — code block extraction from LLM text (#1123)."""
from __future__ import annotations

import os

from agent.backends.text_artifact_extractor import (
    _extract_filename_hint,
    _infer_filename,
    _parse_code_blocks,
    _to_snake_case,
    _write_artifact_safely,
    extract_artifacts_from_text,
    _MIN_TEXT_LENGTH,
)


# -- Unit tests: _parse_code_blocks --


class TestParseCodeBlocks:
    def test_extracts_simple_python_block(self):
        text = "Here is the code:\n```python\nprint('hello')\n```"
        blocks = _parse_code_blocks(text)
        assert len(blocks) == 1
        lang, filename, content = blocks[0]
        assert lang == "python"
        assert filename is None
        assert "print('hello')" in content

    def test_extracts_multiple_blocks(self):
        text = (
            "```python\nx = 1\n```\n"
            "Some text\n"
            "```javascript\nconst y = 2;\n```"
        )
        blocks = _parse_code_blocks(text)
        assert len(blocks) == 2
        assert blocks[0][0] == "python"
        assert blocks[1][0] == "javascript"

    def test_extracts_block_with_filename_hint(self):
        text = "```python\n# file: src/main.py\nprint('hello')\n```"
        blocks = _parse_code_blocks(text)
        assert len(blocks) == 1
        assert blocks[0][1] == "src/main.py"

    def test_no_code_blocks(self):
        text = "Just plain text without any code blocks."
        blocks = _parse_code_blocks(text)
        assert blocks == []

    def test_empty_string(self):
        assert _parse_code_blocks("") == []

    def test_unterminated_block_ignored(self):
        text = "```python\nprint('hello')\n no closing fence"
        blocks = _parse_code_blocks(text)
        assert blocks == []

    def test_block_without_language_tag(self):
        text = "```\nsome code\n```"
        blocks = _parse_code_blocks(text)
        assert len(blocks) == 1
        assert blocks[0][0] == ""


# -- Unit tests: _extract_filename_hint --


class TestExtractFilenameHint:
    def test_python_file_comment(self):
        content = "# file: src/utils.py\nimport os\n"
        assert _extract_filename_hint(content) == "src/utils.py"

    def test_filename_comment(self):
        content = "# filename: helper.py\ndef foo(): pass\n"
        assert _extract_filename_hint(content) == "helper.py"

    def test_path_comment(self):
        content = "# path: app/models.py\nclass User:\n"
        assert _extract_filename_hint(content) == "app/models.py"

    def test_javascript_file_comment(self):
        content = "// file: src/index.js\nconst app = {};\n"
        assert _extract_filename_hint(content) == "src/index.js"

    def test_no_hint(self):
        content = "import os\n\ndef main():\n    pass\n"
        assert _extract_filename_hint(content) is None

    def test_hint_after_5_lines_ignored(self):
        lines = ["line " + str(i) for i in range(10)]
        lines[6] = "# file: late.py"
        content = "\n".join(lines)
        assert _extract_filename_hint(content) is None


# -- Unit tests: _infer_filename --


class TestInferFilename:
    def test_python_class(self):
        content = "class UserService:\n    pass\n"
        result = _infer_filename("python", content, 0)
        assert result == "user_service.py"

    def test_python_function(self):
        content = "def calculate_total(items):\n    return sum(items)\n"
        result = _infer_filename("python", content, 0)
        assert result == "calculate_total.py"

    def test_python_test_function_infers_name(self):
        """A test function def is still a function — infers from its name."""
        content = "import pytest\n\ndef test_something():\n    assert True\n"
        result = _infer_filename("python", content, 0)
        # Function name match takes precedence over test pattern
        assert result == "test_something.py"

    def test_javascript_generic(self):
        content = "const x = 1;\n"
        result = _infer_filename("javascript", content, 0)
        assert result == "artifact_0.js"

    def test_dockerfile(self):
        content = "FROM python:3.11\n"
        result = _infer_filename("dockerfile", content, 0)
        assert result == "Dockerfile"

    def test_unknown_language(self):
        content = "some text"
        result = _infer_filename("", content, 0)
        assert result is None

    def test_index_dedup(self):
        content = "const x = 1;\n"
        result = _infer_filename("javascript", content, 3)
        assert result == "artifact_3.js"


# -- Unit tests: _to_snake_case --


class TestToSnakeCase:
    def test_camel_case(self):
        assert _to_snake_case("UserService") == "user_service"

    def test_already_snake(self):
        assert _to_snake_case("user_service") == "user_service"

    def test_single_word(self):
        assert _to_snake_case("Service") == "service"

    def test_consecutive_caps_unchanged(self):
        """Consecutive capitals like HTTP stay together (no intra-word split)."""
        assert _to_snake_case("HTTPServer") == "httpserver"


# -- Unit tests: _write_artifact_safely --


class TestWriteArtifactSafely:
    def test_writes_file(self, tmp_path):
        workspace = str(tmp_path)
        result = _write_artifact_safely(workspace, "src/main.py", "print('hello')")
        assert result == "src/main.py"
        filepath = os.path.join(workspace, "src", "main.py")
        assert os.path.isfile(filepath)
        with open(filepath) as f:
            assert f.read() == "print('hello')"

    def test_sanitizes_path_traversal(self, tmp_path):
        """Path traversal parts are stripped, leaving a safe relative path."""
        workspace = str(tmp_path)
        result = _write_artifact_safely(workspace, "../../etc/passwd", "bad")
        # `..` parts are filtered, leaving "etc/passwd" which is safe
        assert result == "etc/passwd"
        filepath = os.path.join(workspace, "etc", "passwd")
        assert os.path.isfile(filepath)

    def test_strips_hidden_dirs(self, tmp_path):
        """Hidden directory parts are stripped from the path."""
        workspace = str(tmp_path)
        result = _write_artifact_safely(workspace, ".hidden/file.py", "data")
        # `.hidden` is filtered, leaving just "file.py"
        assert result == "file.py"

    def test_idempotent_same_content(self, tmp_path):
        workspace = str(tmp_path)
        _write_artifact_safely(workspace, "file.py", "content")
        result = _write_artifact_safely(workspace, "file.py", "content")
        assert result == "file.py"

    def test_overwrites_different_content(self, tmp_path):
        workspace = str(tmp_path)
        _write_artifact_safely(workspace, "file.py", "old content")
        result = _write_artifact_safely(workspace, "file.py", "new content")
        assert result == "file.py"
        filepath = os.path.join(workspace, "file.py")
        with open(filepath) as f:
            assert f.read() == "new content"


# -- Integration tests: extract_artifacts_from_text --


def _pad(text: str, min_length: int = _MIN_TEXT_LENGTH) -> str:
    """Pad text to exceed the minimum extraction threshold."""
    if len(text) >= min_length:
        return text
    return text + "\n" * (min_length - len(text) + 1)


class TestExtractArtifactsFromText:
    def test_extracts_python_with_file_hint(self, tmp_path):
        workspace = str(tmp_path)
        text = _pad(
            "Here is the implementation:\n\n"
            "```python\n"
            "# file: src/calculator.py\n"
            "class Calculator:\n"
            "    def add(self, a, b):\n"
            "        return a + b\n"
            "```\n"
        )
        result = extract_artifacts_from_text(text, workspace)
        assert result == ["src/calculator.py"]
        filepath = os.path.join(workspace, "src", "calculator.py")
        assert os.path.isfile(filepath)
        with open(filepath) as f:
            assert "class Calculator" in f.read()

    def test_extracts_multiple_blocks(self, tmp_path):
        workspace = str(tmp_path)
        text = _pad(
            "```python\n"
            "# file: app.py\n"
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            "```\n\n"
            "```python\n"
            "# file: test_app.py\n"
            "import pytest\n"
            "def test_app():\n"
            "    assert True\n"
            "```\n"
        )
        result = extract_artifacts_from_text(text, workspace)
        assert len(result) == 2
        assert "app.py" in result
        assert "test_app.py" in result

    def test_skips_short_text(self, tmp_path):
        workspace = str(tmp_path)
        result = extract_artifacts_from_text("Short text", workspace)
        assert result == []

    def test_skips_empty_string(self, tmp_path):
        workspace = str(tmp_path)
        result = extract_artifacts_from_text("", workspace)
        assert result == []

    def test_skips_trivial_code_blocks(self, tmp_path):
        workspace = str(tmp_path)
        text = "```python\nx\n```"
        # Content is less than 20 chars, should be filtered
        result = extract_artifacts_from_text(text + "\n" * 200, workspace)
        assert result == []

    def test_infers_filename_from_class(self, tmp_path):
        workspace = str(tmp_path)
        text = _pad(
            "```python\n"
            "class TaskManager:\n"
            "    def __init__(self):\n"
            "        self.tasks = []\n"
            "```\n"
        )
        result = extract_artifacts_from_text(text, workspace)
        assert len(result) == 1
        assert result[0] == "task_manager.py"

    def test_extracts_javascript(self, tmp_path):
        workspace = str(tmp_path)
        text = _pad(
            "```javascript\n"
            "// file: server.js\n"
            "const express = require('express');\n"
            "const app = express();\n"
            "app.listen(3000);\n"
            "```\n"
        )
        result = extract_artifacts_from_text(text, workspace)
        assert result == ["server.js"]

    def test_deduplicates_filenames(self, tmp_path):
        workspace = str(tmp_path)
        text = _pad(
            "```python\n"
            "# file: utils.py\n"
            "def a(): pass\n"
            "```\n"
            "```python\n"
            "# file: utils.py\n"
            "def b(): pass\n"
            "```\n"
        )
        result = extract_artifacts_from_text(text, workspace)
        assert len(result) == 2
        # First keeps original name, second gets deduplicated
        assert "utils.py" in result
        assert "utils_1.py" in result

    def test_realistic_llm_output(self, tmp_path):
        """Test with realistic LLM output containing code blocks."""
        workspace = str(tmp_path)
        text = """I'll implement the REST API for todo items.

Here's the main application file:

```python
# file: app.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List

app = FastAPI()

class TodoItem(BaseModel):
    id: Optional[int] = None
    title: str
    completed: bool = False

todos: List[TodoItem] = []
next_id = 1

@app.get("/todos", response_model=List[TodoItem])
def get_todos():
    return todos

@app.post("/todos", response_model=TodoItem, status_code=201)
def create_todo(item: TodoItem):
    global next_id
    item.id = next_id
    next_id += 1
    todos.append(item)
    return item

@app.get("/todos/{todo_id}", response_model=TodoItem)
def get_todo(todo_id: int):
    for todo in todos:
        if todo.id == todo_id:
            return todo
    raise HTTPException(status_code=404, detail="Todo not found")
```

And the test file:

```python
# file: test_app.py
import pytest
from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

def test_create_todo():
    response = client.post("/todos", json={"title": "Test", "completed": False})
    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Test"

def test_get_todos():
    response = client.get("/todos")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

def test_get_todo_not_found():
    response = client.get("/todos/999")
    assert response.status_code == 404
```

These files implement a complete REST API with CRUD operations.
"""
        result = extract_artifacts_from_text(text, workspace)
        assert len(result) == 2
        assert "app.py" in result
        assert "test_app.py" in result

        # Verify actual file contents
        app_path = os.path.join(workspace, "app.py")
        with open(app_path) as f:
            content = f.read()
        assert "FastAPI" in content
        assert "class TodoItem" in content

    def test_node_context_not_required(self, tmp_path):
        """extract_artifacts_from_text works without node_context."""
        workspace = str(tmp_path)
        text = _pad(
            "```python\n"
            "# file: hello.py\n"
            "print('hello world')\n"
            "```\n"
        )
        result = extract_artifacts_from_text(text, workspace)
        assert result == ["hello.py"]
