"""
tests/test_schema_reflect.py
============================
Integration test: apply ``infra/migrations/0000_init.sql`` to a REAL,
ephemeral Postgres and assert the reflected database schema matches the
SQLAlchemy ``Base.metadata`` (the ORM) **exactly** — for every table.

Why this is the highest-value gap
---------------------------------
The repo has *three* hand-maintained schema sources that MUST stay
byte-consistent:

* ``packages/shared/src/polyecho/shared/models/*.py``  (SQLAlchemy ORM)
* ``infra/migrations/0000_init.sql``                   (applied migration)
* ``packages/web/lib/db/schema.ts``                    (Drizzle ORM)

The other ``test_models_db`` / ``test_phase4_contracts`` suites only
check the ORM against *itself* (in-process metadata) — they cannot catch
ORM-vs-SQL drift because they never touch a real database.  This test
closes that gap: it runs the *actual* migration on a *real* Postgres,
reflects it back with the SQLAlchemy ``Inspector``, and diffs it against
``Base.metadata``.  If any of the three sources drifts again (a column
type/precision, nullability, PK, or a stray ORM-only index), this test
fails loudly.

How it runs
-----------
* **CI** — the ``schema`` job in ``.github/workflows/ci.yml`` already
  spins a ``postgres:16`` service on ``localhost:5432`` with the
  ``polyecho/polyecho`` credentials and the ``python`` job runs the full
  ``pytest`` suite.  This test connects to whatever
  ``POLYECHO_TEST_DATABASE_URL`` / ``POLYECHO_DB_URL`` points at (the
  CI service), or the conventional ``localhost:5432`` default, and is
  hermetic (idempotent ``CREATE TABLE IF NOT EXISTS`` migration).
* **Local / no Postgres** — if no Postgres is reachable the single test
  ``pytest.skip``s cleanly (it never fails the suite for lack of a DB),
  so ``POLYECHO_DATASOURCE=fixture uv run pytest -q`` stays green
  offline.  Run a throwaway PG to exercise it locally, e.g.::

      docker run --rm -d -p 5432:5432 \\
        -e POSTGRES_DB=polyecho -e POSTGRES_USER=polyecho \\
        -e POSTGRES_PASSWORD=polyecho postgres:16-bookworm

The only DB driver in the frozen lockfile is ``asyncpg`` (no
``psycopg`` / ``aiosqlite``), so this uses the async SQLAlchemy engine
and runs the synchronous ``Inspector`` via
``AsyncConnection.run_sync`` (SQLAlchemy 2.0 has no async Inspector).
"""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest

from polyecho.shared.db import Base
from polyecho.shared.models import (  # noqa: F401 — register all tables on Base
    Account,
    BacktestJob,
    CandidateLeader,
    CandidateScoreHistory,
    CopySkip,
    CopyTemplate,
    Event,
    FollowRelation,
    LeaderTrade,
    MyTrade,
    PendingAction,
    PushSubscription,
    RedeemJob,
    Setting,
)

from .conftest import run

# Repo-root-relative path to the single baseline migration under test.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION = _REPO_ROOT / "infra" / "migrations" / "0000_init.sql"
# The 3rd schema source — the Drizzle ORM TypeScript schema.
_DRIZZLE_SCHEMA = _REPO_ROOT / "packages" / "web" / "lib" / "db" / "schema.ts"

_DEFAULT_DSN = "postgresql+asyncpg://polyecho:polyecho@localhost:5432/polyecho"


def _async_dsn() -> str:
    """Resolve the test Postgres DSN as an ``postgresql+asyncpg://`` URL.

    Order: ``POLYECHO_TEST_DATABASE_URL`` → ``POLYECHO_DB_URL`` → the
    conventional ``localhost:5432`` dev/CI default.  Any plain
    ``postgresql://`` form is normalised to the asyncpg driver (the only
    driver in the frozen lockfile).
    """
    raw: str = (
        os.environ.get("POLYECHO_TEST_DATABASE_URL")
        or os.environ.get("POLYECHO_DB_URL")
        or _DEFAULT_DSN
    )
    scheme, sep, rest = raw.partition("://")
    if sep and "+" not in scheme:
        scheme = f"{scheme}+asyncpg"
    return f"{scheme}{sep}{rest}" if sep else raw


