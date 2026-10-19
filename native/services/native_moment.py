"""Canonical admission for one new opaque native Moment, on a background owner.

The qualified helper owns document creation and clocks. SQLite admission and
ProjectStore publication have a retained crash boundary, not a joint commit.
"""
from contextlib import ExitStack
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid

from native.core.creator_library import Asset, CreatorLibrary, KitRecord, Moment, RevisionConflict
from native.core.project_store import NativeSnapshot, ProjectStore, StaleGeneration, StoreBusy
from native.services.caption_audio import _source_read_guard
from native.services.caption_worker.process_tree import ProcessTree
from native.services.caption_worker import process_tree as process_owner

MAX_REQUEST = 2 * 1024**2
MAX_DOCUMENT = 2 * 1024**2
MAX_RECEIPT = 2 * 1024**2
MAX_LOG = 128 * 1024


class NativeMomentCreationError(RuntimeError):
    def __init__(self, reason, directory=None, *, state='failed', owner=None):
        super().__init__(reason)
        self.directory = directory
        self.state = state
        self.owner = owner


class RetainedNativeMomentOwner:
    """Keep admitted inputs protected until this exact owned process is settled."""

    def __init__(self, scope, process, guards):
        self.scope = scope
        self.process = process
        self.guards = guards
        self.settled = False

    @property
    def job(self):
        return self.scope.job if self.scope is not None else None

    def stop(self):
        if self.scope is not None:
            receipt = self.scope.stop()
        else:
            # Attachment failed before releasing stdin; only this gate can exist.
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=3)
            receipt = {'scope': 'unreleased_gate', 'pid': self.process.pid,
                       'disposition': 'stopped', 'active_processes': 0}
        if receipt.get('disposition') == 'stopped' and receipt.get('active_processes') == 0:
            self.settled = True
            self.guards.close()
        return receipt

    def close(self):
        if not self.settled:
            raise RuntimeError('Retain the owner until explicit stop proves cessation')
        if self.scope is not None:
            self.scope.close()
        self.guards.close()


@dataclass(frozen=True)
class QualifiedCreatorKitHelper:
    path: Path
    sha256: str
    version: str


@dataclass(frozen=True)
class CreatedNativeMoment:
    snapshot: NativeSnapshot
    directory: Path
    request_sha256: str
    native_receipt: Path
    process_receipt: Path


@dataclass(frozen=True)
class Admission:
    moment: Moment
    kit: KitRecord
    source: Asset
    template: Asset
    profile: dict


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            value.update(block)
    return value.hexdigest()


