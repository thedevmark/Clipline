"""Correction-induced ownership cleanup regression check."""
import pytest
from tests.test_twitch_durable_intake import case,submit,background
from native.core.creator_library import CreatorLibrary
from native.services.caption_worker.evidence import hash_file
from native.services.durable_native_exports import ExportOwnershipError
from native.services.durable_twitch_intake import DurableTwitchIntake
from native.services.twitch_intake_client import TwitchIntakeClient,IntakeError


def test_actual_guard_only_owner_can_retry_explicit_cleanup(case,monkeypatch):
    import native.services.native_export_client as runtime
    c=case; submitted=submit(c)
    acquire=runtime._source_read_guard
    allow_close=[False]
    class RetryableClose:
        def __init__(self,library): self.library=library
        def CloseHandle(self,handle):
            return self.library.CloseHandle(handle) if allow_close[0] else False
    def guarded(path):
        library,handle=acquire(path)
        return RetryableClose(library),handle
    def admit_then_fail(self,execution):
        execution.hold(c['fixture'],hash_file(c['fixture']))
        raise IntakeError('AUTHORED_ADMISSION_FAILURE')
    monkeypatch.setattr(runtime,'_source_read_guard',guarded)
    monkeypatch.setattr(TwitchIntakeClient,'admit',admit_then_fail)
    def work():
        actual=None
        try:
            with CreatorLibrary(c['db']) as lib:
                owner=DurableTwitchIntake(lib,c['service'],c['client'])
                with pytest.raises(ExportOwnershipError) as caught: owner.run_next()
                actual=caught.value.executions[0]
                assert owner._execution is actual and not actual.closed
                assert not actual.processes and actual.guards
                assert lib.jobs.job(submitted.job['id'])['state']=='interrupted'
                assert lib.jobs.resources()
                allow_close[0]=True
                result=owner.cleanup_pending(submitted.job['id'],execution=actual)
                assert result['closed'] and not actual.guards
                assert lib.jobs.resources(),'cleanup cannot fabricate settlement'
        finally:
            allow_close[0]=True
            if actual is not None: assert actual.stop()['closed']
    background(work)