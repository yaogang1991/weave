"""Tests for unfenced-markdown recovery (#1137 track 3).

Covers the case where a generator (notably GLM-5.2) emits a README as
plain markdown prose with no fenced code block — the extractor must
recover it as an artifact instead of returning nothing.
"""
from __future__ import annotations

import os

from agent.backends.text_artifact_extractor import (
    _looks_like_markdown_document,
    _recover_unfenced_markdown,
    _unique_doc_filename,
    extract_artifacts_from_text,
)


# A realistic README emitted as plain prose — no ``` fences anywhere.
_README_PROSE = """# Todo API

A small FastAPI service for managing todo items.

## Installation

Install dependencies with pip:

    pip install fastapi uvicorn

## Usage

Run the development server:

    uvicorn app:app --reload

Then open http://localhost:8000/docs for the interactive API docs.

## Endpoints

- GET /todos — list all todos
- POST /todos — create a todo
- GET /todos/{id} — fetch one todo

Created as part of the weave demo project.
"""


class TestLooksLikeMarkdownDocument:
    def test_section_heading_detected(self):
        assert _looks_like_markdown_document(_README_PROSE) is True

    def test_single_hash_only_is_not_a_document(self):
        # Single-# lines look like code comments, not markdown sections.
        text = "# main.py\nimport os\n# do stuff\npass\n" * 10
        assert _looks_like_markdown_document(text) is False

    def test_plain_prose_without_headings_is_not_a_document(self):
        text = "This is just a paragraph of prose. " * 40
        assert _looks_like_markdown_document(text) is False

    def test_empty_is_not_a_document(self):
        assert _looks_like_markdown_document("") is False


class TestUniqueDocFilename:
    def test_uses_base_when_free(self, tmp_path):
        workspace = str(tmp_path)
        assert _unique_doc_filename(workspace, "README.md", "x") == "README.md"

    def test_reuses_when_same_content(self, tmp_path):
        workspace = str(tmp_path)
        path = os.path.join(workspace, "README.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("same")
        assert _unique_doc_filename(workspace, "README.md", "same") == "README.md"

    def test_falls_back_when_different_content(self, tmp_path):
        workspace = str(tmp_path)
        path = os.path.join(workspace, "README.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("old")
        assert _unique_doc_filename(workspace, "README.md", "new") == "README_1.md"

    def test_increments_index_across_collisions(self, tmp_path):
        workspace = str(tmp_path)
        for name in ("README.md", "README_1.md"):
            with open(os.path.join(workspace, name), "w", encoding="utf-8") as f:
                f.write("existing")
        assert _unique_doc_filename(workspace, "README.md", "fresh") == "README_2.md"


class TestRecoverUnfencedMarkdown:
    def test_recovers_readme_prose(self, tmp_path):
        workspace = str(tmp_path)
        result = _recover_unfenced_markdown(_README_PROSE, workspace)
        assert result == ["README.md"]
        path = os.path.join(workspace, "README.md")
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as f:
            written = f.read()
        assert "## Installation" in written
        assert "## Usage" in written

    def test_no_headings_returns_empty(self, tmp_path):
        workspace = str(tmp_path)
        text = "Just prose, no headings here. " * 40
        assert _recover_unfenced_markdown(text, workspace) == []
        assert not os.path.exists(os.path.join(workspace, "README.md"))

    def test_does_not_clobber_existing_readme(self, tmp_path):
        workspace = str(tmp_path)
        existing_path = os.path.join(workspace, "README.md")
        with open(existing_path, "w", encoding="utf-8") as f:
            f.write("PRE-EXISTING")
        result = _recover_unfenced_markdown(_README_PROSE, workspace)
        assert result == ["README_1.md"]
        # Original README untouched.
        with open(existing_path, encoding="utf-8") as f:
            assert f.read() == "PRE-EXISTING"


class TestExtractArtifactsFromTextFallback:
    def test_readme_prose_recovered_as_artifact(self, tmp_path):
        """End-to-end: unfenced README prose yields an artifact (#1137)."""
        workspace = str(tmp_path)
        result = extract_artifacts_from_text(
            _README_PROSE, workspace, node_context={"agent_type": "generator"},
        )
        assert result == ["README.md"]
        assert os.path.isfile(os.path.join(workspace, "README.md"))

    def test_non_generator_node_skips_recovery(self, tmp_path):
        workspace = str(tmp_path)
        result = extract_artifacts_from_text(
            _README_PROSE, workspace, node_context={"agent_type": "planner"},
        )
        assert result == []
        assert not os.path.exists(os.path.join(workspace, "README.md"))

    def test_source_code_without_fences_not_recovered(self, tmp_path):
        """Raw Python without fences or markdown headings is not mislabeled."""
        workspace = str(tmp_path)
        code = "# file: app.py\n" + "x = 1\n" * 60
        result = extract_artifacts_from_text(
            code, workspace, node_context={"agent_type": "generator"},
        )
        assert result == []

    def test_fenced_block_takes_precedence(self, tmp_path):
        """When a fenced block exists, the unfenced fallback never fires."""
        workspace = str(tmp_path)
        text = (
            "Here is the code:\n\n"
            "```python\n"
            "# file: app.py\n"
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "```\n"
        )
        # Pad past _MIN_TEXT_LENGTH.
        text = text + "\n" * 200
        result = extract_artifacts_from_text(
            text, workspace, node_context={"agent_type": "generator"},
        )
        assert result == ["app.py"]
