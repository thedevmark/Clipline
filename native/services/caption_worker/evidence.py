"""Source-clock machine evidence; this module owns no edit or presentation timeline."""
from __future__ import annotations
import hashlib
import json
import math
import re
import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime,timezone
from pathlib import Path

MAX_REQUEST = 65_536
MAX_EVENT = 1_048_576
MAX_WIRE = 3 * MAX_EVENT
MAX_LOG = 131_072

class EvidenceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def hash_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def integer(value, name, low=0, high=2**63-1):
    if type(value) is not int or not low <= value <= high:
        raise EvidenceError("INVALID_REQUEST", f"{name} must be an integer in [{low}, {high}]")
    return value


def validate_vocabulary(value, kit_pin):
    """A nullable, bounded snapshot in the existing vocabulary_revision field."""
    if value is None:
        return None
    try:
        fields = {"schema", "kit_id", "version", "descriptor_sha256", "vocabulary_sha256", "terms"}
        if not isinstance(value, dict) or set(value) != fields or type(value["schema"]) is not int or value["schema"] != 1:
            raise ValueError("Unsupported vocabulary snapshot")
        if str(uuid.UUID(value["kit_id"])) != value["kit_id"]:
            raise ValueError("Canonical Kit ID required")
        integer(value["version"], "Kit version", 1)
        if (not isinstance(kit_pin, dict) or set(kit_pin) != {"kit_id", "version"}
                or type(kit_pin["version"]) is not int
                or kit_pin != {"kit_id": value["kit_id"], "version": value["version"]}):
            raise ValueError("Vocabulary and Kit version differ")
        for key in ("descriptor_sha256", "vocabulary_sha256"):
            if not isinstance(value[key], str) or not re.fullmatch("[0-9a-f]{64}", value[key]):
                raise ValueError("Invalid vocabulary provenance hash")
        terms = value["terms"]
        if not isinstance(terms, list) or not 1 <= len(terms) <= 64:
            raise ValueError("Use 1 to 64 authored vocabulary terms")
        for term in terms:
            if not isinstance(term, str) or not term.strip() or len(term) > 80 or not term.isprintable():
                raise ValueError("Vocabulary terms must be printable single lines of at most 80 characters")
        text = ", ".join(term.strip() for term in terms)
        if len(text.encode("utf-8")) > 2048:
            raise ValueError("Vocabulary hints exceed 2048 UTF-8 bytes")
        if digest(terms) != value["vocabulary_sha256"]:
            raise ValueError("Authored vocabulary hash differs")
        return text
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise EvidenceError("INVALID_VOCABULARY", str(exc)) from exc


def validate_hint_evidence(request, evidence):
    value = request.get("vocabulary_revision")
    if value is None:
        return
    text = validate_vocabulary(value, request.get("kit_revision"))
    runtime = evidence.get("runtime")
    hints = runtime.get("asr_hints") if isinstance(runtime, dict) else None
    expected = {"mechanism": "faster-whisper.hotwords", "vocabulary_revision": value,
                "text": text, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "token_limit": 223}
    if (not isinstance(hints, dict) or set(hints) != {*expected, "token_count"}
            or any(hints.get(k) != v for k, v in expected.items())
            or type(hints["token_count"]) is not int or not 1 <= hints["token_count"] <= 223):
        raise EvidenceError("INVALID_EVIDENCE", "Applied ASR hints differ from the request")
    if "vocabulary_revision" in evidence and evidence["vocabulary_revision"] != value:
        raise EvidenceError("INVALID_EVIDENCE", "Evidence vocabulary differs from the request")

