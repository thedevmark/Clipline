"""Per-Moment lifecycle barrier and admission of qualified native exit evidence.

The barrier is not a process registry. Only the retained NativeSession observes
the original process scope. Native files remain opaque bytes throughout.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from native.services.caption_audio import _source_read_guard
from native.services.native_checkpoint import MAX_DOCUMENT


MAX_RECEIPT = 256 * 1024
MAX_BARRIER = 2 * 1024**2


class ExitEvidenceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ExitEvidenceError(message)


def _object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate managed-exit field')
        result[key] = value
    return result


def _nonfinite(_):
    raise ExitEvidenceError('Nonfinite managed-exit value')


def strict_json(encoded):
    return json.loads(encoded, object_pairs_hook=_object, parse_constant=_nonfinite)


def sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def validate_status(value, nonce, request_id=None):
    require(isinstance(value, dict)
            and set(value) == {'schema', 'nonce', 'requestId', 'decisionSerial', 'pending', 'decision'}
            and type(value['schema']) is int and value['schema'] == 1 and value['nonce'] == nonce
            and isinstance(value['requestId'], str)
            and (request_id is None or value['requestId'] == request_id)
            and type(value['decisionSerial']) is int and 0 <= value['decisionSerial'] < 2**64
            and type(value['pending']) is bool
            and value['decision'] in (None, 'clean', 'save', 'dont_save', 'cancel'),
            'The native close status does not match this request')
    parsed = uuid.UUID(value['requestId'])
    require(str(parsed) == value['requestId'] and parsed.version == 4,
            'The native close request ID is invalid')
    require((value['pending'] and value['decision'] is None)
            or (not value['pending'] and value['decision'] is not None), 'Native close decision state is inconsistent')
    return value


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf8')


def write_new(path, data):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def bounded(path, limit):
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    require(0 < len(data) <= limit, 'Managed evidence exceeds its bound or is empty')
    require((before.st_size, before.st_mtime_ns, before.st_ino) ==
            (after.st_size, after.st_mtime_ns, after.st_ino), 'Managed evidence changed during reading')
    return data


class Lifecycle:
    """One durable barrier under the existing ProjectStore writer."""

    def __init__(self, store, nonce):
        self.store = store
        self.nonce = nonce
        self.path = store.directory / 'editor-lifecycle.json'

    def _read_owned(self):
        value = strict_json(bounded(self.path, MAX_BARRIER))
        require(isinstance(value, dict) and value.get('schema') == 1
                and value.get('nonce') == self.nonce, 'Another lifecycle barrier owns this Moment')
        return value

    def reserve(self, snapshot, working, facts):
        value = {'schema': 1, 'nonce': self.nonce, 'state': 'launch_pending',
                 'facts': facts, 'working': str(working.path),
                 'generation': snapshot.generation, 'revision': snapshot.revision,
                 'initial_sha256': snapshot.sha256}
        data = encoded(value)
        require(len(data) <= MAX_BARRIER, 'Lifecycle facts exceed their bound')
        with self.store._writer():
            current = self.store.load()
            require(current is not None and current.generation == snapshot.generation
                    and current.revision == snapshot.revision, 'Project publication changed before launch')
            write_new(self.path, data)

    def update(self, **changes):
        with self.store._writer():
            value = self._read_owned()
            require('nonce' not in changes and 'schema' not in changes, 'Lifecycle identity is immutable')
            value.update(changes)
            data = encoded(value)
            require(len(data) <= MAX_BARRIER, 'Lifecycle record exceeds its bound')
            pending = self.store.directory / ('editor-lifecycle-' + str(uuid.uuid4()) + '.pending')
            write_new(pending, data)
            os.replace(pending, self.path)

    def settle(self, outcome, snapshot):
        with self.store._writer():
            value = self._read_owned()
            current = self.store.load()
            require(current is not None and current.generation == snapshot.generation
                    and current.revision == snapshot.revision, 'Final publication changed before lifecycle settlement')
            value.update(state='settled', outcome=outcome, final_generation=snapshot.generation,
                         final_revision=snapshot.revision)
            archive = self.store.directory / ('editor-lifecycle-' + self.nonce + '.settled.json')
            write_new(archive, encoded(value))
            self.path.unlink()


@contextmanager
def read_exit(session):
    """Guard and verify exact native receipt/artifact bytes after actual scope0.

    The caller must validate canonical metadata and publish while this context
    remains active. No persisted field can establish process cessation.
    """
    managed = session.managed_exit
    require(isinstance(managed, dict), 'This session has no admitted managed-exit contract')
    directory = Path(managed['directory'])
    require(directory.is_dir() and not directory.is_symlink() and not directory.is_junction(),
            'The managed output directory is absent or redirected')
    directory = directory.resolve(strict=True)
    with ExitStack() as guards:
        def read_owned(name, limit):
            require(isinstance(name, str) and name not in ('', '.', '..')
                    and Path(name).name == name and '/' not in name and '\\' not in name
                    and ':' not in name, 'An artifact escapes the managed directory')
            path = directory / name
            require(not path.is_symlink() and not path.is_junction()
                    and path.resolve(strict=True).parent == directory, 'An artifact is redirected')
            protected = _source_read_guard(path)
            require(protected is not None, 'Managed evidence requires the Windows read guard')
            guards.callback(protected[0].CloseHandle, protected[1])
            return bounded(path, limit)

        receipt = strict_json(read_owned('receipt.json', MAX_RECEIPT))
        fields = {'schema', 'sealedFinalExit', 'state', 'nativeGenerator', 'projectContentFingerprint',
                  'nonce', 'launchManifestSHA256', 'workingPath', 'initialWorkingSHA256', 'initialIdentity',
                  'decision', 'directBypass', 'fault', 'decisionEvidence', 'postDiscard', 'finalMemory',
                  'savedContent', 'authority', 'managedRequest'}
        require(isinstance(receipt, dict) and set(receipt) == fields
                and type(receipt['schema']) is int and receipt['schema'] == 1
                and receipt['sealedFinalExit'] is True, 'The native final receipt is malformed or unsealed')
        require(receipt['nonce'] == managed['nonce']
                and receipt['launchManifestSHA256'] == managed['manifest_sha256']
                and receipt['workingPath'] == str(session.path)
                and receipt['initialWorkingSHA256'] == session.initial_hash,
                'The native final receipt belongs to another launch')
        fingerprint = receipt['projectContentFingerprint']
        require(isinstance(fingerprint, dict) and set(fingerprint) == {'version', 'viewIncluded', 'nativeSchema'}
                and type(fingerprint['version']) is int and fingerprint['version'] == 1
                and fingerprint['viewIncluded'] is False and type(fingerprint['nativeSchema']) is int
                and fingerprint['nativeSchema'] > 0 and isinstance(receipt['nativeGenerator'], str)
                and 0 < len(receipt['nativeGenerator']) <= 128, 'Unsupported native content fingerprint')
        require(receipt['state'] in ('saved', 'discarded') and receipt['directBypass'] is False
                and receipt['fault'] is None, 'The native document requires reconciliation')
        require(receipt['decision'] in (('clean', 'save') if receipt['state'] == 'saved' else ('dont_save',)),
                'The native decision does not support this settlement')
        if receipt['managedRequest'] is not None:
            validate_status(receipt['managedRequest'], managed['nonce'])
        seen = {}

        def artifact(value, role):
            require(isinstance(value, dict) and value.get('role') == role
                    and sha(value.get('sha256')) and type(value.get('bytes')) is int
                    and 0 < value['bytes'] <= MAX_DOCUMENT and isinstance(value.get('identity'), dict),
                    'Malformed native artifact identity')
            name = value.get('artifact')
            require(isinstance(name, str) and name.endswith('.fcproj'), 'A native project artifact is required')
            data = seen.get(name)
            if data is None:
                data = read_owned(name, MAX_DOCUMENT)
                seen[name] = data
            require(len(data) == value['bytes'] and hashlib.sha256(data).hexdigest() == value['sha256'],
                    'Native artifact bytes differ from the sealed receipt')
            require(sha(value['identity'].get('projectContentSHA256')), 'Native project content hash is missing')
            return data

        artifact(receipt['finalMemory'], 'final_memory')
        saved = receipt['savedContent']
        require(isinstance(saved, dict) and saved.get('present') is True
                and saved.get('path') == str(session.path), 'The final saved document uses a foreign path')
        document = artifact(saved, 'saved_content')
        require(saved['identity'].get('projectPath') == str(session.path)
                and saved['identity'].get('identityOrigin') == 'native_decoded_saved_file',
                'The saved project was not decoded from the owned native file')
        if receipt['state'] == 'saved':
            require(saved.get('nativeProjectEqualsMemory') is True
                    and receipt['finalMemory']['identity'].get('dirty') is False
                    and receipt['finalMemory']['identity']['projectContentSHA256'] ==
                    saved['identity']['projectContentSHA256'], 'Unsaved native memory cannot be published')
        else:
            evidence = receipt['decisionEvidence']
            require(isinstance(evidence, dict) and evidence.get('saved') == saved,
                    'Explicit discard lacks separately decoded saved evidence')
            artifact(evidence.get('memory'), 'discarded_memory')
            artifact(receipt['postDiscard'], 'post_discard')
            require(receipt['postDiscard']['identity'] == receipt['finalMemory']['identity']
                    and receipt['postDiscard']['sha256'] == receipt['finalMemory']['sha256'],
                    'The native document changed after explicit discard')
        yield receipt, document
