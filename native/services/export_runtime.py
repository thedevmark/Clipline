"""Explicit native export configuration; no discovery, launch, or UI binding."""
from dataclasses import dataclass
import json
from pathlib import Path
import threading

from native.services.native_export_client import NativeExportClient, QualifiedProgram
from native.services.export_recipes import SOFTWARE_RECIPE_ID, export_recipe


MAX_MANIFEST_BYTES = 65536
PROGRAM_NAMES = ('helper', 'ffprobe', 'ffmpeg', 'python', 'gate')


@dataclass(frozen=True)
class ExportRuntime:
    helper: QualifiedProgram
    ffprobe: QualifiedProgram
    ffmpeg: QualifiedProgram
    python: QualifiedProgram
    gate: QualifiedProgram
    program_versions: tuple[tuple[str, str], ...]
    engine_version: str
    native_source_pin_sha256: str
    recipe_id: str = SOFTWARE_RECIPE_ID

    def client(self) -> NativeExportClient:
        """Construct on the background owner, then admit the captured gate hash.

        The returned client still uses its existing admit() guards and executable
        hash checks before launch. This method starts no process and owns none.
        """
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError('Construct the export client on a background owner.')
        client = NativeExportClient(
            self.helper,
            self.ffprobe,
            self.ffmpeg,
            self.python,
            self.gate.path,
            engine_version=self.engine_version,
            source_pin_sha256=self.native_source_pin_sha256,
            recipe_id=self.recipe_id,
        )
        if client.gate_sha256 != self.gate.sha256:
            raise ValueError('The export tool gate differs from the runtime manifest.')
        return client


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('The export runtime contains a duplicate setting.')
        value[key] = item
    return value


def _nonfinite(_):
    raise ValueError('The export runtime requires finite JSON values.')


def _sha256(value):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in '0123456789abcdef' for character in value)):
        raise ValueError('The export runtime requires lowercase SHA256 identities.')
    return value


def _version(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise ValueError('The export runtime requires bounded program versions.')
    return value


def _program(value):
    if not isinstance(value, dict) or set(value) != {'path', 'sha256', 'version'}:
        raise ValueError('The export runtime requires explicit program identities.')
    path = value['path']
    if (not isinstance(path, str) or not path or len(path) > 32768
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or not Path(path).is_absolute()):
        raise ValueError('The export runtime requires absolute local program paths.')
    return QualifiedProgram(Path(path), _sha256(value['sha256'])), _version(value['version'])


def load_export_runtime(path) -> ExportRuntime:
    """Read a bounded explicit manifest without reading any selected program.

    native_source_pin_sha256 is the identity supplied by the qualified native
    source owner. This parser does not derive it from Python or prose records.
    """
    with Path(path).open('rb') as stream:
        encoded = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError('The export runtime configuration exceeds 64 KiB.')
    value = json.loads(encoded.decode('utf8'), object_pairs_hook=_object, parse_constant=_nonfinite)
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] not in (1, 2):
        raise ValueError('Unsupported export runtime configuration.')
    expected = {
        'schema', *PROGRAM_NAMES, 'engine_version', 'native_source_pin_sha256',
    }
    if value['schema'] == 2:
        expected.add('recipe_id')
    if set(value) != expected:
        raise ValueError('Unsupported export runtime configuration.')
    recipe_id = value.get('recipe_id', SOFTWARE_RECIPE_ID)
    export_recipe(recipe_id)
    programs = {}
    versions = []
    for name in PROGRAM_NAMES:
        programs[name], version = _program(value[name])
        versions.append((name, version))
    return ExportRuntime(
        **programs,
        program_versions=tuple(versions),
        engine_version=_version(value['engine_version']),
        native_source_pin_sha256=_sha256(value['native_source_pin_sha256']),
        recipe_id=recipe_id,
    )
