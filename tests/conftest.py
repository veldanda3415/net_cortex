"""Suite-wide fixtures."""

from __future__ import annotations

import pytest_asyncio

from providers.adapters.mcp_adapter import close_all_sessions


@pytest_asyncio.fixture(autouse=True)
async def _close_telemetry_sessions():
    """Telemetry endpoint sessions are long-lived by design; close them before
    each test's event loop is torn down so no owner task is left pending."""
    yield
    await close_all_sessions()
