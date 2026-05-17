"""
polyecho.cli.backtest
=====================
Backtest engine sub-commands (Phase 2).

Commands
--------
* ``polyecho backtest new``  — enqueue a new backtest job
* ``polyecho backtest list`` — list historical backtest results
* ``polyecho backtest show`` — display a completed backtest result
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from polyecho.shared.db import session_scope
from polyecho.shared.models import BacktestJob

app = typer.Typer(no_args_is_help=True)
_console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_id(job_id: uuid.UUID) -> str:
    """Return the first 8 hex chars of a UUID for compact display."""
    return str(job_id)[:8]


# ---------------------------------------------------------------------------
# backtest new
# ---------------------------------------------------------------------------


@app.command(name="new")
def new(
    leader: Annotated[
        list[str],
        typer.Option("--leader", help="Leader wallet address (0x…). Repeatable."),
    ],
    days: Annotated[
        int,
        typer.Option("--days", help="Number of historical days to backtest."),
    ] = 90,
    capital: Annotated[
        float,
        typer.Option("--capital", help="Initial capital in USD."),
    ] = 1000.0,
    copy_ratio: Annotated[
        float,
        typer.Option("--copy-ratio", help="Copy ratio (e.g. 0.001 = 0.1%)."),
    ] = 0.001,
    max_position: Annotated[
        float | None,
        typer.Option("--max-position", help="Max position size in USD."),
    ] = None,
    min_trade: Annotated[
        float | None,
        typer.Option("--min-trade", help="Minimum trade size in USD."),
    ] = None,
    daily_loss: Annotated[
        float | None,
        typer.Option("--daily-loss", help="Daily loss limit in USD."),
    ] = None,
    slippage_bps: Annotated[
        int,
        typer.Option("--slippage-bps", help="Slippage in basis points (default 150 = 1.5%)."),
    ] = 150,
    delay: Annotated[
        int,
        typer.Option("--delay", help="Execution delay in seconds (default 30)."),
    ] = 30,
    category: Annotated[
        list[str] | None,
        typer.Option("--category", help="Category whitelist entry. Repeatable."),
    ] = None,
) -> None:
    """Create and queue a new backtest job for one or more leader addresses."""
    end_dt = date.today()
    start_dt = end_dt - timedelta(days=days)

    template_params: dict[str, object] = {
        "copy_ratio": copy_ratio,
        "fixed_size_usd": None,
        "max_position_usd": max_position,
        "min_trade_size_usd": min_trade,
        "max_concurrent_positions": None,
        "daily_loss_limit_usd": daily_loss,
        "category_whitelist": list(category) if category else None,
        "slippage_bps": slippage_bps,
        "execution_delay_seconds": delay,
    }

    async def _insert() -> uuid.UUID:
        async with session_scope() as session:
            job = BacktestJob(
                leader_addresses=list(leader),
                template_params=template_params,
                start_date=start_dt,
                end_date=end_dt,
                initial_capital_usd=capital,
                slippage_bps=slippage_bps,
                execution_delay_seconds=delay,
                status="pending",
                progress=0,
            )
            session.add(job)
            await session.flush()  # populate job.id before commit
            return job.id

    job_id = asyncio.run(_insert())

    _console.print(
        Panel(
            f"[bold green]Job enqueued[/bold green]\n"
            f"[dim]id[/dim]      [cyan]{job_id}[/cyan]\n"
            f"[dim]leaders[/dim] {', '.join(leader)}\n"
            f"[dim]period[/dim]  {start_dt} → {end_dt}  ({days}d)\n"
            f"[dim]capital[/dim] ${capital:,.2f}  "
            f"[dim]slippage[/dim] {slippage_bps}bps  "
            f"[dim]delay[/dim] {delay}s",
            title="[dim]polyecho backtest new[/dim]",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# backtest list
# ---------------------------------------------------------------------------


@app.command(name="list")
def list_backtests(
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum number of jobs to show."),
    ] = 20,
) -> None:
    """List recent backtest jobs and their status."""
    from sqlalchemy import select

    async def _fetch() -> list[BacktestJob]:
        async with session_scope() as session:
            result = await session.execute(
                select(BacktestJob)
                .order_by(BacktestJob.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    jobs = asyncio.run(_fetch())

    if not jobs:
        _console.print("[dim]No backtest jobs found.[/dim]")
        return

    table = Table(title="Backtest Jobs", show_lines=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Progress", justify="right")
    table.add_column("Leaders")
    table.add_column("Created At", no_wrap=True)

    _status_style = {
        "pending": "yellow",
        "running": "blue",
        "completed": "green",
        "failed": "red",
    }

    for job in jobs:
        status_style = _status_style.get(job.status, "white")
        leaders_short = ", ".join(
            f"{addr[:6]}…{addr[-4:]}" if len(addr) > 10 else addr
            for addr in (job.leader_addresses or [])
        )
        table.add_row(
            _short_id(job.id),
            f"[{status_style}]{job.status}[/{status_style}]",
            f"{float(job.progress):.0f}%",
            leaders_short,
            str(job.created_at)[:19],
        )

    _console.print(table)


# ---------------------------------------------------------------------------
# backtest show
# ---------------------------------------------------------------------------


@app.command(name="show")
def show(
    job_id: Annotated[str, typer.Argument(help="Backtest job UUID (full or 8-char prefix).")],
) -> None:
    """Display the full result of a backtest job."""
    import json

    from sqlalchemy import select

    async def _fetch() -> BacktestJob | None:
        async with session_scope() as session:
            # Try exact UUID first
            try:
                uid = uuid.UUID(job_id)
                result = await session.execute(
                    select(BacktestJob).where(BacktestJob.id == uid)
                )
                return result.scalar_one_or_none()
            except ValueError:
                pass
            # Fall back to prefix search
            result = await session.execute(
                select(BacktestJob).where(
                    BacktestJob.id.cast(
                        __import__("sqlalchemy").Text
                    ).like(f"{job_id}%")
                )
            )
            return result.scalar_one_or_none()

    job = asyncio.run(_fetch())

    if job is None:
        _console.print(f"[red]Job not found:[/red] {job_id}")
        raise typer.Exit(code=1)

    _console.print(
        Panel(
            f"[dim]id[/dim]      [cyan]{job.id}[/cyan]\n"
            f"[dim]status[/dim]  [bold]{job.status}[/bold]  "
            f"[dim]progress[/dim] {float(job.progress):.0f}%\n"
            f"[dim]leaders[/dim] {', '.join(job.leader_addresses or [])}\n"
            f"[dim]period[/dim]  {job.start_date} → {job.end_date}\n"
            f"[dim]capital[/dim] ${float(job.initial_capital_usd):,.2f}  "
            f"[dim]slippage[/dim] {job.slippage_bps}bps  "
            f"[dim]delay[/dim] {job.execution_delay_seconds}s\n"
            f"[dim]params[/dim]  {json.dumps(job.template_params, default=str)}\n"
            f"[dim]created[/dim] {job.created_at}",
            title=f"[dim]Backtest Job {_short_id(job.id)}[/dim]",
            border_style="cyan",
        )
    )

    if job.status not in ("completed", "failed"):
        _console.print(
            f"[yellow]Result not yet available — job is {job.status}.[/yellow]"
        )
        return

    if job.result is None:
        _console.print("[dim]No result payload stored.[/dim]")
        return

    result = job.result

    # --- per-leader stats ---
    if "leaders" in result:
        table = Table(title="Per-Leader Stats", show_lines=True)
        table.add_column("Leader", style="cyan")
        table.add_column("Return %", justify="right")
        table.add_column("Sharpe", justify="right")
        table.add_column("Max DD %", justify="right")
        table.add_column("Win Rate %", justify="right")
        table.add_column("Trades", justify="right")
        for addr, stats in result["leaders"].items():
            table.add_row(
                f"{addr[:6]}…{addr[-4:]}" if len(addr) > 10 else addr,
                f"{stats.get('total_return_pct', 0):.1f}",
                f"{stats.get('sharpe', 0):.2f}",
                f"{stats.get('max_drawdown_pct', 0):.1f}",
                f"{stats.get('win_rate_pct', 0):.1f}",
                str(stats.get("total_trades", 0)),
            )
        _console.print(table)

    # --- 3-scenario summary ---
    if "scenarios" in result:
        s_table = Table(title="Three-Scenario Summary", show_lines=True)
        s_table.add_column("Scenario")
        s_table.add_column("Slippage")
        s_table.add_column("Delay")
        s_table.add_column("Return %", justify="right")
        s_table.add_column("Sharpe", justify="right")
        for name, sc in result["scenarios"].items():
            s_table.add_row(
                name,
                f"{sc.get('slippage_bps', 0)}bps",
                f"{sc.get('delay_seconds', 0)}s",
                f"{sc.get('total_return_pct', 0):.1f}",
                f"{sc.get('sharpe', 0):.2f}",
            )
        _console.print(s_table)
