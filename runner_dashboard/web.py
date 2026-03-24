"""
web.py
------
Built-in HTTP server, TUI→HTML fragment renderer, and serve_web() entry point.
"""

import html as _html
import http.cookies
import json
import re
import secrets
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Console

from .collector import find_runner_path, get_dashboard_data, get_service_name
from .tui import build_dashboard

# ── PAM authentication ────────────────────────────────────────────────────────

try:
    import pam as _pam_module
    _PAM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _pam_module = None  # type: ignore[assignment]
    _PAM_AVAILABLE = False

_PAM_TIMEOUT = 10.0  # maximum seconds to wait for the PAM stack to respond


def _pam_authenticate(username: str, password: str) -> bool:
    """Authenticate *username*/*password* via the system PAM stack.

    The blocking PAM call runs inside a worker thread with a hard timeout so a
    stalled PAM module cannot hang the web server indefinitely.
    """
    if not _PAM_AVAILABLE:
        raise RuntimeError(
            "python-pam is not installed. Run: pip install python-pam"
        )

    def _auth() -> bool:
        p = _pam_module.pam()
        return bool(p.authenticate(username, password, service="login"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_auth)
        try:
            return future.result(timeout=_PAM_TIMEOUT)
        except _FuturesTimeout:
            return False


# ── Login page shell ──────────────────────────────────────────────────────────

LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark">
<title>Sign In \u2013 GitHub Actions Runner Dashboard</title>
<style>
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#0d1117;color:#c9d1d9;
  font-family:ui-monospace,'Cascadia Code','Fira Code',monospace;
  min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#161b22;border:1px solid #30363d;border-radius:6px;
  padding:32px;width:360px;max-width:90vw}
h1{margin:0 0 24px;font-size:14px;font-weight:700;color:#fff;text-align:center;
  letter-spacing:.02em}
