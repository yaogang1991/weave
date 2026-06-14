"""Extract code artifacts from LLM text output (#1123).

When third-party LLMs output code as text (fenced code blocks)
instead of using tool_use calls, this module extracts the code blocks
and writes them as files to the workspace.

Only activates for generator nodes — planners and evaluators typically
don't need to write files.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["extract_artifacts_from_text"]

# Regex to match fenced code blocks.
# Supports optional language tag and optional filename comment.
# Patterns: ```python\n# file: src/main.py\n...code...\n```
_CODE_BLOCK_RE = re.compile(
    r"```(\w*)\s*\n"         # Opening fence with optional language
    r"(.*?)"                 # Code content (greedy until closing fence)
    r"\n```",                # Closing fence
    re.DOTALL,
)

# Filename hint patterns at the start of a code block
_FILENAME_HINT_PATTERNS: list[re.Pattern] = [
    re.compile(r"^#\s*(?:file|filename|path|File):\s*(\S+)", re.MULTILINE),
    re.compile(r"^//\s*(?:file|filename|path):\s*(\S+)", re.MULTILINE),
    re.compile(r"^<!--\s*(?:file|filename|path):\s*(\S+)\s*-->", re.MULTILINE),
]

# Minimum character length to consider extraction worthwhile
_MIN_TEXT_LENGTH = 200

# Language tag to file extension mapping
_LANG_TO_EXT: dict[str, str] = {
    "python": ".py", "py": ".py",
    "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts",
    "tsx": ".tsx", "jsx": ".jsx",
    "java": ".java",
    "go": ".go",
    "rust": ".rs", "rs": ".rs",
    "ruby": ".rb", "rb": ".rb",
    "cpp": ".cpp", "c++": ".cpp",
    "c": ".c",
    "css": ".css",
    "html": ".html",
    "json": ".json",
    "yaml": ".yaml", "yml": ".yml",
    "toml": ".toml",
    "sql": ".sql",
    "sh": ".sh", "bash": ".sh", "shell": ".sh",
    "markdown": ".md", "md": ".md",
    "dockerfile": "Dockerfile",
}


def extract_artifacts_from_text(
    text: str,
    workspace_path: str,
    node_context: dict[str, Any] | None = None,
) -> list[str]:
    """Extract code blocks from text and write them as files.

    Parses fenced code blocks from the LLM's text output, infers filenames
    from hints (``# file: path.py``) or language/content, and writes them
    to the workspace directory.

    Args:
        text: The LLM text output potentially containing code blocks.
        workspace_path: Absolute path to the workspace directory.
        node_context: Optional context about the node (e.g. agent_type).

    Returns:
        List of relative file paths written to the workspace.
    """
    if not text or len(text) < _MIN_TEXT_LENGTH:
        logger.info(
            "text artifact extraction skipped: text below minimum length "
            "(len=%d < %d) (#1137 track 3b)",
            len(text or ""), _MIN_TEXT_LENGTH,
        )
        return []

    # Only extract for generator nodes — planners and evaluators
    # typically don't need to write files.
    if node_context:
        agent_type = node_context.get("agent_type")
        if agent_type and agent_type not in ("generator",):
            logger.info(
                "text artifact extraction skipped: non-generator node "
                "(agent_type=%s) (#1137 track 3b)",
                agent_type,
            )
            return []

    blocks = _parse_code_blocks(text)
    if not blocks:
        return []

    # Filter out trivially short blocks (e.g. inline examples)
    meaningful_blocks = [
        (lang, filename, content)
        for lang, filename, content in blocks
        if len(content.strip()) >= 20
    ]
    if not meaningful_blocks:
        logger.info(
            "text artifact extraction skipped: all %d fenced block(s) below "
            "the %d-char threshold (#1137 track 3b)",
            len(blocks), 20,
        )
        return []

    written: list[str] = []
    used_filenames: set[str] = set()

    for i, (lang, filename, content) in enumerate(meaningful_blocks):
        if not filename:
            filename = _infer_filename(lang, content, i)
        if not filename:
            logger.debug("Skipping code block %d: no filename inferred", i)
            continue

        # Deduplicate filenames
        if filename in used_filenames:
            base, ext = os.path.splitext(filename)
            filename = f"{base}_{i}{ext}"
        used_filenames.add(filename)

        rel_path = _write_artifact_safely(workspace_path, filename, content)
        if rel_path:
            written.append(rel_path)

    if written:
        logger.info(
            "Extracted %d artifacts from text output (#1123)",
            len(written),
        )
    return written


def _parse_code_blocks(
    text: str,
) -> list[tuple[str, str | None, str]]:
    """Parse fenced code blocks from LLM text output.

    Returns list of (language, filename_or_None, content) tuples.
    """
    results: list[tuple[str, str | None, str]] = []

    for match in _CODE_BLOCK_RE.finditer(text):
        lang = match.group(1) or ""
        raw_content = match.group(2) or ""

        # Look for filename hint in the first few lines
        filename = _extract_filename_hint(raw_content)

        results.append((lang, filename, raw_content))

    return results


def _extract_filename_hint(content: str) -> str | None:
    """Try to extract a filename from hint comments in code content."""
    # Only search in the first 5 lines for filename hints
    first_lines = "\n".join(content.split("\n")[:5])
    for pattern in _FILENAME_HINT_PATTERNS:
        hint_match = pattern.search(first_lines)
        if hint_match:
            return hint_match.group(1).strip()
    return None


def _infer_filename(
    lang: str,
    content: str,
    index: int,
) -> str | None:
    """Infer a filename from language tag and code content.

    Strategy:
    1. Check for class/function definitions (Python)
    2. Check for module/import patterns
    3. Fall back to language-based extension with index
    """
    ext = _LANG_TO_EXT.get(lang.lower())

    # Python-specific heuristics
    if lang.lower() in ("python", "py") or ext == ".py":
        # Look for class definition
        class_match = re.search(r"^class\s+(\w+)", content, re.MULTILINE)
        if class_match:
            return f"{_to_snake_case(class_match.group(1))}{ext}"

        # Look for top-level function definition
        func_match = re.search(r"^def\s+(\w+)", content, re.MULTILINE)
        if func_match:
            return f"{func_match.group(1)}{ext}"

        # Look for test patterns
        if re.search(r"\b(test_|Test|unittest|pytest)\b", content[:500]):
            return f"test_artifact_{index}{ext}"

    # Generic: use language extension with index
    if ext:
        if ext == "Dockerfile":
            return "Dockerfile"
        return f"artifact_{index}{ext}"

    # No language tag and no recognizable patterns
    return None


def _to_snake_case(name: str) -> str:
    """Convert CamelCase to snake_case."""
    result = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    return result.lower()


def _write_artifact_safely(
    workspace_path: str,
    filename: str,
    content: str,
) -> str | None:
    """Write content to a file in the workspace with path safety checks.

    Returns the relative path if successful, None otherwise.
    """
    # Sanitize filename — prevent path traversal
    filename = filename.replace("\\", "/")
    parts = filename.split("/")
    safe_parts = [
        p for p in parts
        if p and not p.startswith("..") and not p.startswith(".")
    ]
    if not safe_parts:
        return None

    safe_filename = "/".join(safe_parts)
    full_path = os.path.join(workspace_path, safe_filename)

    # Verify resolved path stays within workspace
    real_workspace = os.path.realpath(workspace_path)
    real_path = os.path.realpath(full_path)
    if (
        not real_path.startswith(real_workspace + os.sep)
        and real_path != real_workspace
    ):
        logger.warning("Skipping path traversal attempt: %s", filename)
        return None

    # Don't overwrite existing files unless our content is different
    if os.path.isfile(real_path):
        try:
            with open(real_path, "r", encoding="utf-8") as f:
                existing = f.read()
            if existing == content:
                logger.debug(
                    "Artifact %s already exists with same content",
                    safe_filename,
                )
                return safe_filename
        except (OSError, UnicodeDecodeError):
            pass

    # Create parent directories if needed
    parent = os.path.dirname(full_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    try:
        # Atomic write via temp file
        tmp_path = full_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, full_path)
        logger.info("Extracted artifact: %s", safe_filename)
        return safe_filename
    except OSError as exc:
        logger.warning("Failed to write artifact %s: %s", safe_filename, exc)
        # Clean up temp file if it exists
        try:
            os.unlink(full_path + ".tmp")
        except OSError:
            pass
        return None
