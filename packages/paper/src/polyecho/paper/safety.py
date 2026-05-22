"""Money-safety boundary for the paper-trading worker (C1, non-negotiable).

The paper worker is a *validation* layer.  It simulates copy-trades and
advances the lifecycle ladder; it must NEVER cross into real money.  The
roadmap states the boundary plainly: the ladder never inserts a
``follow_relations`` row and never sets ``enabled=true``.  ``trial_ready``
is advisory only; the sole path to a real follow is an explicit operator
action gated by the danger-passphrase confirm.

This module makes that boundary an *enforced assertion* rather than a
convention.  :class:`PaperSafetyViolation` is raised if any paper-worker
code path is ever pointed at a real-money write, and :func:`assert_no_real_money_write`
is called by the worker before it commits each cycle's session.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Table / state names the paper worker is categorically forbidden to write.
#: ``follow_relations`` is the real-money copy-trade link; ``enabled`` is the
#: column that activates a follow.  The paper worker touches neither.
FORBIDDEN_TABLES: frozenset[str] = frozenset({"follow_relations"})


class PaperSafetyViolation(RuntimeError):
    """Raised when paper-worker code attempts a real-money write.

    A hard, never-suppressed error: a paper worker that somehow reached a
    ``follow_relations`` insert (or an ``enabled=true`` set) is a
    money-safety breach and MUST crash the cycle rather than silently
    activate a real follow.
    """


def assert_no_real_money_write(table_names: Iterable[object]) -> None:
    """Assert that none of ``table_names`` is a forbidden real-money table.

    Called by the paper worker before every commit: it inspects the set of
    ORM table names the pending transaction is about to write and raises
    :class:`PaperSafetyViolation` if ``follow_relations`` is among them.
    This is the enforced money-safety guard -- the paper worker can never
    insert a follow / set ``enabled=true``, even via a future code change,
    without tripping this assertion.

    Args:
        table_names: Any iterable of table-name strings (e.g. the
            ``__tablename__`` of each model in ``session.new`` /
            ``session.dirty``).

    Raises:
        PaperSafetyViolation: If a forbidden table is present.
    """
    names = {str(n) for n in table_names}
    breach = names & FORBIDDEN_TABLES
    if breach:
        raise PaperSafetyViolation(
            "paper-trading worker attempted to write a real-money table "
            f"{sorted(breach)}; the paper ladder is advisory-only and must "
            "NEVER insert follow_relations or set enabled=true. The only "
            "path to a real follow is an explicit operator action."
        )


def guarded_session_tables(session: object) -> set[str]:
    """Return the set of ``__tablename__`` values a session is about to write.

    Reads the SQLAlchemy ``Session.new`` (pending inserts) and
    ``Session.dirty`` (pending updates) collections and collects each
    mapped object's ``__tablename__``.  The paper worker passes the result
    to :func:`assert_no_real_money_write` right before it commits.

    Defensive: any attribute the session does not expose is skipped so a
    mocked / partial session in a test never crashes the guard itself.

    Args:
        session: A SQLAlchemy ``AsyncSession`` (or any object exposing
            ``new`` / ``dirty`` iterables of mapped instances).

    Returns:
        The set of table names with a pending write.
    """
    tables: set[str] = set()
    for attr in ("new", "dirty"):
        collection = getattr(session, attr, None)
        if collection is None:
            continue
        try:
            for obj in collection:
                name = getattr(type(obj), "__tablename__", None)
                if name is not None:
                    tables.add(str(name))
        except TypeError:  # pragma: no cover - defensive
            continue
    return tables
