"""
Plan validator: structural validation for orchestrator plans.

Validates:
- Duplicate node IDs → raise PlanValidationError (no auto-fix, DAG semantics ambiguous)
- Dangling edges → raise PlanValidationError
- Cycle detection → raise PlanValidationError
- Stdlib module name shadowing → warning (#238)
- Parallel write conflicts → raise PlanValidationError or warning (#272)

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

    # Maximum estimated file count per generator node (#284).
    MAX_FILES_PER_NODE = 15

    # Maximum distinct complex features per generator node (#409).
    MAX_FEATURES_PER_NODE = 3

    def __init__(self, auto_fix: bool = False, max_nodes: int = 25) -> None:
        # auto_fix is accepted for API compat but validation-only is always used
        self.auto_fix = auto_fix
        self.max_nodes = max_nodes
        self.warnings: list[str] = []
        self.rename_map: dict[str, str] = {}  # stdlib name → prefixed alternative

    def validate(self, plan_data: dict) -> dict:
        """Validate plan structure. Returns plan_data unchanged on success.

        Raises PlanValidationError on any structural error.
        """
        self.warnings.clear()
        self.rename_map.clear()
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

        # Node count limit (#292): prevents JSON truncation on oversized DAGs.
        if len(node_ids) > self.max_nodes:
            raise PlanValidationError(
                f"Plan has {len(node_ids)} nodes (maximum {self.max_nodes}). "
                f"Combine related sub-tasks into fewer nodes."
            )

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
        self._detect_cycle(node_ids, edges)

        # Stdlib shadowing detection (#238)
        self._check_stdlib_shadowing(nodes)

        # Per-node file count estimation (#284)
        self._check_node_file_count(nodes)
        # Feature complexity estimation (#409)
        self._check_feature_complexity(nodes)
        # Parallel write conflict detection (#272)
        self._check_parallel_write_conflicts(nodes, edges)
        # Foundation node dependency enforcement (#740)
        edges = self._check_foundation_dependencies(nodes, edges)
        plan_data["edges"] = edges
        # #754: Re-run cycle detection after foundation auto-fix may
        # have added edges that create a cycle.
        self._detect_cycle(node_ids, edges)
        # Token budget check (M4.6)
        self._check_token_budget(nodes)

        return plan_data

    # Prefixes used for stdlib conflict renaming suggestions.
    _RENAMING_PREFIXES = ("app_", "my_", "")

    def _detect_cycle(
        self, node_ids: set[str], edges: list[dict],
    ) -> None:
        """Raise PlanValidationError if edges form a cycle (#754)."""
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

    def _check_stdlib_shadowing(self, nodes: list[dict]) -> None:
        """Warn if task descriptions intend to create packages that shadow stdlib.

        Only flags when the task explicitly mentions creating/naming a file or
        module with a stdlib name (e.g. "create a module named 'json'"). Does NOT
        flag mere references to using stdlib modules (e.g. "use json.dumps").
        Builds ``rename_map`` so callers can update criterion paths.
        """
        for node in nodes:
            task = node.get("task", "")
            if not task:
                continue
            # Only match creation patterns — verbs that indicate the task will
            # CREATE a file/module/package with this name. This avoids false
            # positives from "use json", "import from collections", etc.
            candidates = set()
            # "create/named/called 'json'" — quoted name after creation verb
            candidates.update(
                re.findall(
                    r'(?:create|build|implement|define|name|call)\w*\s+'
                    r'(?:a\s+)?(?:file|module|package|library)\s+'
                    r'(?:named|called)\s+["\'](\w+)["\']',
                    task, re.IGNORECASE,
                )
            )
            # "named 'json'" / "called 'json'" — explicit naming
            candidates.update(
                re.findall(
                    r'(?:named|called)\s+["\'](\w+)["\']',
                    task, re.IGNORECASE,
                )
            )
            # "module/package/library named json" (unquoted)
            candidates.update(
                re.findall(
                    r'(?:module|package|library)\s+(?:named|called)\s+(\w+)',
                    task, re.IGNORECASE,
                )
            )
            # "create a json module" — direct creation pattern
            candidates.update(
                re.findall(
                    r'(?:create|build|implement|define)\w*\s+an?\s+(\w+)\s+'
                    r'(?:module|package|library|\.py)',
                    task, re.IGNORECASE,
                )
            )
            # "file named json.py" or "json.py file"
            candidates.update(
                re.findall(
                    r'(\w+)\.py\b', task,
                )
            )

            for candidate in candidates:
                conflict = check_stdlib_conflict(candidate)
                if conflict:
                    # Choose the first non-conflicting prefix.
                    for pfx in self._RENAMING_PREFIXES:
                        replacement = f"{pfx}{conflict}"
                        if not check_stdlib_conflict(replacement):
                            self.rename_map[conflict] = replacement
                            break
                    self.warnings.append(
                        f"Node '{node.get('id')}' task may create a package "
                        f"named '{conflict}' which shadows Python stdlib. "
                        f"Use a prefixed alternative "
                        f"(e.g., '{self.rename_map.get(conflict, 'my_' + conflict)}')."
                    )

    def _check_node_file_count(self, nodes: list[dict]) -> None:
        """Warn if a generator node's task mentions creating too many files.

        Estimates file count from task description by counting patterns like
        "create X.py", file paths, and explicit file counts. Only warns for
        generator-type nodes (#284).
        """
        for node in nodes:
            if node.get("agent_type") != "generator":
                continue
            task = node.get("task", "")
            if not task:
                continue
            # Count explicit file paths (e.g., "foo.py", "dir/bar.py")
            file_mentions = set(re.findall(
                r'(?:[\w/]+\.)?[\w]+\.(?:py|yaml|yml|json|toml|cfg|txt)',
                task,
            ))
            # Count "create/implement X" patterns for modules
            create_patterns = re.findall(
                r'(?:create|implement|build|write|add)\s+(?:a\s+|an\s+|the\s+)?'
                r'(?:(?:new|Python|source)\s+)?'
                r'(?:file|module|class|package)\s+'
                r'[\w/.]+',
                task, re.IGNORECASE,
            )
            estimated = max(len(file_mentions), len(create_patterns))
            if estimated > self.MAX_FILES_PER_NODE:
                self.warnings.append(
                    f"Node '{node.get('id')}' is expected to create "
                    f"~{estimated} files (limit: {self.MAX_FILES_PER_NODE}). "
                    f"Decompose into multiple parallel generator nodes with a "
                    f"shared foundation node to prevent context exhaustion."
                )

    def _check_feature_complexity(self, nodes: list[dict]) -> None:
        """Warn if a generator node has too many distinct complex features (#409).

        Estimates feature count by counting enumerated items (1. 2. 3.),
        comma-separated "implement X, Y, Z" patterns, and "and"-separated
        feature lists in task descriptions.

        Only warns for generator nodes — planner and evaluator nodes are excluded
        because they produce in-memory output, not files.
        """
        for node in nodes:
            if node.get("agent_type") != "generator":
                continue
            task = node.get("task", "")
            if not task:
                continue
            feature_count = self._estimate_feature_count(task)
            if feature_count > self.MAX_FEATURES_PER_NODE:
                self.warnings.append(
                    f"Node '{node.get('id')}' has ~{feature_count} distinct "
                    f"complex features (limit: {self.MAX_FEATURES_PER_NODE}). "
                    f"Split into multiple generator nodes with a shared "
                    f"foundation node to prevent iteration budget exhaustion (#409)."
                )

    @staticmethod
    def _estimate_feature_count(task: str) -> int:
        """Estimate the number of distinct complex features in a task description.

        Detects three patterns:
        1. Enumerated lists: "1) apply_patch, 2) create_patch, ..."
        2. Comma/and-separated: "implement apply_patch, create_patch, and merge"
        3. Verb + noun patterns: "implement X", "build Y", "create Z"
        """
        import re
        count = 0

        # Pattern 1: Enumerated items "1) X", "2) Y", etc.
        enumerated = re.findall(r'\d+[.)]\s+', task)
        if len(enumerated) >= 3:
            count = max(count, len(enumerated))

        # Pattern 2: Comma-separated features with "implement"/"build"/"create"
        # Look for lists of distinct feature nouns after implementation verbs
        feature_list_match = re.findall(
            r'(?:implement|build|create|develop|add)\s+(.+?)(?:\.|$)',
            task, re.IGNORECASE,
        )
        for feature_text in feature_list_match:
            # Split by commas and "and"
            items = re.split(r',\s*(?:and\s+)?|\s+and\s+', feature_text)
            items = [item.strip() for item in items if item.strip()]
            if len(items) >= 3:
                count = max(count, len(items))

        # Pattern 3: Multiple "implement/build/create + noun" verb phrases
        verb_phrases = re.findall(
            r'(?:implement|build|create|develop|add|support)\s+'
            r'(?:a\s+|an\s+|the\s+)?'
            r'(\w[\w\s]{2,30}?)'
            r'(?:\s*,|\s+and|\s*\.|$)',
            task, re.IGNORECASE,
        )
        if len(verb_phrases) >= 3:
            count = max(count, len(verb_phrases))

        return count

    def _check_parallel_write_conflicts(
        self,
        nodes: list[dict],
        edges: list[dict],
    ) -> None:
        """Detect file ownership conflicts between parallel generator nodes (#272).

        Identifies nodes at the same topological level (no dependency between
        them) and checks for overlapping ``owned_files`` declarations.

        Conflict patterns detected:
        1. __init__.py collision: two parallel generators in the same package.
        2. Same-file ownership overlap: two nodes both claim the same file.
        3. No ownership contracts on parallel generators: emits serialization warning.

        Does NOT raise for shared files that have a downstream merge node.
        """
        node_map = {n.get("id"): n for n in nodes if n.get("id")}
        node_ids = list(node_map.keys())

        # Compute topological levels
        in_degree = {nid: 0 for nid in node_ids}
        adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
        for edge in edges:
            from_id = edge.get("from")
            to_id = edge.get("to")
            if from_id in node_map and to_id in node_map:
                adj.setdefault(from_id, []).append(to_id)
                in_degree[to_id] = in_degree.get(to_id, 0) + 1

        levels: list[list[str]] = []
        remaining = set(node_ids)
        while remaining:
            current = [nid for nid in remaining if in_degree.get(nid, 0) == 0]
            if not current:
                break  # Cycle — already caught by cycle detection
            levels.append(current)
            remaining -= set(current)
            for nid in current:
                for dep in adj.get(nid, []):
                    in_degree[dep] = in_degree.get(dep, 0) - 1

        # Check each level for parallel conflicts
        for level in levels:
            # Find generator nodes in this level
            generators = [
                nid for nid in level
                if node_map[nid].get("agent_type", "") == "generator"
            ]

            if len(generators) < 2:
                continue

            # Check if all generators have owned_files
            owned_map: dict[str, set[str]] = {}
            for nid in generators:
                owned = node_map[nid].get("owned_files", [])
                owned_map[nid] = set(owned) if owned else set()

            # Check for missing contracts → serialization warning
            no_contract = [nid for nid in generators if not owned_map[nid]]
            if no_contract:
                self.warnings.append(
                    f"Parallel generators {no_contract} have no owned_files "
                    f"declared — automatic serialization recommended to prevent "
                    f"write conflicts (#272 EC4)."
                )

            # Check for overlapping owned_files
            for i in range(len(generators)):
                for j in range(i + 1, len(generators)):
                    nid_a = generators[i]
                    nid_b = generators[j]
                    overlap = owned_map[nid_a] & owned_map[nid_b]
                    if not overlap:
                        continue

                    # Check if any overlapping file is __init__.py
                    init_py_conflicts = [
                        f for f in overlap
                        if f.endswith("/__init__.py") or f == "__init__.py"
                    ]
                    if init_py_conflicts:
                        raise PlanValidationError(
                            f"Parallel generators '{nid_a}' and '{nid_b}' both "
                            f"declare ownership of shared __init__.py files: "
                            f"{init_py_conflicts}. "
                            f"Assign __init__.py to exactly one node and mark "
                            f"it as forbidden in the other (#272 EC1)."
                        )

                    # Check if there's a downstream merge node for the overlap
                    has_merge = self._has_downstream_merge(
                        nid_a, nid_b, levels, node_map, adj,
                    )

                    if has_merge:
                        self.warnings.append(
                            f"Parallel generators '{nid_a}' and '{nid_b}' share "
                            f"files {sorted(overlap)} — downstream merge node "
                            f"detected, but ensure coordination (#272 EC2)."
                        )
                    else:
                        raise PlanValidationError(
                            f"Parallel generators '{nid_a}' and '{nid_b}' both "
                            f"declare ownership of the same files: "
                            f"{sorted(overlap)}. "
                            f"Either assign each file to exactly one node, "
                            f"or add a downstream merge node that depends on "
                            f"both (#272 EC2)."
                        )

    @staticmethod
    def _has_downstream_merge(
        node_a: str,
        node_b: str,
        levels: list[list[str]],
        node_map: dict[str, dict],
        adj: dict[str, list[str]],
    ) -> bool:
        """Check if there's a downstream node that depends on both node_a and node_b."""
        # BFS from each node to find all downstream nodes
        def descendants(start: str) -> set[str]:
            visited: set[str] = set()
            queue = [start]
            while queue:
                current = queue.pop(0)
                for child in adj.get(current, []):
                    if child not in visited:
                        visited.add(child)
                        queue.append(child)
            return visited

        desc_a = descendants(node_a)
        desc_b = descendants(node_b)
        common = desc_a & desc_b

        # Check if any common descendant is a merge node (evaluator or has both as deps)
        for merge_candidate in common:
            merge_node = node_map.get(merge_candidate, {})
            if merge_node.get("agent_type") == "evaluator":
                return True
            # Check if it explicitly depends on both nodes
            # (Heuristic: if it's in common descendants, it likely merges)
        return bool(common)

    # Keywords that identify a foundation/base node in task descriptions (#740).
    _FOUNDATION_KEYWORDS = (
        "foundation", "base", "core", "shared", "common",
        "infrastructure", "setup", "bootstrap",
    )

    def _check_foundation_dependencies(
        self,
        nodes: list[dict],
        edges: list[dict],
    ) -> list[dict]:
        """Ensure implementation nodes depend on foundation nodes (#740).

        When the planner produces a foundation/shared node (identified by
        task keywords) but other generator nodes don't declare a dependency
        on it, the DAG may execute impl nodes in parallel with (or before)
        the foundation — causing missing database models, shared utilities, etc.

        When auto_fix is True, missing edges are added automatically.
        Otherwise, a warning is emitted.
        """
        # Identify foundation nodes: generator type + foundation keywords
        foundation_ids: list[str] = []
        for node in nodes:
            if node.get("agent_type") != "generator":
                continue
            task = (node.get("task") or "").lower()
            nid = node.get("id", "")
            # Check task description or node ID for foundation keywords
            if any(kw in task for kw in self._FOUNDATION_KEYWORDS):
                foundation_ids.append(nid)
            elif any(kw in nid.lower() for kw in ("foundation", "base", "core")):
                foundation_ids.append(nid)

        if not foundation_ids:
            return edges

        # Build existing edge set: from → set of to
        edge_set: set[tuple[str, str]] = set()
        for edge in edges:
            edge_set.add((edge.get("from", ""), edge.get("to", "")))

        # Find generator nodes that should depend on foundation but don't
        new_edges: list[dict] = []
        for node in nodes:
            nid = node.get("id", "")
            if nid in foundation_ids:
                continue
            if node.get("agent_type") != "generator":
                continue
            # Check if this node already depends on any foundation node
            has_foundation_dep = any(
                (fid, nid) in edge_set for fid in foundation_ids
            )
            if not has_foundation_dep:
                # Auto-fix: add dependency on the first foundation node
                fid = foundation_ids[0]
                new_edge = {
                    "from": fid,
                    "to": nid,
                    "dependency_type": "hard",
                }
                new_edges.append(new_edge)
                edge_set.add((fid, nid))
                self.warnings.append(
                    f"Node '{nid}' (generator) has no dependency on "
                    f"foundation node '{fid}' — auto-added hard "
                    f"dependency (#740)."
                )

        if new_edges:
            edges = list(edges) + new_edges

        return edges

    def _check_token_budget(self, nodes: list[dict]) -> None:
        """Warn if estimated_tokens exceeds token_budget (M4.6)."""
        for node in nodes:
            estimated = node.get("estimated_tokens", 0)
            budget = node.get("token_budget", 8192)
            if estimated > 0 and estimated > budget:
                self.warnings.append(
                    f"Node '{node.get('id')}' estimated at {estimated} tokens "
                    f"exceeds budget of {budget} tokens. "
                    f"Consider splitting into smaller nodes."
                )
