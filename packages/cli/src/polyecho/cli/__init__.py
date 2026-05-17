"""
polyecho.cli
============
PolyEcho command-line interface.

Console entry point: ``polyecho``  (see pyproject.toml ``[project.scripts]``).

Sub-command groups
------------------
* ``account``   — detect / import / list / balance
* ``discovery`` — run / schedule / promote
* ``backtest``  — new / list / show
* ``trader``    — start / status / pause-all / pause
* ``db``        — migrate / backup / reconcile
* ``logs``      — <service>
* ``doctor``    — Phase 0 health check (fully implemented)
"""

__version__ = "0.0.0"
