"""Shared constants."""

import os
from typing import Dict, List

RUNNER_SEARCH_PATHS: List[str] = [
    os.path.expanduser("~/actions-runner"),
    "/opt/actions-runner",
    "/home/runner/actions-runner",
    "/home/github/actions-runner",
    "/var/lib/actions-runner",
    "/runner",
    "/actions-runner",
]

RUNNER_PROCESS_NAMES = {"Runner.Listener", "Runner.Worker", "runsvc.sh"}

# Log line colorization keywords
LOG_KEYWORDS: Dict[str, str] = {
    "ERROR":           "bold red",
    "WARN":            "bold yellow",
    "WARNING":         "bold yellow",
    "FAIL":            "bold red",
    "Running job":     "bold cyan",
    "Job completed":   "bold green",
    "Succeeded":       "bold green",
    "Listening for Jobs": "green",
    "INFO":            "default",
}

# Service identifiers used by the installer
SVC_NAME    = "runner-dashboard"
SYSTEMD_SVC = f"{SVC_NAME}.service"
LAUNCHD_ID  = f"com.github.{SVC_NAME}"