def validate_request(r):
    try:
        if not isinstance(r,dict):
            raise ValueError("request must be an object")
        if r["schema_version"] != 1 or r["task"] != "transcribe" or r["language"] != "en":
            raise ValueError("unsupported schema, task or language")
        if not isinstance(r["run_id"],str) or not isinstance(r["source"]["asset_id"],str):
            raise ValueError("run and asset IDs must be UUID strings")
        uuid.UUID(r["run_id"])
        uuid.UUID(r["source"]["asset_id"])
        for d, key in [(r["source"], "sha256"), (r["audio"], "wav_sha256"), (r["audio"], "pcm_sha256"), (r, "profile_sha256")]:
            if not re.fullmatch("[0-9a-f]{64}", d[key]):
                raise ValueError(f"invalid {key}")
        for field in ["path", "profile_path"]:
            value = r["source"][field] if field == "path" else r[field]
            if not isinstance(value, str) or not value or value.startswith(("\\\\", "//")) or not Path(value).is_absolute():
                raise ValueError("local paths required")
        if not isinstance(r["audio"]["path"],str) or r["audio"]["path"].startswith(("\\\\", "//")) or not Path(r["audio"]["path"]).is_absolute():
            raise ValueError("local PCM path required")
        integer(r["source"]["audio_stream_index"], "stream index", 0, 64)
        if r["source"]["mix"] not in ["unknown", "isolated_microphone", "mixed"]:
            raise ValueError("unknown audio ownership")
        channels = r["source"]["channels"]
        if not channels or len(channels) > 32 or len(set(channels)) != len(channels):
            raise ValueError("invalid selected channels")
        for channel in channels:
            integer(channel, "channel", 0, 31)
        a = r["audio"]
        if a["sample_rate"] != 16000 or a["channels"] != 1 or a["sample_width"] != 2:
            raise ValueError("profile requires mono 16 kHz signed 16-bit PCM")
        integer(a["sample_count"], "sample count", 1, 16000 * 180)
        zero = integer(a["sample_zero_source_us"], "sample-zero source time")
        start = integer(r["range"]["start_us"], "range start")
        end = integer(r["range"]["end_us"], "range end")
        if not zero <= start < end or end-start > 120_000_000 or (end-zero)*16000 > a["sample_count"]*1_000_000:
            raise ValueError("range is outside the verified audio clock or exceeds 120 seconds")
        if ((start-zero)*16000) % 1_000_000 or ((end-zero)*16000) % 1_000_000:
            raise ValueError("range must fall on exact prepared-audio samples")
        if a["extraction"]["source_sha256"] != r["source"]["sha256"] or not a["extraction"]["argv"]:
            raise ValueError("extraction receipt is not bound to this source")
        validate_vocabulary(r.get("vocabulary_revision"), r.get("kit_revision"))
        if len(canonical(r))+1 > MAX_REQUEST:
            raise ValueError("request too large")
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as e:
        if isinstance(e, EvidenceError):
            raise
        raise EvidenceError("INVALID_REQUEST", str(e)) from e
    return r

def source_us(seconds, origin):
    if not isinstance(seconds, (float, int)) or isinstance(seconds, bool) or not math.isfinite(seconds):
        return None
    if abs(seconds)>86400:
        return None
    return origin + int((Decimal(str(seconds))*1_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))

def bounds(start, end, request, previous_end=None):
    origin = request["range"]["start_us"]
    a, b = source_us(start, origin), source_us(end, origin)
    status, reason = "aligned", None
    if start is None or end is None:
        status, reason = "missing", "missing_boundary"
    elif a is None or b is None:
        status, reason = "unresolved", "invalid_numeric_boundary"
    elif not request["range"]["start_us"] <= a < b <= request["range"]["end_us"]:
        status, reason = "unresolved", "zero_duration_or_out_of_range"
    elif previous_end is not None and a < previous_end:
        status, reason = "unresolved", "overlap"
    return {"start_us":a if status == "aligned" else None, "end_us":b if status == "aligned" else None,
            "reported_start_us":a, "reported_end_us":b, "status":status, "review_reason":reason}

def identity(run, value):
    return str(uuid.uuid5(uuid.UUID(run), value))

