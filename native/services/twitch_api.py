"""Twitch Helix reads for ingest — the logged-in user's VODs and clips.

Uses the user token from ``twitch_auth`` plus the configured client ID. Metadata
reads need no additional scopes; explicit official download resolution does:

    GET /helix/users                              -> who am I (id, login)
    GET /helix/videos?user_id=&type=archive       -> past broadcasts (VODs)
    GET /helix/clips?broadcaster_id=&first=        -> clips
    GET /helix/clips/downloads                    -> temporary source URL

Legacy rows normalize to ``TwitchItem``. The persistent inbox uses ``TwitchClip``.
Temporary signed downloads are separate from both metadata/provenance records.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Literal, Optional

from native.services import twitch_auth
from native.core.twitch_inbox import TwitchClip, TwitchClipPage, parse_timestamp

HELIX = "https://api.twitch.tv/helix"


@dataclass
class TwitchItem:
    kind: Literal["vod", "clip"]
    id: str
    title: str
    url: str
    created_at: str
    duration: str          # human string, e.g. "1h2m3s" (VOD) or "30s" (clip)
    thumbnail_url: str


class TwitchAuthError(RuntimeError):
    """Raised when the stored token is missing or rejected (401) — reconnect."""


class ClipDownloadUnavailable(RuntimeError):
    """The selected clip has no usable official landscape source."""


@dataclass(frozen=True)
class TemporaryClipDownload:
    """In-memory transport input. Never persist this signed URL as provenance."""
    clip_id: str
    landscape_url: str = field(repr=False)


def own_clip_download(clip: TwitchClip, *, connection: twitch_auth.ValidatedTwitchConnection | None = None) -> TemporaryClipDownload:
    """Resolve one selected broadcaster-owned clip immediately before transfer.

    This does not download media or change OAuth grants. Official downloading
    requires an explicitly granted channel:manage:clips permission. Keep the
    resulting signed URL inside the transfer owner, away from UI/job metadata.
    """
    if type(clip) is not TwitchClip:
        raise ValueError("Select a validated Twitch clip")
    # Revalidate frozen records; callers can still bypass dataclass construction.
    TwitchClip(**vars(clip))
    if connection is not None:
        try:
            connection.require_own_clip(clip.broadcaster_id)
        except Exception:
            raise TwitchAuthError("Twitch own-clip authorization required.") from None
    try:
        account = current_user() if connection is None else _bound_user(connection)
    except TwitchAuthError:
        raise TwitchAuthError("Reconnect Twitch before downloading clips.") from None
    except RuntimeError:
        raise ClipDownloadUnavailable("Twitch account could not be verified. Try again later.") from None
    if account['id'] != clip.broadcaster_id:
        raise TwitchAuthError("Connect the broadcaster's Twitch account to download this clip.")
    try:
        params = {
            "broadcaster_id": clip.broadcaster_id,
            "editor_id": account['id'], "clip_id": clip.id,
        }
        response = _get("clips/downloads", params) if connection is None else _get_bound("clips/downloads", params, connection)
    except TwitchAuthError:
        raise TwitchAuthError("Reconnect Twitch with clip download permission.") from None
    except RuntimeError as exc:
        # Provider errors can contain a signed URL. Do not forward their body.
        if isinstance(exc.__cause__, urllib.error.HTTPError) and exc.__cause__.code == 403:
            raise TwitchAuthError("Twitch did not authorize clip downloads for this account.") from None
        raise ClipDownloadUnavailable("Twitch could not provide this clip's download. Try again later.") from None
    items = response.get('data') if isinstance(response, dict) else None
    if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
            or items[0].get('clip_id') != clip.id):
        raise ClipDownloadUnavailable("Twitch did not return the selected clip's download.")
    url = items[0].get('landscape_download_url')
    if url is None:
        raise ClipDownloadUnavailable("The landscape download is unavailable for this clip.")
    if not isinstance(url, str) or not url or len(url) > 32768:
        raise ClipDownloadUnavailable("Twitch returned an invalid clip download.")
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname and parsed.port in (None, 443)
                 and not parsed.username and not parsed.password and not parsed.fragment
                 and bool(parsed.path) and not any(ord(c) <= 32 or ord(c) == 127 for c in url))
    except ValueError:
        valid = False
    if not valid:
        raise ClipDownloadUnavailable("Twitch returned an invalid clip download.")
    return TemporaryClipDownload(clip.id, url)


def _get_bound(path: str, params: dict, connection: twitch_auth.ValidatedTwitchConnection) -> dict:
    """Intake-only requests with one pinned connection and bounded safe errors."""
    if path not in {"users", "clips/downloads"}:
        raise ValueError("Unsupported bound Twitch request")
    connection.require_own_clip(connection.user_id)
    req = urllib.request.Request(f"{HELIX}/{path}?{urllib.parse.urlencode(params)}", headers={
        "Authorization": f"Bearer {connection._access_token}", "Client-Id": connection.client_id})
    try:
        with twitch_auth._open_bound_request(req) as resp:
            raw = resp.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        def invalid_constant(_):
            raise ValueError()
        value = json.loads(raw.decode(), parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            exc.close()
        except Exception:
            pass
        if status in (401, 403):
            raise TwitchAuthError("Reconnect Twitch with clip download permission.") from None
        raise ClipDownloadUnavailable("Twitch bound request failed.") from None
    except Exception:
        raise ClipDownloadUnavailable("Twitch bound request failed.") from None


def _bound_user(connection):
    value = _get_bound("users", {}, connection)
    entries = value.get("data")
    if (not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict)
            or entries[0].get("id") != connection.user_id):
        raise TwitchAuthError("Twitch bound account differs.")
    return {"id": connection.user_id}


def _get(path: str, params: dict) -> dict:
    token = twitch_auth.current_token()
    cid = twitch_auth.client_id()
    if not token or not cid:
        raise TwitchAuthError("Not connected to Twitch.")
    url = f"{HELIX}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Client-Id": cid,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise TwitchAuthError("Twitch session expired — reconnect.") from exc
        body = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Twitch API error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error reaching Twitch: {exc.reason}") from exc


def current_user() -> dict:
    """Return the authenticated user's {id, login, display_name}."""
    data = _get("users", {})
    entries = data.get("data", [])
    if not entries:
        raise TwitchAuthError("Twitch did not return a user for this token.")
    u = entries[0]
    return {"id": u["id"], "login": u["login"], "display_name": u.get("display_name", u["login"])}


