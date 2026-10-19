"""These assertions exercise old behavior without relying on the new owner API."""
from pathlib import Path
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch
import uuid
import wave

import pytest

from native.services.caption_worker import client as module
from native.services.caption_worker.evidence import hash_file

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Actual Windows JobObject')


def test_failed_handle_release_preserves_the_exact_query_handle():
    tree = object.__new__(module.ProcessTree)
    handle = object()
    tree.job = handle
    tree.kernel = SimpleNamespace(CloseHandle=lambda _: False)
    tree.close()
    assert tree.job is handle, 'CloseHandle false must not erase the actual handle'


def test_unknown_scope_does_not_dispose_the_actual_query_handle(tmp_path):
    audio = tmp_path / 'fixture.wav'
    with wave.open(str(audio), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0' * 160)
    profile = tmp_path / 'profile.json'; profile.write_text('{}')
    import hashlib
    request = {'schema_version': 1, 'run_id': str(uuid.uuid4()), 'task': 'transcribe', 'language': 'en',
               'profile_path': str(profile), 'profile_sha256': hash_file(profile),
               'source': {'asset_id': str(uuid.uuid4()), 'path': str(audio), 'sha256': hash_file(audio),
                          'audio_stream_index': 0, 'channels': [0], 'mix': 'unknown'},
               'audio': {'path': str(audio), 'wav_sha256': hash_file(audio),
                         'pcm_sha256': hashlib.sha256(b'\0\0' * 160).hexdigest(), 'sample_rate': 16000,
                         'sample_width': 2, 'sample_count': 160, 'channels': 1, 'sample_zero_source_us': 0,
                         'extraction': {'source_sha256': hash_file(audio), 'argv': ['fixture']}},
               'range': {'start_us': 0, 'end_us': 10000}}
    actual = []
    tree_type = module.ProcessTree
    stop = tree_type.stop
    def attach(process):
        tree = tree_type(process)
        actual.append(tree)
        return tree
    owner = module.CaptionClient(sys.executable, 'unused', command=[sys.executable, '-u', '-B', '-S',
        str(ROOT / 'tests/fixtures/caption_fake_worker.py'), 'success'])
    try:
        with patch.object(tree_type, 'stop', return_value={'disposition': 'unknown', 'active_processes': None}), patch.object(module, 'ProcessTree', side_effect=attach):
            owner.run(request, tmp_path / 'runs', timeout=5)
        assert len(actual) == 1 and actual[0].process.poll() == 0
        assert actual[0].job is not None, 'An unknown scope must retain its original query handle'
    finally:
        if getattr(owner, 'has_unsettled', False):
            assert owner.cleanup_execution(wait_seconds=1)['ceased']
        elif actual and actual[0].job:
            assert stop(actual[0])['disposition'] == 'stopped'
            actual[0].close()