def write_once(path, value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode('utf8')
    with path.open('xb') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return encoded


def integer(value, minimum=0):
    return type(value) is int and minimum <= value < 2**64


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 512


def cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise NativeMomentCreationError('Creation cancelled', state='cancelled')


def admission(library, moment_id, expected_revision, profile_id, scene_key):
    moment = library.moment(moment_id)
    if moment.revision != expected_revision:
        raise RevisionConflict('The selected Moment revision changed')
    kit = library.kit_record(moment.kit_id, moment.kit_version)
    descriptor = json.loads(kit.descriptor_json)
    require(set(descriptor['asset_refs']) == {'native_template'},
            'This native factory requires only the canonical native_template binding')
    require(not descriptor['properties'] and not moment.overrides,
            'This native factory does not support generic properties or overrides')
    profiles = [profile for profile in descriptor['layout_profiles']
                if profile['id'] == profile_id and profile['scene_key'] == scene_key]
    require(len(profiles) == 1, 'Select an exact canonical profile and scene')
    source = library.asset(moment.source_asset_id)
    template = library.asset(descriptor['asset_refs']['native_template'])
    return Admission(moment, kit, source, template, profiles[0])


def request_value(facts, profile_id, scene_key):
    return {
        'schema': 2,
        'moment': asdict(facts.moment),
        'kit': {
            'id': facts.kit.kit_id, 'version': facts.kit.version,
            'descriptor_json': facts.kit.descriptor_json,
            'descriptor_sha256': facts.kit.descriptor_sha256,
        },
        'source': asdict(facts.source), 'template': asdict(facts.template),
        'profile_id': profile_id, 'scene_key': scene_key,
    }


def verify_files(library, facts):
    require(not library.connection.in_transaction, 'File hashing cannot hold a SQLite transaction')
    for expected in (facts.source, facts.template):
        if library.verify_asset(expected.id) != expected:
            raise RevisionConflict('A canonical asset registration changed')


def check_receipt(receipt, request, request_sha, output, document):
    require(isinstance(receipt, dict) and receipt.get('status') == 'pass', 'Native creation did not pass')
    require(receipt.get('project') == str(output), 'Native output path differs')
    require(receipt.get('projectSha256') == hashlib.sha256(document).hexdigest(), 'Native output hash differs')
    require(integer(receipt.get('projectBytes'), 1) and receipt['projectBytes'] == len(document),
            'Native output byte count differs')
    moment = request['moment']
    require(receipt.get('momentId') == moment['id']
            and integer(receipt.get('momentRevision'), 1)
            and receipt['momentRevision'] == moment['revision'], 'Native Moment identity differs')
    require(receipt.get('requestSha256') == request_sha, 'Native consumed-request hash differs')
    interval = receipt.get('requestedSourceRangeUs')
    require(isinstance(interval, dict) and set(interval) == {'start', 'end'}
            and integer(interval['start']) and integer(interval['end'], 1)
            and interval == {'start': moment['source_in_us'], 'end': moment['source_out_us']},
            'Native requested source range differs')
    require(receipt.get('profileId') == request['profile_id']
            and receipt.get('sceneKey') == request['scene_key'], 'Native scene selection differs')
    kit = receipt.get('kit')
    require(isinstance(kit, dict) and set(kit) == {'id', 'version', 'descriptorSha256'}
            and integer(kit['version'], 1) and kit == {
                'id': request['kit']['id'], 'version': request['kit']['version'],
                'descriptorSha256': request['kit']['descriptor_sha256'],
            }, 'Native canonical Kit identity differs')
    require(receipt.get('sourceAsset') == request['source']['id']
            and receipt.get('templateAsset') == request['template']['id'], 'Native asset identity differs')
    require(receipt.get('exactSaveReopen') is True
            and receipt.get('nativeEffectsAndAudioPreserved') is True, 'Native save/reopen or role preservation failed')
    require(type(receipt.get('mediaCopies')) is int and receipt['mediaCopies'] == 0
            and type(receipt.get('nativeImportCalls')) is int and receipt['nativeImportCalls'] == 1,
            'Unexpected native import or source-copy result')
    native = receipt.get('nativeInterval')
    require(isinstance(native, dict) and integer(native.get('sourceInTicks'))
            and integer(native.get('durationTicks'), 1) and integer(native.get('frames'), 1),
            'Invalid native interval receipt')
    rate = native.get('frameRate')
    require(isinstance(rate, dict) and set(rate) == {'num', 'den'}
            and integer(rate['num'], 1) and integer(rate['den'], 1), 'Invalid native rational frame rate')

    compositions = receipt.get('compositions')
    require(isinstance(compositions, list) and len(compositions) == 3, 'Three native compositions are required')
    sequences = {}
    expected_roles = None
    profile = next(profile for profile in json.loads(request['kit']['descriptor_json'])['layout_profiles']
                   if profile['id'] == request['profile_id'] and profile['scene_key'] == request['scene_key'])
    for name, composition in zip(('shared', 'landscape', 'portrait'), compositions):
        require(isinstance(composition, dict) and composition.get('name') == name
                and integer(composition.get('sequence'), 1)
                and integer(composition.get('width'), 1) and integer(composition.get('height'), 1),
                'Invalid ordered native composition')
        sequences[name] = composition['sequence']
        width, height = composition['width'], composition['height']
        require(name != 'landscape' or width * 9 == height * 16, 'Landscape canvas is not 16:9')
        require(name != 'portrait' or width * 16 == height * 9, 'Portrait canvas is not 9:16')
        slots = composition.get('slots')
        require(isinstance(slots, list) and 1 <= len(slots) <= 2, 'Unsupported native role slots')
        roles = set()
        for slot in slots:
            require(isinstance(slot, dict) and slot.get('role') in ('gameplay', 'facecam')
                    and slot['role'] not in roles and slot['role'] in profile['regions']
                    and integer(slot.get('video_track'), 1) and integer(slot.get('prototype_clip'), 1),
                    'Native role differs from the canonical profile')
            role = slot['role']
            roles.add(role)
            require((role == 'gameplay' and integer(slot.get('audio_track'), 1)
                     and integer(slot.get('prototype_audio'), 1))
                    or (role == 'facecam' and slot.get('audio_track') is None
                        and slot.get('prototype_audio') is None), 'Native role audio policy differs')
        require('gameplay' in roles and (expected_roles is None or roles == expected_roles),
                'Native compositions have inconsistent roles')
        expected_roles = roles
    require(len(set(sequences.values())) == 3, 'Native sequences are not distinct')
    validation = receipt.get('validation')
    require(isinstance(validation, dict) and validation.get('valid') is True
            and integer(validation.get('revision')), 'Native document validation failed')
    owners = validation.get('owners')
    require(isinstance(owners, list) and len(owners) == 1 and isinstance(owners[0], dict),
            'Native linked owner differs')
    owner = owners[0]
    require(type(owner.get('schema')) is int and owner['schema'] == 2
            and integer(owner.get('owner'), 1) and owner['owner'] == sequences['shared']
            and isinstance(owner.get('sequences'), dict)
            and all(integer(value, 1) for value in owner['sequences'].values())
            and owner['sequences'] == sequences and integer(owner.get('linksRevision')),
            'Native linked composition identity differs')
    units = owner.get('units')
    require(isinstance(units, list) and len(units) == 1 and isinstance(units[0], dict)
            and units[0].get('id') == moment['id'] and units[0].get('parent') is None,
            'Native linked Moment unit differs')
    roles = units[0].get('roles')
    require(isinstance(roles, list) and len(roles) == len(expected_roles)
            and {role.get('role') for role in roles if isinstance(role, dict)} == expected_roles,
            'Native linked role membership differs')
    all_clips = set()
    all_audio = set()
    media = None
    for role in roles:
        clips, audio = role.get('clips'), role.get('nativeAudioClips')
        require(isinstance(clips, dict) and set(clips) == set(sequences)
                and all(integer(value, 1) for value in clips.values()), 'Invalid native role clip identities')
        require(not all_clips.intersection(clips.values()) and len(set(clips.values())) == 3,
                'Native video role clip identity repeats')
        all_clips.update(clips.values())
        require(isinstance(audio, dict) and set(audio) == set(sequences), 'Invalid native audio role identities')
        for peer in sequences:
            values = audio[peer]
            inherit = role['role'] == 'gameplay'
            require(isinstance(values, list) and len(values) == int(inherit)
                    and all(integer(value, 1) for value in values)
                    and role.get('audio') == ('inherit' if inherit else 'none'), 'Native audio ownership differs')
            require(not all_audio.intersection(values), 'Native audio identity repeats')
            all_audio.update(values)
        require(role.get('resolvedMediaPath') == request['source']['path']
                and integer(role.get('sourceItem'), 1)
                and integer(role.get('resolvedMediaItem'), 1)
                and role.get('resolvedMediaItem') == role['sourceItem'], 'Native resolved source differs')
        identity = role.get('storedMediaIdentity')
        require(isinstance(identity, dict) and integer(identity.get('size'))
                and identity['size'] == request['source']['bytes'], 'Native imported source size differs')
        require(media is None or media == role['sourceItem'], 'Native roles use different source items')
        media = role['sourceItem']
    require(not all_clips.intersection(all_audio), 'Native video and audio identities overlap')


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'Duplicate native receipt JSON field')
        value[key] = item
    return value


