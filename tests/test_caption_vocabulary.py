"""Canonical SQLite and model/worker doubles; no ASR or external tools."""
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest

from native.core.creator_library import CreatorLibrary
from native.services.caption_vocabulary import kit_vocabulary, verify_kit_vocabulary
from native.services.caption_worker.backend import asr_hint_options, transcribe_with_hints
from native.services.caption_worker.evidence import (
    EvidenceError, digest, validate_vocabulary, validate_request, validate_hint_evidence,
    make_hypothesis, make_alignment,
)
from native.services.durable_captions import DurableCaptionRunner
from tests import test_workspace_captions as workflow_fixture
from tests import test_caption_worker as worker_fixture
from tests import test_caption_runtime as runtime_fixture
from native.services.caption_runtime import load_caption_runtime


@pytest.fixture
def catalog(tmp_path):
    with CreatorLibrary(tmp_path / "library.sqlite") as library:
        source = tmp_path / "source.bin"
        source.write_bytes(b"authored fixture")
        asset = library.register_asset(source)
        descriptor = {"name": "Streamer", "vocabulary": ["deutschmark", "Apex Legends"]}
        kit = library.create_kit(descriptor)
        yield library, library.kit_record(kit, 1), asset, descriptor


def request_for(record):
    return {"kit_revision": {"kit_id": record.kit_id, "version": record.version},
            "vocabulary_revision": kit_vocabulary(record)}


class Model:
    max_length = 448

    def __init__(self, count=6):
        self.calls = []
        self.encoded = []
        self.count = count
        self.hf_tokenizer = SimpleNamespace(encode=self.encode)

    def encode(self, text, **kwargs):
        self.encoded.append((text, kwargs))
        return SimpleNamespace(ids=list(range(self.count)))

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return iter(()), "info"


def test_saved_version_not_latest_and_descriptor_bytes_are_pinned(catalog):
    library, record, _, descriptor = catalog
    request = request_for(record)
    changed = {**descriptor, "vocabulary": ["new game"]}
    library.revise_kit(record.kit_id, changed, expected_version=1)
    verify_kit_vocabulary(library, request)
    value = request["vocabulary_revision"]
    assert value["version"] == 1 and value["terms"] == ["deutschmark", "Apex Legends"]
    assert value["descriptor_sha256"] == hashlib.sha256(record.descriptor_json.encode()).hexdigest()
    assert value["vocabulary_sha256"] == digest(value["terms"])
    assert value != kit_vocabulary(library.kit_record(record.kit_id, 2))


@pytest.mark.parametrize("field", ["terms", "descriptor_sha256", "kit_id", "version"])
def test_forged_request_cannot_enter_canonical_runner(catalog, tmp_path, field):
    library, record, asset, _ = catalog
    request = request_for(record)
    value = request["vocabulary_revision"]
    if field == "terms":
        value[field] = ["invented term"]
        value["vocabulary_sha256"] = digest(value[field])
    elif field == "descriptor_sha256": value[field] = "0" * 64
    elif field == "kit_id": value[field] = str(uuid.uuid4())
    else: value[field] = 2
    request.update(schema_version=1, run_id=str(uuid.uuid4()), task="transcribe", language="en",
        source={"asset_id": asset.id, "sha256": asset.sha256, "path": asset.path,
                "audio_stream_index": 0, "channels": [0], "mix": "unknown"},
        audio={"path": asset.path, "wav_sha256": "a" * 64, "pcm_sha256": "b" * 64,
               "sample_rate": 16000, "channels": 1, "sample_width": 2, "sample_count": 160,
               "sample_zero_source_us": 0, "extraction": {"source_sha256": asset.sha256, "argv": ["fixture"]}},
        range={"start_us": 0, "end_us": 10000}, profile_path=asset.path, profile_sha256="c" * 64)
    runner = DurableCaptionRunner(library, tmp_path / "jobs", SimpleNamespace())
    with pytest.raises(EvidenceError):
        runner._bind({"schema": 1, "transcript_id": str(uuid.uuid4()),
                      "accepted_revision": None, "request": request})
    assert library.jobs.list_jobs()["items"] == []


@pytest.mark.parametrize("terms", [[], [" "], ["line\nbreak"], ["x" * 81], ["x"] * 65,
                                  ["é" * 80] * 14])
def test_bounds_reject_without_truncating(catalog, terms):
    _, record, _, _ = catalog
    request = request_for(record)
    value = request["vocabulary_revision"]
    value.update(terms=terms, vocabulary_sha256=digest(terms))
    with pytest.raises(EvidenceError): validate_vocabulary(value, request["kit_revision"])
    assert value["terms"] == terms


def test_authored_raw_terms_retained_with_explicit_whitespace_normalization(catalog):
    library, record, _, descriptor = catalog
    descriptor["vocabulary"] = [" deutschmark ", "Apex Legends"]
    library.revise_kit(record.kit_id, descriptor, expected_version=1)
    request = request_for(library.kit_record(record.kit_id, 2))
    options, receipt = asr_hint_options(request, Model())
    assert options == {"hotwords": "deutschmark, Apex Legends"}
    assert receipt["vocabulary_revision"]["terms"][0] == " deutschmark "
    assert receipt["text_sha256"] == hashlib.sha256(options["hotwords"].encode()).hexdigest()


