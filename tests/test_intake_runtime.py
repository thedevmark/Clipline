from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import patch

import pytest
from native.services import intake_runtime as subject


def manifest(root):
    value = {'schema': 1, 'code_root': str(root), 'client_id': 'public-fixture-client',
             'code': {subject.WORKER: 'a' * 64, 'native/__init__.py': 'b' * 64}}
    for name in subject.PROGRAM_NAMES:
        value[name] = {'path': str(root / (name + '.exe')), 'sha256': 'a' * 64}
    value['worker']['path'] = str(root / subject.WORKER)
    return value


def save(root, value):
    path = root / 'intake-runtime.json'
    path.write_text(json.dumps(value), encoding='utf8')
    return path


def test_explicit_pins_are_loaded_without_program_io_or_provider_work(tmp_path):
    value = manifest(tmp_path)
    with patch.object(subject.QualifiedProgram, 'verify', side_effect=AssertionError('No executable IO')), \
            patch.object(subject, 'TwitchIntakeClient', side_effect=AssertionError('No constructor')):
        runtime = subject.load_intake_runtime(save(tmp_path, value))
    assert runtime.python.path == tmp_path / 'python.exe'
    assert runtime.python.sha256 == 'a' * 64
    assert runtime.worker.path == tmp_path / subject.WORKER
    assert dict(runtime.code) == value['code']
    assert runtime.client_id == 'public-fixture-client'
    assert not runtime.python.path.exists()


def test_client_requires_background_owner_and_passes_detached_code(tmp_path):
    runtime = subject.load_intake_runtime(save(tmp_path, manifest(tmp_path)))
    with patch.object(subject, 'TwitchIntakeClient') as factory:
        with pytest.raises(RuntimeError, match='background owner'):
            runtime.client()
        factory.assert_not_called()
        with ThreadPoolExecutor(max_workers=1) as pool:
            client = pool.submit(runtime.client).result(5)
        assert client is factory.return_value
        tools = factory.call_args.args[0]
        assert tools.python == runtime.python and tools.gate == runtime.gate
        assert tools.worker == runtime.worker and tools.ffprobe == runtime.ffprobe
        assert tools.client_id == runtime.client_id and tools.code_root == tmp_path
        tools.code.clear()
        assert dict(runtime.code) == manifest(tmp_path)['code']


@pytest.mark.parametrize('change', [
    {'schema': True}, {'schema': 2}, {'client_id': ''}, {'client_id': 'has whitespace'},
    {'client_id': 'x' * 513}, {'unexpected': True}, {'code_root': 'relative'}, {'code': {}},
])
def test_invalid_runtime_policy_is_rejected(tmp_path, change):
    with pytest.raises(ValueError):
        subject.load_intake_runtime(save(tmp_path, manifest(tmp_path) | change))


@pytest.mark.parametrize('change', [
    {'path': 'python'}, {'sha256': 'A' * 64}, {'sha256': True}, {'extra': 'ignored'},
])
def test_invalid_program_identity_is_rejected(tmp_path, change):
    value = manifest(tmp_path)
    value['python'] |= change
    with pytest.raises(ValueError):
        subject.load_intake_runtime(save(tmp_path, value))


@pytest.mark.parametrize('name', ['', '../escape.py', 'native/../escape.py', 'native//file.py',
    'native\\file.py', 'elsewhere/file.py', 'native/metadata.json', '/native/file.py'])
def test_inventory_paths_cannot_escape_or_alias(tmp_path, name):
    value = manifest(tmp_path)
    value['code'][name] = 'c' * 64
    with pytest.raises(ValueError):
        subject.load_intake_runtime(save(tmp_path, value))


@pytest.mark.parametrize('worker_change', ['path', 'hash', 'missing'])
def test_worker_must_match_its_qualified_code_entry(tmp_path, worker_change):
    value = manifest(tmp_path)
    if worker_change == 'path': value['worker']['path'] = str(tmp_path / 'other.py')
    elif worker_change == 'hash': value['code'][subject.WORKER] = 'c' * 64
    else: del value['code'][subject.WORKER]
    with pytest.raises(ValueError, match='worker differs'):
        subject.load_intake_runtime(save(tmp_path, value))


@pytest.mark.parametrize('raw', [b' ' * 65537, b'{"schema":NaN}', b'{"schema":1,"schema":1}',
    b'{"python":{"sha256":"a","sha256":"b"}}', b'null', b'[]'],
    ids=['oversized', 'nonfinite', 'duplicate-top', 'duplicate-program', 'null', 'array'])
def test_bounded_strict_json_is_required(tmp_path, raw):
    path = tmp_path / 'intake-runtime.json'
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        subject.load_intake_runtime(path)
