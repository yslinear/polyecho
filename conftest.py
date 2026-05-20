"""Root pytest conftest — shared, order-independent test isolation.

Why this exists
---------------
Several synchronous tests (``packages/shared/tests/test_datasource.py``,
``packages/discovery/tests/test_discovery.py``) drive async datasource /
worker code through a small ``asyncio.get_event_loop().run_until_complete(...)``
helper.

Under ``pytest-asyncio`` (>= 1.x) with ``asyncio_mode = "auto"``, the event
loop is created and **torn down around each async test**. On Python 3.12
``asyncio.get_event_loop()`` raises ``RuntimeError: There is no current event
loop`` once no loop is installed on the thread. So those *sync* helper tests
pass in isolation but fail in the full collection whenever they happen to run
after an async test has closed the loop — a pure test-ordering/isolation bug,
not a product defect.

The fixture below restores the invariant the helpers depend on: every test
starts with a fresh, open event loop installed on the current thread, and the
loop is closed afterwards. This is applied repo-wide (autouse) so the full
``pytest`` collection is order-independent without weakening any assertion,
skipping, or xfailing tests. Async tests are unaffected: ``pytest-asyncio``
installs and manages its own loop for the duration of each async test, and
this fixture simply guarantees a clean baseline around it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest

# ---------------------------------------------------------------------------
# P1-4 fail-fast env default for the WHOLE test suite.
#
# ``polyecho.shared.datasource.factory.get_datasource()`` now RAISES
# ``RuntimeError`` when ``POLYECHO_DATASOURCE`` is unset (no silent
# fixture-fallback in production).  The pytest collections across all
# packages (shared, discovery, core, backtest, push, cli) routinely call
# ``get_datasource()`` without an explicit settings argument, so we declare
# the explicit fixture opt-in here exactly once at import time.
#
# Individual tests that assert the unset-env-raises contract still
# ``monkeypatch.delenv("POLYECHO_DATASOURCE", raising=False)`` locally; this
# only provides a default when the variable was not previously set.
# ---------------------------------------------------------------------------
os.environ.setdefault("POLYECHO_DATASOURCE", "fixture")


@pytest.fixture(autouse=True)
def _isolated_event_loop() -> Iterator[None]:
    """Give every test a fresh thread event loop; tear it down afterwards."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield
    finally:
        # Close the loop this fixture created, then install a brand-new one
        # so the thread always has a current, usable loop for the next test
        # (its sync helpers call asyncio.get_event_loop()).
        if not loop.is_closed():
            loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())