def test_actual_backend_call_uses_hotwords_without_prefix_or_prompt(catalog):
    _, record, _, _ = catalog
    request = request_for(record)
    model = Model()
    _, info, receipt = transcribe_with_hints(request, model, "verified PCM")
    assert info == "info"
    assert model.calls == [("verified PCM", {"language": "en", "beam_size": 5, "vad_filter": True,
        "word_timestamps": True, "condition_on_previous_text": False,
        "hotwords": "deutschmark, Apex Legends"})]
    assert model.encoded == [(" deutschmark, Apex Legends", {"add_special_tokens": False})]
    assert receipt["token_count"] == 6


@pytest.mark.parametrize("count", [0, 224])
def test_model_token_overflow_never_starts_transcription(catalog, count):
    _, record, _, _ = catalog
    model = Model(count)
    with pytest.raises(EvidenceError): transcribe_with_hints(request_for(record), model, "PCM")
    assert model.calls == []


def test_exact_upstream_hint_token_limit_is_usable(catalog):
    _, record, _, _ = catalog
    model = Model(223)
    assert transcribe_with_hints(request_for(record), model, "PCM")[2]["token_count"] == 223
    assert len(model.calls) == 1


def test_legacy_and_empty_kit_keep_original_call(catalog):
    library, record, _, descriptor = catalog
    descriptor["vocabulary"] = []
    library.revise_kit(record.kit_id, descriptor, expected_version=1)
    assert kit_vocabulary(library.kit_record(record.kit_id, 2)) is None
    for request in ({}, {"vocabulary_revision": None}):
        model = Model()
        assert transcribe_with_hints(request, model, "PCM")[2] is None
        assert "hotwords" not in model.calls[0][1] and model.encoded == []
        verify_kit_vocabulary(library, request)


def test_hints_bind_both_machine_evidence_artifacts(catalog):
    _, record, _, _ = catalog
    request = request_for(record)
    request.update(run_id=str(uuid.uuid4()), source={}, audio={}, range={"start_us": 0, "end_us": 10000},
                   language="en", task="transcribe")
    _, receipt = asr_hint_options(request, Model())
    runtime = {"asr_hints": receipt}
    h = make_hypothesis(request, [], runtime)
    a = make_alignment(request, h, {"word_segments": []}, runtime)
    for value in (h, a):
        validate_hint_evidence(request, value)
        broken = copy.deepcopy(value)
        broken["runtime"]["asr_hints"]["text"] = "different"
        with pytest.raises(EvidenceError): validate_hint_evidence(request, broken)
    for bad in ({}, {"runtime": []}, {"runtime": {"asr_hints": {}}}):
        with pytest.raises(EvidenceError): validate_hint_evidence(request, bad)


@pytest.fixture
def workflow():
    case = workflow_fixture.CaptionWorkflowTests(methodName="runTest")
    original_create = CreatorLibrary.create_kit
    def authored_kit(library, descriptor, *args, **kwargs):
        descriptor["vocabulary"] = ["deutschmark", "Apex Legends"]
        return original_create(library, descriptor, *args, **kwargs)
    with patch.object(CreatorLibrary, "create_kit", authored_kit): case.setUp()
    case.runtime.vocabulary_hints = 'faster-whisper.hotwords.v1'
    prepare = case.prepare
    def transport(selection, *args, **kwargs):
        prepared = prepare(selection, *args, **kwargs)
        request = json.loads(prepared.request_path.read_text())
        request["vocabulary_revision"] = copy.deepcopy(selection["vocabulary_revision"])
        prepared.request_path.write_text(json.dumps(request))
        (prepared.directory / "receipt.json").write_text(json.dumps({"request_sha256": digest(request)}))
        return prepared
    worker = case.worker
    def hinted_worker(request, *args, **kwargs):
        result = worker(request, *args, **kwargs)
        _, receipt = asr_hint_options(request, Model())
        hashes = {}
        for kind in ("hypothesis", "alignment"):
            path = result.directory / (kind + ".json")
            value = json.loads(path.read_text())
            value["runtime"] = {"asr_hints": receipt}
            if kind == "hypothesis": value["vocabulary_revision"] = request["vocabulary_revision"]
            path.write_text(json.dumps(value))
            hashes[kind] = digest(value)
        path = result.directory / "receipt.json"
        value = json.loads(path.read_text()); value["hashes"].update(hashes)
        path.write_text(json.dumps(value))
        return result
    case.workflow.preparer.prepare.side_effect = transport
    case.workflow.client.run.side_effect = hinted_worker
    try: yield case
    finally: case.tearDown()


