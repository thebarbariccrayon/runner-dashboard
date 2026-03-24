"""
collector.py
------------
All data-gathering: runner discovery, service status, current job, system
resources, runner config, and recent logs.

Nothing in this module imports from tui, web, or installer — it is a pure
data layer that can be used independently (e.g. in tests or future API routes).
"""

import json
import os
import platform
import re
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

from .constants import RUNNER_PROCESS_NAMES, RUNNER_SEARCH_PATHS
from . import console


# ── Runner discovery ──────────────────────────────────────────────────────────

def find_runner_path(hint: Optional[str] = None) -> Optional[Path]:
    """Locate the GitHub Actions runner installation directory."""
    if hint:
        p = Path(hint).expanduser().resolve()
        if p.exists():
            return p
        console.print(f"[yellow]Warning: specified runner path not found: {hint}[/yellow]")

    for path_str in RUNNER_SEARCH_PATHS:
        p = Path(path_str)
        if p.exists() and ((p / ".runner").exists() or (p / "config.sh").exists()):
            return p

    # Infer from live processes
    try:
        for proc in psutil.process_iter(["cmdline", "name"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
                name = proc.info.get("name") or ""
                if "Runner.Listener" in name or "Runner.Listener" in cmdline:
                    try:
                        cwd = Path(proc.cwd())
                        if cwd.exists():
                            return cwd
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass

    return None


# ── Service status ────────────────────────────────────────────────────────────

def _find_service_name_systemd() -> Optional[str]:
    """Discover the runner systemd unit name."""
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--no-pager", "-l", "--plain"],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.splitlines():
            lower = line.lower()
            if "actions.runner" in lower or "github-runner" in lower:
                parts = line.split()
                if parts:
                    return parts[0]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def get_service_name(runner_path: Optional[Path]) -> Optional[str]:
    """Return the service unit / daemon name for this runner."""
    if runner_path:
        svc_file = runner_path / ".service"
        if svc_file.exists():
            return svc_file.read_text().strip()
    if platform.system() == "Linux":
        return _find_service_name_systemd()
    return None


def get_service_status(
    service_name: Optional[str],
    runner_path: Optional[Path],
) -> Dict[str, Any]:
    status: Dict[str, Any] = {
        "state": "unknown",
        "pid": None,
        "uptime": None,
        "service_name": service_name,
    }

    # ── systemd (Linux) ──────────────────────────────────────────────────────
    if service_name and platform.system() == "Linux":
        try:
            result = subprocess.run(
                [
                    "systemctl", "show", service_name, "--no-pager",
                    "--property=ActiveState,SubState,MainPID,ActiveEnterTimestamp",
                ],
                capture_output=True, text=True, timeout=3,
            )
            props: Dict[str, str] = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v

            active = props.get("ActiveState", "unknown")
            sub    = props.get("SubState", "")
            if active == "active" and sub == "running":
                status["state"] = "running"
            elif active == "active":
                status["state"] = "active"
            elif active == "inactive":
                status["state"] = "stopped"
            elif active == "failed":
                status["state"] = "failed"
            else:
                status["state"] = active

            pid_str = props.get("MainPID", "0")
            if pid_str and pid_str != "0":
                status["pid"] = int(pid_str)

            ts = props.get("ActiveEnterTimestamp", "")
            if ts and ts not in ("n/a", ""):
                for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
                    try:
                        dt = datetime.strptime(ts.strip(), fmt)
                        status["uptime"] = datetime.utcnow() - dt
                        break
                    except ValueError:
                        continue

            return status
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # ── launchd (macOS) ──────────────────────────────────────────────────────
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["launchctl", "list"],
                capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.splitlines():
                lower = line.lower()
                if "actions" in lower or "github" in lower:
                    parts = line.split()
                    if parts and parts[0].lstrip("-").isdigit() and parts[0] != "-":
                        status["pid"]   = int(parts[0])
                        status["state"] = "running"
                    else:
                        status["state"] = "stopped"
                    break
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # ── process fallback ─────────────────────────────────────────────────────
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                name    = proc.info.get("name") or ""
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "Runner.Listener" in name or "Runner.Listener" in cmdline:
                    status["state"] = "running"
                    status["pid"]   = proc.info["pid"]
                    ct = proc.info.get("create_time")
                    if ct:
                        status["uptime"] = timedelta(seconds=time.time() - ct)
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass

    if status["state"] == "unknown":
        status["state"] = "stopped"

    return status


# ── Current job detection ────────────────────────────────────────────────────

def get_current_job(runner_path: Optional[Path]) -> Dict[str, Any]:
    """Detect whether a job is running and extract context from logs."""
    job: Dict[str, Any] = {
        "running":    False,
        "workflow":   None,
        "job_name":   None,
        "step":       None,
        "repo":       None,
        "run_id":     None,
        "started_at": None,
    }

    worker_start: Optional[float] = None
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                name    = proc.info.get("name") or ""
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if "Runner.Worker" in name or "Runner.Worker" in cmdline:
                    job["running"] = True
                    ct = proc.info.get("create_time")
                    if ct and (worker_start is None or ct < worker_start):
                        worker_start = ct
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass

    if worker_start:
        job["started_at"] = datetime.fromtimestamp(worker_start)

    if runner_path:
        diag = runner_path / "_diag"
        if diag.exists():
            log_files = sorted(
                diag.glob("Runner_*.log"),
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )
            if log_files:
                _parse_runner_log(log_files[0], job)

    return job


def _parse_runner_log(log_path: Path, job: Dict[str, Any]) -> None:
    """Scan recent log lines and populate job metadata."""
    try:
        with open(log_path, errors="replace") as fh:
            lines = list(deque(fh, maxlen=300))
    except OSError:
        return

    for line in reversed(lines):
        m = re.search(r"Running job:\s*(.+?)(?:\s*\(run #(\d+)\))?$", line)
        if m:
            if not job["job_name"]:
                job["job_name"] = m.group(1).strip()
            if m.group(2) and not job["run_id"]:
                job["run_id"] = m.group(2)
            job["running"] = True
            continue

        m = re.search(r"Start step '(.+?)'", line)
        if m and not job["step"]:
            job["step"] = m.group(1)

        if not job["workflow"]:
            m = re.search(r'"workflowRef"\s*:\s*"([^"]+)"', line)
            if m:
                job["workflow"] = m.group(1).split("/")[-1]

        if not job["repo"]:
            m = re.search(r'"repoFullName"\s*:\s*"([^"]+)"', line)
            if not m:
                m = re.search(r'"repository"\s*:\s*"([^"]+)"', line)
            if m:
                job["repo"] = m.group(1)

        if not job["run_id"]:
            m = re.search(r'"runId"\s*:\s*(\d+)', line)
            if m:
                job["run_id"] = m.group(1)

        if "Job completed" in line or "Finished job" in line:
            break


# ── Log tail ──────────────────────────────────────────────────────────────────

def get_recent_logs(runner_path: Optional[Path], n: int = 18) -> List[str]:
    """Return the last N lines from the most recently modified runner log."""
    if not runner_path:
        return ["[No runner path configured]"]

    diag = runner_path / "_diag"
    if not diag.exists():
        return [f"[No _diag folder at {diag}]"]

    logs = sorted(diag.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True)
    if not logs:
        return ["[No log files found in _diag/]"]

    try:
        with open(logs[0], errors="replace") as fh:
            lines = list(deque(fh, maxlen=n))
        return [line.rstrip() for line in lines]
    except OSError as exc:
        return [f"[Error reading log: {exc}]"]


# ── System resources ──────────────────────────────────────────────────────────

def get_system_resources(runner_path: Optional[Path]) -> Dict[str, Any]:
    res: Dict[str, Any] = {
        "cpu_percent":   0.0,
        "mem_percent":   0.0,
        "mem_used_gb":   0.0,
        "mem_total_gb":  0.0,
        "disk_percent":  0.0,
        "disk_used_gb":  0.0,
        "disk_total_gb": 0.0,
        "load_avg":      (0.0, 0.0, 0.0),
        "runner_cpu":    0.0,
        "runner_mem_mb": 0.0,
    }
    try:
        res["cpu_percent"] = psutil.cpu_percent(interval=None)

        mem = psutil.virtual_memory()
        res["mem_percent"]  = mem.percent
        res["mem_used_gb"]  = mem.used  / (1024 ** 3)
        res["mem_total_gb"] = mem.total / (1024 ** 3)

        disk_target = str(runner_path) if runner_path and runner_path.exists() else "/"
        disk = psutil.disk_usage(disk_target)
        res["disk_percent"]  = disk.percent
        res["disk_used_gb"]  = disk.used  / (1024 ** 3)
        res["disk_total_gb"] = disk.total / (1024 ** 3)

        if hasattr(os, "getloadavg"):
            res["load_avg"] = os.getloadavg()

        r_cpu = 0.0
        r_mem = 0.0
        for proc in psutil.process_iter(["name", "cmdline", "cpu_percent", "memory_info"]):
            try:
                name    = proc.info.get("name") or ""
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if any(rn in name or rn in cmdline for rn in RUNNER_PROCESS_NAMES):
                    r_cpu += proc.info.get("cpu_percent") or 0.0
                    mi = proc.info.get("memory_info")
                    if mi:
                        r_mem += mi.rss / (1024 ** 2)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        res["runner_cpu"]    = r_cpu
        res["runner_mem_mb"] = r_mem
    except Exception:
        pass
    return res


# ── Runner identity ───────────────────────────────────────────────────────────

def get_runner_config(runner_path: Optional[Path]) -> Dict[str, str]:
    info: Dict[str, str] = {}
    if not runner_path:
        return info
    config_file = runner_path / ".runner"
    if config_file.exists():
        try:
            with open(config_file) as fh:
                data = json.load(fh)
            info["name"] = data.get("agentName", "")
            info["url"]  = data.get("serverUrl", "")
            info["pool"] = data.get("poolName", "")
        except (json.JSONDecodeError, OSError):
            pass
    return info


# ── JSON snapshot (used by web /api/data) ─────────────────────────────────────

def get_dashboard_data(
    runner_path: Optional[Path],
    service_name: Optional[str],
) -> Dict[str, Any]:
    """Return all dashboard state as a JSON-serialisable dict."""
    status = get_service_status(service_name, runner_path)
    job    = get_current_job(runner_path)
    res    = get_system_resources(runner_path)
    logs   = get_recent_logs(runner_path, n=18)
    config = get_runner_config(runner_path)

    uptime_s  = int(status["uptime"].total_seconds()) if status.get("uptime") else None
    elapsed_s = (
        int((datetime.now() - job["started_at"]).total_seconds())
        if job.get("started_at") else None
    )

    return {
        "ts":          datetime.now().strftime("%Y-%m-%d  %H:%M:%S"),
        "runner_path": str(runner_path) if runner_path else None,
        "status": {
            "state":        status["state"],
            "pid":          status.get("pid"),
            "uptime_s":     uptime_s,
            "service_name": status.get("service_name"),
        },
        "job": {
            "running":   job["running"],
            "workflow":  job.get("workflow"),
            "job_name":  job.get("job_name"),
            "step":      job.get("step"),
            "repo":      job.get("repo"),
            "run_id":    job.get("run_id"),
            "elapsed_s": elapsed_s,
        },
        "config":    config,
        "resources": {
            "cpu_percent":   res["cpu_percent"],
            "mem_percent":   res["mem_percent"],
            "mem_used_gb":   res["mem_used_gb"],
            "mem_total_gb":  res["mem_total_gb"],
            "disk_percent":  res["disk_percent"],
            "disk_used_gb":  res["disk_used_gb"],
            "disk_total_gb": res["disk_total_gb"],
            "load_avg":      list(res["load_avg"]),
            "runner_cpu":    res["runner_cpu"],
            "runner_mem_mb": res["runner_mem_mb"],
        },
        "logs": logs,
    }
