"""Twitch login for the native shell — OAuth Device Code Flow.

Why device-code and not a loopback redirect: the new native shell has no web
server and no ``localhost`` origin, which is exactly what tripped the old
shared-auth path (``auth.deutschmark.online`` only allowlists web origins, so a
desktop ``localhost:<port>`` redirect got rejected). The device-code grant
needs no redirect URI and no local server at all — the user approves a short
code at ``twitch.tv/activate`` and we poll for the token. Public client, so no
client secret ships in the app.

Endpoints (Twitch):
    POST https://id.twitch.tv/oauth2/device   -> device_code + user_code
    POST https://id.twitch.tv/oauth2/token    -> poll until approved
    GET  https://id.twitch.tv/oauth2/validate -> check a stored token

The **client ID** is resolved from ``CLIPLINE_TWITCH_CLIENT_ID`` (env) or the
``twitch_client_id`` settings key. Until one is set the UI shows a "connect"
panel explaining how to register a Twitch app — the rest of the flow is wired
and waiting.
"""
from __future__ import annotations

import http.server
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional

from native.services.settings import load_settings, save_settings

DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Loopback "Sign in with Twitch" (implicit grant). The redirect URI must match
# exactly what's registered on the Twitch app — http://localhost:3000.
LOOPBACK_PORT = 3000
REDIRECT_URI = f"http://localhost:{LOOPBACK_PORT}"
PORT_BUSY_SENTINEL = "LOOPBACK_PORT_BUSY"

# No scopes needed: listing your own VODs/clips uses public Helix reads, and a
# bare user token still identifies the caller via /helix/users.
DEFAULT_SCOPES = ""

_SESSION_KEY = "twitch_session"

# Clipline's registered Twitch app (public client, Device Code Flow — no secret,
# so it's safe to ship embedded; this is how desktop OAuth clients work). Lets
# every user connect Twitch out of the box without registering their own app.
# Override via CLIPLINE_TWITCH_CLIENT_ID or the in-app "Set Client ID" prompt.
_DEFAULT_CLIENT_ID = "uclg74tt07ucpqfuo3302gyaj45uft"


# ── Client ID resolution ───────────────────────────────────────────

def client_id() -> str:
    """The Twitch app client ID. Env > user override > the bundled default."""
    env = os.environ.get("CLIPLINE_TWITCH_CLIENT_ID", "").strip()
    if env:
        return env
    configured = str(load_settings().get("twitch_client_id", "")).strip()
    return configured or _DEFAULT_CLIENT_ID


def set_client_id(value: str) -> None:
    settings = load_settings()
    settings["twitch_client_id"] = value.strip()
    save_settings(settings)


# ── HTTP helpers (stdlib only — no `requests` in the frozen build) ──

def _post_form(url: str, data: dict) -> tuple[int, dict]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, _read_json(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _read_json(exc.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error reaching Twitch: {exc.reason}") from exc


def _read_json(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode() or "{}")
    except Exception:
        return {}


# ── Loopback "Sign in with Twitch" (implicit grant) ────────────────

_CAPTURE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Clipline</title></head>
<body style="background:#0D1521;color:#CBD6E1;font-family:system-ui,sans-serif;text-align:center;padding-top:80px">
<h2 style="color:#7BD5E5">Finishing sign-in…</h2>
<script>
// Twitch returns the token in the URL fragment, which the browser never sends
// to the server. Re-deliver it as a query so the loopback listener can read it.
var h = window.location.hash.substring(1);
window.location.replace("/capture?" + (h || "error=no_fragment"));
</script></body></html>"""

_DONE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Clipline</title></head>
<body style="background:#0D1521;color:#CBD6E1;font-family:system-ui,sans-serif;text-align:center;padding-top:80px">
<h2 style="color:#7BD5E5">&#10003; Connected to Twitch</h2>
<p>You can close this tab and return to Clipline.</p></body></html>"""


def authorize_url(state: str, scopes: str = DEFAULT_SCOPES) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id(),
        "redirect_uri": REDIRECT_URI,
        "response_type": "token",
        "scope": scopes,
        "state": state,
    })


