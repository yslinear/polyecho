"""
packages/shared/tests/conftest.py
=================================
Shared-package test fixtures.

Scope
-----
This conftest is intentionally **package-local**.  The repo-root
``conftest.py`` already guarantees every test starts with a fresh thread
event loop (so the ``asyncio.get_event_loop().run_until_complete(...)``
helper used by sync tests is order-independent on Python 3.12).  Here we add:

* ``_clear_settings_cache`` (autouse): clears the ``functools.lru_cache`` on
  :func:`polyecho.shared.config.get_settings` *before and after* every test
  so environment-variable monkeypatching is never leaked between tests and
  the whole suite stays order-independent.
* ``run``: tiny sync→async bridge mirroring the helper already used in
  ``test_datasource.py`` (kept here so new test modules can import it).

No network, no Postgres: ``db.session_scope`` is exercised against a
mocked async session (``aiosqlite`` is intentionally not in the frozen
lockfile), ``models`` metadata is checked via SQLAlchemy introspection +
a synchronous stdlib-``sqlite3`` engine, and ``cryptography`` runs fully
in-process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Ensure ``get_settings()`` is never cached across tests.

    Without this, a test that sets ``POLYECHO_*`` env vars and calls
    ``get_settings()`` would poison every later test that reads settings
    (config, crypto, datasource factory), making the suite order-dependent.
    """
    from polyecho.shared.config import get_settings

    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def run(coro: Any) -> Any:
    """Drive *coro* to completion on the current thread's event loop.

    The repo-root ``_isolated_event_loop`` autouse fixture guarantees a
    fresh, open loop is installed on the thread for every test, so this is
    safe and order-independent on Python 3.12.
    """
    return asyncio.get_event_loop().run_until_complete(coro)
