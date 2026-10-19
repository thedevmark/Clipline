"""Focused exception/lifetime probes against ordinary imported candidate owners."""
import copy
import threading
from unittest.mock import patch
import pytest
from tests.test_live_ownership import payload, owners, client, durable
from native.services.caption_worker import client as module
from native.services.caption_worker.process_tree import ProcessTree
from native.services.caption_worker.client import ExecutionUncertain
from native.services.durable_captions import CaptionJobError


def test_started_reader_is_retained_when_start_is_interrupted(payload,tmp_path,owners):
    owner=client(owners,'sleep')
    actual=[]
    original=threading.Thread.start
    def start_then_interrupt(thread):
        original(thread)
        actual.append(thread)
        raise KeyboardInterrupt('interrupted after actual OS thread start')
    try:
        with patch.object(threading.Thread,'start',start_then_interrupt), patch.object(ProcessTree,'stop',
                return_value={'disposition':'unknown','active_processes':None}):
            with pytest.raises(ExecutionUncertain):owner.run(payload,tmp_path/'runs',timeout=.1)
        assert owner.has_unsettled and owner._captured.process.poll() is None
        assert actual and actual[0].is_alive()
        observed=owner.observe_execution(wait_seconds=0)
        assert observed['readers_stopped'] is False, 'Observation reports stopped readers while the interrupted-start reader is still alive'
        assert actual[0] in owner._captured.threads, 'The live reader started before the abort is absent from the retained owner thread set'
    finally:
        if owner.has_unsettled:assert owner.cleanup_execution(wait_seconds=5)['ceased']
        for thread in actual:
            thread.join(5)
            assert not thread.is_alive()


def test_returned_interruption_cannot_erase_last_retained_claim(durable):
    library,runner,submit,owner=durable
    first,second=submit(),submit()
    with patch.object(ProcessTree,'stop',return_value={'disposition':'unknown','active_processes':None}):
        outcome=runner.run_next(job_id=first['id'],timeout=5)
    expected=copy.deepcopy(runner.interrupted_job)
    assert expected['id']==first['id'] and runner.retained_owner is owner
    holds=library.jobs.resources()
    outcome.clear()
    assert owner.has_unsettled and runner.retained_owner is owner
    assert library.jobs.resources()==holds and library.jobs.settlement(first['id']) is None
    with pytest.raises(CaptionJobError):runner.run_next(job_id=second['id'],timeout=5)
    assert library.jobs.job(second['id'])['state']=='queued'
    assert runner.interrupted_job==expected, 'Mutating the returned status erased the exact internally retained claim'