def loopback_login(job, cancel: Optional["object"] = None, scopes: str = DEFAULT_SCOPES) -> dict:
    """Worker: open Twitch in the browser, catch the redirect on a local port.

    Implicit grant (no secret). The user just clicks Authorize and Twitch
    bounces back to ``http://localhost:3000`` — no code, no activate page. If
    the port can't bind, raises ``PORT_BUSY_SENTINEL`` so the caller can fall
    back to the device-code flow.
    """
    if not client_id():
        raise RuntimeError("No Twitch client ID configured.")

    state = secrets.token_urlsafe(16)
    holder: dict = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):  # silence stderr access logs
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/capture":
                holder["params"] = {
                    k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()
                }
                self._send(_DONE_PAGE)
            elif parsed.path in ("/", "/favicon.ico"):
                self._send(_CAPTURE_PAGE if parsed.path == "/" else "")
            else:
                self._send(_CAPTURE_PAGE)

        def _send(self, body: str):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    try:
        server = http.server.HTTPServer(("127.0.0.1", LOOPBACK_PORT), _Handler)
    except OSError as exc:
        raise RuntimeError(PORT_BUSY_SENTINEL) from exc
    server.timeout = 1.0

    job.progress.emit("Opening Twitch in your browser…")
    webbrowser.open(authorize_url(state, scopes))

    deadline = time.monotonic() + 300
    try:
        while "params" not in holder and time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Twitch login cancelled.")
            server.handle_request()  # blocks up to server.timeout
    finally:
        server.server_close()

    got = holder.get("params")
    if not got:
        raise RuntimeError("Twitch login timed out — try again.")
    if got.get("error"):
        raise RuntimeError(got.get("error_description") or got["error"])
    if got.get("state") != state:
        raise RuntimeError("Twitch login state mismatch — try again.")
    token = got.get("access_token")
    if not token:
        raise RuntimeError("No access token returned by Twitch.")
    return _finalize_session({
        "access_token": token,
        "refresh_token": "",  # implicit grant returns none
        "expires_in": int(got.get("expires_in", 0) or 0),
        "scope": (got.get("scope", "") or "").split(),
    })


# ── Device Code Flow (fallback when the loopback port is busy) ──────

@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


def request_device_code(scopes: str = DEFAULT_SCOPES) -> DeviceCode:
    """Step 1: ask Twitch for a device + user code. Fast single POST."""
    cid = client_id()
    if not cid:
        raise RuntimeError("No Twitch client ID configured.")
    status, data = _post_form(DEVICE_URL, {"client_id": cid, "scopes": scopes})
    if status != 200 or "device_code" not in data:
        raise RuntimeError(data.get("message") or f"Twitch device request failed ({status}).")
    return DeviceCode(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data.get("verification_uri") or "https://www.twitch.tv/activate",
        interval=int(data.get("interval", 5)),
        expires_in=int(data.get("expires_in", 1800)),
    )


def poll_token(job, device: DeviceCode, cancel: Optional["object"] = None) -> dict:
    """Step 2 (worker thread): poll until the user approves, then persist.

    ``cancel`` is anything with ``.is_set()`` (a threading.Event) — checked
    between polls so the UI can abort. Returns the saved session dict.
    """
    cid = client_id()
    job.progress.emit(f"Waiting for approval — code {device.user_code}")
    deadline = time.monotonic() + device.expires_in
    interval = max(1, device.interval)

    while time.monotonic() < deadline:
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Twitch login cancelled.")
        # Cancellable sleep: wait() returns True if the event fired meanwhile.
        if cancel is not None and hasattr(cancel, "wait"):
            if cancel.wait(interval):
                raise RuntimeError("Twitch login cancelled.")
        else:
            time.sleep(interval)

        status, data = _post_form(TOKEN_URL, {
            "client_id": cid,
            "device_code": device.device_code,
            "grant_type": DEVICE_GRANT,
        })
        if status == 200 and data.get("access_token"):
            return _finalize_session(data)
        message = str(data.get("message", "")).lower()
        if "authorization_pending" in message or "pending" in message:
            continue
        if "slow_down" in message:
            interval += 1
            continue
        if "expired" in message:
            raise RuntimeError("Twitch login timed out — try again.")
        # Any other non-pending error is terminal.
        raise RuntimeError(data.get("message") or f"Twitch token error ({status}).")

    raise RuntimeError("Twitch login timed out — try again.")


def _finalize_session(token: dict) -> dict:
    """Validate the fresh token to capture login/user_id, then persist."""
    info = validate(token["access_token"]) or {}
    session = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token", ""),
        "expires_at": time.time() + int(token.get("expires_in", 0)),
        "login": info.get("login", ""),
        "user_id": info.get("user_id", ""),
        "scopes": token.get("scope", []),
    }
    save_session(session)
    return session


# ── Token validation + session storage ─────────────────────────────

def validate(access_token: str) -> Optional[dict]:
    """GET /oauth2/validate — returns {login, user_id, client_id, ...} or None."""
    req = urllib.request.Request(
        VALIDATE_URL, headers={"Authorization": f"OAuth {access_token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return _read_json(resp.read())
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError:
        return None


def load_session() -> Optional[dict]:
    session = load_settings().get(_SESSION_KEY)
    return session if isinstance(session, dict) and session.get("access_token") else None


def save_session(session: dict) -> None:
    settings = load_settings()
    settings[_SESSION_KEY] = session
    save_settings(settings)


def clear_session() -> None:
    settings = load_settings()
    settings.pop(_SESSION_KEY, None)
    save_settings(settings)


def current_token() -> Optional[str]:
    session = load_session()
    return session.get("access_token") if session else None
