"""
polyecho.cli.__main__
=====================
Root Typer application.  All sub-apps are mounted here.

Entry point (pyproject.toml)::

    polyecho = "polyecho.cli.__main__:app"

Usage::

    python -m polyecho.cli          # via module
    polyecho                        # via installed console script
"""

from __future__ import annotations

import typer

from polyecho.cli.account import app as account_app
from polyecho.cli.alerts import app as alerts_app
from polyecho.cli.backtest import app as backtest_app
from polyecho.cli.db import app as db_app
from polyecho.cli.discovery import app as discovery_app
from polyecho.cli.doctor import doctor
from polyecho.cli.logs import app as logs_app
from polyecho.cli.push import app as push_app
from polyecho.cli.trader import app as trader_app

app = typer.Typer(
    name="polyecho",
    help="PolyEcho — Polymarket copy-trading system CLI.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

# Mount sub-command groups
app.add_typer(account_app, name="account", help="Manage PolyEcho trading accounts.")
app.add_typer(discovery_app, name="discovery", help="Leader candidate pool discovery.")
app.add_typer(backtest_app, name="backtest", help="Backtest engine.")
app.add_typer(
    trader_app,
    name="trader",
    help="Trader-core controls (DB-driven; never holds keys).",
)
app.add_typer(db_app, name="db", help="Database operations.")
app.add_typer(logs_app, name="logs", help="View service logs.")
app.add_typer(
    push_app,
    name="push",
    help="Web-push operator tools (VAPID keygen).",
)
app.add_typer(
    alerts_app,
    name="alerts",
    help="View recent push events (the events log).",
)

# Top-level commands
app.command(name="doctor")(doctor)

if __name__ == "__main__":
    app()
