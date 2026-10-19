"""Temporary config files and constructor doubles only; no executable runs."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import native.services.export_runtime as subject
from native.services.export_recipes import GPU_RECIPE_ID, SOFTWARE_RECIPE_ID


def manifest(root):
    value = {
        'schema': 1,
        'engine_version': 'qualified-native-fixture-1',
        'native_source_pin_sha256': 'b' * 64,
    }
    for name in subject.PROGRAM_NAMES:
        value[name] = {'path': str(root / (name + '.exe')), 'sha256': 'a' * 64, 'version': name + '-fixture-1'}
    return value


def save(root, value):
    path = root / 'runtime.json'
    path.write_text(json.dumps(value, allow_nan=True), encoding='utf8')
    return path


def background(function):
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(function).result(5)


def test_load_preserves_explicit_pins_without_reading_or_discovering_programs(tmp_path):
    value = manifest(tmp_path)
    with patch.object(subject.QualifiedProgram, 'verify', side_effect=AssertionError('No program IO during parsing')), \
            patch.object(subject, 'NativeExportClient', side_effect=AssertionError('No constructor during parsing')):
        runtime = subject.load_export_runtime(save(tmp_path, value))
    assert runtime.helper.path == tmp_path / 'helper.exe'
    assert runtime.python.sha256 == 'a' * 64
    assert runtime.native_source_pin_sha256 == 'b' * 64
    assert runtime.engine_version == 'qualified-native-fixture-1'
    assert dict(runtime.program_versions)['ffprobe'] == 'ffprobe-fixture-1'
    assert not runtime.helper.path.exists()
    assert runtime.recipe_id == SOFTWARE_RECIPE_ID


@pytest.mark.parametrize('change', [
    {'schema': True}, {'schema': 2}, {'schema': None}, {'extra': 'ignored'},
    {'engine_version': 1}, {'engine_version': ''}, {'engine_version': ' '},
    {'engine_version': 'x' * 129}, {'engine_version': 'line\nbreak'},
    {'native_source_pin_sha256': True}, {'native_source_pin_sha256': 'B' * 64},
    {'native_source_pin_sha256': 'b' * 63}, {'native_source_pin_sha256': 'g' * 64},
])
def test_malformed_top_level_is_rejected(tmp_path, change):
    with pytest.raises(ValueError):
        subject.load_export_runtime(save(tmp_path, manifest(tmp_path) | change))


@pytest.mark.parametrize('change', [
    {'path': 'ffprobe'}, {'path': None}, {'path': 'C:relative.exe'},
    {'path': 'relative/file.exe'}, {'path': 'x' * 32769}, {'path': 'C:/bad\nfile.exe'},
    {'sha256': 123}, {'sha256': 'A' * 64}, {'sha256': 'f' * 63},
    {'version': 3}, {'version': ''}, {'version': 'v' * 129}, {'version': '\x00'},
    {'unexpected': False},
])
def test_malformed_program_record_is_rejected(tmp_path, change):
    value = manifest(tmp_path)
    value['gate'] |= change
    with pytest.raises(ValueError):
        subject.load_export_runtime(save(tmp_path, value))


@pytest.mark.parametrize('field', ['helper', 'ffprobe', 'ffmpeg', 'python', 'gate', 'engine_version', 'native_source_pin_sha256'])
def test_each_required_identity_is_mandatory(tmp_path, field):
    value = manifest(tmp_path)
    del value[field]
    with pytest.raises(ValueError):
        subject.load_export_runtime(save(tmp_path, value))


@pytest.mark.parametrize('body', ['[]', 'null', '42', '{', '\ufeff{}', '{"schema":NaN}', '{"schema":Infinity}'])
def test_nonobject_malformed_or_nonfinite_json_is_rejected(tmp_path, body):
    path = tmp_path / 'runtime.json'
    path.write_text(body, encoding='utf8')
    with pytest.raises(ValueError):
        subject.load_export_runtime(path)


@pytest.mark.parametrize('nested', [False, True])
def test_duplicate_fields_are_rejected_at_every_depth(tmp_path, nested):
    body = json.dumps(manifest(tmp_path))
    if nested:
        body = body.replace('"sha256":', '"sha256":"' + 'c' * 64 + '","sha256":', 1)
    else:
        body = body.replace('"schema": 1', '"schema": 2,"schema": 1', 1)
    path = tmp_path / 'runtime.json'
    path.write_text(body, encoding='utf8')
    with pytest.raises(ValueError, match='duplicate'):
        subject.load_export_runtime(path)


def test_manifest_read_is_bounded(tmp_path):
    path = tmp_path / 'runtime.json'
    path.write_bytes(b' ' * 65537)
    with pytest.raises(ValueError, match='64 KiB'):
        subject.load_export_runtime(path)


def test_main_thread_is_refused_before_constructor_or_gate_io(tmp_path):
    runtime = subject.load_export_runtime(save(tmp_path, manifest(tmp_path)))
    with patch.object(subject, 'NativeExportClient') as constructor:
        with pytest.raises(RuntimeError, match='background owner'):
            runtime.client()
        constructor.assert_not_called()


def test_background_construction_returns_only_manifest_matched_captured_gate(tmp_path):
    value = manifest(tmp_path)
    gate = Path(value['gate']['path'])
    gate.write_bytes(b'authored non-executed gate fixture')
    value['gate']['sha256'] = hashlib.sha256(gate.read_bytes()).hexdigest()
    runtime = subject.load_export_runtime(save(tmp_path, value))
    client = background(runtime.client)
    assert type(client) is subject.NativeExportClient
    assert client.gate_sha256 == value['gate']['sha256']
    assert client.helper is runtime.helper
    assert client.ffprobe is runtime.ffprobe
    assert client.ffmpeg is runtime.ffmpeg
    assert client.python is runtime.python
    assert client.source_pin_sha256 == value['native_source_pin_sha256']
    assert client.engine_version == value['engine_version']
    assert client.recipe_id == SOFTWARE_RECIPE_ID


@pytest.mark.parametrize('recipe_id', [SOFTWARE_RECIPE_ID, GPU_RECIPE_ID])
def test_explicit_profile_reaches_client_without_changing_program_pins(tmp_path, recipe_id):
    value = manifest(tmp_path) | {'schema': 2, 'recipe_id': recipe_id}
    gate = Path(value['gate']['path'])
    gate.write_bytes(b'non-executed fixture gate')
    value['gate']['sha256'] = hashlib.sha256(gate.read_bytes()).hexdigest()
    runtime = subject.load_export_runtime(save(tmp_path, value))
    client = background(runtime.client)
    assert client.recipe_id == recipe_id
    assert client.helper.sha256 == value['helper']['sha256']
    assert client.source_pin_sha256 == value['native_source_pin_sha256']


@pytest.mark.parametrize('change', [
    {'schema': 1, 'recipe_id': GPU_RECIPE_ID},
    {'schema': 2, 'recipe_id': 'unqualified'},
    {'schema': 2, 'recipe_id': True},
])
def test_profile_selection_is_explicit_and_bounded(tmp_path, change):
    with pytest.raises(ValueError):
        subject.load_export_runtime(save(tmp_path, manifest(tmp_path) | change))


def test_gate_changed_after_config_load_is_not_admitted(tmp_path):
    value = manifest(tmp_path)
    gate = Path(value['gate']['path'])
    gate.write_bytes(b'original fixture gate')
    value['gate']['sha256'] = hashlib.sha256(gate.read_bytes()).hexdigest()
    runtime = subject.load_export_runtime(save(tmp_path, value))
    gate.write_bytes(b'different fixture gate')
    with pytest.raises(ValueError, match='gate differs'):
        background(runtime.client)


def test_constructor_capture_must_match_even_if_gate_is_restored_before_return(tmp_path):
    value = manifest(tmp_path)
    runtime = subject.load_export_runtime(save(tmp_path, value))
    # Model a swap inside construction: a later filesystem hash could match the
    # manifest again, but the client's captured identity must still be rejected.
    double = SimpleNamespace(gate_sha256='c' * 64)
    with patch.object(subject, 'NativeExportClient', return_value=double) as constructor:
        with pytest.raises(ValueError, match='gate differs'):
            background(runtime.client)
        assert constructor.call_count == 1


def test_constructor_exception_propagates_without_retry(tmp_path):
    runtime = subject.load_export_runtime(save(tmp_path, manifest(tmp_path)))
    with patch.object(subject, 'NativeExportClient', side_effect=OSError('fixture gate unavailable')) as constructor:
        with pytest.raises(OSError, match='fixture gate unavailable'):
            background(runtime.client)
        assert constructor.call_count == 1