def _reject_nonfinite(_value):
    raise ValueError('Nonfinite native receipt')


def _read_pipe(pipe, path, limit, overflow, errors):
    try:
        with path.open('xb') as output:
            total = 0
            while block := pipe.read(4096):
                remaining = max(0, limit - total)
                output.write(block[:remaining])
                total += len(block)
                if total > limit:
                    overflow.set()
    except Exception as error:
        errors.append(str(error))
        overflow.set()
    finally:
        pipe.close()


def create_native_moment(library_path, moment_id, expected_revision, *, profile_id, scene_key,
                         helper, python, store, runs, timeout_seconds=120, cancel=None):
    if threading.current_thread() is threading.main_thread():
        raise RuntimeError('Native Moment creation requires a background owner')
    require(os.name == 'nt', 'Native helper process ownership is qualified on Windows only')
    require(type(helper) is QualifiedCreatorKitHelper and type(store) is ProjectStore,
            'Use an explicit qualified helper and ProjectStore')
    require(integer(expected_revision, 1) and text(profile_id) and text(scene_key), 'Invalid explicit Moment selection')
    require(type(timeout_seconds) in (int, float) and math.isfinite(timeout_seconds)
            and 0 < timeout_seconds <= 900, 'Use a finite helper deadline within 900 seconds')
    require(isinstance(helper.path, Path) and helper.path.is_absolute() and helper.path.is_file()
            and isinstance(helper.sha256, str) and re.fullmatch('[0-9a-f]{64}', helper.sha256)
            and text(helper.version) and len(helper.version) <= 128, 'Invalid qualified helper identity')
    require(Path(python).is_absolute(), 'An explicit absolute helper interpreter is required')
    python = Path(python).resolve(strict=True)
    require(python.is_file(), 'An explicit helper interpreter is required')
    directory = Path(runs).resolve() / str(uuid.uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    output = directory / 'moment.fcproj'
    process = scope = None
    process_receipt = None
    stop_attempted = False
    publication_started = False
    readers = []
    primary = None
    guards = ExitStack()
    try:
        cancelled(cancel)
        library_path = Path(library_path)
        require(library_path.is_absolute() and library_path.is_file(), 'Use an existing absolute canonical library path')
        require(store.load() is None, 'This caller only creates an initial ProjectStore generation')
        with CreatorLibrary(library_path) as library:
            with library._write():
                facts = admission(library, moment_id, expected_revision, profile_id, scene_key)
            gate = Path(process_owner.__file__).resolve().parent.parent / 'filmcraft_launch_gate.py'
            require(gate.is_file(), 'The existing generic native launch gate is required')
            for path in dict.fromkeys((helper.path, python, gate, Path(facts.source.path), Path(facts.template.path))):
                protected = _source_read_guard(path)
                if protected is not None:
                    guards.callback(protected[0].CloseHandle, protected[1])
            require(digest(helper.path) == helper.sha256, 'The qualified native helper changed')
            verify_files(library, facts)
            cancelled(cancel)
            with library._write():
                require(admission(library, moment_id, expected_revision, profile_id, scene_key) == facts,
                        'Canonical admission changed before launch')
            request = request_value(facts, profile_id, scene_key)
            raw = write_once(directory / 'request.json', request)
            require(len(raw) <= MAX_REQUEST, 'Native creation request exceeds its bound')
            request_sha = hashlib.sha256(raw).hexdigest()
            protected = _source_read_guard(directory / 'request.json')
            if protected is not None:
                guards.callback(protected[0].CloseHandle, protected[1])
            require((directory / 'request.json').read_bytes() == raw,
                    'Native request changed before protection')
            intent = {
                'argv': [str(helper.path), 'instantiate', str(directory / 'request.json'), str(output)],
                'child_receipt': str(directory / 'child.json'),
                'helper': {'path': str(helper.path), 'sha256': helper.sha256, 'version': helper.version},
                'python_sha256': digest(python), 'gate_sha256': digest(gate),
                'request_sha256': request_sha,
            }
            launch_raw = write_once(directory / 'launch.json', intent)
            protected = _source_read_guard(directory / 'launch.json')
            if protected is not None:
                guards.callback(protected[0].CloseHandle, protected[1])
            require((directory / 'launch.json').read_bytes() == launch_raw,
                    'Native launch intent changed before protection')
            cancelled(cancel)
            process = subprocess.Popen(
                [str(python), '-I', str(gate), str(directory / 'launch.json')],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=directory, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            scope = ProcessTree(process)
            overflow = threading.Event()
            read_errors = []
            for pipe, name, bound in ((process.stdout, 'stdout.json', MAX_RECEIPT),
                                      (process.stderr, 'stderr.log', MAX_LOG)):
                reader = threading.Thread(target=_read_pipe, args=(pipe, directory / name, bound, overflow, read_errors), daemon=True)
                reader.start()
                readers.append(reader)
            process.stdin.write(b'start\n')
            process.stdin.flush()
            process.stdin.close()
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                cancelled(cancel)
                require(not overflow.is_set(), 'Native helper output exceeds its bound or cannot be retained')
                require(not output.exists() or output.stat().st_size <= MAX_DOCUMENT,
                        'Staged native document exceeds its bound')
                if time.monotonic() >= deadline:
                    raise NativeMomentCreationError('Native helper deadline expired', state='timed_out')
                time.sleep(.01)
            require(process.returncode == 0, 'Native helper failed; staged output is retained')
            stop_attempted = True
            process_receipt = scope.stop()
            write_once(directory / 'process.json', {'exit_code': process.returncode, 'scope': process_receipt})
            require(process_receipt.get('disposition') == 'stopped'
                    and process_receipt.get('active_processes') == 0, 'Owned native helper cleanup is unknown')
            scope.close()
            scope = None
            for reader in readers:
                reader.join(2)
            require(not any(reader.is_alive() for reader in readers), 'Native helper output readers did not stop')
            require(not overflow.is_set() and not read_errors, 'Native helper output exceeds its bound or cannot be retained')
            require(process.returncode == 0, 'Native helper failed; staged output is retained')
            cancelled(cancel)
            require(output.resolve(strict=True) == output and output.is_file()
                    and 0 < output.stat().st_size <= MAX_DOCUMENT, 'Invalid staged native document')
            document = output.read_bytes()
            require(0 < len(document) <= MAX_DOCUMENT, 'Native document exceeds its bound')
            receipt = json.loads((directory / 'stdout.json').read_bytes(), object_pairs_hook=_strict_object,
                                 parse_constant=_reject_nonfinite)
            require(digest(directory / 'request.json') == request_sha, 'Retained native request changed')
            check_receipt(receipt, request, request_sha, output, document)
            write_once(directory / 'native-receipt.json', receipt)
            verify_files(library, facts)
            cancelled(cancel)
            write_once(directory / 'publish-intent.json', {'request_sha256': request_sha,
                       'store': str(store.directory), 'expected_generation': None,
                       'document_sha256': hashlib.sha256(document).hexdigest()})
            with library._write():
                require(admission(library, moment_id, expected_revision, profile_id, scene_key) == facts,
                        'Canonical admission changed after native creation')
                cancelled(cancel)
                publication_started = True
                try:
                    snapshot = store.publish(document, expected_generation=None, revision=1,
                                             engine='filmcraft', engine_version=helper.version, suffix='.fcproj')
                except (StaleGeneration, StoreBusy):
                    publication_started = False
                    raise
            write_once(directory / 'published.json', {'generation': snapshot.generation,
                       'sha256': snapshot.sha256, 'request_sha256': request_sha})
            return CreatedNativeMoment(snapshot, directory, request_sha,
                                       directory / 'native-receipt.json', directory / 'process.json')
    except Exception as error:
        primary = error
        state = error.state if isinstance(error, NativeMomentCreationError) else 'failed'
        if publication_started:
            state = 'publication_unknown'
        cleanup = None
        if scope is not None and not stop_attempted:
            try:
                stop_attempted = True
                process_receipt = scope.stop()
                write_once(directory / 'process.json', {'exit_code': process.poll(), 'scope': process_receipt})
            except Exception as stop_error:
                cleanup = str(stop_error)
        elif process is not None and scope is None and process.poll() is None:
            # The gate was never released if Job Object attachment failed.
            try:
                process.stdin.close()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
                write_once(directory / 'gate-process.json', {
                    'pid': process.pid, 'exit_code': process.returncode,
                    'native_start_gate_released': False,
                })
            except Exception as stop_error:
                cleanup = str(stop_error)
        unknown = scope is not None and (process_receipt is None
                  or process_receipt.get('disposition') != 'stopped'
                  or process_receipt.get('active_processes') != 0)
        gate_unknown = process is not None and scope is None and process.poll() is None
        unknown = unknown or gate_unknown
        if unknown:
            state = 'outcome_unknown'
        elif scope is not None:
            scope.close()
            scope = None
        for reader in readers:
            reader.join(2)
        if process is not None and not readers:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
        failure = {'state': state, 'error': str(primary), 'cleanup_error': cleanup,
                   'process': process_receipt, 'publication_started': publication_started,
                   'staged_output_retained': output.exists()}
        try:
            write_once(directory / 'failure.json', failure)
        except OSError as receipt_error:
            failure['receipt_error'] = str(receipt_error)
        retained = RetainedNativeMomentOwner(scope, process, guards.pop_all()) if unknown else None
        raised = NativeMomentCreationError(str(primary), directory, state=state, owner=retained)
        raised.details = failure
        raise raised from primary
    finally:
        guards.close()