h1 span{color:#3fb950}
label{display:block;font-size:11px;color:#6e7681;margin-bottom:4px;
  text-transform:uppercase;letter-spacing:.05em}
input[type=text],input[type=password]{
  display:block;width:100%;padding:8px 10px;margin-bottom:16px;
  background:#0d1117;border:1px solid #30363d;border-radius:4px;
  color:#c9d1d9;font-family:inherit;font-size:13px;outline:none;
  transition:border-color .15s}
input:focus{border-color:#58a6ff}
button{display:block;width:100%;padding:9px;background:#238636;
  border:1px solid #2ea043;border-radius:4px;color:#fff;
  font-family:inherit;font-size:13px;font-weight:700;cursor:pointer;
  transition:background .15s}
button:hover{background:#2ea043}
.err{background:#3b1f1f;border:1px solid #f85149;border-radius:4px;
  color:#f85149;font-size:12px;padding:8px 10px;margin-bottom:16px;
  text-align:center}
footer{margin-top:20px;text-align:center;font-size:11px;color:#6e7681}
</style>
</head>
<body>
<div class="card">
  <h1>&#9654; <span>Runner</span> Dashboard</h1>
  __ERROR__
  <form method="POST" action="/login">
    <label for="u">Username</label>
    <input id="u" type="text" name="username" autocomplete="username" autofocus>
    <label for="p">Password</label>
    <input id="p" type="password" name="password" autocomplete="current-password">
    <button type="submit">Sign In</button>
  </form>
  <footer>Authenticated via system PAM</footer>
</div>
</body>
</html>
"""

# ── Embedded page shell ───────────────────────────────────────────────────────

WEB_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark">
<title>GitHub Actions Runner Dashboard</title>
<style>
html,body{margin:0;padding:0;background:#0d1117;overflow-x:auto;color-scheme:dark}
#bar{
  background:#161b22;border-bottom:1px solid #30363d;
  padding:5px 16px;display:flex;align-items:center;gap:12px;
  font-family:ui-monospace,'Cascadia Code','Fira Code',monospace;
  font-size:12px;color:#6e7681;position:sticky;top:0;z-index:10;
}
#bar .title{color:#fff;font-weight:700;font-size:13px}
#bar .spacer{flex:1}
#bar .cd{font-size:11px}
#badge{padding:2px 10px;border-radius:3px;font-size:11px;font-weight:700}
#badge.ok{background:#1f3b2f;color:#3fb950}
#badge.err{background:#3b1f1f;color:#f85149}
#logout{color:#6e7681;text-decoration:none;font-size:11px;
  padding:2px 8px;border:1px solid #30363d;border-radius:3px}
#logout:hover{color:#c9d1d9;border-color:#6e7681}
#screen{padding:4px 6px}
#screen pre{margin:0}
</style>
</head>
<body>
<div id="bar">
  <span class="title">&#9654; GitHub Actions Runner Dashboard</span>
  <span class="spacer"></span>
  <span class="cd">next refresh in <span id="cdn">—</span>s</span>
  <span id="badge" class="ok">&#9679; LIVE</span>
  <a href="/logout" id="logout">Sign out</a>
</div>
<span id="_cw" aria-hidden="true" style="position:absolute;visibility:hidden;
  font-family:ui-monospace,'Cascadia Code','Fira Code',monospace;font-size:14px">M</span>
<div id="screen"><pre style="color:#c9d1d9;background:#0d1117;
  font-family:ui-monospace,monospace;padding:8px">Loading\u2026</pre></div>
<script>
const IV=__INTERVAL__;
let cd=IV,tm=null;
function cols(){
  const cw=document.getElementById('_cw').offsetWidth||8;
  const pad=12;
  return Math.max(80,Math.floor((window.innerWidth-pad)/cw));
}
function tick(){cd--;document.getElementById('cdn').textContent=Math.max(cd,0);if(cd<=0)poll();}
function poll(){
  clearInterval(tm);
  fetch('./api/render?cols='+cols()).then(r=>{if(!r.ok)throw r;return r.text();})
  .then(html=>{
    document.getElementById('screen').innerHTML=html;
    document.getElementById('badge').className='ok';
    document.getElementById('badge').textContent='\u25cf LIVE';
    cd=IV;tm=setInterval(tick,1000);
  }).catch(()=>{
    document.getElementById('badge').className='err';
    document.getElementById('badge').textContent='\u2717 DISCONNECTED';
    cd=IV;tm=setInterval(tick,1000);
  });
}
poll();
</script>
</body>
</html>
"""


# ── TUI → HTML fragment renderer ─────────────────────────────────────────────

def render_tui_fragment(
    runner_path: Optional[Path],
    service_name: Optional[str],
    cols: int = 220,
) -> bytes:
    """
    Render the Rich TUI into an HTML <pre> fragment at the requested column width.
    The browser measures its viewport in characters and passes `cols` as a query
    param so the layout re-flows to fill the screen exactly.
    """
    cols = max(cols, 80)
    tmp = Console(
        record=True,
        width=cols,
        force_terminal=True,
        no_color=False,
        color_system="truecolor",
        highlight=False,
    )
    tmp.print(build_dashboard(runner_path, service_name))
    full_html = tmp.export_html(inline_styles=True)
    m = re.search(r'(<pre [^>]*>.*?</pre>)', full_html, re.DOTALL)
    fragment = (
        m.group(1) if m
        else f'<pre style="background:#0d1117;color:#c9d1d9">{full_html}</pre>'
    )
    return fragment.encode()


# ── HTTP handler factory ──────────────────────────────────────────────────────

_MAX_POST_BODY = 4096  # bytes – caps memory allocated when reading login form body


def _make_handler(runner_path_hint: Optional[str], interval: float, session_timeout: float):
    """Return a BaseHTTPRequestHandler subclass that serves the dashboard."""
    _html_bytes = WEB_HTML.replace("__INTERVAL__", str(int(interval))).encode()

    # Re-discover runner path at most once per TTL to avoid hammering the FS
    _cache: Dict[str, Any] = {"runner_path": None, "service_name": None, "ts": 0.0}
    _ttl = max(interval, 30.0)

    # Sessions: token -> expiry UNIX timestamp (sliding window on each request)
    _sessions: Dict[str, float] = {}

    def _resolved() -> tuple:
        now = time.time()
        if now - _cache["ts"] >= _ttl:
            rp = find_runner_path(runner_path_hint)
            _cache["runner_path"]  = rp
            _cache["service_name"] = get_service_name(rp)
            _cache["ts"] = now
        return _cache["runner_path"], _cache["service_name"]

    class _Handler(BaseHTTPRequestHandler):
        # ── Session helpers ────────────────────────────────────────────────────

        def _session_token(self) -> Optional[str]:
            """Extract the session token from the Cookie header, or None."""
            raw = self.headers.get("Cookie", "")
            if not raw:
                return None
            jar: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
            try:
                jar.load(raw)
            except http.cookies.CookieError:
                return None
            morsel = jar.get("session")
            return morsel.value if morsel else None

        def _is_authenticated(self) -> bool:
            """Return True if the request carries a valid, non-expired session.

            Valid sessions have their expiry refreshed (sliding window).
            """
            token = self._session_token()
            if token is None:
                return False
            expiry = _sessions.get(token)
            if expiry is None:
                return False
            if time.time() > expiry:
                _sessions.pop(token, None)
                return False
            # Sliding window: refresh on each authenticated request
            _sessions[token] = time.time() + session_timeout
            return True

        def _create_session(self) -> str:
            """Mint a new session token, store it, and return it."""
            now = time.time()
            # Prune already-expired sessions
            expired = [t for t, exp in list(_sessions.items()) if now > exp]
            for t in expired:
                _sessions.pop(t, None)
            token = secrets.token_urlsafe(32)
            _sessions[token] = now + session_timeout
            return token

        def _destroy_session(self) -> None:
            """Remove the caller's session from the store."""
            token = self._session_token()
            if token:
                _sessions.pop(token, None)

        def _require_auth(self) -> bool:
            """Redirect to /login if not authenticated; return True if OK."""
            if not self._is_authenticated():
                self._redirect("/login")
                return False
            return True

        # ── Route helpers ──────────────────────────────────────────────────────

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # ── GET ────────────────────────────────────────────────────────────────

        def do_GET(self):
            path = self.path.split("?", 1)[0]  # strip query string for routing

            # Public routes (no auth required)
            if path == "/login":
                body = LOGIN_HTML.replace("__ERROR__", "").encode()
                self._send(200, "text/html; charset=utf-8", body)
                return

            if path == "/logout":
                self._destroy_session()
                self.send_response(303)
                self.send_header("Location", "/login")
                self.send_header(
                    "Set-Cookie",
                    "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            # All remaining routes require a valid session
            if not self._require_auth():
                return

            if path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", _html_bytes)

            elif path == "/api/render":
                try:
                    rp, sn = _resolved()
                    qs   = self.path.split("?", 1)[1] if "?" in self.path else ""
                    cols = 220
                    for part in qs.split("&"):
                        if part.startswith("cols="):
                            try:
                                cols = int(part[5:])
                            except ValueError:
                                pass
                    self._send(200, "text/html; charset=utf-8",
                               render_tui_fragment(rp, sn, cols))
                except Exception as exc:
                    self._send(500, "text/plain", str(exc).encode())

            elif path == "/api/data":
                try:
                    rp, sn = _resolved()
                    body = json.dumps(get_dashboard_data(rp, sn)).encode()
                    self._send(200, "application/json", body)
                except Exception as exc:
                    self._send(500, "application/json",
                               json.dumps({"error": str(exc)}).encode())

            else:
                self._send(404, "text/plain", b"Not Found")

        # ── POST ───────────────────────────────────────────────────────────────

        def do_POST(self):
            if self.path != "/login":
                self._send(405, "text/plain", b"Method Not Allowed")
                return

            ctype = self.headers.get("Content-Type", "")
            if not ctype.startswith("application/x-www-form-urlencoded"):
                self._send(415, "text/plain", b"Unsupported Media Type")
                return

            try:
                length = min(int(self.headers.get("Content-Length", 0)), _MAX_POST_BODY)
            except ValueError:
                length = 0
            raw_body = self.rfile.read(length)
            params = urllib.parse.parse_qs(
                raw_body.decode("utf-8", errors="replace"),
                keep_blank_values=True,
            )
            username = params.get("username", [""])[0].strip()
            password = params.get("password", [""])[0]

            error = ""
            if not username or not password:
                error = "Username and password are required."
            else:
                try:
                    ok = _pam_authenticate(username, password)
                except RuntimeError as exc:
                    ok = False
                    error = str(exc)
                if not ok and not error:
                    time.sleep(1.0)  # slow brute-force attempts
                    error = "Invalid credentials."

            if error:
                page = LOGIN_HTML.replace(
                    "__ERROR__",
                    f'<div class="err">{_html.escape(error)}</div>',
                )
                self._send(200, "text/html; charset=utf-8", page.encode())
                return

            token = self._create_session()
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"session={token}; HttpOnly; SameSite=Strict; "
                f"Path=/; Max-Age={int(session_timeout)}",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET()

        def log_message(self, fmt, *args):  # silence access log
            pass

    return _Handler


# ── Public entry point ────────────────────────────────────────────────────────

def serve_web(
    runner_path_hint: Optional[str],
    host: str,
    port: int,
    interval: float,
    session_timeout: float = 1800.0,
) -> None:
    handler = _make_handler(runner_path_hint, interval, session_timeout)
    server  = HTTPServer((host, port), handler)
    from . import console
    console.print(f"[bold green]Web dashboard running at http://{host}:{port}/[/bold green]")
    console.print(
        f"[dim]Session timeout: {int(session_timeout // 60)} min  •  Press Ctrl+C to stop[/dim]"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Web server stopped.[/dim]")
