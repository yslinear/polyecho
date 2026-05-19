"""
polyecho.cli.settings
======================
Runtime-settings sub-command — the **CLI half** of the Polymarket proxy
control, mirroring the web ``/settings`` page (PRD §6.1 / §8.1 "安全寫入").

Single source of truth
----------------------
The proxy lives in **one** place: the ``settings`` table row keyed
``polymarket_proxy_url`` (stored as a bare JSON string).  All three
surfaces read/write that *same* row:

* the web ``/settings`` form  → ``setPolymarketProxy`` server action,
* this CLI                     → :func:`polyecho.shared.http.set_polymarket_proxy`
  / :func:`~polyecho.shared.http.clear_polymarket_proxy`,
* the live trader-core path    → :func:`~polyecho.shared.http.get_effective_proxy_url`
  (``settings`` row → ``POLYECHO_POLYMARKET_PROXY_URL`` env → ``None``).

Because ``set_polymarket_proxy`` reuses the *same* SSRF/scheme validator
(``polyecho.shared.http.validate_proxy_url`` — a faithful port of the web
action's checks), a URL accepted/rejected here is accepted/rejected
identically by the web form, and a value written here renders identically
on the web /settings page (same row, same bare-string shape that
``_coerce_proxy`` reads).

Commands
--------
* ``polyecho settings proxy-show``  — resolve the *effective* proxy via
  :func:`~polyecho.shared.http.get_effective_proxy_url`, print it
  **redacted**, and say whether it came from the DB ``settings`` row or
  the env fallback (or that the connection is direct).
* ``polyecho settings proxy-set <url>`` — SSRF-validate then write the DB
  row.  Takes effect on the next Polymarket call.
* ``polyecho settings proxy-clear`` — delete the DB row → direct
  connection (env fallback, then none).

Out of scope (noted, not fixed): the CLOB ``requests``-based client and
``polyecho doctor`` read the **env-only** ``get_proxy_url`` rather than the
DB-aware ``get_effective_proxy_url`` — that env-only asymmetry is
pre-existing and explicitly NOT addressed here.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.panel import Panel

app = typer.Typer(no_args_is_help=True)
_console = Console()


@app.command(name="proxy-show")
def proxy_show() -> None:
    """Show the effective Polymarket proxy (redacted) and its source.

    Resolution is identical to the live trader-core path:
    ``settings`` DB row → ``POLYECHO_POLYMARKET_PROXY_URL`` env → direct.
    """
    from polyecho.shared.http import (
        get_effective_proxy_url,
        get_proxy_url,
        redact_proxy_url,
    )

    async def _resolve() -> tuple[str | None, str | None]:
        # Effective value (DB row first, env fallback) — same call the
        # live Polymarket clients use.
        effective = await get_effective_proxy_url()
        # The pure env value lets us attribute the source precisely:
        # if effective != env (or env is unset but effective is set), the
        # value came from the DB ``settings`` row.
        env_val = get_proxy_url()
        return effective, env_val

    try:
        effective, env_val = asyncio.run(_resolve())
    except Exception as exc:  # noqa: BLE001 - clean CLI error, no traceback
        _console.print(f"[red]Could not resolve proxy:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if effective is None:
        _console.print(
            Panel(
                "No proxy configured — Polymarket calls go [bold]direct[/bold].\n"
                "[dim]Resolution: settings row → "
                "POLYECHO_POLYMARKET_PROXY_URL env → direct.[/dim]",
                title="[dim]polyecho settings proxy[/dim]",
                border_style="green",
            )
        )
        return

    # Attribute the source: it is the env fallback only when there is no
    # overriding DB row, i.e. the effective value equals the env value.
    from_env = env_val is not None and effective == env_val
    source = (
        "[yellow]env[/yellow] (POLYECHO_POLYMARKET_PROXY_URL)"
        if from_env
        else "[cyan]DB[/cyan] settings row (key=polymarket_proxy_url)"
    )
    _console.print(
        Panel(
            f"Proxy : [bold]{redact_proxy_url(effective)}[/bold]\n"
            f"Source: {source}\n"
            "[dim]Mirrors the web /settings value (same settings row).[/dim]",
            title="[dim]polyecho settings proxy[/dim]",
            border_style="cyan",
        )
    )


@app.command(name="proxy-set")
def proxy_set(
    url: str = typer.Argument(
        ...,
        metavar="URL",
        help="http(s) proxy URL, e.g. http://203.0.113.10:8080 "
        "(SOCKS / loopback / RFC1918 / link-local rejected).",
    ),
) -> None:
    """Set the Polymarket proxy (DB-backed; SSRF-validated like the web)."""
    from polyecho.shared.http import (
        ProxyValidationError,
        redact_proxy_url,
        set_polymarket_proxy,
        validate_proxy_url,
    )

    # Validate eagerly so a rejected URL exits cleanly *before* any DB work
    # (set_polymarket_proxy re-validates too — this just gives a crisp msg).
    try:
        normalised = validate_proxy_url(url)
    except ProxyValidationError as exc:
        _console.print(f"[red]Rejected:[/red] {exc}")
        _console.print(
            "[dim]Nothing was written. Use an externally-routable "
            "http(s) proxy.[/dim]"
        )
        raise typer.Exit(code=2) from exc

    try:
        asyncio.run(set_polymarket_proxy(url))
    except ProxyValidationError as exc:  # defence in depth
        _console.print(f"[red]Rejected:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # noqa: BLE001 - clean CLI error, no traceback
        _console.print(f"[red]Could not save proxy:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _console.print(
        Panel(
            f"Proxy saved: [bold]{redact_proxy_url(normalised)}[/bold]\n"
            "[dim]Takes effect on the next Polymarket call. Same "
            "settings row the web /settings page shows.[/dim]",
            title="[green]polyecho settings proxy-set[/green]",
            border_style="green",
        )
    )


@app.command(name="proxy-clear")
def proxy_clear() -> None:
    """Clear the Polymarket proxy row → direct connection (env fallback)."""
    from polyecho.shared.http import clear_polymarket_proxy

    try:
        asyncio.run(clear_polymarket_proxy())
    except Exception as exc:  # noqa: BLE001 - clean CLI error, no traceback
        _console.print(f"[red]Could not clear proxy:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _console.print(
        Panel(
            "Proxy cleared. Polymarket calls now go "
            "[bold]direct[/bold]\n"
            "[dim](resolution falls back to "
            "POLYECHO_POLYMARKET_PROXY_URL env, then direct).[/dim]",
            title="[green]polyecho settings proxy-clear[/green]",
            border_style="green",
        )
    )


__all__ = ["app", "proxy_show", "proxy_set", "proxy_clear"]
