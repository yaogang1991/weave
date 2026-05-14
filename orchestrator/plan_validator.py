"""
Plan validator: structural validation for orchestrator plans.

Validates:
- Duplicate node IDs → raise PlanValidationError (no auto-fix, DAG semantics ambiguous)
- Dangling edges → raise PlanValidationError
- Cycle detection → raise PlanValidationError
- Stdlib module name shadowing → warning (#238)

Design: validation-only, never silently mutates the plan. This avoids
introducing subtle semantic changes when duplicate IDs make edge
ownership ambiguous. Instead, the orchestrator is asked to replan.
"""
from __future__ import annotations

import re
import sys


class PlanValidationError(Exception):
    """Raised when a plan has structural errors."""


def check_stdlib_conflict(name: str) -> str | None:
    """Check if a name conflicts with a Python stdlib module.

    Returns the stdlib module name if there's a conflict, None otherwise.
    Uses sys.stdlib_module_names (Python 3.10+) with a hardcoded fallback.
    """
    name_lower = name.lower().replace("-", "_")
    stdlib_names = _get_stdlib_names()
    if name_lower in stdlib_names:
        return name_lower
    return None


_stdlib_cache: set[str] | None = None


def _get_stdlib_names() -> set[str]:
    global _stdlib_cache
    if _stdlib_cache is not None:
        return _stdlib_cache
    try:
        _stdlib_cache = sys.stdlib_module_names  # Python 3.10+
    except AttributeError:
        # Fallback for Python < 3.10
        _stdlib_cache = {
            "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio",
            "asyncore", "atexit", "audioop", "base64", "bdb", "binascii",
            "bisect", "builtins", "bz2", "calendar", "cgi", "cgitb", "chunk",
            "cmath", "cmd", "code", "codecs", "codeop", "collections",
            "colorsys", "compileall", "concurrent", "configparser", "contextlib",
            "contextvars", "copy", "copyreg", "cProfile", "crypt", "csv",
            "ctypes", "curses", "dataclasses", "datetime", "dbm", "decimal",
            "difflib", "dis", "distutils", "doctest", "email", "encodings",
            "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput",
            "fnmatch", "fractions", "ftplib", "functools", "gc", "getopt",
            "getpass", "gettext", "glob", "graphlib", "grp", "gzip", "hashlib",
            "heapq", "hmac", "html", "http", "idlelib", "imaplib", "imghdr",
            "imp", "importlib", "inspect", "io", "ipaddress", "itertools",
            "json", "keyword", "lib2to3", "linecache", "locale", "logging",
            "lzma", "mailbox", "mailcap", "marshal", "math", "mimetypes",
            "mmap", "modulefinder", "multiprocessing", "netrc", "nis", "nntplib",
            "numbers", "operator", "optparse", "os", "ossaudiodev", "pathlib",
            "pdb", "pickle", "pickletools", "pipes", "pkgutil", "platform",
            "plistlib", "poplib", "posix", "posixpath", "pprint", "profile",
            "pstats", "pty", "pwd", "py_compile", "pyclbr", "pydoc",
            "queue", "quopri", "random", "re", "readline", "reprlib",
            "resource", "rlcompleter", "runpy", "sched", "secrets", "select",
            "selectors", "shelve", "shlex", "shutil", "signal", "site",
            "smtpd", "smtplib", "sndhdr", "socket", "socketserver", "spwd",
            "sqlite3", "ssl", "stat", "statistics", "string", "stringprep",
            "struct", "subprocess", "sunau", "symtable", "sys", "sysconfig",
            "syslog", "tabnanny", "tarfile", "telnetlib", "tempfile", "termios",
            "test", "textwrap", "threading", "time", "timeit", "tkinter",
            "token", "tokenize", "tomllib", "trace", "traceback", "tracemalloc",
            "tty", "turtle", "turtledemo", "types", "typing", "unicodedata",
            "unittest", "urllib", "uu", "uuid", "venv", "warnings", "wave",
            "weakref", "webbrowser", "winreg", "winsound", "wsgiref", "xdrlib",
            "xml", "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib",
            "zoneinfo",
        }
    return _stdlib_cache


class PlanValidator:
    """Validates orchestrator plan structure. No mutations."""

    def __init__(self, auto_fix: bool = False) -> None:
        # auto_fix is accepted for API compat but validation-only is always used
        self.auto_fix = auto_fix
        self.warnings: list[str] = []

    def validate(self, plan_data: dict) -> dict:
        """Validate plan structure. Returns plan_data unchanged on success.

        Raises PlanValidationError on any structural error.
        """
        self.warnings.clear()
        nodes = plan_data.get("nodes", [])
        edges = plan_data.get("edges", [])

        node_ids = set()
        for node in nodes:
            nid = node.get("id")
            if not nid:
                continue
            if nid in node_ids:
                raise PlanValidationError(f"Duplicate node ID: {nid}")
            node_ids.add(nid)

        # Check for dangling edges
        valid_dep_types = {"hard", "soft"}
        for edge in edges:
            from_id = edge.get("from")
            to_id = edge.get("to")
            if from_id not in node_ids:
                raise PlanValidationError(
                    f"Dangling edge: source node '{from_id}' does not exist"
                )
            if to_id not in node_ids:
                raise PlanValidationError(
                    f"Dangling edge: target node '{to_id}' does not exist"
                )
            dep_type = edge.get("dependency_type", "hard")
            if dep_type not in valid_dep_types:
                raise PlanValidationError(
                    f"Invalid dependency_type '{dep_type}' on edge "
                    f"{from_id} → {to_id}: must be 'hard' or 'soft'"
                )

        # Cycle detection via DFS
        adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
        for edge in edges:
            adj[edge["from"]].append(edge["to"])

        visited: set[str] = set()
        in_stack: set[str] = set()

        def has_cycle(node_id: str) -> bool:
            visited.add(node_id)
            in_stack.add(node_id)
            for neighbor in adj.get(node_id, []):
                if neighbor in in_stack:
                    return True
                if neighbor not in visited and has_cycle(neighbor):
                    return True
            in_stack.remove(node_id)
            return False

        for nid in node_ids:
            if nid not in visited:
                if has_cycle(nid):
                    raise PlanValidationError("Plan contains a cycle")

        # Stdlib shadowing detection (#238)
        self._check_stdlib_shadowing(nodes)

        return plan_data

    def _check_stdlib_shadowing(self, nodes: list[dict]) -> None:
        """Warn if task descriptions reference stdlib module names as packages.

        Detects patterns like "create a urllib library" or quoted module names
        where the name matches a Python stdlib module.
        """
        for node in nodes:
            task = node.get("task", "")
            if not task:
                continue
            # Extract potential package names from task descriptions
            candidates = set(re.findall(r'["\'](\w+)["\']', task))
            for prefix in ("library", "module", "package", "named", "called"):
                candidates.update(
                    re.findall(
                        rf"{prefix}\s+(\w+)", task, re.IGNORECASE,
                    )
                )

            for candidate in candidates:
                conflict = check_stdlib_conflict(candidate)
                if conflict:
                    self.warnings.append(
                        f"Node '{node.get('id')}' task may create a package "
                        f"named '{conflict}' which shadows Python stdlib. "
                        f"Use a prefixed alternative (e.g., 'my_{conflict}', "
                        f"'{conflict}_lib')."
                    )