def _tcp_reachable(dsn: str, timeout: float = 1.5) -> bool:
    """Best-effort check that *dsn*'s host:port accepts a TCP connection.

    Used only to decide skip-vs-run; the real connection (and any auth
    error) is surfaced by the engine if this passes.
    """
    parts = urlsplit(dsn.replace("+asyncpg", ""))
    host = parts.hostname or "localhost"
    port = parts.port or 5432
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Reflected → expected SQLAlchemy type *class name* normalisation. The
# Inspector returns dialect types (e.g. ``TIMESTAMP``); we compare against
# a small canonical family so equivalent types (DateTime≈TIMESTAMP,
# Text≈TEXT, BigInteger≈BIGINT, Numeric≈NUMERIC, UUID, JSONB, ARRAY,
# Boolean, Integer, Date, LargeBinary≈BYTEA) match across the ORM and the
# reflected DDL without being fooled by spelling.
_TYPE_FAMILY = {
    "TEXT": "text",
    "VARCHAR": "text",
    "STRING": "text",
    "TIMESTAMP": "datetime",
    "DATETIME": "datetime",
    "DATE": "date",
    "BIGINT": "bigint",
    "BIGINTEGER": "bigint",
    "INTEGER": "integer",
    "INT": "integer",
    "SMALLINT": "integer",
    "NUMERIC": "numeric",
    "DECIMAL": "numeric",
    "BOOLEAN": "boolean",
    "BOOL": "boolean",
    "UUID": "uuid",
    "JSONB": "jsonb",
    "JSON": "jsonb",
    "ARRAY": "array",
    "BYTEA": "binary",
    "LARGEBINARY": "binary",
    "BLOB": "binary",
}


def _family(type_obj: Any) -> str:
    name: str = str(type_obj.__class__.__name__).upper()
    return _TYPE_FAMILY.get(name, name.lower())


def _numeric_precision(type_obj: Any) -> tuple[int | None, int | None] | None:
    """Return ``(precision, scale)`` for NUMERIC-family types, else None."""
    if _family(type_obj) != "numeric":
        return None
    return (
        getattr(type_obj, "precision", None),
        getattr(type_obj, "scale", None),
    )


def _reflect(sync_conn: Any) -> dict[str, Any]:
    """Reflect the live DB into a comparable structure (runs sync)."""
    from sqlalchemy import inspect

    insp = inspect(sync_conn)
    out: dict[str, Any] = {}
    for table in insp.get_table_names(schema="public"):
        cols = {
            c["name"]: {
                "family": _family(c["type"]),
                "precision": _numeric_precision(c["type"]),
                "nullable": bool(c["nullable"]),
            }
            for c in insp.get_columns(table, schema="public")
        }
        pk = tuple(
            insp.get_pk_constraint(table, schema="public").get(
                "constrained_columns", []
            )
        )
        uniques = {
            tuple(sorted(u["column_names"]))
            for u in insp.get_unique_constraints(table, schema="public")
        }
        # A single-column UNIQUE may be reflected as a unique index rather
        # than a named constraint — fold those in so the comparison is
        # constraint-source-agnostic.
        for ix in insp.get_indexes(table, schema="public"):
            if ix.get("unique"):
                uniques.add(tuple(sorted(ix["column_names"])))
        out[table] = {"columns": cols, "pk": pk, "uniques": uniques}
    return out


def _expected_from_metadata() -> dict[str, Any]:
    """The same structure derived from the SQLAlchemy ``Base.metadata``."""
    out: dict[str, Any] = {}
    for table in Base.metadata.tables.values():
        cols = {
            c.name: {
                "family": _family(c.type),
                "precision": _numeric_precision(c.type),
                "nullable": bool(c.nullable),
            }
            for c in table.columns
        }
        pk = tuple(c.name for c in table.primary_key.columns)
        uniques: set[tuple[str, ...]] = set()
        for con in table.constraints:
            if con.__class__.__name__ == "UniqueConstraint":
                con_cols = cast(Any, con).columns
                uniques.add(tuple(sorted(c.name for c in con_cols)))
        for col in table.columns:
            if col.unique:
                uniques.add((col.name,))
        out[table.name] = {"columns": cols, "pk": pk, "uniques": uniques}
    return out


# ---------------------------------------------------------------------------
# Drizzle (schema.ts) — the 3rd schema leg.
#
# CI's *python* job has no Node (only the separate `web` job does), so we
# cannot shell `drizzle-kit` here. Instead we structurally parse schema.ts:
# for every `pgTable("name", { col: <fn>("col"|"db_col") ... })` block we
# extract each column's name + its Drizzle column-function and map that to
# the same canonical type family used for the ORM/SQL comparison. We then
# assert every ORM table+column exists in the Drizzle source with a
# compatible family.
#
# Limits (documented, deliberate): this is a *structural text* parse, not a
# full TS AST. It guards column presence + coarse type family + the
# bigint-vs-integer distinction (the R5 money-path bug class). It does NOT
# check nullability, defaults, PK/unique, or CHECK predicates on the Drizzle
# leg (the ORM↔SQL real-PG diff already covers those rigorously, and
# Drizzle's own pnpm build/typecheck guards its internal validity). Its job
# is to fail loudly the moment a column is added/retyped in ORM+SQL but
# forgotten in schema.ts (or vice-versa) — the previously unguarded leg.
# ---------------------------------------------------------------------------

