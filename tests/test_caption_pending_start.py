"""A real reader bootstrap can expose ident before public start completes."""
import threading
from unittest.mock import patch

import pytest

from tests.test_live_ownership import payload, owners, client, audio_case
from native.services.caption_worker.client import ExecutionUncertain


@pytest.mark.parametrize('error', [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize('kind', ['client', 'audio'])
def test_ident_visible_before_public_start_requires_successful_join(
        payload, tmp_path, owners, error, kind):
    original = threading.Thread
    ident_ready = threading.Event()
    permit_started = threading.Event()
    skip_io = threading.Event()
    actual, peers = [], []

    class PendingStartThread(original):
        # Test-only bootstrap interception. Production uses public Thread APIs.
        def _set_ident(self):
            super()._set_ident()
            ident_ready.set()
            assert permit_started.wait(20), 'fixture bootstrap was not released'

        def start(self):
            actual.append(self)
            peer = original(target=lambda: original.start(self), daemon=True)
            peers.append(peer)
            peer.start()
            assert ident_ready.wait(5), 'fixture never exposed its actual ident'
            raise error('OS reader exists before public start completion')

        def run(self):
            if not skip_io.is_set():
                super().run()

    if kind == 'client':
        owner = client(owners)
    else:
        owner, selection, _ = audio_case(tmp_path, owners)
    try:
        with patch.object(threading, 'Thread', PendingStartThread):
            try:
                if kind == 'client':
                    owner.run(payload, tmp_path / 'runs', timeout=5)
                else:
                    owner.prepare(selection, tmp_path / 'runs', timeout=5)
            except ExecutionUncertain:
                pass
        assert actual and actual[0].ident is not None
        assert not actual[0].is_alive()
        with pytest.raises(RuntimeError, match='before it is started'):
            actual[0].join(0)
        assert owner.has_unsettled, 'pending real reader lost its exact owner'
        captured = owner._captured
        proof = owner.observe_execution(wait_seconds=0)
        assert not proof['ceased'] and not proof['readers_stopped']
        assert owner.has_unsettled and actual[0] in captured.threads
        assert not captured.process.stdout.closed
        if kind == 'audio':
            assert owner._source_guard is not None
    finally:
        if not owner.has_unsettled:
            skip_io.set()
        permit_started.set()
        for peer in peers:
            peer.join(5)
            assert not peer.is_alive()
        for thread in actual:
            thread.join(5)
            assert not thread.is_alive()
        if owner.has_unsettled:
            proof = owner.cleanup_execution(wait_seconds=5)
            assert proof['ceased'] and proof['parent_reaped'] and proof['streams_closed']
            assert not owner.has_unsettled
