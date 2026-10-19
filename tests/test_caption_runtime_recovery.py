"""Trusted launch and same-live-owner recovery; fake Python workloads only."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch
import uuid

import pytest

from tests.test_live_ownership import payload, owners, client, durable, audio_case
from native.core.creator_library import CreatorLibrary
from native.core.durable_jobs import VerificationRequired, StaleTransition
from native.services import caption_audio
from native.services.caption_worker import client as module
from native.services.caption_worker.client import CaptionClient, ExecutionUncertain
from native.services.caption_worker.process_tree import ProcessTree
from native.services.durable_captions import CaptionJobError, DurableCaptionRunner

ROOT = Path(__file__).resolve().parents[1]


def interrupt(durable, *, live=False):
    library, runner, submit, owner = durable
    if live: owner.command[-1] = 'sleep'
    job = submit()
    with patch.object(ProcessTree, 'stop', return_value={'disposition': 'unknown', 'active_processes': None}):
        result = runner.run_next(job_id=job['id'], timeout=.2 if live else 5)
    assert result['state'] == 'interrupted'
    return job, result


def test_all_relevant_modules_are_normal_snapshot_imports():
    from native.core import creator_library
    from native.services import durable_captions
    from native.services.caption_worker import process_tree
    for imported in (module, caption_audio, durable_captions, process_tree, creator_library):
        assert Path(imported.__file__).resolve().is_relative_to(ROOT)
    assert not list(ROOT.rglob('conftest.py'))


def test_custom_command_cannot_run_before_job_attachment(payload, tmp_path, owners):
    marker = tmp_path / 'worker-started'
    owner = CaptionClient(sys.executable, 'unused', command=[sys.executable, '-c',
        'from pathlib import Path;Path(' + repr(str(marker)) + ').write_text("ran")'])
    owners.append(owner)
    with patch.object(module, 'ProcessTree', side_effect=OSError('attachment failed')):
        failed = owner.run(payload, tmp_path / 'runs', timeout=5)
    assert failed.state == 'failed' and not marker.exists() and not owner.has_unsettled
    proof = json.loads((failed.directory / 'receipt.json').read_text())['process_scope']
    assert proof == {'disposition': 'never_started', 'scope': 'unreleased_gate_parent',
                     'gate_released': False, 'requested_work_started': False}
    assert owner.current_execution is not None
    receipt = json.loads((failed.directory / 'receipt.json').read_text())
    assert receipt['worker_exit_code'] is None
    assert receipt['launch_process_exit_code'] is not None
    assert owner.observe_execution(expected_execution=owner.current_execution)['ceased']
    # This later explicit request is released after actual successful attachment.
    owner.run({**payload, 'run_id': str(uuid.uuid4())}, tmp_path / 'retry', timeout=5)
    assert marker.read_text() == 'ran' and not owner.has_unsettled


def test_custom_preparation_gate_cannot_run_before_attachment(tmp_path, owners):
    owner, selection, source = audio_case(tmp_path, owners)
    marker = tmp_path / 'tool-started'
    owner.gate.write_text('from pathlib import Path;Path(' + repr(str(marker)) + ').write_text("ran")')
    original = source.read_bytes()
    with patch.object(caption_audio, 'ProcessTree', side_effect=OSError('attachment failed')):
        result = owner.prepare(selection, tmp_path / 'runs', timeout=5)
    assert result.state == 'failed' and not marker.exists() and not owner.has_unsettled
    assert source.read_bytes() == original and owner._source_guard is None
    source.write_bytes(original)  # The actual share-read guard was released.
    proof = json.loads((result.directory / 'receipt.json').read_text())['commands'][0]['process_scope']
    assert proof['scope'] == 'unreleased_gate_parent' and proof['disposition'] == 'never_started'
    assert 'active_processes' not in proof


def test_gate_preserves_worker_input_bytes_without_read_ahead(payload, tmp_path, owners):
    owner = client(owners)
    payload['test_padding'] = 'x' * 50000
    result = owner.run(payload, tmp_path / 'runs', timeout=5)
    assert result.state == 'complete' and not owner.has_unsettled
    receipt = json.loads((result.directory / 'receipt.json').read_text())
    assert receipt['process_scope']['active_processes'] == 0 and receipt['streams_closed']


@pytest.mark.parametrize('which', ['reader', 'sender'])
def test_abort_after_actual_thread_start_keeps_original_thread(payload, tmp_path, owners, which):
    owner = client(owners, 'sleep')
    actual = []
    original = threading.Thread.start
    def abort(thread):
        target_name = thread._target.__name__
        original(thread)
        if (target_name == 'send') == (which == 'sender'):
            actual.append(thread)
            raise KeyboardInterrupt('after actual start')
    try:
        with patch.object(threading.Thread, 'start', side_effect=abort, autospec=True), patch.object(ProcessTree, 'stop', return_value={'disposition': 'unknown', 'active_processes': None}):
            with pytest.raises(ExecutionUncertain): owner.run(payload, tmp_path / 'runs', timeout=5)
        assert actual and actual[0] in owner._captured.threads
        assert owner.has_unsettled
    finally:
        if owner.has_unsettled: assert owner.cleanup_execution(wait_seconds=5)['ceased']
        for thread in actual:
            thread.join(5)
            assert not thread.is_alive()


def test_entered_start_with_no_ident_is_not_retried_by_cleanup(payload, tmp_path, owners):
    owner = client(owners)
    with patch.object(threading.Thread, 'start', side_effect=RuntimeError('before OS thread start')):
        with pytest.raises(ExecutionUncertain): owner.run(payload, tmp_path / 'runs', timeout=5)
    assert owner.has_unsettled
    thread = owner._captured.threads[0]
    try:
        with patch.object(threading.Thread, 'start', side_effect=AssertionError('cleanup must not start a reader')):
            proof = owner.cleanup_execution(wait_seconds=0)
        assert not proof['ceased'] and owner.has_unsettled and thread.ident is None
    finally:
        # A fixture peer makes the original pending-start reference observable.
        # Production never starts/retries a reader during cleanup.
        thread.start(); thread.join(5)
        assert owner.observe_execution()['ceased'] and not owner.has_unsettled


@pytest.mark.parametrize('error', [RuntimeError, KeyboardInterrupt])
def test_real_started_reader_with_pending_public_ident_stays_retained(payload, tmp_path, owners, error):
    owner = client(owners)
    original_thread = threading.Thread
    visible = threading.Event()
    actual = []
    class PendingIdentityThread(original_thread):
        @property
        def ident(self):
            return super().ident if visible.is_set() else None
        def start(self):
            super().start()
            actual.append(self)
            raise error('after low-level launch, public ident pending')
    try:
        with patch.object(threading, 'Thread', PendingIdentityThread), patch.object(ProcessTree, 'stop', return_value={'disposition': 'unknown', 'active_processes': None}):
            with pytest.raises(ExecutionUncertain): owner.run(payload, tmp_path / 'runs', timeout=5)
        assert actual and actual[0].is_alive() and actual[0].ident is None
        proof = owner.observe_execution()
        assert not proof['readers_stopped'] and owner.has_unsettled
        assert actual[0] in owner._captured.threads
    finally:
        visible.set()
        if owner.has_unsettled: assert owner.cleanup_execution(wait_seconds=5)['ceased']
        for thread in actual:
            thread.join(5)
            assert not thread.is_alive()


def test_nested_status_mutation_cannot_change_retained_claim(durable):
    library, runner, submit, owner = durable
    _, result = interrupt(durable)
    expected = runner.interrupted_job
    result['input']['request_ref']['sha256'] = '0' * 64
    result['input']['assets'].clear()
    detached = runner.interrupted_job
    detached['input'].clear()
    assert runner.interrupted_job == expected
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    done = runner.reconcile_execution(expected['id'], expected_revision=expected['revision'])
    assert done['state'] == 'failed' and not library.jobs.resources()


def test_reconcile_is_explicit_and_never_stops_a_live_process(durable):
    library, runner, submit, owner = durable
    first, current = interrupt(durable, live=True)
    holds = library.jobs.resources()
    with patch.object(ProcessTree, 'stop', side_effect=AssertionError('reconcile must not terminate')):
        with pytest.raises(ExecutionUncertain): runner.reconcile_execution(first['id'], expected_revision=current['revision'])
    assert owner.has_unsettled and library.jobs.resources() == holds
    receipt_path = owner._captured.directory / 'receipt.json'
    initial = receipt_path.read_bytes()
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    assert library.jobs.resources() == holds
    done = runner.reconcile_execution(first['id'], expected_revision=current['revision'])
    assert done['state'] == 'failed' and library.jobs.resources() == []
    assert receipt_path.read_bytes() == initial
    assert library.jobs.settlement(first['id'])['outputs_disposition'] == 'quarantined'
    with pytest.raises(CaptionJobError): runner.import_completed(first['id'])


def test_fresh_connection_and_different_thread_reconcile_after_original_closed(durable):
    library, runner, submit, owner = durable
    first, current = interrupt(durable)
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    library.connection.close()
    results, failures = [], []
    def settle():
        try: results.append(runner.reconcile_execution(first['id'], expected_revision=current['revision']))
        except BaseException as exc: failures.append(exc)
    thread = threading.Thread(target=settle)
    thread.start(); thread.join(10)
    assert not thread.is_alive() and failures == []
    assert results[0]['state'] == 'failed' and runner.retained_owner is None
    with CreatorLibrary(runner.database) as fresh:
        assert fresh.jobs.resources() == []
        assert fresh.jobs.job(first['id'])['state'] == 'failed'


def test_stale_revision_does_not_release_holds_and_explicit_cancel_reconciles(durable):
    library, runner, submit, owner = durable
    first, current = interrupt(durable)
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    updated = library.jobs.request_cancel(first['id'], expected_revision=current['revision'])
    holds = library.jobs.resources()
    with pytest.raises(StaleTransition): runner.reconcile_execution(first['id'], expected_revision=current['revision'])
    assert library.jobs.resources() == holds
    assert runner.reconcile_execution(first['id'], expected_revision=updated['revision'])['state'] == 'cancelled'
    assert library.jobs.resources() == []


def test_wrong_or_replaced_claim_does_not_release_holds(durable):
    library, runner, submit, owner = durable
    first, current = interrupt(durable)
    second = submit()
    holds = library.jobs.resources()
    with pytest.raises(VerificationRequired): runner.reconcile_execution(second['id'], expected_revision=second['revision'])
    assert library.jobs.resources() == holds
    library.connection.execute('UPDATE durable_job_heads SET claim_token=?,revision=revision+1 WHERE job_id=?',
                               (str(uuid.uuid4()), first['id']))
    changed = library.jobs.job(first['id'])
    with pytest.raises(VerificationRequired): runner.reconcile_execution(first['id'], expected_revision=changed['revision'])
    assert library.jobs.resources() == holds


def test_replaced_client_and_unrelated_original_client_run_are_rejected(durable, tmp_path):
    library, runner, submit, owner = durable
    first, current = interrupt(durable)
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    runner.client = CaptionClient(sys.executable, 'unused')
    with pytest.raises(VerificationRequired): runner.reconcile_execution(first['id'], expected_revision=current['revision'])
    runner.client = owner
    token = owner.current_execution
    with pytest.raises(ExecutionUncertain): owner.observe_execution(expected_execution=replace(token))
    request = json.loads((token.directory / 'request.json').read_text())
    request['run_id'] = str(uuid.uuid4())
    assert owner.run(request, tmp_path / 'unrelated', timeout=5).state == 'complete'
    holds = library.jobs.resources()
    with pytest.raises(VerificationRequired): runner.reconcile_execution(first['id'], expected_revision=current['revision'])
    assert library.jobs.resources() == holds


def test_cold_runner_cannot_adopt_artifacts_for_reconciliation(durable):
    library, runner, submit, owner = durable
    first, current = interrupt(durable)
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    cold = DurableCaptionRunner(library, runner.directory, CaptionClient(sys.executable, 'unused'))
    with pytest.raises(VerificationRequired): cold.reconcile_execution(first['id'], expected_revision=current['revision'])
    assert library.jobs.resources()


def test_client_lock_blocks_unrelated_run_during_settlement(durable, tmp_path):
    library, runner, submit, owner = durable
    first, current = interrupt(durable)
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    original = runner._verify
    def verify(job, reference):
        with pytest.raises(ExecutionUncertain): owner.run({}, tmp_path / 'must-not-launch')
        return original(job, reference)
    with patch.object(runner, '_verify', side_effect=verify):
        assert runner.reconcile_execution(first['id'], expected_revision=current['revision'])['state'] == 'failed'
    assert not (tmp_path / 'must-not-launch').exists()


def test_initial_missing_receipt_diagnostic_can_fail_settle_without_rewriting(durable):
    library, runner, submit, owner = durable
    job = submit()
    original = module.write_once
    def fault(path, value):
        if path.name == 'receipt.json': raise OSError('initial receipt write failed')
        return original(path, value)
    with patch.object(module, 'write_once', side_effect=fault):
        current = runner.run_next(job_id=job['id'], timeout=5)
    assert current['state'] == 'interrupted' and owner.has_unsettled
    receipt = owner.current_execution.directory / 'receipt.json'
    assert not receipt.exists()
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    assert runner.reconcile_execution(job['id'], expected_revision=current['revision'])['state'] == 'failed'
    assert not receipt.exists() and library.jobs.resources() == []


def test_unexpected_abort_running_claim_requires_explicit_interruption_and_reconcile(durable):
    library, runner, submit, owner = durable
    job = submit()
    original = owner.run
    def abort(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt('after actual invocation')
    with patch.object(owner, 'run', side_effect=abort), patch.object(ProcessTree, 'stop', return_value={'disposition': 'unknown', 'active_processes': None}):
        with pytest.raises(KeyboardInterrupt): runner.run_next(job_id=job['id'], timeout=5)
    current = library.jobs.job(job['id'])
    assert current['state'] == 'running'
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    result = runner.reconcile_execution(job['id'], expected_revision=current['revision'])
    assert result['state'] == 'failed' and result['revision'] == current['revision'] + 2


@pytest.mark.parametrize('which', ['drain', 'send'])
def test_audio_start_abort_retains_exact_thread_and_source_guard(tmp_path, owners, which):
    owner, selection, source = audio_case(tmp_path, owners)
    (tmp_path / 'mode.json').write_text(json.dumps({'mode': 'blocked'}))
    original = threading.Thread.start
    actual = []
    def abort(thread):
        target_name = thread._target.__name__
        original(thread)
        if target_name == which:
            actual.append(thread)
            raise KeyboardInterrupt('after actual audio thread start')
    with patch.object(threading.Thread, 'start', side_effect=abort, autospec=True), patch.object(ProcessTree, 'stop', return_value={'disposition': 'unknown', 'active_processes': None}):
        result = owner.prepare(selection, tmp_path / 'runs', timeout=5)
    assert result.owner is owner and owner.has_unsettled
    assert actual and actual[0] in owner._captured.threads and owner._source_guard is not None
    with pytest.raises(OSError): source.write_bytes(b'must remain guarded')
    assert owner.cleanup_execution(wait_seconds=5)['ceased']
    assert all(not thread.is_alive() for thread in actual)


def test_cancellation_between_attestation_and_cas_preserves_holds(durable):
    library, runner, submit, owner = durable
    first, current = interrupt(durable)
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    original = runner._verify
    def cancel_during_verify(job, reference):
        attested = original(job, reference)
        with CreatorLibrary(runner.database) as fresh:
            fresh.jobs.request_cancel(job['id'], expected_revision=job['revision'])
        return attested
    holds = library.jobs.resources()
    with patch.object(runner, '_verify', side_effect=cancel_during_verify):
        with pytest.raises(ExecutionUncertain): runner.reconcile_execution(first['id'], expected_revision=current['revision'])
    assert library.jobs.resources() == holds and library.jobs.settlement(first['id']) is None
    changed = library.jobs.job(first['id'])
    assert runner.reconcile_execution(first['id'], expected_revision=changed['revision'])['state'] == 'cancelled'


def test_changed_request_artifact_refuses_settlement_without_releasing_holds(durable):
    library, runner, submit, owner = durable
    first, current = interrupt(durable)
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    path = runner.directory / 'requests' / (current['input']['request_ref']['id'] + '.json')
    original = path.read_bytes()
    path.write_bytes(b'{}')
    holds = library.jobs.resources()
    with pytest.raises(CaptionJobError): runner.reconcile_execution(first['id'], expected_revision=current['revision'])
    assert library.jobs.resources() == holds and library.jobs.settlement(first['id']) is None
    path.write_bytes(original)


def test_next_queued_job_needs_explicit_run_after_explicit_reconciliation(durable):
    library, runner, submit, owner = durable
    first, current = interrupt(durable)
    second = submit()
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    assert library.jobs.job(second['id'])['state'] == 'queued'
    assert runner.reconcile_execution(first['id'], expected_revision=current['revision'])['state'] == 'failed'
    assert library.jobs.job(second['id'])['state'] == 'queued'
    assert runner.retained_owner is None and runner.interrupted_job is None
    assert runner.run_next(job_id=second['id'], timeout=5)['state'] == 'completed'
    with pytest.raises(VerificationRequired): runner.reconcile_execution(first['id'], expected_revision=current['revision'])


def test_live_watcher_retention_is_visible_and_blocks_reconciliation(durable):
    from types import SimpleNamespace
    library, runner, submit, owner = durable
    first, current = interrupt(durable)
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    actual_watcher = runner._retained_watcher
    runner._retained_watcher = SimpleNamespace(is_alive=lambda: True)
    try:
        status = runner.observe_execution()
        assert status['ceased'] and status['owner_unsettled']
        assert not status['controller_watchers_stopped'] and runner.has_unsettled
        with pytest.raises(VerificationRequired): runner.reconcile_execution(first['id'], expected_revision=current['revision'])
        assert library.jobs.resources()
    finally:
        runner._retained_watcher = actual_watcher


def test_oversize_parent_receipt_does_not_release_holds(durable):
    library, runner, submit, owner = durable
    first, current = interrupt(durable)
    assert runner.cleanup_execution(wait_seconds=1)['ceased']
    receipt = owner.current_execution.directory / 'receipt.json'
    original = receipt.read_bytes()
    receipt.write_bytes(b'x' * 65537)
    holds = library.jobs.resources()
    try:
        with pytest.raises(ExecutionUncertain): runner.reconcile_execution(first['id'], expected_revision=current['revision'])
        assert library.jobs.resources() == holds and library.jobs.settlement(first['id']) is None
    finally:
        receipt.write_bytes(original)


def test_execution_identity_binds_the_actual_detached_request_and_command(payload, tmp_path, owners):
    owner = client(owners)
    original_payload = copy.deepcopy(payload)
    original_command = list(owner.command)
    frame = module.gate_frame
    def mutate_caller_aliases(command):
        payload['run_id'] = str(uuid.uuid4())
        payload['source']['sha256'] = '0' * 64
        owner.command[-1] = 'crash'
        return frame(command)
    with patch.object(module, 'gate_frame', side_effect=mutate_caller_aliases):
        result = owner.run(payload, tmp_path / 'runs', timeout=10)
    assert result.state == 'complete'
    identity = owner.current_execution
    assert identity.run_id == original_payload['run_id']
    assert identity.request_sha256 == module.digest(original_payload)
    assert identity.command_sha256 == module.digest(original_command)
    receipt = json.loads((result.directory / 'receipt.json').read_text())
    assert receipt['requested_run_id'] == original_payload['run_id']
    assert json.loads((result.directory / 'request.json').read_text()) == original_payload
