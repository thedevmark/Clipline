"""Only the subprocess imports this optional, cached CPU model stack."""
from __future__ import annotations
import hashlib
import importlib.metadata
import json
import os
import platform
import socket
import sys
import wave
import time
from pathlib import Path
from .evidence import EvidenceError, hash_file, validate_vocabulary


PROFILE_MODES = {
    "best-local-cpu-small-en-v1": ("cpu", "int8", None),
    "clipline-quick-cuda-small-en-v1": ("cuda", "float16", "small.en"),
    "clipline-best-cuda-large-v3-v1": ("cuda", "float16", "large-v3"),
}

def profile_mode(profile):
    mode = PROFILE_MODES.get(profile.get("id"))
    if mode is None:
        raise EvidenceError("PROFILE_UNAVAILABLE", "Unsupported explicit local profile")
    if mode[0] == "cuda" and profile.get("asr_model_name") != mode[2]:
        raise EvidenceError("PROFILE_UNAVAILABLE", "GPU model identity differs from the selected profile")
    return mode[:2]

def validate_execution(profile, actual_device, actual_compute):
    device, compute_type = profile_mode(profile)
    # CPU int8 can resolve to int8_float32; the existing CPU profile permits that.
    if actual_device != device or device == "cuda" and actual_compute != compute_type:
        raise EvidenceError("PROFILE_UNAVAILABLE", "Actual ASR device/precision differs from the pinned profile")

def validate_cuda_assets(profile):
    runtime = profile.get("cuda_runtime")
    if not isinstance(runtime, list) or not runtime or len(runtime) > 32:
        raise EvidenceError("PROFILE_UNAVAILABLE", "Pinned CUDA runtime files are required")
    roles = {asset["role"]: asset for asset in profile["assets"]}
    seen = set()
    for item in runtime:
        if (not isinstance(item, dict) or set(item) != {"role", "path", "sha256"}
                or item["role"] in seen or item["role"] not in roles
                or roles[item["role"]]["path"] != item["path"]
                or roles[item["role"]]["sha256"] != item["sha256"]
                or not Path(item["path"]).is_absolute()
                or Path(item["path"]).suffix.lower() != ".dll"):
            raise EvidenceError("PROFILE_UNAVAILABLE", "CUDA runtime pin differs from the profile assets")
        seen.add(item["role"])
    names = [Path(item["path"]).name.lower() for item in runtime]
    if len(names) != len(set(names)) or not {"cublas64_12.dll", "cublaslt64_12.dll", "cudnn64_9.dll"}.issubset(names):
        raise EvidenceError("PROFILE_UNAVAILABLE", "CUDA runtime closure is incomplete or ambiguous")
    return runtime

def configure_cuda(profile):
    import ctypes
    runtime = validate_cuda_assets(profile)
    handles = []
    directories = sorted({str(Path(item["path"]).parent) for item in runtime})
    for directory in directories:
        handles.append(os.add_dll_directory(directory))
    os.environ["PATH"] = os.pathsep.join([*directories, os.environ.get("PATH", "")])
    for name in ("cublasLt64_12.dll", "cublas64_12.dll", "cudnn64_9.dll"):
        item = next(item for item in runtime if Path(item["path"]).name.lower() == name.lower())
        handles.append(ctypes.WinDLL(item["path"]))
    return handles


def deny_network():
    def denied(*args,**kwargs):
        raise EvidenceError("NETWORK_DISABLED","Caption worker cannot use network services")
    socket.socket.connect=denied
    socket.socket.connect_ex=denied
    socket.create_connection=denied
    socket.getaddrinfo=denied