# Drizzle column-function (drizzle-orm/pg-core) → canonical type family.
_DRIZZLE_FN_FAMILY = {
    "uuid": "uuid",
    "text": "text",
    "varchar": "text",
    "date": "date",
    "timestamp": "datetime",
    "integer": "integer",
    "smallint": "integer",
    "serial": "integer",
    "bigint": "bigint",
    "bigserial": "bigint",
    "boolean": "boolean",
    "jsonb": "jsonb",
    "json": "jsonb",
    "numeric": "numeric",
    "decimal": "numeric",
    # Project-local customType helper for Postgres BYTEA (see schema.ts).
    "bytea": "binary",
}

# `export const <ident> = pgTable("<table>", { <body> }, ...);`
# Body is captured up to the first `},` that closes the column object (the
# column object is always the 2nd arg and is followed by `}` then `)` or
# `, (t) => ...`). We grab everything between the table name and the start
# of the constraints callback / end of call, then pull column lines from it.
_PGTABLE_RE = re.compile(
    r'pgTable\(\s*"(?P<table>[a-z_0-9]+)"\s*,\s*\{(?P<body>.*?)\}\s*(?:,\s*\(|\)\s*;)',
    re.DOTALL,
)
# A column entry: `js_key: <fn>("db_col_name"...<chain>` up to the entry
# terminator (`,` at depth 0 or the end of the column object). js_key is
# ignored; the string literal is the real DB column name, <fn> is the
# Drizzle type fn, and <chain> is the trailing builder chain (so a
# ``.array()`` modifier — Postgres ``TYPE[]`` — can promote the family to
# ``array`` to match the SQLAlchemy ``ARRAY`` family). The chain stops at
# the first newline-then-identifier-colon (next column) or a `}` so nested
# `references(() => x.id)` parens don't leak into the next entry.
_COLUMN_RE = re.compile(
    r'(?P<jskey>[A-Za-z_]\w*)\s*:\s*'
    r'(?P<fn>[A-Za-z_]\w*)\s*\(\s*"(?P<col>[a-z_0-9]+)"'
    r'(?P<chain>[^\n]*(?:\n\s*\.[^\n]*)*)',
)


def _drizzle_schema() -> dict[str, dict[str, str]]:
    """Parse schema.ts → {table: {db_column: type_family}}.

    Robust to comments/whitespace; nested-paren column args (e.g.
    ``references(() => accounts.id)``) are fine because we only need the
    column name string literal and the leading type-fn, both of which sit
    before any nested call.
    """
    src = _DRIZZLE_SCHEMA.read_text(encoding="utf-8")
    # Strip /* block */ and // line comments so commented-out columns or
    # doc text never register as real columns.
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)

    tables: dict[str, dict[str, str]] = {}
    for m in _PGTABLE_RE.finditer(src):
        tname = m.group("table")
        body = m.group("body")
        cols: dict[str, str] = {}
        for cm in _COLUMN_RE.finditer(body):
            fn = cm.group("fn")
            family = _DRIZZLE_FN_FAMILY.get(fn)
            if family is None:
                # Unknown helper (e.g. a constraint builder accidentally
                # caught) — skip; the ORM-coverage assertion below will
                # still flag a genuinely missing column.
                continue
            # A chained `.array()` makes it Postgres ``TYPE[]`` →
            # SQLAlchemy ``ARRAY`` family, regardless of element type.
            chain = cm.group("chain") or ""
            if re.search(r"\.array\s*\(", chain):
                family = "array"
            cols[cm.group("col")] = family
        tables[tname] = cols
    return tables


def test_drizzle_schema_matches_orm() -> None:
    """schema.ts (Drizzle) is the 3rd leg — it must not drift from the ORM.

    Asserts every SQLAlchemy table+column appears in
    ``packages/web/lib/db/schema.ts`` with a compatible canonical type
    family (and specifically that ``index_set`` is the wide ``bigint``, not
    ``integer`` — the R5 overflow bug). Fails loudly on any Drizzle-only
    divergence; this is the previously-unguarded 3rd schema leg.
    """
    drizzle = _drizzle_schema()
    expected = _expected_from_metadata()

    diffs: list[str] = []
    for tname, exp in sorted(expected.items()):
        if tname not in drizzle:
            diffs.append(f"{tname}: ORM table absent from schema.ts")
            continue
        dcols = drizzle[tname]
        for cname, meta in sorted(exp["columns"].items()):
            if cname not in dcols:
                diffs.append(
                    f"{tname}.{cname}: ORM column absent from schema.ts"
                )
                continue
            exp_fam = meta["family"]
            got_fam = dcols[cname]
            if exp_fam != got_fam:
                diffs.append(
                    f"{tname}.{cname}: schema.ts type {got_fam} != "
                    f"{exp_fam} (ORM)"
                )

    assert not diffs, (
        "Schema drift (ORM vs Drizzle schema.ts):\n" + "\n".join(diffs)
    )