def test_workspace_generation_uses_exact_saved_kit_and_imports_proposal_only(workflow):
    with CreatorLibrary(workflow.workspace.database) as library:
        new = copy.deepcopy(workflow.descriptor); new["vocabulary"] = ["new Kit term"]
        library.revise_kit(workflow.kit_id, new, expected_version=1)
    proposal = workflow.generate()
    selected = workflow.prepared_selections[0]["vocabulary_revision"]
    assert selected["terms"] == ["deutschmark", "Apex Legends"] and selected["version"] == 1
    assert selected["descriptor_sha256"] == workflow.kit.descriptor_sha256
    assert workflow.worker_requests[0]["vocabulary_revision"] == selected
    with CreatorLibrary(workflow.workspace.database) as library:
        assert library.captions.head(proposal.proposal["transcript_id"]) == 0
        assert library.moment(workflow.moment.id).kit_version == 1
        assert len(library.captions.list_proposals(proposal.proposal["transcript_id"])["items"]) == 2
        stored = library.captions.proposal(proposal.proposal["transcript_id"], proposal.proposal["hypothesis_id"])
        assert stored["vocabulary_revision"] == selected
        assert stored["runtime"]["asr_hints"]["vocabulary_revision"] == selected


def test_new_hinted_proposal_does_not_change_previous_accepted_corrections(workflow):
    first = workflow.generate()
    accepted = workflow.accept(first)
    accepted = workflow.bg(workflow.controller.repair_captions, first.context_token, "apply", accepted.repair)
    assert accepted.state == "saved_applied", accepted.message
    corrected = workflow.bg(workflow.controller.repair_captions, first.context_token, "replace",
        accepted.repair, word_id=accepted.repair.words[1].id, text="shot")
    assert corrected.state == "saved_applied", corrected.message
    transcript = first.proposal["transcript_id"]
    with CreatorLibrary(workflow.workspace.database) as library:
        before = library.captions.revision(transcript)
    second = workflow.generate()
    assert second.proposal["transcript_id"] != transcript
    with CreatorLibrary(workflow.workspace.database) as library:
        assert library.captions.revision(transcript) == before
        assert library.captions.head(second.proposal["transcript_id"]) == 0


def test_parent_rejects_worker_that_omits_requested_hint_evidence(catalog):
    _, record, _, _ = catalog
    case = worker_fixture.WorkerTests(methodName="runTest")
    case.setUp()
    try:
        case.r.update(request_for(record))
        client = case.client("success")  # Existing JSONL fake worker omits hint evidence.
        result = client.run(case.r, case.root / "runs", timeout=10)
        assert result.error_code == "INVALID_EVIDENCE"
        assert result.hypothesis_path is None and result.alignment_path is None
        assert not client.has_unsettled
        receipt = json.loads((result.directory / "receipt.json").read_text())
        assert receipt["process_scope"]["disposition"] == "stopped"
        assert receipt["process_scope"]["active_processes"] == 0 and receipt["streams_closed"]
        print("Hint omission fake worker: stopped, active_processes=0, streams_closed=true")
    finally:
        case.doCleanups()


def hint_manifest(tmp_path):
    value = runtime_fixture.manifest(tmp_path)
    value.update(schema=2, vocabulary_hints='faster-whisper.hotwords.v1')
    value['worker'] = {'path': str(tmp_path / 'worker.py'), 'sha256': 'b' * 64}
    value['assets_verified'] += [
        {'path': str(tmp_path / name), 'sha256': ('b' if name == 'worker.py' else 'c') * 64}
        for name in ('worker.py', 'backend.py', 'evidence.py')]
    return value


def test_explicit_hint_capability_requires_its_closure_and_legacy_stays_disabled(tmp_path):
    path = tmp_path / 'runtime.json'
    path.write_text(json.dumps(runtime_fixture.manifest(tmp_path)))
    assert load_caption_runtime(path).vocabulary_hints is None
    path.write_text(json.dumps(hint_manifest(tmp_path)))
    runtime = load_caption_runtime(path)
    assert runtime.vocabulary_hints == 'faster-whisper.hotwords.v1'
    assert len(json.loads(runtime.assets_json)) == 4


@pytest.mark.parametrize('change', [
    lambda x: x.update(vocabulary_hints='unsupported'),
    lambda x: x.update(schema=1),
    lambda x: x['assets_verified'].pop(),
    lambda x: x['assets_verified'].append(dict(x['assets_verified'][-1])),
    lambda x: x['assets_verified'][1].update(sha256='d' * 64),
])
def test_incompatible_manifest_rejected_before_worker_use(tmp_path, change):
    value = hint_manifest(tmp_path); change(value)
    path = tmp_path / 'runtime.json'; path.write_text(json.dumps(value))
    with pytest.raises(ValueError): load_caption_runtime(path)


def test_old_runtime_refuses_nonempty_vocabulary_before_audio_preparation(workflow):
    workflow.runtime.vocabulary_hints = None
    context = workflow.context()
    tracks = [{'sequence': g['sequence'], 'track': g['tracks'][0]['id']} for g in context.track_choices]
    result = workflow.bg(workflow.controller.generate_captions, context.context_token,
        {'stream_index': 7, 'channels': [0, 1], 'tracks': tracks})
    assert result.state == 'blocked' and 'hint-capable' in result.message
    assert workflow.prepared_selections == [] and workflow.worker_requests == []
