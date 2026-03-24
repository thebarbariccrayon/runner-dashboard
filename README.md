# runner-dashboard

Lightweight terminal & browser dashboard for monitoring a self-hosted GitHub Actions runner on the host where it executes.

## Features

- **Rich TUI** — live panels for runner status, current job, identity, system resources, and log tail
- **Web UI** — the same TUI rendered as HTML in any browser, auto-refreshed; scales to viewport width
- **Service installer** — one-command install as a systemd service (Linux) or LaunchAgent (macOS)
- **nginx reverse proxy** — optional installer writes a ready-to-use nginx vhost config with configurable path prefix

## Demo

https://github.com/user-attachments/assets/6e8d7b7f-3c7b-4344-94e9-2662cc548c70

## Requirements

- Python ≥ 3.9
- `rich` and `psutil`

## Install

```bash
pip install -e .
# or without editable install:
pip install rich psutil
python -m runner_dashboard
```

## Usage

```bash
# Terminal UI (default)
runner-dashboard
runner-dashboard --runner-path /opt/actions-runner
runner-dashboard --interval 10

# Print once and exit
runner-dashboard --once

# Browser UI on :8080
runner-dashboard --web
runner-dashboard --web --port 9090 --bind 127.0.0.1

# Install as a persistent background service (systemd / launchd)
sudo runner-dashboard --install
sudo runner-dashboard --install --port 9090
runner-dashboard --install --user-service        # Linux user-scope (no sudo)
runner-dashboard --install-status
sudo runner-dashboard --uninstall

# nginx reverse proxy
sudo runner-dashboard --install-nginx                          # :80/ → :8080
sudo runner-dashboard --install-nginx --nginx-path /runner    # :80/runner → :8080
sudo runner-dashboard --install-nginx \
    --nginx-listen-port 443 \
    --nginx-server-name dashboard.example.com
sudo runner-dashboard --uninstall-nginx
```

## Project layout

```
runner-dashboard/
├── pyproject.toml
├── README.md
└── runner_dashboard/
    ├── __init__.py       # version + shared console
    ├── constants.py      # RUNNER_SEARCH_PATHS, LOG_KEYWORDS, service name constants
    ├── collector.py      # all data-gathering (runner discovery, service status, job, resources)
    ├── tui.py            # Rich panel builders + build_dashboard()
    ├── web.py            # HTTP handler, TUI→HTML renderer, serve_web()
    ├── installer.py      # systemd, launchd, nginx install/uninstall helpers
    └── cli.py            # argparse entry point (main())
```
