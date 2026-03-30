"""
installer.py
------------
Service installers: systemd (Linux), launchd (macOS), and nginx reverse proxy.

All functions are self-contained — they print their own status lines and call
system tools directly.  No TUI or web module imports needed.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from . import console
from .constants import LAUNCHD_ID, SVC_NAME, SYSTEMD_SVC


# ── Privilege escalation ──────────────────────────────────────────────────────

def _require_root() -> None:
    """On Linux, if not already root, re-exec the current command under sudo."""
    if platform.system() != "Linux" or os.geteuid() == 0:
        return
    import shutil
    sudo = shutil.which("sudo")
    if sudo is None:
        _print_err("Root privileges required. Please run as root.")
        sys.exit(1)
    console.print("[dim]  → root required for /etc/nginx — re-running with sudo...[/dim]")
    # Replace current process image with: sudo <same argv>
    os.execvp(sudo, [sudo] + sys.argv)


# ── Shell helpers ─────────────────────────────────────────────────────────────

def _run_cmd(cmd: List[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _print_ok(msg: str)   -> None: console.print(f"[bold green]  \u2714  {msg}[/bold green]")
def _print_err(msg: str)  -> None: console.print(f"[bold red]  \u2718  {msg}[/bold red]")
def _print_info(msg: str) -> None: console.print(f"[dim]     {msg}[/dim]")


# ── Virtualenv management ─────────────────────────────────────────────────────

def _venv_path(user_scope: bool) -> Path:
    """Return the canonical venv directory for this install type."""
    if user_scope:
        return Path.home() / ".local" / "runner-dashboard" / "venv"
    return Path("/opt/runner-dashboard/venv")


def _ensure_venv(venv_dir: Path) -> Path:
    """Create (or reuse) the venv and install the package + all deps into it.

    Returns the path to the venv's Python interpreter.
    """
    import shutil
    venv_python = venv_dir / "bin" / "python3"

    # Locate the package source (the directory containing pyproject.toml)
    pkg_root = Path(__file__).resolve().parent.parent

    console.print(f"\n[bold]Setting up virtual environment[/bold]")
    _print_info(f"Location: {venv_dir}")

    if not venv_python.exists():
        python_bin = shutil.which("python3") or sys.executable
        r = _run_cmd([python_bin, "-m", "venv", str(venv_dir)], check=False)
        if r.returncode != 0:
            _print_err(f"venv creation failed: {r.stderr.strip() or r.stdout.strip()}")
            sys.exit(1)
        _print_ok(f"Created venv at {venv_dir}")
    else:
        _print_info("Reusing existing venv")

    # Remove any stale build directory left by a previous install (possibly run
    # as a different user / root) to avoid "Permission denied" errors from setuptools.
    import shutil as _shutil
    build_dir = pkg_root / "build"
    if build_dir.exists():
        try:
            _shutil.rmtree(build_dir)
        except OSError:
            pass  # pip will surface the real error if it still can't write here

    pip = venv_dir / "bin" / "pip"
    # Upgrade pip quietly first to avoid stale-pip warnings
    _run_cmd([str(pip), "install", "--quiet", "--upgrade", "pip"], check=False)
    r = _run_cmd([str(pip), "install", "--quiet", str(pkg_root)], check=False)
    if r.returncode != 0:
        _print_err(f"pip install failed: {r.stderr.strip() or r.stdout.strip()}")
        sys.exit(1)
    _print_ok("Installed runner-dashboard and dependencies")

    return venv_python


# ── Service unit / plist generators ──────────────────────────────────────────

def _build_exec_args(args: argparse.Namespace, venv_python: Optional[str] = None) -> List[str]:
    """Reconstruct the web-mode CLI flags from parsed args for service files."""
    py = venv_python or sys.executable
    parts  = [py, "-m", "runner_dashboard",
              "--web",
              "--port",     str(args.port),
              "--bind",     args.bind,
              "--interval", str(int(args.interval))]
    if args.runner_path:
        parts += ["--runner-path", str(args.runner_path)]
    if getattr(args, "url_prefix", "/") not in ("/", ""):
        parts += ["--url-prefix", args.url_prefix]
    return parts


def _systemd_unit(args: argparse.Namespace, user_scope: bool, venv_python: str) -> str:
    exec_start = " ".join(_build_exec_args(args, venv_python))
    user_line  = "" if user_scope else f"User={os.environ.get('USER', 'root')}\n"
    return (
        f"[Unit]\n"
        f"Description=GitHub Actions Runner Dashboard (web UI)\n"
        f"After=network.target\n\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"{user_line}"
        f"ExecStart={exec_start}\n"
        f"Restart=on-failure\n"
        f"RestartSec=5\n"
        f"StandardOutput=journal\n"
        f"StandardError=journal\n\n"
        f"[Install]\n"
        f"WantedBy={'default' if user_scope else 'multi-user'}.target\n"
    )


def _launchd_plist(args: argparse.Namespace, venv_python: str) -> str:
    exec_args = _build_exec_args(args, venv_python)
    log_dir   = Path.home() / "Library" / "Logs"
    log_out   = str(log_dir / f"{SVC_NAME}.out.log")
    log_err   = str(log_dir / f"{SVC_NAME}.err.log")
    prog_args = "\n".join(f"        <string>{a}</string>" for a in exec_args)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        f' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        f'<plist version="1.0">\n'
        f'<dict>\n'
        f'    <key>Label</key>\n'
        f'    <string>{LAUNCHD_ID}</string>\n'
        f'    <key>ProgramArguments</key>\n'
        f'    <array>\n'
        f'{prog_args}\n'
        f'    </array>\n'
        f'    <key>RunAtLoad</key><true/>\n'
        f'    <key>KeepAlive</key><true/>\n'
        f'    <key>StandardOutPath</key><string>{log_out}</string>\n'
        f'    <key>StandardErrorPath</key><string>{log_err}</string>\n'
        f'</dict>\n'
        f'</plist>\n'
    )


# ── systemd ───────────────────────────────────────────────────────────────────

def _systemd_install(args: argparse.Namespace, user_scope: bool) -> None:
    unit_dir = (
        Path.home() / ".config" / "systemd" / "user"
        if user_scope else Path("/etc/systemd/system")
    )
    unit_file  = unit_dir / SYSTEMD_SVC
    scope_flag = ["--user"] if user_scope else []

    console.print(f"\n[bold]Installing systemd service[/bold] ({'user' if user_scope else 'system'} scope)")

    venv_python = str(_ensure_venv(_venv_path(user_scope)))
    content    = _systemd_unit(args, user_scope, venv_python)
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_file.write_text(content)
        _print_ok(f"Unit file \u2192 {unit_file}")
    except PermissionError:
        _print_err(f"Permission denied writing {unit_file}")
        console.print("[yellow]  Hint: run with sudo for system-wide install, or add --user-service[/yellow]")
        sys.exit(1)

    for cmd, label in [
        (["systemctl"] + scope_flag + ["daemon-reload"],           "daemon-reload"),
        (["systemctl"] + scope_flag + ["enable",  SYSTEMD_SVC],    f"enable {SYSTEMD_SVC}"),
        (["systemctl"] + scope_flag + ["start",   SYSTEMD_SVC],    f"start  {SYSTEMD_SVC}"),
    ]:
        r = _run_cmd(cmd, check=False)
        if r.returncode == 0:
            _print_ok(label)
        else:
            _print_err(f"{label}  →  {r.stderr.strip() or r.stdout.strip()}")

    _print_info(f"Journal: journalctl {' '.join(scope_flag)} -u {SYSTEMD_SVC} -f")
    _print_info(f"Status:  systemctl  {' '.join(scope_flag)} status {SYSTEMD_SVC}")
    console.print(f"\n[bold green]Web dashboard will be available at http://localhost:{args.port}/[/bold green]")


def _systemd_uninstall(user_scope: bool) -> None:
    scope_flag = ["--user"] if user_scope else []
    unit_dir   = (
        Path.home() / ".config" / "systemd" / "user"
        if user_scope else Path("/etc/systemd/system")
    )
    unit_file = unit_dir / SYSTEMD_SVC

    console.print(f"\n[bold]Removing systemd service[/bold] ({'user' if user_scope else 'system'} scope)")
    for cmd, label in [
        (["systemctl"] + scope_flag + ["stop",    SYSTEMD_SVC], f"stop    {SYSTEMD_SVC}"),
        (["systemctl"] + scope_flag + ["disable", SYSTEMD_SVC], f"disable {SYSTEMD_SVC}"),
    ]:
        r = _run_cmd(cmd, check=False)
        if r.returncode == 0:
            _print_ok(label)
        else:
            _print_info(f"{label} (skipped: {r.stderr.strip() or 'not active'})")
    if unit_file.exists():
        try:
            unit_file.unlink()
            _print_ok(f"Removed {unit_file}")
        except PermissionError:
            _print_err(f"Cannot remove {unit_file} — run with sudo")
    _run_cmd(["systemctl"] + scope_flag + ["daemon-reload"], check=False)
    _print_ok("daemon-reload")


def _systemd_status(user_scope: bool) -> None:
    scope_flag = ["--user"] if user_scope else []
    r = _run_cmd(["systemctl"] + scope_flag + ["status", SYSTEMD_SVC, "--no-pager"], check=False)
    console.print(r.stdout or r.stderr)


# ── launchd ───────────────────────────────────────────────────────────────────

def _launchd_install(args: argparse.Namespace) -> None:
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    plist_file = agents_dir / f"{LAUNCHD_ID}.plist"
    log_dir    = Path.home() / "Library" / "Logs"

    console.print("\n[bold]Installing launchd service[/bold] (user LaunchAgent)")

    venv_python = str(_ensure_venv(_venv_path(user_scope=True)))
    content    = _launchd_plist(args, venv_python)
    try:
        agents_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        plist_file.write_text(content)
        _print_ok(f"Plist \u2192 {plist_file}")
    except OSError as exc:
        _print_err(f"Could not write plist: {exc}")
        sys.exit(1)

    _run_cmd(["launchctl", "unload", str(plist_file)], check=False)
    r = _run_cmd(["launchctl", "load", "-w", str(plist_file)], check=False)
    if r.returncode == 0:
        _print_ok(f"launchctl load -w {plist_file.name}")
    else:
        _print_err(f"launchctl load: {r.stderr.strip() or r.stdout.strip()}")

    log_out = log_dir / f"{SVC_NAME}.out.log"
    _print_info(f"Logs:   tail -f {log_out}")
    _print_info(f"Status: launchctl list {LAUNCHD_ID}")
    console.print(f"\n[bold green]Web dashboard will be available at http://localhost:{args.port}/[/bold green]")


def _launchd_uninstall() -> None:
    plist_file = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_ID}.plist"
    console.print("\n[bold]Removing launchd service[/bold]")
    if plist_file.exists():
        r = _run_cmd(["launchctl", "unload", "-w", str(plist_file)], check=False)
        if r.returncode == 0:
            _print_ok(f"launchctl unload {plist_file.name}")
        else:
            _print_info(f"unload skipped: {r.stderr.strip() or 'not loaded'}")
        plist_file.unlink()
        _print_ok(f"Removed {plist_file}")
    else:
        _print_info(f"Plist not found at {plist_file} — nothing to remove")


def _launchd_status() -> None:
    r = _run_cmd(["launchctl", "list", LAUNCHD_ID], check=False)
    console.print(r.stdout or r.stderr)


# ── nginx ─────────────────────────────────────────────────────────────────────

# Marker: lives at /etc/nginx/ root, outside any include glob
_NGINX_DISABLED_MARKER   = Path("/etc/nginx/.runner-dashboard-disabled-defaults.json")
_NGINX_CONF_DEFAULT_BAK  = Path("/etc/nginx/default.conf.runner-dashboard-bak")


def _nginx_conf(upstream_port: int, server_name: str, listen_port: int,
                path: str = "/") -> str:
    path = "/" + path.strip("/")
    if path == "/":
        default_kw     = " default_server" if server_name == "_" else ""
        location_block = (
            f"    location / {{\n"
            f"        proxy_pass         http://127.0.0.1:{upstream_port}/;\n"
            f"        proxy_http_version 1.1;\n"
            f"        proxy_set_header   Host              $host;\n"
            f"        proxy_set_header   X-Real-IP         $remote_addr;\n"
            f"        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;\n"
            f"        proxy_set_header   X-Forwarded-Proto $scheme;\n"
            f"        proxy_read_timeout 60s;\n"
            f"    }}"
        )
    else:
        default_kw     = ""
        location_block = (
            f"    # Redirect {path} \u2192 {path}/ so relative URLs resolve correctly\n"
            f"    location = {path} {{\n"
            f"        return 301 {path}/;\n"
            f"    }}\n\n"
            f"    location {path}/ {{\n"
            f"        proxy_pass         http://127.0.0.1:{upstream_port}/;\n"
            f"        proxy_http_version 1.1;\n"
            f"        proxy_set_header   Host              $host;\n"
            f"        proxy_set_header   X-Real-IP         $remote_addr;\n"
            f"        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;\n"
            f"        proxy_set_header   X-Forwarded-Proto $scheme;\n"
            f"        proxy_read_timeout 60s;\n"
            f"    }}"
        )
    return (
        f"# Managed by runner-dashboard \u2014 do not edit manually\n"
        f"server {{\n"
        f"    listen {listen_port}{default_kw};\n"
        f"    server_name {server_name};\n\n"
        f"{location_block}\n"
        f"}}\n"
    )


def _nginx_conf_path() -> Optional[Path]:
    candidates: List[Path] = []
    if platform.system() == "Linux":
        candidates = [Path("/etc/nginx/conf.d"), Path("/etc/nginx/sites-available")]
    elif platform.system() == "Darwin":
        candidates = [
            Path("/opt/homebrew/etc/nginx/servers"),
            Path("/usr/local/etc/nginx/servers"),
        ]
    for d in candidates:
        if d.is_dir():
            return d
    return None


def _nginx_disable_default_site() -> None:
    """
    Move the nginx default server block(s) OUTSIDE their include directory.
    Renaming in-place (*.disabled) does NOT work — nginx's include glob matches
    every filename regardless of extension.
    """
    if platform.system() != "Linux":
        return

    records: dict = {}

    se_default = Path("/etc/nginx/sites-enabled/default")
    for stale in Path("/etc/nginx/sites-enabled").glob("default.*"):
        try:
            stale.unlink()
            _print_ok(f"Removed stale artifact: {stale}")
        except OSError:
            pass
    if se_default.exists() or se_default.is_symlink():
        try:
            target = str(os.readlink(se_default)) if se_default.is_symlink() else ""
            records["sites-enabled/default"] = target
            se_default.unlink()
            _print_ok("Disabled default site: removed /etc/nginx/sites-enabled/default")
        except PermissionError:
            _print_err("Cannot remove /etc/nginx/sites-enabled/default — run with sudo")

    conf_default = Path("/etc/nginx/conf.d/default.conf")
    for stale in Path("/etc/nginx/conf.d").glob("default.conf.*"):
        try:
            stale.unlink()
            _print_ok(f"Removed stale artifact: {stale}")
        except OSError:
            pass
    if conf_default.exists() and not _NGINX_CONF_DEFAULT_BAK.exists():
        try:
            conf_default.rename(_NGINX_CONF_DEFAULT_BAK)
            records["conf.d/default.conf"] = str(_NGINX_CONF_DEFAULT_BAK)
            _print_ok(f"Moved conf.d/default.conf → {_NGINX_CONF_DEFAULT_BAK.name}")
        except PermissionError:
            _print_err(f"Cannot move {conf_default} — run with sudo")

    if records:
        try:
            _NGINX_DISABLED_MARKER.write_text(json.dumps(records))
        except OSError:
            pass


def _nginx_restore_default_site() -> None:
    if platform.system() != "Linux" or not _NGINX_DISABLED_MARKER.exists():
        return
    try:
        records = json.loads(_NGINX_DISABLED_MARKER.read_text())
    except (OSError, ValueError):
        records = {}

    se_default = Path("/etc/nginx/sites-enabled/default")
    target_str = records.get("sites-enabled/default", "")
    if not (se_default.exists() or se_default.is_symlink()):
        target = Path(target_str) if target_str else Path("/etc/nginx/sites-available/default")
        if target.exists():
            try:
                se_default.symlink_to(target)
                _print_ok(f"Restored symlink sites-enabled/default → {target}")
            except OSError as exc:
                _print_info(f"Could not restore sites-enabled/default: {exc}")

    conf_default = Path("/etc/nginx/conf.d/default.conf")
    if _NGINX_CONF_DEFAULT_BAK.exists() and not conf_default.exists():
        try:
            _NGINX_CONF_DEFAULT_BAK.rename(conf_default)
            _print_ok("Restored conf.d/default.conf")
        except OSError as exc:
            _print_info(f"Could not restore conf.d/default.conf: {exc}")

    try:
        _NGINX_DISABLED_MARKER.unlink()
    except OSError:
        pass


def _nginx_reload() -> None:
    if platform.system() == "Linux":
        r = _run_cmd(["systemctl", "reload", "nginx"], check=False)
        if r.returncode == 0:
            _print_ok("systemctl reload nginx")
            return
    r = _run_cmd(["nginx", "-s", "reload"], check=False)
    if r.returncode == 0:
        _print_ok("nginx -s reload")
    else:
        _print_err(f"reload failed: {r.stderr.strip() or r.stdout.strip()}")


def _nginx_install(args: argparse.Namespace) -> None:
    _require_root()
    upstream_port = args.port
    listen_port   = args.nginx_listen_port
    server_name   = args.nginx_server_name
    nginx_path    = "/" + args.nginx_path.strip("/") or "/"

    conf_dir = _nginx_conf_path()
    if conf_dir is None:
        _print_err("nginx config directory not found. Is nginx installed?")
        sys.exit(1)

    conf_file    = conf_dir / f"{SVC_NAME}.conf"
    content      = _nginx_conf(upstream_port, server_name, listen_port, nginx_path)
    path_display = nginx_path if nginx_path != "/" else ""

    console.print("\n[bold]Installing nginx reverse proxy[/bold]")
    console.print(f"[dim]    :{listen_port}{path_display} \u2192 http://127.0.0.1:{upstream_port}/[/dim]")
    try:
        conf_file.write_text(content)
        _print_ok(f"Config \u2192 {conf_file}")
    except PermissionError:
        _print_err(f"Permission denied writing {conf_file} \u2014 run with sudo")
        sys.exit(1)

    sites_enabled = conf_dir.parent / "sites-enabled"
    if conf_dir.name == "sites-available" and sites_enabled.is_dir():
        link = sites_enabled / conf_file.name
        if not link.exists():
            try:
                link.symlink_to(conf_file)
                _print_ok(f"Symlink \u2192 {link}")
            except OSError as exc:
                _print_err(f"Could not create symlink: {exc}")

    if server_name == "_" and nginx_path == "/":
        _nginx_disable_default_site()

    r = _run_cmd(["nginx", "-t"], check=False)
    if r.returncode == 0:
        _print_ok("nginx -t (config valid)")
    else:
        _print_err(f"nginx -t failed:\n{r.stderr.strip() or r.stdout.strip()}")
        console.print("[yellow]  Config written but nginx not reloaded. Fix the error and run: nginx -s reload[/yellow]")
        return

    _nginx_reload()

    host_display = server_name if server_name != "_" else "<server-ip>"
    port_suffix  = f":{listen_port}" if listen_port not in (80, 443) else ""
    path_suffix  = nginx_path if nginx_path != "/" else "/"
    console.print(f"\n[bold green]Dashboard now reachable at http://{host_display}{port_suffix}{path_suffix}[/bold green]")
    _print_info(f"Upstream: http://127.0.0.1:{upstream_port}/  (runner-dashboard web service)")


def _nginx_uninstall() -> None:
    _require_root()
    conf_dir = _nginx_conf_path()
    console.print("\n[bold]Removing nginx reverse proxy config[/bold]")

    removed = False
    if conf_dir is not None:
        conf_file     = conf_dir / f"{SVC_NAME}.conf"
        sites_enabled = conf_dir.parent / "sites-enabled"
        if sites_enabled.is_dir():
            link = sites_enabled / conf_file.name
            if link.exists() or link.is_symlink():
                try:
                    link.unlink()
                    _print_ok(f"Removed symlink {link}")
                except OSError as exc:
                    _print_err(f"Could not remove symlink: {exc}")
        if conf_file.exists():
            try:
                conf_file.unlink()
                _print_ok(f"Removed {conf_file}")
                removed = True
            except PermissionError:
                _print_err(f"Permission denied removing {conf_file} \u2014 run with sudo")
                return
        else:
            _print_info(f"Config not found at {conf_file} \u2014 nothing to remove")
    else:
        _print_info("nginx config directory not found \u2014 nothing to remove")

    if not removed:
        return

    _nginx_restore_default_site()

    if platform.system() == "Linux":
        r = _run_cmd(["systemctl", "reload", "nginx"], check=False)
        label = "systemctl reload nginx"
    else:
        r = _run_cmd(["nginx", "-s", "reload"], check=False)
        label = "nginx -s reload"

    if r.returncode == 0:
        _print_ok(label)
    else:
        _print_info(f"reload skipped (nginx may not be running): {r.stderr.strip()}")


# ── Public dispatchers ────────────────────────────────────────────────────────

def install_service(args: argparse.Namespace) -> None:
    if platform.system() == "Linux":
        _systemd_install(args, user_scope=args.user_service)
    elif platform.system() == "Darwin":
        _launchd_install(args)
    else:
        console.print(f"[red]Service install not supported on {platform.system()}[/red]")
        sys.exit(1)


def uninstall_service(args: argparse.Namespace) -> None:
    if platform.system() == "Linux":
        _systemd_uninstall(user_scope=args.user_service)
    elif platform.system() == "Darwin":
        _launchd_uninstall()
    else:
        console.print(f"[red]Service uninstall not supported on {platform.system()}[/red]")
        sys.exit(1)


def service_status(args: argparse.Namespace) -> None:
    if platform.system() == "Linux":
        _systemd_status(user_scope=args.user_service)
    elif platform.system() == "Darwin":
        _launchd_status()
    else:
        console.print(f"[yellow]Service status not supported on {platform.system()}[/yellow]")


def install_nginx(args: argparse.Namespace) -> None:
    _nginx_install(args)


def uninstall_nginx() -> None:
    _nginx_uninstall()
