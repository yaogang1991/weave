"""Tests for WASM runtime PoC (#507 P0)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.wasm import (
    WasmBackend,
    WasmRuntimeError,
    WasmSandbox,
    build_wasm_status_report,
    check_wasm_available,
    detect_runtime,
)


class TestDetectRuntime:
    def test_returns_none_when_no_runtime(self):
        with patch("builtins.__import__", side_effect=ImportError):
            result = detect_runtime()
            assert result is None

    def test_returns_wasmtime_when_available(self):
        with patch("builtins.__import__", return_value=MagicMock()):
            result = detect_runtime()
            assert result == "wasmtime"

    def test_falls_back_to_wasmer(self):
        original_import = __import__

        def mock_import(name, *args, **kwargs):
            if name == "wasmtime":
                raise ImportError("no wasmtime")
            if name == "wasmer":
                return MagicMock()
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = detect_runtime()
            assert result == "wasmer"


class TestCheckWasmAvailable:
    def test_not_available(self):
        with patch("backend.wasm.detect_runtime", return_value=None):
            info = check_wasm_available()
            assert info["available"] is False
            assert info["runtime"] is None
            assert info["wasi_support"] is False

    def test_available_with_wasmtime(self):
        mock_wasmtime = MagicMock()
        mock_wasmtime.WasiConfig = MagicMock()
        with patch("backend.wasm.detect_runtime", return_value="wasmtime"), \
             patch("builtins.__import__", return_value=mock_wasmtime):
            info = check_wasm_available()
            assert info["available"] is True
            assert info["runtime"] == "wasmtime"

    def test_available_without_wasi(self):
        with patch("backend.wasm.detect_runtime", return_value="wasmer"):
            info = check_wasm_available()
            assert info["available"] is True
            assert info["runtime"] == "wasmer"
            assert info["wasi_support"] is False


class TestWasmSandbox:
    def test_not_available_without_runtime(self):
        sandbox = WasmSandbox(runtime=None)
        assert sandbox.is_available() is False

    def test_available_with_runtime(self):
        sandbox = WasmSandbox(runtime="wasmtime")
        assert sandbox.is_available() is True

    def test_run_command_returns_error(self):
        """WASM sandbox does not support shell commands."""
        sandbox = WasmSandbox(runtime="wasmtime")
        import asyncio
        result = asyncio.run(sandbox.run_command("echo hi", "/tmp"))
        assert result.success is False
        assert "does not support shell commands" in result.stderr

    def test_run_wasm_missing_file(self):
        sandbox = WasmSandbox(runtime="wasmtime")
        import asyncio
        result = asyncio.run(sandbox.run_wasm("/nonexistent.wasm"))
        assert result.success is False
        assert "not found" in result.stderr

    def test_run_wasm_no_runtime(self):
        sandbox = WasmSandbox(runtime=None)
        import asyncio
        result = asyncio.run(sandbox.run_wasm("/fake.wasm"))
        assert result.success is False
        assert "No WASM runtime" in result.stderr


class TestWasmBackend:
    def test_not_available(self):
        backend = WasmBackend()
        assert backend.is_available() is False

    def test_setup_raises(self):
        backend = WasmBackend()
        with pytest.raises(NotImplementedError):
            backend.setup("j1", "r1")

    def test_cleanup_raises(self):
        backend = WasmBackend()
        with pytest.raises(NotImplementedError):
            backend.cleanup("j1", "r1")

    def test_preserve_raises(self):
        backend = WasmBackend()
        with pytest.raises(NotImplementedError):
            backend.preserve("j1", "r1")

    def test_get_work_dir_raises(self):
        backend = WasmBackend()
        with pytest.raises(NotImplementedError):
            backend.get_work_dir("j1", "r1")


class TestBuildWasmStatusReport:
    def test_report_structure(self):
        with patch("backend.wasm.detect_runtime", return_value=None):
            report = build_wasm_status_report()
            assert "available" in report
            assert "runtime" in report
            assert "wasi_support" in report
            assert "sandbox_ready" in report
            assert "backend_ready" in report

    def test_report_unavailable(self):
        with patch("backend.wasm.detect_runtime", return_value=None):
            report = build_wasm_status_report()
            assert report["available"] is False
            assert report["backend_ready"] is False

    def test_report_available(self):
        with patch("backend.wasm.detect_runtime", return_value="wasmtime"):
            report = build_wasm_status_report()
            assert report["available"] is True
            assert report["sandbox_ready"] is True


class TestWasmRuntimeError:
    def test_exception_type(self):
        err = WasmRuntimeError("test error")
        assert str(err) == "test error"
        assert isinstance(err, Exception)
