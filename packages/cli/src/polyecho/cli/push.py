"""
polyecho.cli.push
=================
Web-push operator sub-commands (Phase-5 Wave-2).

Commands
--------
* ``polyecho push vapid-keygen`` — generate a fresh VAPID (RFC 8292)
  keypair and print the ``POLYECHO_VAPID_*`` env lines the operator pastes
  into ``.env``.  The **private-key line goes to STDERR** (P3 hardening):
  redirecting/capturing stdout (``> keys.env``, CI log capture) can never
  swallow the secret.  Public key + subject stay on stdout.  Nothing is
  written to disk and the private key is never logged.

This CLI never persists key material.  It consumes the frozen
:mod:`polyecho.shared.vapid` contract verbatim.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.panel import Panel

app = typer.Typer(no_args_is_help=True)
_console = Console()
# The secret warning rides the SAME stream as the private key (stderr) so
# the secret + its warning are never split into a captured stdout file.
_err_console = Console(stderr=True)


@app.command(name="vapid-keygen")
def vapid_keygen() -> None:
    """Generate a fresh VAPID keypair; print the three ``.env`` lines.

    Every invocation produces a brand-new keypair.  Output is three
    paste-ready env lines, **split across two streams** so the secret
    cannot be captured by a stdout redirect / CI log (P3 hardening)::

        # stdout  (safe to capture)
        POLYECHO_VAPID_PUBLIC_KEY=<pub>
        POLYECHO_VAPID_SUBJECT=<settings.vapid_subject>

        # stderr  (the SECRET — never lands in `> file` / piped logs)
        POLYECHO_VAPID_PRIVATE_KEY=<priv>

    followed by a blunt secret warning (also stderr).  Nothing is written
    to disk and the private key is never logged — it is shown to the
    operator once so it can be pasted into ``.env`` (which must never be
    committed/shared).  Regenerating keys invalidates every existing
    browser push subscription: all clients must re-subscribe.
    """
    from polyecho.shared.config import get_settings
    from polyecho.shared.vapid import generate_vapid_keypair

    public_b64, private_b64 = generate_vapid_keypair()
    subject = get_settings().vapid_subject

    # Non-secret lines → stdout (byte-exact, no Rich markup so a
    # copy/paste / `>> .env` is clean).
    typer.echo(f"POLYECHO_VAPID_PUBLIC_KEY={public_b64}")
    typer.echo(f"POLYECHO_VAPID_SUBJECT={subject}")
    # The PRIVATE KEY → STDERR so `polyecho push vapid-keygen > out` (or a
    # CI step that captures stdout) can NEVER swallow the secret into a
    # file/log.  The operator still sees it on their terminal.
    typer.echo(f"POLYECHO_VAPID_PRIVATE_KEY={private_b64}", err=True)

    _err_console.print(
        Panel(
            "[bold red]⚠  SECRET — VAPID PRIVATE KEY[/bold red]\n\n"
            "The [bold]POLYECHO_VAPID_PRIVATE_KEY[/bold] line above is a "
            "[bold red]secret[/bold red]. Paste all three lines into your "
            "[bold].env[/bold] file now — this is the only time the private "
            "key is shown and it is [bold]never[/bold] written to disk by "
            "this command.\n\n"
            "[bold]Never[/bold] commit [bold].env[/bold] to git and "
            "[bold]never[/bold] share the private key.\n\n"
            "[yellow]Regenerating keys invalidates every existing browser "
            "push subscription[/yellow] — all clients must re-subscribe "
            "after you deploy a new keypair.",
            title="[dim]polyecho push vapid-keygen[/dim]",
            border_style="red",
        )
    )
