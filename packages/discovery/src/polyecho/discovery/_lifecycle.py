"""Discovery-local datasource-lifecycle adapter.

Why this exists
---------------
``polyecho.shared.datasource.get_datasource()`` returns one of two concrete
implementations selected by ``POLYECHO_DATASOURCE``:

* ``LiveDataSource`` — a **real async context manager**.  ``__aenter__``
  builds its ``httpx.AsyncClient``; calling any data method before that
  raises ``RuntimeError: LiveDataSource must be used as an async context
  manager``.  It MUST be entered via ``async with``.
* ``FixtureDataSource`` — has **no** I/O lifecycle and (verified
  empirically against the frozen shared package) defines *neither*
  ``__aenter__`` *nor* ``__aexit__``.  A bare ``async with
  FixtureDataSource()`` therefore raises ``TypeError`` ("not an async
  context manager").

The discovery entry-point previously did ``ds = get_datasource()`` and used
``ds`` directly — which worked for fixture but left ``LiveDataSource``
clientless, so every live run died with the ``RuntimeError`` above.  The
naive fix (``async with get_datasource() as ds``) trades one bug for
another: it would break the *fixture* path with a ``TypeError``.

This adapter is the minimal LOCAL fix (shared is frozen — not modified):
it enters/exits the datasource as an async context manager **iff** it
implements that protocol, and is otherwise a transparent no-op that yields
the datasource unchanged.  Net effect:

* live  → entered via ``async with`` (client built, then closed) — FIXED.
* fixture → yielded as-is, no ``__aenter__``/``__aexit__`` call — its
  behaviour is preserved byte-for-byte.

Both datasources are thus usable through one ``async with`` in
``__main__``.  The async-CM detection follows the CPython data-model
contract (``contextlib`` docs, verified via context7): an object is an
asynchronous context manager iff it provides both ``__aenter__`` and
``__aexit__`` (``contextlib.AbstractAsyncContextManager`` uses exactly
this ``__subclasshook__``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

logger = logging.getLogger(__name__)


def _is_async_context_manager(obj: object) -> bool:
    """True iff *obj* implements the async-context-manager protocol.

    Per the Python data model / ``contextlib`` contract an object is an
    async context manager iff it defines BOTH ``__aenter__`` and
    ``__aexit__`` (this is precisely
    ``contextlib.AbstractAsyncContextManager.__subclasshook__``).
    ``LiveDataSource`` satisfies this; ``FixtureDataSource`` does not (it
    has no I/O lifecycle).
    """
    return isinstance(obj, AbstractAsyncContextManager)


@asynccontextmanager
async def datasource_lifecycle[DS](datasource: DS) -> AsyncIterator[DS]:
    """Manage *datasource*'s async lifecycle when it has one; else no-op.

    Usage::

        async with datasource_lifecycle(get_datasource()) as ds:
            worker = DiscoveryWorker(ds)
            ...

    * ``LiveDataSource`` → ``__aenter__`` is awaited (httpx client built)
      on entry and ``__aexit__`` on exit (client closed) — the lifecycle
      bug fix.
    * ``FixtureDataSource`` → returned unchanged; no ``__aenter__`` /
      ``__aexit__`` is invoked, so its behaviour is identical to the
      pre-fix ``ds = get_datasource()`` path.

    The same value object is always yielded (live's ``__aenter__`` returns
    ``self``), so callers can rely on identity.
    """
    if _is_async_context_manager(datasource):
        logger.debug(
            "entering datasource as async context manager",
            extra={"datasource": type(datasource).__name__},
        )
        # ``datasource`` IS an async CM here (checked above).  Enter/exit
        # it via a separately-typed handle so the public ``DS`` type of
        # the yielded value is preserved (live's ``__aenter__`` returns
        # ``self`` → same object, same ``DS`` type).
        cm: AbstractAsyncContextManager[object] = datasource  # type: ignore[assignment]
        await cm.__aenter__()
        try:
            yield datasource
        except BaseException as exc:
            if not await cm.__aexit__(type(exc), exc, exc.__traceback__):
                raise
        else:
            await cm.__aexit__(None, None, None)
    else:
        logger.debug(
            "datasource has no async lifecycle; using as-is",
            extra={"datasource": type(datasource).__name__},
        )
        yield datasource
