"""
Shared CLI utilities — project path resolution, stdlib checks, service factories.

Extracted from main.py as part of #438.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.config import HarnessConfig
from core.agent_registry import AgentRegistry
from core.models import DAG, SuccessCriterion

from control_plane.repository import JobRepository
from control_plane.service import RunService
from control_plane.approval import ApprovalRepository


def _resolve_project_path(project: str | None, allow_self_modify: bool = False) -> str | None:
    """Resolve and validate the project path for mutating operations.

    Prevents agents from accidentally modifying the harness source tree when
    --project is not specified. Returns the resolved project path, or raises
    SystemExit with a clear error message.
    """
    if project:
        resolved = str(Path(project).resolve())
        # Check if explicitly targeting harness tree
        harness_root = Path(__file__).parent.parent.resolve()
        target = Path(resolved).resolve()
        if (target == harness_root or harness_root in target.parents) and not allow_self_modify:
            sys.stderr.write(
                "ERROR: --project points to the harness source tree.\n"
                "Agents would modify harness itself, which is usually unintended.\n\n"
                "Use --allow-self-modify to opt in (NOT recommended for production).\n"
            )
            sys.exit(2)
        return resolved

    cwd = Path.cwd().resolve()
    harness_root = Path(__file__).parent.parent.resolve()

    if cwd == harness_root or harness_root in cwd.parents:
        if not allow_self_modify:
            sys.stderr.write(
                "ERROR: --project not specified and cwd is inside the harness source tree.\n"
                "Running without --project would let agents modify harness itself.\n\n"
                "Pick one:\n"
                "  (1) --project ./my-project        target an existing project\n"
                "  (2) --allow-self-modify           explicit opt-in (NOT recommended)\n"
            )
            sys.exit(2)
        sys.stderr.write("WARN: --allow-self-modify set; agents may modify harness source tree.\n")
        return str(cwd)

    # Outside harness tree: use cwd, but warn
    sys.stderr.write(f"WARN: --project not given, defaulting to {cwd}\n")
    return str(cwd)


def _check_dirty_workspace(project: str | None) -> None:
    """Warn if the project workspace has uncommitted changes (#147)."""
    if not project:
        return

    project_path = Path(project).resolve()
    if not (project_path / ".git").is_dir():
        return

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True,
            timeout=10,
            cwd=str(project_path),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return

    dirty_files = result.stdout.strip().split("\n")
    count = len(dirty_files)
    msg = (
        f"WARN: Workspace has {count} uncommitted file(s) "
        f"in {project_path}.\n"
        f"This may be from a previous incomplete run.\n"
    )

    non_interactive = os.environ.get("HARNESS_NON_INTERACTIVE", "").lower() in (
        "true", "1", "yes",
    )
    if non_interactive:
        sys.stderr.write(msg + "Proceeding anyway (non-interactive mode).\n")
        return

    sys.stderr.write(msg)
    try:
        answer = input("Continue anyway? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            sys.stderr.write("Aborted.\n")
            sys.exit(1)
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\nAborted.\n")
        sys.exit(1)


def _is_legitimate_package(path: Path) -> bool:
    """Heuristic: detect a legitimate package (not a leftover shadow dir)."""
    py_files = list(path.glob("**/*.py"))
    if len(py_files) == 0:
        return False
    if len(py_files) == 1 and py_files[0].name == "__init__.py":
        try:
            content = py_files[0].read_text(encoding="utf-8", errors="replace").strip()
            if len(content) < 50:
                return False
        except OSError:
            return False
    return True


def _quarantine_shadowing_dir(path: Path, project_path: Path) -> Path:
    """Move a shadowing directory to .harness/quarantine/<timestamp>/<name>."""
    import shutil

    quarantine_base = project_path / ".harness" / "quarantine"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = quarantine_base / timestamp / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    return dest


def _check_stdlib_shadowing(
    project: str | None,
    cleanup: bool = False,
) -> None:
    """Check for directories that shadow Python stdlib modules (#240, #246)."""
    if not project:
        return

    project_path = Path(project).resolve()
    if not project_path.is_dir():
        return

    stdlib_names = getattr(sys, "stdlib_module_names", None) or {
        "abc", "argparse", "array", "ast", "asyncio", "atexit", "base64",
        "bisect", "builtins", "bz2", "calendar", "cgi", "cmath", "cmd",
        "codecs", "collections", "colorsys", "concurrent", "configparser",
        "contextlib", "copy", "csv", "ctypes", "curses", "dataclasses",
        "datetime", "dbm", "decimal", "difflib", "dis", "doctest", "email",
        "enum", "errno", "fileinput", "fnmatch", "fractions", "ftplib",
        "functools", "gc", "getopt", "getpass", "glob", "gzip", "hashlib",
        "heapq", "hmac", "html", "http", "importlib", "inspect", "io",
        "itertools", "json", "keyword", "lib2to3", "linecache", "locale",
        "logging", "lzma", "mailbox", "marshal", "math", "mimetypes", "mmap",
        "multiprocessing", "numbers", "operator", "os", "pathlib", "pdb",
        "pickle", "pipes", "pkgutil", "platform", "plistlib", "poplib",
        "pprint", "profile", "pstats", "queue", "random", "re", "reprlib",
        "runpy", "sched", "secrets", "select", "shelve", "shlex", "shutil",
        "signal", "site", "smtplib", "socket", "socketserver", "sqlite3",
        "ssl", "stat", "statistics", "string", "struct", "subprocess",
        "symtable", "sys", "sysconfig", "tabnanny", "tarfile", "tempfile",
        "test", "textwrap", "threading", "time", "timeit", "tkinter",
        "token", "tokenize", "trace", "traceback", "types", "typing",
        "unicodedata", "unittest", "urllib", "uu", "uuid", "venv",
        "warnings", "weakref", "webbrowser", "wsgiref", "xml", "xmlrpc",
        "zipfile", "zlib", "zoneinfo",
    }

    shadowing = []
    for child in sorted(project_path.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith((".", "_")):
            continue
        if child.name.lower() in stdlib_names:
            shadowing.append((child, child.name.lower()))

    if not shadowing:
        return

    msg = (
        f"WARNING: Found {len(shadowing)} directory(ies) shadowing Python stdlib:\n"
    )
    for path, name in shadowing:
        msg += f"  - {path.name}/ shadows stdlib '{name}'\n"
    msg += "These will cause import failures in pytest, httpx, and other tools.\n"

    non_interactive = os.environ.get("HARNESS_NON_INTERACTIVE", "").lower() in (
        "true", "1", "yes",
    )

    if non_interactive:
        sys.stderr.write(msg)
        if cleanup:
            quarantined = 0
            protected = 0
            for path, _ in shadowing:
                if _is_legitimate_package(path):
                    sys.stderr.write(
                        f"  PROTECTED: {path.name}/ appears to be a legitimate "
                        f"package — keeping.\n"
                    )
                    protected += 1
                else:
                    dest = _quarantine_shadowing_dir(path, project_path)
                    sys.stderr.write(
                        f"  Quarantined: {path.name}/ → {dest}\n"
                    )
                    quarantined += 1
            sys.stderr.write(
                f"Quarantined {quarantined} leftover dir(s), "
                f"protected {protected} legitimate package(s).\n"
            )
            if protected > 0:
                sys.stderr.write(
                    "Cannot proceed: remaining legitimate package(s) still "
                    "shadow stdlib. Rename or move them manually.\n"
                )
                sys.exit(1)
            return
        else:
            sys.stderr.write(
                "Aborting: remove or rename these directories before running, "
                "or use --cleanup-stdlib-shadowing to quarantine leftovers.\n"
            )
            sys.exit(1)

    sys.stderr.write(msg)
    try:
        answer = input("Quarantine leftover directories? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            for path, _ in shadowing:
                if _is_legitimate_package(path):
                    sys.stderr.write(
                        f"  PROTECTED: {path.name}/ appears to be a legitimate "
                        f"package — keeping.\n"
                    )
                else:
                    dest = _quarantine_shadowing_dir(path, project_path)
                    sys.stderr.write(
                        f"  Quarantined: {path.name}/ → {dest}\n"
                    )
        else:
            sys.stderr.write("Kept. Proceeding may fail due to import conflicts.\n")
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\nKept existing directories.\n")


def load_registry(project_path: str | None = None) -> AgentRegistry:
    """Load agent registry with defaults + project custom agents."""
    registry = AgentRegistry()

    if project_path:
        agents_yaml = Path(project_path) / ".harness" / "agents.yaml"
        if agents_yaml.exists():
            print(f"Loading project agents from {agents_yaml}")
            registry.load_from_yaml(agents_yaml)

    return registry


def _serialize_dag(dag: DAG) -> dict:
    """Serialize a DAG to a JSON-compatible dict."""
    return {
        "reasoning": dag.reasoning,
        "nodes": [
            {
                "id": n.id,
                "agent_type": n.agent_type,
                "task": n.task_description,
                "success_criteria": [
                    sc.model_dump(mode="json") if isinstance(sc, SuccessCriterion) else sc
                    for sc in n.success_criteria
                ],
            }
            for n in dag.nodes.values()
        ],
        "edges": [{"from": e.from_node, "to": e.to_node} for e in dag.edges],
    }


def _parse_template_vars(var_list: list[str]) -> dict[str, str]:
    """Parse KEY=VALUE pairs from --var arguments."""
    variables: dict[str, str] = {}
    for item in var_list:
        if "=" in item:
            key, value = item.split("=", 1)
            variables[key.strip()] = value.strip()
        else:
            raise ValueError(f"Invalid --var format: {item} (expected KEY=VALUE)")
    return variables


def _write_error(code: str, message: str) -> None:
    """Write a structured JSON error to stderr and exit with code 1."""
    sys.stderr.write(json.dumps({"error": message, "code": code}) + "\n")
    sys.exit(1)


def _make_repository() -> JobRepository:
    """Create a JobRepository with the default data path."""
    return JobRepository(base_path="./data/jobs")


def _make_run_service(repository: JobRepository, non_interactive: bool = False) -> RunService:
    """Create a RunService with LLM config from environment."""
    harness_config = HarnessConfig.from_env()
    approval_repo = ApprovalRepository()

    llm_router = None
    routing_cfg = harness_config.model_routing
    if routing_cfg.routing or routing_cfg.fallback_chain != ["claude-sonnet-4-6"]:
        from core.llm_router import LLMRouter
        llm_router = LLMRouter(routing_cfg, harness_config.llm)

    service = RunService(
        repository=repository,
        llm_config=harness_config.llm,
        default_backend=harness_config.default_backend,
        backend_base_path=harness_config.backend_base_path,
        approval_repo=approval_repo,
        non_interactive=non_interactive,
        approval_timeout_sec=harness_config.approval_timeout_sec,
    )
    if llm_router:
        service.llm_router = llm_router
    return service
