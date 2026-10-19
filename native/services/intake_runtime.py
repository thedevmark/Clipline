"""Explicit local Twitch intake runtime; loading starts no process or provider request."""
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import threading

from native.services.native_export_client import QualifiedProgram
from native.services.twitch_intake_client import IntakeTools, TwitchIntakeClient


PROGRAM_NAMES = ('python', 'worker', 'gate', 'ffprobe')
WORKER = 'native/services/twitch_transfer_worker.py'


@dataclass(frozen=True)
class IntakeRuntime:
    python: QualifiedProgram
    worker: QualifiedProgram
    gate: QualifiedProgram
    ffprobe: QualifiedProgram
    code_root: Path
    code: tuple[tuple[str, str], ...]
    client_id: str

    def client(self):
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError('Construct the Twitch intake client on a background owner.')
        return TwitchIntakeClient(IntakeTools(self.python, self.worker, self.gate, self.ffprobe,
                                             self.code_root, dict(self.code), self.client_id))


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('The Twitch intake runtime contains a duplicate setting.')
        value[key] = item
    return value


def _nonfinite(_):
    raise ValueError('The Twitch intake runtime requires finite JSON values.')


def _sha(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('The Twitch intake runtime requires lowercase SHA256 identities.')
    return value


def _path(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 32768
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or not Path(value).is_absolute()):
        raise ValueError('The Twitch intake runtime requires absolute local paths.')
    return Path(value)


def load_intake_runtime(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('The Twitch intake runtime configuration exceeds 64 KiB.')
    value = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
    if (not isinstance(value, dict)
            or set(value) != {'schema', *PROGRAM_NAMES, 'code_root', 'code', 'client_id'}
            or type(value['schema']) is not int or value['schema'] != 1):
        raise ValueError('Unsupported Twitch intake runtime configuration.')
    programs = {}
    for name in PROGRAM_NAMES:
        record = value[name]
        if not isinstance(record, dict) or set(record) != {'path', 'sha256'}:
            raise ValueError('The Twitch intake runtime requires exact program identities.')
        programs[name] = QualifiedProgram(_path(record['path']), _sha(record['sha256']))
    code_root = _path(value['code_root'])
    code = value['code']
    if not isinstance(code, dict) or not 1 <= len(code) <= 256:
        raise ValueError('The Twitch intake runtime requires its complete managed code inventory.')
    for name, pin in code.items():
        relative = PurePosixPath(name)
        if (not 1 <= len(name) <= 1024 or '\\' in name or relative.as_posix() != name
                or relative.is_absolute() or '..' in relative.parts
                or not relative.parts or relative.parts[0] != 'native' or relative.suffix not in ('.py', '.sql')):
            raise ValueError('The Twitch intake runtime contains an invalid managed code path.')
        _sha(pin)
    if (code.get(WORKER) != programs['worker'].sha256
            or programs['worker'].path != code_root / WORKER):
        raise ValueError('The Twitch intake worker differs from its managed code inventory.')
    client_id = value['client_id']
    if not isinstance(client_id, str) or re.fullmatch('[A-Za-z0-9_-]{1,512}', client_id) is None:
        raise ValueError('The Twitch intake runtime requires an explicit public client ID.')
    return IntakeRuntime(**programs, code_root=code_root, code=tuple(sorted(code.items())), client_id=client_id)