def make_hypothesis(request, segments, runtime):
    """Whitespace words retain character spans and all contributing ASR pieces, without averaged confidence."""
    result = {"schema_version":1,"run_id":request["run_id"],"request_sha256":digest(request),
              "created_utc":request.get("created_utc") or datetime.now(timezone.utc).isoformat(),"processing_state":"complete",
              "source":request["source"],"audio":request["audio"],"range":request["range"],
              "language":request["language"],"task":request["task"],"kit_revision":request.get("kit_revision"),
              "vocabulary_revision":request.get("vocabulary_revision"),"runtime":runtime,
              "normalization":"segment whitespace words; exact concatenated ASR-piece correspondence; no averaged probability",
              "segments":[],"words":[],"asr_pieces":[],"speaker_identity":None}
    for si, seg in enumerate(segments):
        segment_id = identity(request["run_id"], f"segment:{si}")
        result["segments"].append({"id":segment_id,"text":seg["text"],"timing":bounds(seg.get("start"),seg.get("end"),request),
            "avg_logprob":seg.get("avg_logprob"),"no_speech_prob":seg.get("no_speech_prob"),"tokens":seg.get("tokens"),
            "raw_start_seconds":seg.get("start"),"raw_end_seconds":seg.get("end")})
        pieces, offset = [], 0
        for pi, p in enumerate(seg.get("words") or []):
            raw = p["word"]
            piece={"id":identity(request["run_id"],f"segment:{si}:piece:{pi}"),"segment_id":segment_id,"text":raw,
                "char_start":offset,"char_end":offset+len(raw),"probability":p.get("probability"),
                "timing":bounds(p.get("start"),p.get("end"),request)}
            pieces.append(piece)
            result["asr_pieces"].append(piece)
            offset += len(raw)
        exact = "".join(p["text"] for p in pieces) == seg["text"]
        for wi, match in enumerate(re.finditer(r"\S+",seg["text"])):
            linked=[p for p in pieces if exact and p["char_start"] < match.end() and p["char_end"] > match.start()]
            valid=bool(linked) and all(p["timing"]["status"] == "aligned" for p in linked)
            result["words"].append({"id":identity(request["run_id"],f"segment:{si}:word:{wi}"),"segment_id":segment_id,
                "text":match.group(),"char_start":match.start(),"char_end":match.end(),"asr_piece_ids":[p["id"] for p in linked],
                "asr_start_us":linked[0]["timing"]["start_us"] if valid else None,
                "asr_end_us":linked[-1]["timing"]["end_us"] if valid else None,
                "asr_probability":linked[0]["probability"] if len(linked)==1 and linked[0]["text"].strip()==match.group() else None})
    result["raw_result_sha256"] = digest(segments)
    return result

def make_alignment(request, hypothesis, raw, runtime):
    candidates = raw.get("word_segments", [])
    words = hypothesis["words"]
    exact = len(candidates)==len(words) and all(c.get("word")==w["text"] for c,w in zip(candidates,words))
    output=[]
    previous=None
    for i,w in enumerate(words):
        candidate=candidates[i] if exact else {}
        timing=bounds(candidate.get("start"),candidate.get("end"),request,previous)
        if not exact:
            timing.update(status="unresolved",review_reason="alignment_word_correspondence_mismatch")
        if timing["end_us"] is not None:
            previous=timing["end_us"]
        output.append({"word_id":w["id"],"text":w["text"],"timing":timing,"alignment_score":candidate.get("score")})
    return {"schema_version":1,"run_id":identity(request["run_id"],"alignment:1"),"hypothesis_run_id":hypothesis["run_id"],
        "target_transcript_revision":None,"request_sha256":digest(request),"runtime":runtime,
        "interpolate_method":"ignore","correspondence":"exact full ordered word text match" if exact else "unresolved",
        "words":output,"raw_result_sha256":digest(raw),"raw_character_and_word_evidence":raw,
        "raw_character_time_origin_source_us":request["range"]["start_us"],
        "accuracy_verified":False}