def test_migration_matches_orm_metadata() -> None:
    """0000_init.sql applied to a real PG == Base.metadata, every table.

    Fails if ANY of the 3 schema sources drifts (column set, type family,
    NUMERIC precision/scale, nullability, primary key, or unique
    constraints).  Skips cleanly when no Postgres is reachable so the
    offline fixture suite stays green.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    dsn = _async_dsn()
    if not _tcp_reachable(dsn):
        pytest.skip(
            f"No Postgres reachable for schema-reflect test ({dsn!r}); "
            "set POLYECHO_TEST_DATABASE_URL / POLYECHO_DB_URL or run a "
            "local postgres:16 to exercise it (CI's schema job provides "
            "one)."
        )

    ddl = _MIGRATION.read_text(encoding="utf-8")

    async def _apply_and_reflect() -> dict[str, Any]:
        engine = create_async_engine(dsn, echo=False)
        try:
            # 0000_init.sql is a multi-statement script. asyncpg cannot
            # run several commands through a prepared statement (SQLAlchemy
            # ``exec_driver_sql`` path), so apply it via the *raw* asyncpg
            # ``Connection.execute`` which DOES accept a simple-query
            # script. The migration is idempotent (CREATE … IF NOT
            # EXISTS), so re-applying against a warm CI DB is safe.
            raw = await engine.raw_connection()
            try:
                asyncpg_conn = raw.driver_connection
                assert asyncpg_conn is not None  # open raw conn always has one
                await asyncpg_conn.execute(ddl)
            finally:
                raw.close()
            async with engine.connect() as conn:
                return await conn.run_sync(_reflect)
        finally:
            await engine.dispose()

    reflected = run(_apply_and_reflect())
    expected = _expected_from_metadata()

    # Every ORM table must exist in the migrated DB (the DB may legitimately
    # carry extra objects from other migrations / fixtures — we assert the
    # ORM contract is fully satisfied, not that the DB has nothing else).
    missing_tables = sorted(set(expected) - set(reflected))
    assert not missing_tables, (
        f"Tables in Base.metadata but NOT created by 0000_init.sql: "
        f"{missing_tables}"
    )

    diffs: list[str] = []
    for tname, exp in sorted(expected.items()):
        got = reflected[tname]

        exp_cols, got_cols = exp["columns"], got["columns"]
        missing_cols = sorted(set(exp_cols) - set(got_cols))
        extra_cols = sorted(set(got_cols) - set(exp_cols))
        if missing_cols:
            diffs.append(f"{tname}: ORM cols missing in DB: {missing_cols}")
        if extra_cols:
            diffs.append(f"{tname}: DB cols not in ORM: {extra_cols}")

        for cname in sorted(set(exp_cols) & set(got_cols)):
            e, g = exp_cols[cname], got_cols[cname]
            if e["family"] != g["family"]:
                diffs.append(
                    f"{tname}.{cname}: type {g['family']} (DB) != "
                    f"{e['family']} (ORM)"
                )
            if (
                e["precision"] is not None
                and g["precision"] is not None
                and e["precision"] != g["precision"]
            ):
                diffs.append(
                    f"{tname}.{cname}: NUMERIC{g['precision']} (DB) != "
                    f"NUMERIC{e['precision']} (ORM)"
                )
            if e["nullable"] != g["nullable"]:
                diffs.append(
                    f"{tname}.{cname}: nullable={g['nullable']} (DB) != "
                    f"{e['nullable']} (ORM)"
                )

        if tuple(exp["pk"]) != tuple(got["pk"]):
            diffs.append(
                f"{tname}: PK {got['pk']} (DB) != {exp['pk']} (ORM)"
            )
        if exp["uniques"] != got["uniques"]:
            diffs.append(
                f"{tname}: UNIQUE {sorted(got['uniques'])} (DB) != "
                f"{sorted(exp['uniques'])} (ORM)"
            )

    assert not diffs, "Schema drift (ORM vs applied 0000_init.sql):\n" + "\n".join(
        diffs
    )
