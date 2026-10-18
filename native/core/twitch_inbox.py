"""Stable Twitch clip provenance, independent of media downloads or timelines."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
from urllib.parse import urlsplit


def _text(value, field, *, empty=False):
    if not isinstance(value, str) or len(value) > 2048 or (not empty and not value):
        raise ValueError(f"Invalid Twitch {field}")
    return value


def _integer(value, field, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value <= 2**63 - 1:
        raise ValueError(f"Invalid Twitch {field}")
    return value


def _seconds_us(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("Invalid Twitch duration")
    try:
        seconds = Decimal(str(value))
        if (not seconds.is_finite() or seconds < Decimal("0.000001")
                or seconds > Decimal("9223372036854.775807")):
            raise ValueError("Invalid Twitch duration")
        with localcontext() as context:
            context.prec = max(28, len(seconds.as_tuple().digits) + 6)
            micros = seconds * 1_000_000
        if micros != micros.to_integral_value():
            raise ValueError("Twitch duration cannot be represented in microseconds")
        return _integer(int(micros), "duration", positive=True)
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError("Invalid Twitch duration") from exc


def parse_timestamp(value: str) -> datetime:
    _text(value, "timestamp")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("Twitch timestamp requires RFC3339 date, time and timezone")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid Twitch timestamp") from exc


def timestamp_identity(value: str) -> tuple[datetime, str]:
    """Compare timezones without discarding RFC3339 fractional precision."""
    whole_second = parse_timestamp(value).replace(microsecond=0).astimezone(timezone.utc)
    fraction = re.search(r"\.(\d+)", value)
    return whole_second, fraction.group(1).rstrip("0") if fraction else ""


@dataclass(frozen=True)
class TwitchClip:
    id: str
    broadcaster_id: str
    creator_id: str
    game_id: str
    title: str
    created_at: str
    duration_us: int
    view_count: int
    source_url: str
    video_id: str | None
    vod_offset_us: int | None

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", self.id):
            raise ValueError("Invalid Twitch clip ID")
        for field in ("broadcaster_id", "creator_id"):
            _text(getattr(self, field), field)
        _text(self.game_id, "game ID", empty=True)
        _text(self.title, "title", empty=True)
        parse_timestamp(self.created_at)
        _integer(self.duration_us, "duration", positive=True)
        _integer(self.view_count, "view count")
        if self.video_id is not None:
            _text(self.video_id, "VOD ID")
        if self.vod_offset_us is not None:
            _integer(self.vod_offset_us, "VOD offset")
        _text(self.source_url, "source URL")
        url = urlsplit(self.source_url)
        if (url.scheme != "https" or url.netloc not in {"clips.twitch.tv", "www.twitch.tv", "twitch.tv"}
                or url.query or url.fragment or url.username or url.password):
            raise ValueError("Twitch source URL must be a public clip URL without query credentials")
        path_matches = (url.path == "/" + self.id if url.netloc == "clips.twitch.tv" else
                        re.fullmatch(r"/[A-Za-z0-9_]{1,25}/clip/" + re.escape(self.id), url.path))
        if not path_matches:
            raise ValueError("Twitch source URL does not match the clip ID")

    @property
    def has_vod_context(self) -> bool:
        return self.video_id is not None and self.vod_offset_us is not None

    @classmethod
    def from_helix(cls, value: dict) -> TwitchClip:
        if not isinstance(value, dict):
            raise ValueError("Twitch clip must be an object")
        clip_id = value.get("id")
        offset = value.get("vod_offset")
        if offset is not None:
            offset = _integer(offset, "VOD offset seconds") * 1_000_000
        return cls(
            id=clip_id, broadcaster_id=value.get("broadcaster_id"), creator_id=value.get("creator_id"),
            game_id=value.get("game_id", ""), title=value.get("title", ""), created_at=value.get("created_at"),
            duration_us=_seconds_us(value.get("duration")), view_count=value.get("view_count", 0),
            source_url=value.get("url", f"https://clips.twitch.tv/{clip_id}"),
            video_id=value.get("video_id") or None, vod_offset_us=offset,
        )


@dataclass(frozen=True)
class TwitchClipPage:
    items: tuple[TwitchClip, ...]
    next_cursor: str | None
