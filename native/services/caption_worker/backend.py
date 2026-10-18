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
from pathlib import Path
from .evidence import EvidenceError, hash_file

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
        if profile["id"]!="best-local-cpu-small-en-v1" or sys.version_info[:2]!=(3,12):
            raise EvidenceError("PROFILE_UNAVAILABLE","This profile requires isolated Python 3.12")
        expected={"asr_model":Path(profile["asr_model"])/"model.bin","asr_config":Path(profile["asr_model"])/"config.json",
            "asr_tokenizer":Path(profile["asr_model"])/"tokenizer.json","asr_vocabulary":Path(profile["asr_model"])/"vocabulary.txt",
            "alignment_model":Path(profile["alignment_dir"])/"wav2vec2_fairseq_base_ls960_asr_ls960.pth",
            "vad_model":Path(profile["packages"])/"faster_whisper/assets/silero_vad_v6.onnx","ffmpeg":Path(profile["ffmpeg"]),"python":Path(sys.executable)}
        roles={a["role"]:a for a in profile["assets"]}
        if len(roles)!=len(profile["assets"]) or any(role not in roles or Path(roles[role]["path"]).resolve()!=p.resolve() for role,p in expected.items()):
            raise EvidenceError("PROFILE_UNAVAILABLE","Profile omits or misbinds a required asset")
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

def load(request, profile):
    deny_network()
    os.environ.update(HF_HUB_OFFLINE="1",HF_HUB_DISABLE_TELEMETRY="1",TRANSFORMERS_OFFLINE="1",DO_NOT_TRACK="1",
        HF_HOME=profile["hf_home"],TORCH_HOME=profile["torch_home"],NLTK_DATA=profile["nltk_data"],
        OMP_NUM_THREADS="2",MKL_NUM_THREADS="2")
    os.environ["PATH"]=str(Path(profile["ffmpeg"]).parent)+os.pathsep+os.environ.get("PATH","")
    sys.path.insert(0,profile["packages"])
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
    print("phase=asr_model_load",file=sys.stderr,flush=True)
    model=faster_whisper.WhisperModel(profile["asr_model"],device="cpu",compute_type="int8",cpu_threads=2,local_files_only=True)
    print("phase=asr_inference",file=sys.stderr,flush=True)
    iterator,info=model.transcribe(audio,language="en",beam_size=5,vad_filter=True,word_timestamps=True,condition_on_previous_text=False)
    segments=[]
    for s in iterator:
        segments.append({"start":s.start,"end":s.end,"text":s.text,"avg_logprob":s.avg_logprob,"no_speech_prob":s.no_speech_prob,
            "tokens":list(s.tokens),"words":[{"word":w.word,"start":w.start,"end":w.end,"probability":w.probability} for w in s.words or []]})
    del model
    print("phase=asr_complete",file=sys.stderr,flush=True)
    def align():
        if not segments:
            return {"segments":[],"word_segments":[],"skipped":"empty ASR hypothesis"}
        print("phase=alignment_model_load",file=sys.stderr,flush=True)
        model,metadata=whisperx.load_align_model("en","cpu",model_name="WAV2VEC2_ASR_BASE_960H",model_dir=profile["alignment_dir"])
        print("phase=alignment_inference",file=sys.stderr,flush=True)
        return whisperx.align(segments,model,metadata,audio,"cpu",interpolate_method="ignore",return_char_alignments=True)
    runtime={"profile_id":profile["id"],"profile_sha256":request["profile_sha256"],"python":sys.version,
        "versions":profile["observed_versions"],"assets":profile["assets"],"device":"cpu","compute_type":"int8","threads":2,
        "beam_size":5,"vad_filter":True,"word_timestamps":True,"condition_on_previous_text":False,
        "network_policy":"Python socket calls denied plus offline model settings; not an OS firewall",
        "audio_decode":"WhisperX.load_audio: FFmpeg to NumPy, reused by ASR and alignment",
        "sample_zero_source_us":request["range"]["start_us"],"speaker_identity":None}
    runtime["source_time_conversion"]="reported selected-range seconds to integer source microseconds, ROUND_HALF_UP; precision is not asserted"
    runtime["hardware"]={"machine":platform.machine(),"processor":platform.processor(),"logical_cpu_count":os.cpu_count()}
    return segments,align,runtime
