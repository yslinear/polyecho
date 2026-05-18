"""
polyecho.cli.logs
=================
Service log viewer sub-command.

Commands
--------
* ``polyecho logs show <service> [--tail N] [--follow]`` — show / tail the
  logs for a named PolyEcho compose service.

Approach
--------
PolyEcho ships as a ``docker compose`` deployment (``infra/``).  The
operator-facing log surface is therefore ``docker compose logs <service>``.
This command shells out to the compose CLI for the **fixed** known service
set (no shell, fixed argv — the service name is validated against an
allowlist so it can never be an injection vector).  If neither
``docker compose`` nor the legacy ``docker-compose`` is available, it fails
with a clear, non-zero error rather than pretending to succeed.
"""

from __future__ import annotations

import shutil
import subprocess

import typer
from rich.console import Console
from rich.panel import Panel

app = typer.Typer(no_args_is_help=True)
_console = Console()

# The compose service names PolyEcho defines (infra/docker-compose.yml).
_VALID_SERVICES = [
    "trader-core",
    "discovery-worker",
    "web-push-sender",
    "dashboard-web",
    "postgres",
]


def _docker_compose_plugin_available() -> bool:
    """True iff the ``docker compose`` *plugin* actually works.

    R15 #3: ``docker`` being on PATH does NOT imply the Compose V2 plugin
    is installed — a host can have the docker CLI without it. Returning
    ``["docker", "compose"]`` blindly then surfaces a confusing
    "docker: 'compose' is not a docker command" later instead of the
    clean fallback / "no compose CLI" message. Probe it explicitly with a
    cheap, side-effect-free ``docker compose version`` (no shell, fixed
    argv, ``check=False`` — we inspect the return code ourselves).
    """
    try:
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["docker", "compose", "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _compose_base_cmd() -> list[str] | None:
    """Resolve the compose invocation: ``docker compose`` or fallback.

    Returns the argv prefix (e.g. ``["docker", "compose"]``) or ``None``
    when no working compose CLI is available on PATH.
    """
    if shutil.which("docker") is not None and _docker_compose_plugin_available():
        return ["docker", "compose"]
    # Legacy standalone binary (older operator hosts, or docker present
    # but the Compose V2 plugin missing).
    if shutil.which("docker-compose") is not None:
        return ["docker-compose"]
    return None


@app.command(name="show")
def show(
    service: str = typer.Argument(
        ...,
        help=f"Service name. One of: {', '.join(_VALID_SERVICES)}.",
    ),
    tail: int = typer.Option(
        200,
        "--tail",
        "-n",
        min=1,
        max=100_000,
        help="Number of trailing log lines to show.",
    ),
    follow: bool = typer.Option(
        False,
        "--follow",
        "-f",
        help="Stream new log lines (docker compose logs -f). Ctrl-C stops.",
    ),
) -> None:
    """Show (or follow) the logs for a PolyEcho service container.

    Shells out to ``docker compose logs`` for the known service set. Exits
    non-zero with a clear message if the service name is unknown or no
    compose CLI is available (docker unavailable).
    """
    if service not in _VALID_SERVICES:
        _console.print(
            Panel(
                f"[red]Unknown service [bold]{service}[/bold].[/red]\n"
                f"Valid services: {', '.join(_VALID_SERVICES)}.",
                title="[dim]polyecho logs show[/dim]",
                border_style="red",
            )
        )
        raise typer.Exit(code=2)

    base = _compose_base_cmd()
    if base is None:
        _console.print(
            Panel(
                "[red]No compose CLI found.[/red] Neither "
                "[bold]docker[/bold] (for `docker compose`) nor "
                "[bold]docker-compose[/bold] is on PATH.\n"
                "[dim]Run this on the host where the PolyEcho compose stack "
                "is deployed (infra/docker-compose.yml).[/dim]",
                title="[dim]polyecho logs show[/dim]",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)

    # Fixed argv (no shell). ``service`` is allowlisted above so it can
    # never be an injection vector.
    cmd = [*base, "logs", "--tail", str(tail)]
    if follow:
        cmd.append("--follow")
    cmd.append(service)

    _console.print(
        f"[dim]$ {' '.join(cmd)}[/dim]"
    )

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            check=False,
        )
    except KeyboardInterrupt:
        _console.print("[yellow]Stopped following logs.[/yellow]")
        raise typer.Exit(code=130) from None
    except OSError as exc:
        _console.print(
            f"[red]Failed to invoke compose:[/red] {exc}"
        )
        raise typer.Exit(code=1) from exc

    if completed.returncode != 0:
        _console.print(
            f"[red]`{' '.join(base)} logs` exited "
            f"{completed.returncode}.[/red] "
            "[dim](Is the compose stack up? `docker compose ps`)[/dim]"
        )
        raise typer.Exit(code=completed.returncode)
