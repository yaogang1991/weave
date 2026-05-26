"""Optional Langfuse SDK integration (#941).

Provides a lightweight OTel SpanProcessor that exports traces to Langfuse
for session replay, prompt management, and cost attribution.

Requires: pip install langfuse (optional dependency)

Usage in setup_telemetry or manually::

    from monitoring.langfuse_exporter import setup_langfuse
    setup_langfuse(public_key="pk-xxx", secret_key="sk-xxx", host="http://localhost:3000")
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def setup_langfuse(
    public_key: str | None = None,
    secret_key: str | None = None,
    host: str | None = None,
) -> bool:
    """Configure Langfuse SDK as an additional trace exporter (#941).

    Reads credentials from environment variables if not provided:
    - LANGFUSE_PUBLIC_KEY
    - LANGFUSE_SECRET_KEY
    - LANGFUSE_HOST (default: https://cloud.langfuse.com)

    Returns True if setup succeeded, False if langfuse not installed.
    """
    try:
        from langfuse import Langfuse
    except ImportError:
        logger.info(
            "langfuse not installed — LLMOps integration disabled (#941). "
            "Install with: pip install langfuse"
        )
        return False

    pk = public_key or os.getenv("LANGFUSE_PUBLIC_KEY")
    sk = secret_key or os.getenv("LANGFUSE_SECRET_KEY")
    lh = host or os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

    if not pk or not sk:
        logger.warning(
            "Langfuse credentials not configured (#941). "
            "Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY env vars."
        )
        return False

    try:
        _langfuse = Langfuse(public_key=pk, secret_key=sk, host=lh)
        _langfuse.auth_check()
        logger.info("Langfuse SDK connected to %s (#941)", lh)
        return True
    except Exception as e:
        logger.warning("Langfuse connection failed: %s (#941)", e)
        return False
