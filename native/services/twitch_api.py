"""Twitch Helix reads for ingest — the logged-in user's VODs and clips.

Uses the user token from ``twitch_auth`` plus the configured client ID. Only
public read endpoints are touched, so no extra OAuth scopes are required:

    GET /helix/users                              -> who am I (id, login)
    GET /helix/videos?user_id=&type=archive       -> past broadcasts (VODs)
    GET /helix/clips?broadcaster_id=&first=        -> clips

Items are normalized to ``TwitchItem`` so the Ingest list doesn't care whether
a row is a VOD or a clip — both carry a ``url`` we hand to yt-dlp.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Literal, Optional

from native.services import twitch_auth

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
