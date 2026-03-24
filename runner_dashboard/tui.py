"""
tui.py
------
Rich panel builders and the top-level build_dashboard() function.

Depends only on collector (data) and constants — no web or installer imports.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .collector import (
    get_current_job,
    get_recent_logs,
    get_runner_config,
    get_service_status,
    get_system_resources,
)
from .constants import LOG_KEYWORDS


# ── Formatting helpers ────────────────────────────────────────────────────────

def _bar(percent: float, width: int = 20) -> Text:
    filled  = int(width * min(percent, 100) / 100)
    bar_str = "█" * filled + "░" * (width - filled)
    color   = "red" if percent >= 90 else ("yellow" if percent >= 70 else "green")
    t = Text()
    t.append("[", style="dim")
    t.append(bar_str, style=color)
    t.append("]", style="dim")
    t.append(f" {percent:5.1f}%", style="bold")
    return t


def _fmt_uptime(td: Optional[timedelta]) -> str:
    if td is None:
        return "—"
    secs = int(abs(td.total_seconds()))
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def _fmt_duration(started: Optional[datetime]) -> str:
    if started is None:
        return "—"
    secs = int((datetime.now() - started).total_seconds())
    h, rem = divmod(max(secs, 0), 3600)
    m, s   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── Panel builders ────────────────────────────────────────────────────────────

def _header_panel(runner_path: Optional[Path]) -> Panel:
    path_str = str(runner_path) if runner_path else "not found"
    now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    t = Text()
    t.append("  GitHub Actions Runner Dashboard", style="bold white")
    t.append("   │   ", style="dim")
    t.append(path_str, style="cyan")
    t.append("   │   ", style="dim")
    t.append(now, style="dim")
    return Panel(Align.center(t), style="bold blue", padding=(0, 1))


def _status_panel(status: Dict[str, Any], job: Dict[str, Any]) -> Panel:
    _colors = {
        "running": "bold green", "active": "bold green",
        "stopped": "bold red",   "failed": "bold red",
        "unknown": "yellow",
    }
    _icons = {
        "running": "●", "active": "●",
        "stopped": "○", "failed": "✗",
        "unknown": "?",
    }
    state = status["state"]
    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(no_wrap=True)
    tbl.add_column(no_wrap=True)

    tbl.add_row(
        Text(f"{_icons.get(state, '?')} State:", style="bold"),
        Text(state.upper(), style=_colors.get(state, "white")),
    )
    if status.get("service_name"):
        tbl.add_row(Text("  Service:", style="bold"), Text(status["service_name"], style="cyan"))
    if status.get("pid"):
        tbl.add_row(Text("  PID:", style="bold"), Text(str(status["pid"]), style="cyan"))
    if status.get("uptime"):
        tbl.add_row(Text("  Uptime:", style="bold"), Text(_fmt_uptime(status["uptime"]), style="cyan"))

    tbl.add_row(Text(""), Text(""))
    if job["running"]:
        tbl.add_row(Text("  Mode:", style="bold"), Text("● BUSY", style="bold yellow"))
    else:
        tbl.add_row(Text("  Mode:", style="bold"), Text("◌ IDLE", style="bold green"))

    return Panel(tbl, title="[bold]Runner Status[/bold]", border_style="blue", padding=(1, 2))


def _job_panel(job: Dict[str, Any]) -> Panel:
    if not job["running"]:
        idle = Align.center(Text("\n◌  Runner is idle — waiting for jobs\n", style="dim italic"))
        return Panel(idle, title="[bold]Current Job[/bold]", border_style="blue", padding=(1, 2))

    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(no_wrap=True, style="bold")
    tbl.add_column(no_wrap=True)

    if job.get("repo"):
        tbl.add_row("Repository:", Text(job["repo"], style="cyan"))
    if job.get("workflow"):
        tbl.add_row("Workflow:", Text(job["workflow"], style="cyan"))
    if job.get("job_name"):
        tbl.add_row("Job:", Text(job["job_name"], style="bold yellow"))
    if job.get("step"):
        tbl.add_row("Step:", Text(job["step"], style="white"))
    if job.get("run_id"):
        tbl.add_row("Run ID:", Text(f"#{job['run_id']}", style="dim"))
    if job.get("started_at"):
        tbl.add_row("Duration:", Text(_fmt_duration(job["started_at"]), style="bold green"))

    return Panel(tbl, title="[bold]Current Job[/bold]", border_style="yellow", padding=(1, 2))


def _identity_panel(config: Dict[str, str]) -> Panel:
    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(style="bold", no_wrap=True)
    tbl.add_column()
    if config.get("name"):
        tbl.add_row("Name:", Text(config["name"], style="cyan bold"))
    if config.get("url"):
        tbl.add_row("URL:", Text(config["url"], style="cyan"))
    if config.get("pool"):
        tbl.add_row("Pool:", Text(config["pool"], style="cyan"))
    if not config:
        tbl.add_row("", Text("(.runner config not found)", style="dim"))
    return Panel(tbl, title="[bold]Runner Identity[/bold]", border_style="blue", padding=(1, 2))


def _resources_panel(res: Dict[str, Any]) -> Panel:
    tbl = Table.grid(padding=(0, 1))
    tbl.add_column(min_width=7,  style="bold")
    tbl.add_column(min_width=32)
    tbl.add_column(min_width=9,  style="bold")
    tbl.add_column(min_width=32)

    la = res["load_avg"]
    tbl.add_row(
        "CPU",    _bar(res["cpu_percent"]),
        "Memory", _bar(res["mem_percent"]),
    )
    tbl.add_row(
        "",
        Text(f"  Load: {la[0]:.2f}  {la[1]:.2f}  {la[2]:.2f}", style="dim"),
        "",
        Text(f"  {res['mem_used_gb']:.1f} / {res['mem_total_gb']:.1f} GB", style="dim"),
    )
    tbl.add_row(
        "Disk",   _bar(res["disk_percent"]),
        "Runner", Text(f"CPU {res['runner_cpu']:.1f}%   MEM {res['runner_mem_mb']:.0f} MB", style="cyan"),
    )
    tbl.add_row(
        "",
        Text(f"  {res['disk_used_gb']:.1f} / {res['disk_total_gb']:.1f} GB", style="dim"),
        "", Text(""),
    )
    return Panel(tbl, title="[bold]System Resources[/bold]", border_style="blue", padding=(0, 2))


def _log_panel(lines: List[str]) -> Panel:
    t = Text()
    for raw in lines:
        line  = raw[:130] + "…" if len(raw) > 130 else raw
        style = "dim"
        for kw, kw_style in LOG_KEYWORDS.items():
            if kw in line:
                style = kw_style
                break
        t.append(line + "\n", style=style)
    return Panel(t, title="[bold]Recent Log[/bold]", border_style="blue", padding=(0, 1))


# ── Dashboard assembly ────────────────────────────────────────────────────────

def build_dashboard(runner_path: Optional[Path], service_name: Optional[str]) -> Layout:
    status = get_service_status(service_name, runner_path)
    job    = get_current_job(runner_path)
    res    = get_system_resources(runner_path)
    logs   = get_recent_logs(runner_path, n=18)
    config = get_runner_config(runner_path)

    layout = Layout()
    layout.split_column(
        Layout(name="header",    size=3),
        Layout(name="top_row",   size=11),
        Layout(name="resources", size=8),
        Layout(name="logs"),
    )
    layout["top_row"].split_row(
        Layout(name="status",   ratio=2),
        Layout(name="job",      ratio=1),
        Layout(name="identity", ratio=2),
    )

    layout["header"].update(_header_panel(runner_path))
    layout["status"].update(_status_panel(status, job))
    layout["job"].update(_job_panel(job))
    layout["identity"].update(_identity_panel(config))
    layout["resources"].update(_resources_panel(res))
    layout["logs"].update(_log_panel(logs))

    return layout