def preflight(request):
    profile_path=Path(request["profile_path"])
    try:
        if hash_file(profile_path)!=request["profile_sha256"]:
            raise EvidenceError("PROFILE_MISMATCH","Profile manifest changed")
        profile=json.loads(profile_path.read_text(encoding="utf-8"))
        device, compute_type = profile_mode(profile)
        if sys.version_info[:2]!=(3,12):
            raise EvidenceError("PROFILE_UNAVAILABLE","This profile requires isolated Python 3.12")
        expected={"asr_model":Path(profile["asr_model"])/"model.bin","asr_config":Path(profile["asr_model"])/"config.json",
            "asr_tokenizer":Path(profile["asr_model"])/"tokenizer.json","asr_vocabulary":Path(profile["asr_model"])/("vocabulary.json" if profile.get("asr_model_name")=="large-v3" else "vocabulary.txt"),
            "alignment_model":Path(profile["alignment_dir"])/"wav2vec2_fairseq_base_ls960_asr_ls960.pth",
            "vad_model":Path(profile["packages"])/"faster_whisper/assets/silero_vad_v6.onnx","ffmpeg":Path(profile["ffmpeg"]),"python":Path(sys.executable)}
        if profile.get("asr_model_name") == "large-v3":
            expected["asr_preprocessor"] = Path(profile["asr_model"])/"preprocessor_config.json"
        roles={a["role"]:a for a in profile["assets"]}
        if len(roles)!=len(profile["assets"]) or any(role not in roles or Path(roles[role]["path"]).resolve()!=p.resolve() for role,p in expected.items()):
            raise EvidenceError("PROFILE_UNAVAILABLE","Profile omits or misbinds a required asset")
        if device == "cuda":
            validate_cuda_assets(profile)
        for asset in profile["assets"]:
            p=Path(asset["path"])
            if not p.is_file() or hash_file(p)!=asset["sha256"]:
                raise EvidenceError("PROFILE_UNAVAILABLE",f"Missing or changed cached profile asset: {p.name}")
        required={"torch","torchaudio","numpy","nltk","whisperx","faster-whisper","ctranslate2"}
        if not required.issubset(profile["versions"]):
            raise EvidenceError("PROFILE_UNAVAILABLE","Profile package pins are incomplete")
        versions={d.metadata["Name"].lower():d.version for d in importlib.metadata.distributions(path=[profile["packages"]])}
        for name,version in profile["versions"].items():
            if versions.get(name.lower())!=version:
                raise EvidenceError("PROFILE_UNAVAILABLE",f"Package profile mismatch: {name}")
        profile["observed_versions"]=versions
        verify_source_audio(request)
        if not (Path(profile["nltk_data"])/"tokenizers/punkt_tab/english").is_dir():
            raise EvidenceError("PROFILE_UNAVAILABLE","Cached NLTK tokenizer is missing")
        return profile
    except EvidenceError:
        raise
    except (OSError,KeyError,TypeError,ValueError,wave.Error) as e:
        raise EvidenceError("PROFILE_UNAVAILABLE",str(e)) from e

def verify_source_audio(request):
    try:
        source=Path(request["source"]["path"])
        if source.stat().st_size>2*1024**3:
            raise EvidenceError("SOURCE_TOO_LARGE","Prototype accepts clip assets up to 2 GiB")
        if hash_file(source)!=request["source"]["sha256"]:
            raise EvidenceError("SOURCE_MISMATCH","Source fingerprint changed")
        audio=request["audio"]
        if hash_file(audio["path"])!=audio["wav_sha256"]:
            raise EvidenceError("AUDIO_MISMATCH","Prepared WAV fingerprint changed")
        with wave.open(audio["path"],"rb") as wav:
            if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes())!=(1,2,16000,audio["sample_count"]):
                raise EvidenceError("AUDIO_MISMATCH","Prepared PCM sample clock changed")
            pcm=wav.readframes(wav.getnframes())
        if len(pcm)!=audio["sample_count"]*2 or hashlib.sha256(pcm).hexdigest()!=audio["pcm_sha256"]:
            raise EvidenceError("AUDIO_MISMATCH","PCM bytes changed")
    except EvidenceError:
        raise
    except (OSError,KeyError,TypeError,ValueError,wave.Error) as e:
        raise EvidenceError("AUDIO_MISMATCH",str(e)) from e

def asr_hint_options(request, model):
    text = validate_vocabulary(request.get("vocabulary_revision"), request.get("kit_revision"))
    if text is None:
        return {}, None
    # Match faster-whisper 1.2.1 get_prompt, which otherwise silently truncates.
    if model.max_length != 448:
        raise EvidenceError("PROFILE_UNAVAILABLE", "Unsupported ASR hint token budget")
    count = len(model.hf_tokenizer.encode(" " + text, add_special_tokens=False).ids)
    if not 1 <= count <= 223:
        raise EvidenceError("INVALID_VOCABULARY", "Saved vocabulary exceeds the model's 223 hint-token budget")
    receipt = {"mechanism": "faster-whisper.hotwords",
               "vocabulary_revision": request["vocabulary_revision"], "text": text,
               "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
               "token_count": count, "token_limit": 223}
    return {"hotwords": text}, receipt


def transcribe_with_hints(request, model, audio):
    options, receipt = asr_hint_options(request, model)
    iterator, info = model.transcribe(audio, language="en", beam_size=5, vad_filter=True,
                                     word_timestamps=True, condition_on_previous_text=False, **options)
    return iterator, info, receipt


