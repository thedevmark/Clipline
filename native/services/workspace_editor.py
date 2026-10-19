"""One companion-owned native editor; synchronous background operations only."""
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
import json
import time
import threading
from pathlib import Path
from types import MappingProxyType
import uuid

from native.core.creator_library import CreatorLibrary
from native.core.project_store import NativeSnapshot, ProjectStore
from native.services.workspace_library import WorkspaceLibrary
from native.services import native_moment
from native.services.native_moment import QualifiedCreatorKitHelper, NativeMomentCreationError
from native.services.filmcraft_session import NativeSession, QualifiedEditor, SessionError, SessionUnavailable, _project_identity, digest
from native.services.filmcraft_bridge import OutcomeUnknown, EditorUnavailable
from native.services.native_checkpoint import WorkingCopy, checkout, publish_checkpoint
from native.services.caption_audio import _source_read_guard
from native.services.managed_exit import Lifecycle, read_exit, sha, validate_status


class AdmissionError(ValueError):
    pass


class OperationRefused(ValueError):
    pass


UNCHANGED = object()


@dataclass(frozen=True)
class EditorOutcome:
    state: str
    message: str
    moment_id: str | None = None
    moment_revision: int | None = None
    snapshot: NativeSnapshot | None = None
    working: WorkingCopy | None = None
    directory: Path | None = None
    owner: object | None = None
    requires_reconciliation: bool = False
    can_checkpoint: bool = False
    can_close_editor: bool = False
    can_cleanup: bool = False
    app_close_allowed: bool = True
    error_type: str | None = None
    stop_receipt: object | None = None
    can_check_close: bool = False
    can_recover_working: bool = False


def require(condition, message):
    if not condition:
        raise AdmissionError(message)