def list_vods(user_id: str, first: int = 20) -> list[TwitchItem]:
    data = _get("videos", {"user_id": user_id, "type": "archive", "first": first})
    return [
        TwitchItem(
            kind="vod",
            id=v["id"],
            title=v.get("title", "(untitled VOD)"),
            url=v.get("url", f"https://www.twitch.tv/videos/{v['id']}"),
            created_at=v.get("created_at", ""),
            duration=v.get("duration", ""),
            thumbnail_url=v.get("thumbnail_url", ""),
        )
        for v in data.get("data", [])
    ]


def clips_page(broadcaster_id: str, first: int = 100, *, after: str | None = None,
               started_at: str | None = None, ended_at: str | None = None) -> TwitchClipPage:
    """Read one Helix metadata page; source media and temporary download URLs stay separate.

    Helix orders channel clips by views, not creation time. Callers use explicit
    date windows and pagination rather than treating the first page as a stream inbox.
    """
    if not isinstance(broadcaster_id, str) or not broadcaster_id or len(broadcaster_id) > 512:
        raise ValueError("A broadcaster ID is required")
    if type(first) is not int or not 1 <= first <= 100:
        raise ValueError("Twitch clip page size must be between 1 and 100")
    params = {"broadcaster_id": broadcaster_id, "first": first}
    for key, value in (("after", after), ("started_at", started_at), ("ended_at", ended_at)):
        if value is not None:
            if not isinstance(value, str) or not value or len(value) > 2048:
                raise ValueError(f"Invalid Twitch {key}")
            params[key] = value
    start = parse_timestamp(started_at) if started_at is not None else None
    end = parse_timestamp(ended_at) if ended_at is not None else None
    if start is not None and end is not None and end <= start:
        raise ValueError("Twitch end time must be after the start time")
    data = _get("clips", params)
    if not isinstance(data, dict):
        raise ValueError("Invalid Twitch clips response")
    entries, pagination = data.get("data"), data.get("pagination", {})
    if not isinstance(entries, list) or len(entries) > first or not isinstance(pagination, dict):
        raise ValueError("Invalid Twitch clips page")
    cursor = pagination.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 2048):
        raise ValueError("Invalid Twitch pagination cursor")
    cursor = cursor or None
    clips = tuple(TwitchClip.from_helix(c) for c in entries)
    if any(c.broadcaster_id != broadcaster_id for c in clips):
        raise ValueError("Twitch returned a clip for another broadcaster")
    return TwitchClipPage(clips, cursor)


def list_clips(broadcaster_id: str, first: int = 20) -> list[TwitchItem]:
    data = _get("clips", {"broadcaster_id": broadcaster_id, "first": first})
    return [
        TwitchItem(
            kind="clip",
            id=c["id"],
            title=c.get("title", "(untitled clip)"),
            url=c.get("url", f"https://clips.twitch.tv/{c['id']}"),
            created_at=c.get("created_at", ""),
            duration=f"{round(float(c.get('duration', 0)))}s",
            thumbnail_url=c.get("thumbnail_url", ""),
        )
        for c in data.get("data", [])
    ]
