"""Background access to persistent creator records; no native timeline or decoder."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
import threading

from native.core.creator_library import CreatorLibrary
from native.core.twitch_inbox import parse_timestamp


@dataclass(frozen=True)
class LibraryEntry:
    id: str
    revision: int | None
    columns: tuple[str, ...]
    details: str


@dataclass(frozen=True)
class LibraryPage:
    kind: str
    after: int
    next_cursor: int | None
    entries: tuple[LibraryEntry, ...]
    context: str | None = None


@dataclass(frozen=True)
class ClipRefresh:
    channel: str
    received: int
    cancelled: bool


@dataclass(frozen=True)
class MomentEditContext:
    title: str
    kit_name: str
    kit_version: int
    profiles: tuple[tuple[str, str], ...]
    selected_layout: tuple[str, str] | None = None


def duration(us):
    seconds, micros = divmod(us, 1_000_000)
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes}:{seconds:02d}.{micros // 1000:03d}'


def file_size(size):
    for suffix in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024 or suffix == 'TiB':
            return f'{size:.1f} {suffix}' if suffix != 'B' else f'{size} B'
        size /= 1024


class WorkspaceLibrary:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()
        self.database = self.directory / 'library.sqlite'

    @staticmethod
    def require_background():
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError('Library persistence requires a background owner')

    @contextmanager
    def _library(self):
        self.require_background()
        self.directory.mkdir(parents=True, exist_ok=True)
        with CreatorLibrary(self.database) as library:
            yield library

    def page(self, kind, after=0, *, context=None, limit=50):
        with self._library() as library:
            if kind == 'clips':
                page = library.list_twitch_clips(after=after, limit=limit)
                entries = tuple(LibraryEntry(
                    clip.id, None,
                    (clip.title or 'Untitled clip',
                     parse_timestamp(clip.created_at).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M'),
                     duration(clip.duration_us), str(clip.view_count)),
                    f'{clip.title or "Untitled clip"}\n{clip.source_url}\n'
                    f'Created: {clip.created_at}\nChannel: {clip.broadcaster_id}\nClip: {clip.id}\n'
                    + (f'VOD: {clip.video_id} · {duration(clip.vod_offset_us)}'
                       if clip.has_vod_context else 'VOD context unavailable'),
                ) for clip in page['items'])
            elif kind == 'moments':
                cursor = after
                if after == 0 and context is not None:
                    library.moment(context)
                    row = library.connection.execute('SELECT sequence FROM moments WHERE id=?',(context,)).fetchone()
                    cursor = row[0] - 1
                page = library.list_moments(after=cursor, limit=limit)
                entries = []
                for moment in page['items']:
                    asset = library.asset(moment.source_asset_id)
                    kit = library.kit(moment.kit_id, moment.kit_version)
                    origin = library.moment_origin(moment.id)
                    local = origin is not None and origin.get('kind') == 'local'
                    source_label = (f'Local · {Path(asset.path).name}' if local else
                                    f'Twitch {origin["clip_id"]} · {Path(asset.path).name}' if origin else Path(asset.path).name)
                    origin_detail = (f'\nTwitch clip: {origin["clip_id"]}\n'
                        f'{origin["selected_metadata"]["source_url"]}\n'
                        f'Intake-selected title: {origin["selected_metadata"]["title"]}\n'
                        f'Prepared Kit version: {origin["kit_version"]}\n'
                        f'Prepared OBS scene: {origin["scene_key"]}\n'
                        f'Intake result: {origin["result_id"]}\nIntake result SHA256: {origin["result_sha256"]}'
                        if origin and not local else
                        f'\nLocal shared file\nPrepared Kit version: {origin["kit_version"]}\nPrepared OBS scene: {origin["scene_key"]}' if local else '')
                    interval = f'{duration(moment.source_in_us)}–{duration(moment.source_out_us)}'
                    entries.append(LibraryEntry(
                        moment.id, moment.revision,
                        (moment.title, source_label, f'{kit["name"]} · v{moment.kit_version}', interval),
                        f'{moment.title}\nSource: {asset.path}\nSource range: {interval}\n'
                        f'Kit: {kit["name"]} · version {moment.kit_version}\n'
                        f'Moment: {moment.id}\nRevision: {moment.revision}' + origin_detail,
                    ))
                entries = tuple(entries)
            elif kind in ('kits', 'versions'):
                if kind == 'versions' and not isinstance(context, str):
                    raise ValueError('Select a Creator Kit before listing versions')
                page = (library.list_kits(after=after, limit=limit) if kind == 'kits'
                        else library.list_kit_versions(context, after=after, limit=limit))
                entries = tuple(LibraryEntry(
                    kit.id, kit.version, (kit.name, str(kit.version)),
                    f'{kit.name}\nVersion: {kit.version}\nKit: {kit.id}',
                ) for kit in page['items'])
            elif kind == 'assets':
                page = library.list_assets(after=after, limit=limit)
                entries = tuple(self._asset_entry(asset) for asset in page['items'])
            else:
                raise ValueError('Unknown library view')
            return LibraryPage(kind, after, page['next_cursor'], entries, context)

    @staticmethod
    def _asset_entry(asset):
        return LibraryEntry(
            asset.id, None, (Path(asset.path).name, file_size(asset.bytes), str(Path(asset.path).parent)),
            f'{asset.path}\nSize: {asset.bytes} bytes\nSHA256: {asset.sha256}\nAsset: {asset.id}',
        )

    def register_file(self, path):
        with self._library() as library:
            return self._asset_entry(library.register_asset(path))

    def moment_edit_context(self, moment_id, expected_revision):
        from native.core.creator_library import RevisionConflict
        with self._library() as library:
            moment = library.moment(moment_id)
            if moment.revision != expected_revision:
                raise RevisionConflict('The selected Moment changed. Reload it before opening the edit.')
            kit = library.kit(moment.kit_id, moment.kit_version)
            origin = library.moment_origin(moment_id)
            selected = ((origin['profile_id'], origin['scene_key']) if origin
                        and (origin['kit_id'], origin['kit_version']) == (moment.kit_id, moment.kit_version) else None)
            return MomentEditContext(moment.title, kit['name'], moment.kit_version,
                tuple((profile['id'], profile['scene_key']) for profile in kit['layout_profiles']), selected)

    def verify_file(self, asset_id):
        with self._library() as library:
            return self._asset_entry(library.verify_asset(asset_id))

    def kit_details(self, kit_id, version):
        with self._library() as library:
            record = library.kit_record(kit_id, version)
            descriptor = library.kit(kit_id, version)
            lines = [descriptor['name'], f'Version: {version}', f'Kit: {kit_id}']
            for role, asset_id in descriptor['asset_refs'].items():
                label = 'Template' if role == 'native_template' else role.replace('_', ' ').capitalize()
                lines.append(f'{label}: {library.asset(asset_id).path}')
            if descriptor['layout_profiles']:
                lines.append('Layouts: ' + ', '.join(profile['id'] for profile in descriptor['layout_profiles']))
            if descriptor['vocabulary']:
                lines.append('Vocabulary: ' + ', '.join(descriptor['vocabulary']))
            lines.append('Descriptor SHA256: ' + record.descriptor_sha256)
            return '\n'.join(lines)

    def connect_twitch(self, job, cancel):
        self.require_background()
        from native.services import twitch_auth
        if not twitch_auth.client_id():
            raise ValueError('Twitch connection is not configured in this build.')
        twitch_auth.loopback_login(job, cancel)
        # The persisted session remains inside its auth owner, never in a Qt result.
        return 'Twitch authorization saved. Refresh clips to load your channel.'

    def refresh_clips(self, started_at, ended_at, cancel):
        self.require_background()
        from native.services import twitch_api
        start, end = parse_timestamp(started_at), parse_timestamp(ended_at)
        if end <= start:
            raise ValueError('Choose an end date after the start date.')
        if cancel.is_set():
            return ClipRefresh('', 0, True)
        account = twitch_api.current_user()
        cursor, received, seen = None, 0, set()
        while not cancel.is_set():
            page = twitch_api.clips_page(
                account['id'], first=100, after=cursor,
                started_at=started_at, ended_at=ended_at,
            )
            with self._library() as library:
                library.record_twitch_clips(page.items)
            received += len(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return ClipRefresh(account['display_name'], received, False)
            if cursor in seen:
                raise ValueError('Twitch repeated a page cursor. Saved clips remain available; refresh again later.')
            seen.add(cursor)
        return ClipRefresh(account['display_name'], received, True)
