"""
cli.py
------
Entry point for the `runner-dashboard` command.
"""

import argparse
import time

import psutil
from rich.live import Live

from . import console
from .collector import find_runner_path, get_service_name
from .installer import install_nginx, install_service, service_status, uninstall_nginx, uninstall_service
from .tui import build_dashboard
from .web import serve_web


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GitHub Actions Runner Dashboard \u2014 terminal & web monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  runner-dashboard                                      # TUI (default)
  runner-dashboard --runner-path /opt/actions-runner
  runner-dashboard --interval 10
  runner-dashboard --once                               # print once, exit

  runner-dashboard --web                                # web UI on :8080
  runner-dashboard --web --port 9090
  runner-dashboard --web --bind 127.0.0.1               # localhost only

  # Install / manage as a persistent background service:
  runner-dashboard --install                            # systemd (Linux) or launchd (macOS)
  runner-dashboard --install --port 9090                # custom port baked into service
  runner-dashboard --install --user-service             # (Linux) user-scope, no sudo
  runner-dashboard --install-status                     # check service state
  runner-dashboard --uninstall                          # stop & remove service

  # nginx reverse proxy (exposes web UI externally):
  runner-dashboard --install-nginx                          # proxy :80/ \u2192 :8080
  runner-dashboard --install-nginx --nginx-path /runner     # proxy :80/runner \u2192 :8080
  runner-dashboard --install-nginx --nginx-listen-port 443 --nginx-server-name dashboard.example.com
  runner-dashboard --install-nginx --port 9090              # if web service runs on 9090
  runner-dashboard --uninstall-nginx                        # remove nginx config
""",
    )

    parser.add_argument("--runner-path", "-r", default=None,
                        help="Path to the actions-runner install directory")
    parser.add_argument("--interval", "-i", type=float, default=5.0,
                        help="Refresh interval in seconds (default: 5)")

    # TUI-only flags
    parser.add_argument("--once", action="store_true",
                        help="(TUI) Print once and exit")
    parser.add_argument("--no-screen", action="store_true",
                        help="(TUI) Disable full-screen mode (useful inside tmux panes)")

    # Web UI flags
    parser.add_argument("--web", action="store_true",
                        help="Serve a browser-based dashboard instead of the TUI")
    parser.add_argument("--port", type=int, default=8080,
                        help="(web) TCP port to listen on (default: 8080)")
    parser.add_argument("--bind", default="0.0.0.0",
                        help="(web) Address to bind (default: 0.0.0.0)")
    parser.add_argument("--session-timeout", type=int, default=30,
                        metavar="MINUTES",
                        help="(web) Inactivity timeout for login sessions in minutes (default: 30)")
    parser.add_argument("--url-prefix", default="/",
                        help="(web) URL path prefix when served behind a reverse proxy (e.g. /runner)")

    # Installer flags
    parser.add_argument("--install", action="store_true",
                        help="Install web dashboard as a background system service and start it")
    parser.add_argument("--uninstall", action="store_true",
                        help="Stop and remove the installed system service")
    parser.add_argument("--install-status", action="store_true",
                        help="Show the current state of the installed service")
    parser.add_argument("--user-service", action="store_true",
                        help="(Linux) Install as a user-scope systemd service (no sudo required)")

    # nginx reverse proxy flags
    parser.add_argument("--install-nginx", action="store_true",
                        help="Install an nginx reverse proxy that forwards an external port to the web UI")
    parser.add_argument("--uninstall-nginx", action="store_true",
                        help="Remove the nginx reverse proxy config and reload nginx")
    parser.add_argument("--nginx-listen-port", type=int, default=80,
                        help="(nginx) External port nginx should listen on (default: 80)")
    parser.add_argument("--nginx-server-name", default="_",
                        help="(nginx) server_name directive value (default: _ catch-all)")
    parser.add_argument("--nginx-path", default="/",
                        help="(nginx) URL path prefix for the dashboard (default: /, e.g. /runner)")

    args = parser.parse_args()

    runner_path  = find_runner_path(args.runner_path)
    service_name = get_service_name(runner_path)

    if runner_path:
        console.print(f"[dim]Runner detected at: {runner_path}[/dim]")
    else:
        console.print("[yellow]Warning: runner installation not found \u2014 metrics will be limited.[/yellow]")

    # Prime psutil CPU sampler (first call always returns 0.0)
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass
    time.sleep(0.3)

    # ── Installer actions ─────────────────────────────────────────────────────
    if args.install_status:
        service_status(args)
        return

    if args.uninstall:
        uninstall_service(args)
        return

    if args.install:
        install_service(args)
        return

    if args.install_nginx:
        install_nginx(args)
        return

    if args.uninstall_nginx:
        uninstall_nginx()
        return

    # ── Web mode ──────────────────────────────────────────────────────────────
    if args.web:
        serve_web(args.runner_path, args.bind, args.port, args.interval,
                  session_timeout=args.session_timeout * 60.0,
                  url_prefix=args.url_prefix)
        return

    # ── TUI mode ──────────────────────────────────────────────────────────────
    if args.once:
        console.print(build_dashboard(runner_path, service_name))
        return

    try:
        with Live(
            build_dashboard(runner_path, service_name),
            console=console,
            refresh_per_second=1,
            screen=not args.no_screen,
        ) as live:
            while True:
                time.sleep(args.interval)
                live.update(build_dashboard(runner_path, service_name))
    except KeyboardInterrupt:
        console.print("\n[dim]Dashboard stopped.[/dim]")


if __name__ == "__main__":
    main()
