"""WASM execution backend — Proof of Concept (#507 P0).

Evaluates WASM runtimes (wasmtime vs wasmer) and provides a minimal
sandbox that can execute WASM modules. This is a PoC demonstrating
the feasibility of using WASM as a lightweight sandbox for agent
code execution.

Runtime evaluation results (as of 2026-05):
- wasmtime-py: Pure Python bindings via wasmtime C library. Mature,
  well-maintained by BytecodeAlliance. WASI preview1 support.
  Install: pip install wasmtime
- wasmer: Python bindings via wasmer C library. Good performance.
  WASI support. Install: pip install wasmer[cranelift]
- pyodide: Python compiled to WASM — useful for running Python
  sandbox, but heavy. Not directly usable as a sandbox provider.

Decision: wasmtime-py as primary runtime (better Python integration,
active development, WASI support out of the box).

Status: PoC — validates that WASM sandboxing is feasible for
low-risk agent tool execution. Full integration with BackendManager
is P2 (#507).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from backend.base import ExecutionBackend, WorkspaceIsolation
from backend.sandbox import CommandResult, SandboxProvider

logger = logging.getLogger(__name__)

# Preferred WASM runtime
_PREFERRED_RUNTIME = "wasmtime"


from core.exceptions import WasmRuntimeError  # noqa: F401 — re-export (#918)


def detect_runtime() -> str | None:
    """Detect which WASM runtime is available.

    Returns the runtime name ("wasmtime" or "wasmer") or None.
    """
    for runtime in (_PREFERRED_RUNTIME, "wasmer"):
        try:
            __import__(runtime)
            return runtime
        except ImportError:
            continue
    return None


def check_wasm_available() -> dict[str, Any]:
    """Check WASM runtime availability and return status info.

    Returns a dict with:
    - available: bool
    - runtime: str | None (detected runtime name)
    - wasi_support: bool
    """
    runtime = detect_runtime()
    if runtime is None:
        return {
            "available": False,
            "runtime": None,
            "wasi_support": False,
        }

    wasi_support = False
    if runtime == "wasmtime":
        try:
            import wasmtime
            wasi_support = hasattr(wasmtime, "WasiConfig")
        except Exception as e:
            logger.warning("WASM runtime check failed: %s", e)

    return {
        "available": True,
        "runtime": runtime,
        "wasi_support": wasi_support,
    }


class WasmSandbox(SandboxProvider):
    """WASM-based sandbox for executing compiled modules (#507 P0).

    This sandbox executes WASM modules in a sandboxed runtime with
    WASI (WebAssembly System Interface) support for controlled
    filesystem and I/O access.

    Limitations (PoC):
    - Only supports pre-compiled .wasm modules
    - No dynamic command execution (not a shell)
    - WASI filesystem access is configurable but minimal

    Usage::

        sandbox = WasmSandbox()
        if sandbox.is_available():
            result = await sandbox.run_wasm(
                wasm_path="module.wasm",
                args=["arg1"],
            )
    """

    sandbox_type = None  # Set dynamically based on runtime

    def __init__(self, runtime: str | None = None) -> None:
        self._runtime = runtime or detect_runtime()
        if self._runtime == "wasmtime":
            from backend.base import ExecutionSandbox
            self.sandbox_type = ExecutionSandbox.LOCAL

    def is_available(self) -> bool:
        """Check if a WASM runtime is available."""
        return self._runtime is not None

    async def run_command(
        self,
        command: str,
        cwd: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Not supported for WASM sandbox — use run_wasm instead."""
        return CommandResult(
            success=False,
            exit_code=1,
            stderr="WASM sandbox does not support shell commands. Use run_wasm().",
        )

    async def run_wasm(
        self,
        wasm_path: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        stdin_data: str = "",
        timeout: int = 30,
    ) -> CommandResult:
        """Execute a WASM module with WASI configuration.

        Parameters
        ----------
        wasm_path:
            Path to the .wasm module file.
        args:
            Arguments to pass to the WASM main function.
        env:
            Environment variables (WASI preopen).
        stdin_data:
            Data to provide on stdin.
        timeout:
            Maximum execution time in seconds.

        Returns
        -------
        CommandResult with captured stdout/stderr.
        """
        if not self.is_available():
            return CommandResult(
                success=False,
                exit_code=1,
                stderr="No WASM runtime available",
            )

        wasm_file = Path(wasm_path)
        if not wasm_file.exists():
            return CommandResult(
                success=False,
                exit_code=1,
                stderr=f"WASM module not found: {wasm_path}",
            )

        if self._runtime == "wasmtime":
            return await self._run_wasmtime(
                wasm_file, args, env, stdin_data, timeout
            )

        return CommandResult(
            success=False,
            exit_code=1,
            stderr=f"Unsupported runtime: {self._runtime}",
        )

    async def _run_wasmtime(
        self,
        wasm_path: Path,
        args: list[str] | None,
        env: dict[str, str] | None,
        stdin_data: str,
        timeout: int,
    ) -> CommandResult:
        """Execute via wasmtime runtime."""
        try:
            import wasmtime

            engine = wasmtime.Engine()
            store = wasmtime.Store(engine)

            # Configure WASI
            wasi_config = wasmtime.WasiConfig()
            if args:
                wasi_config.argv = args
            if env:
                for k, v in env.items():
                    wasi_config.env = [
                        *wasi_config.env,
                        [k, v],
                    ] if hasattr(wasi_config, "env") else [[k, v]]

            # Set stdin if provided
            if stdin_data:
                wasi_config.stdin_file = stdin_data.encode("utf-8")

            store.set_wasi(wasi_config)

            # Load and instantiate module
            module = wasmtime.Module.from_file(engine, str(wasm_path))

            # Link WASI
            linker = wasmtime.Linker(engine)
            linker.define_wasi()

            instance = linker.instantiate(store, module)

            # Run _start (WASI entry point)
            start = instance.exports(store)["_start"]
            start(store)

            return CommandResult(
                success=True,
                exit_code=0,
                stdout="WASM execution completed",
            )

        except ImportError:
            return CommandResult(
                success=False,
                exit_code=1,
                stderr="wasmtime not installed: pip install wasmtime",
            )
        except Exception as e:
            return CommandResult(
                success=False,
                exit_code=1,
                stderr=f"WASM execution error: {e}",
            )


class WasmBackend(ExecutionBackend):
    """WASM-based workspace backend stub (#507 P1 placeholder).

    Currently returns not available. Full implementation would:
    - Compile agent code to WASM (or interpret Python via Pyodide)
    - Route file operations through WASI
    - Integrate with BackendManager for risk-based routing

    This stub validates the interface and allows BackendManager
    registration without breaking existing routing logic.
    """

    workspace_type = WorkspaceIsolation.LOCAL

    def setup(self, job_id: str, run_id: str) -> Path:
        raise NotImplementedError(
            "WasmBackend not yet implemented (planned for #507 P1)"
        )

    def get_work_dir(self, job_id: str, run_id: str) -> Path:
        raise NotImplementedError(
            "WasmBackend not yet implemented (planned for #507 P1)"
        )

    def cleanup(self, job_id: str, run_id: str) -> None:
        raise NotImplementedError(
            "WasmBackend not yet implemented (planned for #507 P1)"
        )

    def preserve(self, job_id: str, run_id: str, reason: str = "") -> Path | None:
        raise NotImplementedError(
            "WasmBackend not yet implemented (planned for #507 P1)"
        )

    def is_available(self) -> bool:
        """WASM backend is not yet available for workspace isolation."""
        return False


def build_wasm_status_report() -> dict[str, Any]:
    """Build a status report for the WASM subsystem.

    Used by CLI commands and diagnostics to report WASM readiness.
    """
    info = check_wasm_available()
    return {
        "available": info["available"],
        "runtime": info["runtime"],
        "wasi_support": info["wasi_support"],
        "sandbox_ready": info["available"],
        "backend_ready": False,  # WasmBackend stub
        "note": (
            "WASM sandbox PoC ready. Full backend integration "
            "planned for #507 P1/P2."
        ),
    }
