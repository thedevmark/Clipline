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
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
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


class _RejectAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Twitch authentication redirect refused.")


def _open_bound_request(request):
    """Do not forward a pinned token to redirects or ambient proxy credentials."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectAuthRedirect())
    return opener.open(request, timeout=15)


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


def authorize_url(state: str, scopes: str = DEFAULT_SCOPES, *, bound_client_id: str | None = None) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id() if bound_client_id is None else bound_client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "token",
        "scope": scopes,
        "state": state,
    })


def loopback_login(job, cancel: Optional["object"] = None, scopes: str = DEFAULT_SCOPES, *,
                   bound_client_id: str | None = None, expected_user_id: str | None = None,
                   required_scopes: tuple[str, ...] = ()) -> dict:
    """Worker: open Twitch in the browser, catch the redirect on a local port.

    Implicit grant (no secret). The user just clicks Authorize and Twitch
    bounces back to ``http://localhost:3000`` — no code, no activate page. If
    the port can't bind, raises ``PORT_BUSY_SENTINEL`` so the caller can fall
    back to the device-code flow.
    """
    cid = client_id() if bound_client_id is None else bound_client_id
    if not cid:
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
    webbrowser.open(authorize_url(state, scopes, bound_client_id=cid))

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
    }, expected_client_id=cid, expected_user_id=expected_user_id, required_scopes=required_scopes, cancel=cancel)


# ── Device Code Flow (fallback when the loopback port is busy) ──────

@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int
    bound_client_id: str = ""


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
        bound_client_id=cid,
    )


def poll_token(job, device: DeviceCode, cancel: Optional["object"] = None) -> dict:
    """Step 2 (worker thread): poll until the user approves, then persist.

    ``cancel`` is anything with ``.is_set()`` (a threading.Event) — checked
    between polls so the UI can abort. Returns the saved session dict.
    """
    cid = device.bound_client_id or client_id()
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
            return _finalize_session(data, expected_client_id=cid)
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


def _validated_summary(info, expected_client_id):
    """Validate provider identity without persisting or returning token material."""
    if not isinstance(info, dict):
        raise RuntimeError("Twitch identity validation failed.")
    for name in ("login", "user_id", "client_id"):
        value = info.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,512}", value):
            raise RuntimeError("Twitch identity validation failed.")
    if not expected_client_id or info["client_id"] != expected_client_id:
        raise RuntimeError("Twitch client validation failed.")
    scopes = info.get("scopes")
    if (not isinstance(scopes, list) or len(scopes) > 100
            or any(not isinstance(scope, str) or not re.fullmatch(r"[a-z][a-z0-9:_]{0,127}", scope) for scope in scopes)):
        raise RuntimeError("Twitch scope validation failed.")
    expiry = info.get("expires_in")
    if type(expiry) is not int or not 0 < expiry <= 2**31-1:
        raise RuntimeError("Twitch expiry validation failed.")
    return {"login": info["login"], "user_id": info["user_id"],
            "client_id": info["client_id"], "scopes": sorted(set(scopes)), "expires_in": expiry}


@dataclass(frozen=True)
class ValidatedTwitchConnection:
    """One live-validated intake connection; never serialize the private token."""
    _access_token: str = field(repr=False)
    client_id: str
    user_id: str
    login: str
    scopes: tuple[str, ...]
    expires_at: float

    def require_own_clip(self, broadcaster_id: str) -> None:
        if (self.user_id != broadcaster_id or time.time() >= self.expires_at
                or "channel:manage:clips" not in self.scopes):
            raise RuntimeError("Twitch own-clip authorization required.")


def validated_intake_connection(*, expected_broadcaster_id: str, bound_client_id: str | None = None) -> ValidatedTwitchConnection:
    """Read and validate once inside the owned worker; leave stored auth unchanged."""
    cid = client_id() if bound_client_id is None else bound_client_id
    token = current_token()
    if not isinstance(token, str) or not token or len(token) > 32768:
        raise RuntimeError("Twitch own-clip authorization required.")
    try:
        info = _validated_summary(validate(token, max_bytes=65536), cid)
    except Exception:
        raise RuntimeError("Twitch own-clip authorization required.") from None
    connection = ValidatedTwitchConnection(token, cid, info["user_id"], info["login"],
                                          tuple(info["scopes"]), time.time() + info["expires_in"])
    connection.require_own_clip(expected_broadcaster_id)
    return connection


def clip_download_scopes(validated_scopes) -> str:
    """Explicit incremental-grant input; this function starts no authorization."""
    if not isinstance(validated_scopes, (list, tuple)) or len(validated_scopes) > 100:
        raise RuntimeError("Twitch scope validation failed.")
    summary = _validated_summary({"login": "scope", "user_id": "scope", "client_id": "scope",
                                  "scopes": list(validated_scopes), "expires_in": 1}, "scope")
    return " ".join(sorted(set(summary["scopes"]) | {"channel:manage:clips"}))


def _finalize_session(token: dict, *, expected_client_id: str | None = None,
                      expected_user_id: str | None = None, required_scopes: tuple[str, ...] = (), cancel=None) -> dict:
    """Validate the fresh token to capture login/user_id, then persist."""
    try:
        if not isinstance(token, dict) or not isinstance(token.get("access_token"), str) or not token["access_token"]:
            raise ValueError()
        info = _validated_summary(validate(token["access_token"], max_bytes=65536),
                                  client_id() if expected_client_id is None else expected_client_id)
        if expected_user_id is not None and info['user_id'] != expected_user_id:
            raise ValueError()
        if (not isinstance(required_scopes, (tuple, list)) or len(required_scopes)>100
                or any(not isinstance(scope,str) or not re.fullmatch(r'[a-z][a-z0-9:_]{0,127}',scope) for scope in required_scopes)
                or not set(required_scopes).issubset(info['scopes'])):
            raise ValueError()
        expiry = token.get("expires_in", info["expires_in"])
        if type(expiry) is not int or not 0 < expiry <= 2**31-1:
            raise ValueError()
        refresh = token.get("refresh_token", "")
        if not isinstance(refresh, str) or len(refresh) > 32768:
            raise ValueError()
    except Exception:
        raise RuntimeError("Twitch identity validation failed; the previous session is unchanged.") from None
    session = {
        "access_token": token["access_token"],
        "refresh_token": refresh,
        "expires_at": time.time() + min(expiry, info["expires_in"]),
        "login": info["login"],
        "user_id": info["user_id"],
        "scopes": info["scopes"],
    }
    if cancel is not None and cancel.is_set():
        raise RuntimeError('Twitch authorization cancelled; the previous session is unchanged.')
    save_session(session)
    return session


def authorize_clip_downloads(job, cancel, *, expected_broadcaster_id: str, bound_client_id: str) -> dict:
    """Explicit incremental action. Only the auth owner handles private session data."""
    if (not isinstance(expected_broadcaster_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}',expected_broadcaster_id)
            or not isinstance(bound_client_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}',bound_client_id)):
        raise RuntimeError('The selected Twitch account is invalid.')
    if cancel is not None and cancel.is_set():
        raise RuntimeError('Twitch clip authorization cancelled.')
    try:
        previous=load_session()
        recorded=previous.get('scopes',[]) if isinstance(previous,dict) and previous.get('user_id')==expected_broadcaster_id else []
        try:
            requested=clip_download_scopes(recorded)
        except RuntimeError:
            requested=clip_download_scopes([])
        # Recorded scope names are request input, never proof of permission.
        session=loopback_login(job,cancel,requested,bound_client_id=bound_client_id,
            expected_user_id=expected_broadcaster_id,required_scopes=('channel:manage:clips',))
        return {'user_id':session['user_id'],'login':session['login'],'scopes':tuple(session['scopes'])}
    except Exception:
        if cancel is not None and cancel.is_set():
            raise RuntimeError('Twitch clip authorization cancelled.') from None
        raise RuntimeError('Twitch clip authorization failed. The previous session is unchanged.') from None


# ── Token validation + session storage ─────────────────────────────

def validate(access_token: str, *, max_bytes: int | None = None) -> Optional[dict]:
    """GET /oauth2/validate — returns {login, user_id, client_id, ...} or None."""
    if max_bytes is not None and (type(max_bytes) is not int or not 1 <= max_bytes <= 65536):
        raise ValueError("Bounded validation response limit required")
    req = urllib.request.Request(
        VALIDATE_URL, headers={"Authorization": f"OAuth {access_token}"}
    )
    try:
        response = urllib.request.urlopen(req, timeout=15) if max_bytes is None else _open_bound_request(req)
        with response as resp:
            raw = resp.read() if max_bytes is None else resp.read(max_bytes + 1)
            return None if max_bytes is not None and len(raw) > max_bytes else _read_json(raw)
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