def immutable(value):
    if isinstance(value, dict):
        return MappingProxyType({key: immutable(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(immutable(item) for item in value)
    return value


def same_json(left, right):
    # This compares metadata, not native serialization hashes or timeline values.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate native provenance field')
        result[key] = value
    return result


def nonfinite(_):
    raise AdmissionError('Nonfinite native provenance value')


def known_stop(receipt, kind):
    if not isinstance(receipt, dict):
        return False
    scope = receipt.get('scope') if kind == 'session' else receipt
    if not isinstance(scope, dict):
        return False
    allowed = ('stopped', 'never_started') if kind == 'session' else ('stopped',)
    return (scope.get('disposition') in allowed and type(scope.get('active_processes')) is int
            and scope['active_processes'] == 0
            and (kind != 'session' or receipt.get('streams_closed') is True))


class WorkspaceEditorController:
    """Keep this controller strongly owned until app_close_allowed is true.

    One companion uses one controller. This is not a cross-process editor
    registry. NativeSession still owns its working-file lease and quarantine.
    Outcomes contain no database/bridge; owner is an explicit live capability.
    """

    def __init__(self, workspace, *, helper, editor, python,
                 creation_timeout_seconds=120, readiness_seconds=30, managed_exit=False,
                 close_wait_seconds=10, caption_runtime=None):
        require(type(workspace) is WorkspaceLibrary, 'Use the canonical WorkspaceLibrary owner')
        require(type(helper) is QualifiedCreatorKitHelper and type(editor) is QualifiedEditor,
                'Explicit qualified helper and editor configuration are required')
        self.workspace, self.helper, self.editor = workspace, helper, editor
        self.python = Path(python)
        require(type(managed_exit) is bool, 'Declare managed-exit support explicitly')
        require(type(close_wait_seconds) in (int, float) and 0 < close_wait_seconds <= 60,
                'Use a bounded close wait of at most 60 seconds')
        self.managed_exit = managed_exit
        self.close_wait_seconds = close_wait_seconds
        self._lifecycle = None
        self._quit_attempted = False
        self._close_request = None
        self._publication_attempted = False
        self._checkpoint_digest = None
        self.creation_timeout_seconds, self.readiness_seconds = creation_timeout_seconds, readiness_seconds
        self._operation_lock, self._state_lock = threading.Lock(), threading.RLock()
        self._busy = False
        self._session = self._retained_owner = None
        self._retained_kind = None
        self._guards = ExitStack()
        self._facts = self._store = None
        self._profile_id = self._scene_key = None
        self._blocked_reason = None
        self._view = EditorOutcome('idle', 'No native editor is open.')
        self._captions = None
        if caption_runtime is not None:
            from native.services.workspace_captions import WorkspaceCaptions
            self._captions = WorkspaceCaptions(self,caption_runtime)

    def status(self):
        """Detached state only; safe on the GUI thread, without native/SQLite IO."""
        with self._state_lock:
            return self._view

    def _document_is_open(self):
        """Shared admission for editing actions; operation lock owns busy state."""
        return (self._view.state == 'open' and self._session is not None
                and self._retained_owner is None and self._blocked_reason is None
                and not self._quit_attempted
                and not (self._captions is not None and self._captions.unsettled))

    def _view_update(self, state, message, *, error=UNCHANGED, owner=None, receipt=None):
        with self._state_lock:
            self._view = replace(self._view, state=state, message=message,
                owner=owner if owner is not None else (self._retained_owner or self._session),
                requires_reconciliation=self._blocked_reason is not None,
                can_checkpoint=self._session is not None and self._retained_owner is None and self._blocked_reason is None and not self._busy,
                can_close_editor=self._session is not None and self._retained_owner is None and self._blocked_reason is None and not self._busy,
                can_cleanup=self._retained_owner is not None and not self._busy,
                app_close_allowed=not self._busy and self._session is None and self._retained_owner is None
                    and not (self._captions is not None and self._captions.unsettled),
                error_type=(self._view.error_type if error is UNCHANGED else
                            type(error).__name__ if error is not None else None),
                stop_receipt=immutable(receipt) if receipt is not None else self._view.stop_receipt)
            active = self._session is not None or self._retained_owner is not None
            self._view = replace(self._view,
                can_checkpoint=self._document_is_open() and not self._busy,
                can_close_editor=self.managed_exit and self._document_is_open() and not self._busy,
                can_check_close=self.managed_exit and active and not self._busy,
                can_cleanup=active and not self._busy,
                can_recover_working=self.managed_exit and self._session is not None
                    and self._blocked_reason is not None and not self._publication_attempted and not self._busy)
            return self._view

    def _run(self, operation):
        if threading.current_thread() is threading.main_thread():
            return replace(self.status(), state='blocked', message='Editor operations require a background owner.',
                           error_type='RuntimeError')
        if not self._operation_lock.acquire(blocking=False):
            return replace(self.status(), state='busy', message='An editor operation is already running.',
                           can_checkpoint=False, can_close_editor=False, can_cleanup=False, app_close_allowed=False)
        self._busy = True
        self._view_update(self._view.state, self._view.message)
        refusal = None
        try:
            operation()
        except OperationRefused as error:
            refusal = str(error)
        except Exception as error:
            self._handle_failure(error)
        finally:
            self._busy = False
            self._view_update(self._view.state, self._view.message)
            self._operation_lock.release()
        result = self.status()
        return replace(result, state='blocked', message=refusal, error_type='OperationRefused') if refusal else result

    def _handle_failure(self, error):
        # Retain capabilities before any exception can reach a generic worker.
        owner = getattr(error, 'owner', None)
        if self._lifecycle is not None:
            if self._session is None and owner is not None:
                self._retained_owner, self._retained_kind = owner, 'session'
            actual = self._session or self._retained_owner
            if actual is not None:
                actual.companion_asset_guards = self._guards
            self._blocked_reason = str(error)
            try:
                self._lifecycle.update(state='reconciliation', error=str(error)[:4000])
            except Exception:
                # The launch-time barrier already blocks another editor.
                pass
            self._view_update('reconciliation_required', str(error), error=error,
                              owner=self._session or self._retained_owner)
            return
        if owner is self._session:
            self._session = None
        settled_session_error = (isinstance(error, SessionError) and owner is not None
                                 and known_stop(getattr(owner, 'stop_receipt', None), 'session'))
        if settled_session_error:
            self._view = replace(self._view, stop_receipt=immutable(owner.stop_receipt))
            if error.cleanup_error:
                self._blocked_reason = self._blocked_reason or str(error)
        elif owner is not None:
            self._retained_owner = owner
            self._retained_kind = 'creation' if isinstance(error, NativeMomentCreationError) else 'session'
        elif self._session is not None:
            self._retained_owner, self._retained_kind = self._session, 'session'
            self._session = None
        uncertain = getattr(error, 'state', None) in ('outcome_unknown', 'publication_unknown')
        if uncertain or self._retained_owner is not None:
            self._blocked_reason = self._blocked_reason or str(error)
        if self._retained_owner is None:
            self._guards.close()
            self._guards = ExitStack()
        directory = getattr(error, 'directory', None)
        if directory is not None:
            self._view = replace(self._view, directory=Path(directory))
        self._view_update('reconciliation_required' if self._blocked_reason else 'failed', str(error), error=error,
                          owner=owner if settled_session_error else None)

    def _read_facts(self, moment_id, expected_revision, profile_id, scene_key):
        require(self.workspace.database.is_file(), 'An existing canonical library is required')
        with CreatorLibrary(self.workspace.database) as library:
            return native_moment.admission(library, moment_id, expected_revision, profile_id, scene_key)

    def _assert_facts(self, *, verify=False):
        facts = self._facts
        current = self._read_facts(facts.moment.id, facts.moment.revision, self._profile_id, self._scene_key)
        require(current == facts, 'Canonical Moment, Kit or asset pins changed')
        if verify:
            with CreatorLibrary(self.workspace.database) as library:
                native_moment.verify_files(library, facts)
            current = self._read_facts(facts.moment.id, facts.moment.revision, self._profile_id, self._scene_key)
            require(current == facts, 'Canonical pins changed during source verification')

    def _assert_publication_base(self):
        current = self._store.load()
        working = self._view.working
        require(current is not None and current.generation == working.base_generation
                and current.revision == working.base_revision, 'The project publication base changed')
        return current

    def _protect_assets(self):
        for path in dict.fromkeys((self._facts.source.path, self._facts.template.path)):
            protected = _source_read_guard(Path(path))
            require(protected is not None, 'Source protection requires the qualified Windows guard')
            self._guards.callback(protected[0].CloseHandle, protected[1])

    def _provenance(self, bridge, owner):
        result = bridge.execute('metadata.get', {'item': owner})
        require(isinstance(result, dict) and type(result.get('item')) is int and result['item'] == owner
                and isinstance(result.get('metadata'), dict), 'Native metadata owner differs')
        raw = result['metadata'].get('clipline.creatorKit.v1')
        return self._canonical_provenance(raw)

    def _canonical_provenance(self, raw):
        require(isinstance(raw, str) and len(raw.encode('utf8')) <= 2 * 1024**2,
                'Bounded native Creator Kit provenance is required')
        body = json.loads(raw, object_pairs_hook=strict_object, parse_constant=nonfinite)
        require(isinstance(body, dict) and type(body.get('schema')) is int and body['schema'] == 1,
                'Native Creator Kit provenance is invalid')
        f = self._facts
        descriptor = json.loads(f.kit.descriptor_json)
        profile = json.loads(json.dumps(f.profile))
        for rect in profile['regions'].values():
            for key in rect:
                rect[key] = float(rect[key])
        expected = {'momentId': f.moment.id, 'momentRevision': f.moment.revision,
            'kit': {'id': f.kit.kit_id, 'version': f.kit.version, 'descriptorSha256': f.kit.descriptor_sha256},
            'sourceAsset': asdict(f.source), 'templateAsset': asdict(f.template),
            'assetRefs': descriptor['asset_refs'], 'sceneProfile': profile,
            'requestedSourceRangeUs': {'start': f.moment.source_in_us, 'end': f.moment.source_out_us}}
        require(same_json({key: body.get(key) for key in expected}, expected),
                'Native creation identity differs from the selected canonical Moment')
        identity = body.get('nativeImportedIdentity')
        require(isinstance(identity, dict) and type(identity.get('size')) is int
                and identity['size'] == f.source.bytes, 'Native imported source identity is absent')
        return identity

    def _inspect(self):
        bridge = self._session.bridge
        before = _project_identity(bridge.execute('project.inspect', {}))
        require(before['path'] == str(self._view.working.path.resolve(strict=True)), 'Another native project is open')
        validation = bridge.execute('clipline.validate', {'expectedRevision': before['revision']})
        require(isinstance(validation, dict) and validation.get('valid') is True
                and type(validation.get('revision')) is int and validation['revision'] == before['revision'],
                'Native validation revision differs')
        owners = validation.get('owners')
        require(isinstance(owners, list) and 1 <= len(owners) <= 64, 'A bounded native linked owner inventory is required')
        links = []
        for owner in owners:
            require(isinstance(owner, dict) and type(owner.get('owner')) is int and 0 < owner['owner'] < 2**64
                    and type(owner.get('linksRevision')) is int and 0 <= owner['linksRevision'] < 2**64,
                    'Native linked owner identity is invalid')
            links.append({'owner': owner['owner'], 'revision': owner['linksRevision']})
            identity = self._provenance(bridge, owner['owner'])
            units = owner.get('units')
            require(isinstance(units, list) and 1 <= len(units) <= 4096, 'Native linked unit inventory is invalid')
            for unit in units:
                roles = unit.get('roles') if isinstance(unit, dict) else None
                require(isinstance(roles, list) and 1 <= len(roles) <= 2, 'Native source role inventory is invalid')
                for role in roles:
                    require(isinstance(role, dict) and role.get('resolvedMediaPath') == self._facts.source.path
                            and same_json(role.get('storedMediaIdentity'), identity), 'Native resolved source differs')
        require(len({row['owner'] for row in links}) == len(links), 'Native owner repeats')
        after = _project_identity(bridge.execute('project.inspect', {}))
        require(after == before, 'Native project changed during admission')
        return before['revision'], links

    def open(self, moment_id, expected_revision, *, profile_id, scene_key, cancel=None):
        def operation():
            if self._session is not None or self._retained_owner is not None:
                raise OperationRefused('Close or clean up the existing editor owner first')
            if self._blocked_reason is not None:
                raise OperationRefused('The earlier document outcome requires explicit reconciliation')
            self._lifecycle = None
            self._quit_attempted = self._publication_attempted = False
            self._close_request = self._checkpoint_digest = None
            require(isinstance(moment_id, str) and str(uuid.UUID(moment_id)) == moment_id,
                    'Select a canonical Moment UUID')
            require(type(expected_revision) is int and expected_revision > 0, 'Select an explicit Moment revision')
            self._facts = self._read_facts(moment_id, expected_revision, profile_id, scene_key)
            self._profile_id, self._scene_key = profile_id, scene_key
            self._view = replace(self._view, moment_id=moment_id, moment_revision=expected_revision, owner=None,
                                 snapshot=None, working=None, directory=None, stop_receipt=None)
            self._protect_assets()
            self._assert_facts(verify=True)
            moments = self.workspace.directory.resolve() / 'moments'
            require(not moments.is_symlink() and not moments.is_junction(), 'The moment store root must not be redirected')
            target = moments / moment_id
            require(not target.is_symlink() and not target.is_junction(), 'The selected moment store must not be redirected')
            self._store = ProjectStore(target)
            require(not (self._store.directory / 'editor-lifecycle.json').exists(),
                    'A prior native lifecycle requires reconciliation before opening this Moment')
            snapshot = self._store.load()
            if snapshot is None:
                created = native_moment.create_native_moment(self.workspace.database, moment_id, expected_revision,
                    profile_id=profile_id, scene_key=scene_key, helper=self.helper, python=self.python,
                    store=self._store, runs=self.workspace.directory / 'native-create-runs',
                    timeout_seconds=self.creation_timeout_seconds, cancel=cancel)
                snapshot = created.snapshot
                self._view = replace(self._view, directory=created.directory)
            self._view = replace(self._view, snapshot=snapshot)
            self._assert_facts()
            working = checkout(self._store)
            require((working.base_generation, working.base_revision) == (snapshot.generation, snapshot.revision)
                    and digest(working.path) == snapshot.sha256, 'The committed snapshot changed during checkout')
            self._view = replace(self._view, working=working)
            options = {}
            if self.managed_exit:
                nonce = str(uuid.uuid4())
                lifecycle = Lifecycle(self._store, nonce)
                lifecycle.reserve(snapshot, working, asdict(self._facts))
                self._lifecycle = lifecycle
                options['managed_exit_nonce'] = nonce
            self._session = NativeSession.start(self._store, working, self.editor, python=self.python,
                runs=self.workspace.directory / 'native-editor-runs', cancel=cancel,
                readiness_seconds=self.readiness_seconds, **options)
            self._session.companion_asset_guards = self._guards
            if self._lifecycle is not None:
                require(self._session.managed_exit is not None
                        and self._session.managed_exit['nonce'] == self._lifecycle.nonce,
                        'The native session lacks its admitted managed-exit identity')
                self._lifecycle.update(state='open', run=str(self._session.directory),
                    manifest_sha256=self._session.managed_exit['manifest_sha256'])
            self._view = replace(self._view, directory=self._session.directory)
            try:
                self._inspect()
                self._assert_facts()
            except Exception as error:
                self._blocked_reason = str(error)
                failed_session = self._session
                self._retained_owner, self._retained_kind = failed_session, 'session'
                self._session = None
                try:
                    self._cleanup_owner()
                except Exception as cleanup:
                    raise SessionError(str(error), failed_session.directory, owner=failed_session,
                                       cleanup_error=str(cleanup)) from error
                raise
            if self._captions is not None:self._captions.refresh_barrier()
            self._view_update('open', 'Native editor opened for the selected Moment.', error=None)
        return self._run(operation)

    def checkpoint(self):
        def operation():
            self._caption_normal_admission()
            if not self._document_is_open():
                raise OperationRefused('A verified open editor is required')
            self._assert_facts(verify=True)
            self._assert_publication_base()
            revision, links = self._inspect()
            self._assert_facts()
            try:
                details = {}
                snapshot, working = publish_checkpoint(self._store, self._session.bridge, self._view.working,
                    native_revision=revision, links_revisions=links, engine_version=self.editor.version,
                    capture_details=details)
                self._view = replace(self._view, snapshot=snapshot, working=working)
                self._session.working = working
                if self.managed_exit:
                    content_hash = details['validation'].get('projectContentSHA256')
                    require(sha(content_hash), 'The qualified native content fingerprint is absent')
                    self._checkpoint_digest = content_hash
                    self._lifecycle.update(state='open', generation=snapshot.generation, revision=snapshot.revision,
                                           checkpoint_content_sha256=content_hash)
                self._assert_facts()
            except Exception as error:
                self._blocked_reason = str(error)
                raise
            self._view_update('open', 'Native checkpoint saved.', error=None)
        return self._run(operation)

    def _cleanup_owner(self):
        owner, kind = self._retained_owner, self._retained_kind
        receipt = owner.stop()
        self._view = replace(self._view, stop_receipt=immutable(receipt))
        require(known_stop(receipt, kind), 'Owned native process cessation is not confirmed')
        if kind == 'creation':
            owner.close()
        self._guards.close()
        self._guards = ExitStack()
        self._retained_owner = self._retained_kind = None
        return receipt

    def close(self):
        """Normal closure never falls back to emergency termination."""
        return self._run(lambda: self._refuse_close())

    def _refuse_close(self):
        raise OperationRefused('Use Save and close, or explicit emergency Cleanup')

    def cleanup(self):
        def operation():
            self._caption_normal_admission()
            if self._retained_owner is None and self._session is not None:
                self._retained_owner, self._retained_kind = self._session, 'session'
                self._session = None
                self._blocked_reason = self._blocked_reason or 'Emergency termination requires document reconciliation'
            if self._retained_owner is None:
                raise OperationRefused('No retained owner requires cleanup')
            receipt = self._cleanup_owner()
            self._view_update('reconciliation_required' if self._blocked_reason else 'closed',
                'Owned process stopped. Document uncertainty remains.' if self._blocked_reason else 'Owned process stopped.',
                receipt=receipt)
        return self._run(operation)

    def _validate_final_identity(self, identity):
        require(isinstance(identity, dict) and sha(identity.get('projectContentSHA256')),
                'The decoded native project identity is absent')
        validation = identity.get('validation')
        require(isinstance(validation, dict) and validation.get('valid') is True,
                'Native validation did not accept the saved document')
        owners, metadata = validation.get('owners'), identity.get('ownerMetadata')
        require(isinstance(owners, list) and 1 <= len(owners) <= 64 and isinstance(metadata, dict),
                'A bounded final native owner inventory is required')
        seen = set()
        for owner in owners:
            require(isinstance(owner, dict) and type(owner.get('owner')) is int
                    and 0 < owner['owner'] < 2**64 and owner['owner'] not in seen,
                    'Invalid final native owner identity')
            seen.add(owner['owner'])
            body = metadata.get(str(owner['owner']))
            require(isinstance(body, dict), 'Final native owner metadata is absent')
            imported = self._canonical_provenance(body.get('clipline.creatorKit.v1'))
            units = owner.get('units')
            require(isinstance(units, list) and 1 <= len(units) <= 4096,
                    'Final native source units are absent')
            for unit in units:
                roles = unit.get('roles') if isinstance(unit, dict) else None
                require(isinstance(roles, list) and 1 <= len(roles) <= 2, 'Final native source roles are invalid')
                for role in roles:
                    require(isinstance(role, dict) and role.get('resolvedMediaPath') == self._facts.source.path
                            and same_json(role.get('storedMediaIdentity'), imported),
                            'The final native source differs from the canonical source')
        dependencies = identity.get('mediaDependencies')
        require(isinstance(dependencies, list) and all(isinstance(row, dict)
                and row.get('path') == self._facts.source.path for row in dependencies),
                'The final native document contains an unadmitted source dependency')

    def _settle_exit(self):
        self._caption_normal_admission()
        session = self._session or self._retained_owner
        require(session is not None and self._retained_kind in (None, 'session'),
                'The original native session owner is required')
        observed = session.observe_exit()
        self._view = replace(self._view, stop_receipt=immutable(observed))
        if not known_stop(observed, 'session') or observed.get('parent_reaped') is not True:
            if (self._session is not None and not self._quit_attempted and self._blocked_reason is None
                    and observed.get('parent_reaped') is False
                    and observed.get('scope', {}).get('disposition') == 'running'):
                self._view_update('open', 'The editor is still open.', owner=session, receipt=observed)
                return False
            if self._session is not None and self._quit_attempted and observed.get('parent_reaped') is False:
                try:
                    status = session.bridge.call('clipline.managedExitStatus',
                        {'nonce': self._lifecycle.nonce, 'requestId': self._close_request})
                except (SessionUnavailable, OutcomeUnknown, EditorUnavailable):
                    self._view_update('close_pending',
                        'The editor close status is temporarily unavailable. Check close again.',
                        owner=session, receipt=observed)
                    return False
                validate_status(status, self._lifecycle.nonce, self._close_request)
                if status['pending'] is False and status['decision'] == 'cancel':
                    self._assert_facts()
                    self._inspect()
                    self._lifecycle.update(state='open', cancelled_close_request=self._close_request)
                    self._quit_attempted = False
                    self._blocked_reason = None
                    self._view_update('open', 'Closing cancelled. The editor remains open.', error=None)
                    return True
            self._view_update('close_pending', 'The editor is still open or its process scope is unconfirmed.',
                              owner=session, receipt=observed)
            return False
        require(not self._publication_attempted, 'The previous publication attempt requires explicit reconciliation')
        with read_exit(session) as (receipt, document):
            self._assert_facts(verify=True)
            require(receipt['initialIdentity'].get('projectPath') == str(session.path),
                    'The initial native receipt uses a foreign project path')
            self._validate_final_identity(receipt['initialIdentity'])
            saved_identity = receipt['savedContent']['identity']
            self._validate_final_identity(saved_identity)
            current = self._store.load()
            working = self._view.working
            require(current is not None and current.generation == working.base_generation
                    and current.revision == working.base_revision, 'The canonical project publication changed')
            content_hash = saved_identity['projectContentSHA256']
            self._lifecycle.update(state='final_evidence_admitted', decision=receipt['decision'],
                                   final_content_sha256=content_hash,
                                   saved_artifact=receipt['savedContent']['artifact'])
            snapshot = current
            if content_hash != self._checkpoint_digest:
                self._publication_attempted = True
                self._lifecycle.update(state='publication_pending')
                snapshot = self._store.publish(document, expected_generation=working.base_generation,
                    revision=working.base_revision + 1, engine='filmcraft', engine_version=self.editor.version,
                    suffix='.fcproj')
            self._view = replace(self._view, snapshot=snapshot)
            # SQLite/native/filesystem owners have a retained crash boundary,
            # not a joint commit. A changed canonical row keeps the barrier.
            self._assert_facts()
            self._lifecycle.settle(receipt['state'], snapshot)
        self._guards.close()
        self._guards = ExitStack()
        self._session = self._retained_owner = self._retained_kind = None
        self._blocked_reason = None
        self._view = replace(self._view, owner=None)
        self._view_update('closed', 'The editor closed. Saved edits are retained.'
                          if receipt['state'] == 'saved' else
                          'The editor closed. Unsaved changes were discarded; saved edits are retained.',
                          error=None, receipt=observed)
        return True

    def save_and_close(self, *, cancel=None):
        def operation():
            self._caption_normal_admission()
            if not self.managed_exit:
                raise OperationRefused('Save and close requires an explicitly qualified managed-exit runtime')
            if not self._document_is_open():
                raise OperationRefused('A verified open managed editor is required')
            if self._quit_attempted:
                raise OperationRefused('Check the existing close request; it must not be retried')
            self._assert_facts(verify=True)
            self._assert_publication_base()
            self._inspect()
            close_id = str(uuid.uuid4())
            self._lifecycle.update(state='save_close_pending', close_request=close_id,
                generation=self._view.working.base_generation, revision=self._view.working.base_revision)
            self._session.bridge.execute('file.save', {})
            revision, links = self._inspect()
            details = {}
            snapshot, working = publish_checkpoint(self._store, self._session.bridge, self._view.working,
                native_revision=revision, links_revisions=links, engine_version=self.editor.version,
                capture_details=details)
            self._view = replace(self._view, snapshot=snapshot, working=working)
            self._session.working = working
            self._checkpoint_digest = details['validation'].get('projectContentSHA256')
            require(sha(self._checkpoint_digest), 'The qualified native checkpoint content fingerprint is absent')
            self._assert_facts()
            self._lifecycle.update(state='quit_pending', generation=snapshot.generation, revision=snapshot.revision,
                                   checkpoint_content_sha256=self._checkpoint_digest)
            # Set before transport: an exception may follow native acceptance.
            self._quit_attempted = True
            self._close_request = close_id
            self._session.bridge.call('ui.menu.invoke', {'id': 'app.quit', 'params': {},
                'managedExitNonce': self._lifecycle.nonce, 'managedExitRequestId': close_id})
            deadline = time.monotonic() + self.close_wait_seconds
            while True:
                if self._settle_exit():
                    return
                if cancel is not None and cancel.is_set():
                    self._view_update('close_pending', 'Stopped waiting. The editor can still finish closing.')
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                if cancel is None:
                    time.sleep(min(0.05, remaining))
                else:
                    cancel.wait(min(0.05, remaining))
        return self._run(operation)

    def check_close(self):
        return self._run(self._settle_exit)

    def recover_working(self):
        """Re-admit the same live document, or verify final saved evidence.

        This sends no Save, quit, or capture mutation. An outstanding quit needs
        its correlated native Cancel or final scope0 evidence first.
        """
        def operation():
            if self._publication_attempted:
                raise OperationRefused('A publication may have completed; reconcile its retained generation first')
            if self._session is not None and not self._quit_attempted:
                observed = self._session.observe_exit()
                if (observed.get('parent_reaped') is False
                        and observed.get('scope', {}).get('disposition') == 'running'):
                    self._assert_facts(verify=True)
                    self._assert_publication_base()
                    self._inspect()
                    self._lifecycle.update(state='open', recovery='same_live_document_readmitted')
                    self._blocked_reason = None
                    self._view_update('open', 'The editor identity is verified. Current edits remain open.', error=None)
                    return
            self._settle_exit()
        return self._run(operation)

    def _caption_normal_admission(self):
        if self._captions is not None:
            if not self._captions.unsettled:self._captions.refresh_barrier()
            if self._captions.unsettled:
                raise OperationRefused('Settle the retained caption operation or use its exact Check editor before checkpoint or normal close')

    def caption_status(self):
        return self._captions.status() if self._captions is not None else None

    def caption_context(self,expected_moment_id,expected_moment_revision,*,cancel=None):
        return self._captions.context(expected_moment_id,expected_moment_revision,cancel=cancel)

    def generate_captions(self,context_token,selection,*,cancel=None):
        return self._captions.generate(context_token,selection,cancel=cancel)

    def accept_caption_proposal(self,context_token,job_id,transcript_id,hypothesis_id,alignment_id,*,expected_revision=0,tracks=None):
        return self._captions.accept(context_token,job_id,transcript_id,hypothesis_id,alignment_id,expected_revision=expected_revision,tracks=tracks)

    def repair_captions(self,context_token,action,view=None,**fields):
        return self._captions.repair(context_token,action,view,**fields)

    def observe_captions(self,*,cleanup=False):
        return self._captions.observe(cleanup=cleanup)

    def caption_source_monitor(self,context_token,action,*,word_id=None):
        return self._captions.source_monitor(context_token,action,word_id=word_id)