def load(request, profile):
    deny_network()
    os.environ.update(HF_HUB_OFFLINE="1",HF_HUB_DISABLE_TELEMETRY="1",TRANSFORMERS_OFFLINE="1",DO_NOT_TRACK="1",
        HF_HOME=profile["hf_home"],TORCH_HOME=profile["torch_home"],NLTK_DATA=profile["nltk_data"],
        OMP_NUM_THREADS="2",MKL_NUM_THREADS="2")
    os.environ["PATH"]=str(Path(profile["ffmpeg"]).parent)+os.pathsep+os.environ.get("PATH","")
    sys.path.insert(0,profile["packages"])
    device, compute_type = profile_mode(profile)
    cuda_handles = configure_cuda(profile) if device == "cuda" else []
    import torch
    import whisperx
    import faster_whisper
    torch.set_num_threads(2)
    print("phase=decode_verified_pcm",file=sys.stderr,flush=True)
    # The qualified path bypasses faster-whisper/PyAV file decoding entirely.
    audio=whisperx.load_audio(request["audio"]["path"])
    if len(audio)!=request["audio"]["sample_count"]:
        raise EvidenceError("AUDIO_MISMATCH","FFmpeg decoded a different sample count")
    decoded_pcm=(audio*32768.0).astype("<i2").tobytes()
    if hashlib.sha256(decoded_pcm).hexdigest()!=request["audio"]["pcm_sha256"]:
        raise EvidenceError("AUDIO_MISMATCH","Actual FFmpeg samples do not match the verified PCM bytes")
    zero=request["audio"]["sample_zero_source_us"]
    start=(request["range"]["start_us"]-zero)*16000//1_000_000
    end=(request["range"]["end_us"]-zero)*16000//1_000_000
    audio=audio[start:end]
    import ctranslate2
    if device == "cuda":
        if ctranslate2.get_cuda_device_count() < 1 or compute_type not in ctranslate2.get_supported_compute_types("cuda", 0):
            raise EvidenceError("PROFILE_UNAVAILABLE", "Explicit CUDA float16 execution is unavailable; CPU fallback is forbidden")
    print("phase=asr_model_load",file=sys.stderr,flush=True)
    load_started = time.perf_counter()
    model=faster_whisper.WhisperModel(profile["asr_model"],device=device,compute_type=compute_type,cpu_threads=2,local_files_only=True)
    actual_device = model.model.device
    actual_compute = model.model.compute_type
    validate_execution(profile, actual_device, actual_compute)
    model_load_seconds = time.perf_counter() - load_started
    inference_started = time.perf_counter()
    print("phase=asr_inference",file=sys.stderr,flush=True)
    iterator, info, hint_receipt = transcribe_with_hints(request, model, audio)
    segments=[]
    for s in iterator:
        segments.append({"start":s.start,"end":s.end,"text":s.text,"avg_logprob":s.avg_logprob,"no_speech_prob":s.no_speech_prob,
            "tokens":list(s.tokens),"words":[{"word":w.word,"start":w.start,"end":w.end,"probability":w.probability} for w in s.words or []]})
    asr_seconds = time.perf_counter() - inference_started
    del model
    print("phase=asr_complete",file=sys.stderr,flush=True)
    def align():
        if not segments:
            return {"segments":[],"word_segments":[],"skipped":"empty ASR hypothesis"}
        print("phase=alignment_model_load",file=sys.stderr,flush=True)
        alignment_started = time.perf_counter()
        model,metadata=whisperx.load_align_model("en","cpu",model_name="WAV2VEC2_ASR_BASE_960H",model_dir=profile["alignment_dir"])
        print("phase=alignment_inference",file=sys.stderr,flush=True)
        aligned = whisperx.align(segments,model,metadata,audio,"cpu",interpolate_method="ignore",return_char_alignments=True)
        runtime["phase_seconds"]["alignment_cpu"] = time.perf_counter() - alignment_started
        return aligned
    runtime={"profile_id":profile["id"],"profile_sha256":request["profile_sha256"],"python":sys.version,
        "versions":profile["observed_versions"],"assets":profile["assets"],"device":actual_device,"compute_type":compute_type,"threads":2,
        "actual_compute_type":actual_compute,
        "alignment_device":"cpu","asr_model_name":profile.get("asr_model_name","small"),
        "phase_seconds":{"asr_model_load":model_load_seconds,"asr_inference":asr_seconds},
        "cuda_runtime":profile.get("cuda_runtime",[]),
        "beam_size":5,"vad_filter":True,"word_timestamps":True,"condition_on_previous_text":False,
        "network_policy":"Python socket calls denied plus offline model settings; not an OS firewall",
        "audio_decode":"WhisperX.load_audio: FFmpeg to NumPy, reused by ASR and alignment",
        "sample_zero_source_us":request["range"]["start_us"],"speaker_identity":None}
    if hint_receipt is not None:
        runtime["asr_hints"] = hint_receipt
    runtime["source_time_conversion"]="reported selected-range seconds to integer source microseconds, ROUND_HALF_UP; precision is not asserted"
    runtime["hardware"]={"machine":platform.machine(),"processor":platform.processor(),"logical_cpu_count":os.cpu_count()}
    return segments,align,runtime
