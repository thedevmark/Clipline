import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from native.services.caption_worker import backend
from native.services.caption_worker.evidence import EvidenceError

class Profiles(unittest.TestCase):
    def test_legacy_cpu_int8_resolution_remains_supported(self):
        profile={'id':'best-local-cpu-small-en-v1'}
        self.assertEqual(backend.profile_mode(profile),('cpu','int8'))
        backend.validate_execution(profile,'cpu','int8_float32')

    def test_gpu_does_not_accept_cpu_or_precision_fallback(self):
        profile={'id':'clipline-best-cuda-large-v3-v1','asr_model_name':'large-v3'}
        for device,precision in [('cpu','float16'),('cuda','float32'),('cuda','int8_float16')]:
            with self.subTest(device=device,precision=precision),self.assertRaises(EvidenceError):
                backend.validate_execution(profile,device,precision)
        backend.validate_execution(profile,'cuda','float16')

    def test_new_profile_never_silently_substitutes_model_or_id(self):
        for profile in [{'id':'automatic'}, {'id':'clipline-best-cuda-large-v3-v1','asr_model_name':'small.en'}]:
            with self.assertRaises(EvidenceError):backend.profile_mode(profile)

    def runtime(self):
        directory=Path(__file__).resolve().parent/'fixture-dll-paths'
        items=[{'role':'cuda:'+name,'path':str(directory/name),'sha256':'a'*64}
               for name in ('cublas64_12.dll','cublasLt64_12.dll','cudnn64_9.dll')]
        return {'cuda_runtime':copy.deepcopy(items),'assets':copy.deepcopy(items)}

    def test_detached_cuda_pin_cannot_select_different_asset_bytes(self):
        profile=self.runtime()
        profile['cuda_runtime'][0]['sha256']='b'*64
        with self.assertRaises(EvidenceError):backend.validate_cuda_assets(profile)

    def test_missing_cuda_library_rejects_before_optional_runtime_import(self):
        profile=self.runtime()
        profile['cuda_runtime'].pop()
        with self.assertRaises(EvidenceError):backend.validate_cuda_assets(profile)

    def test_duplicate_dll_basename_is_ambiguous_even_with_distinct_roles(self):
        profile=self.runtime()
        other=copy.deepcopy(profile['cuda_runtime'][0])
        other.update(role='cuda:duplicate',path=str(Path(other['path']).parent/'other'/Path(other['path']).name))
        profile['cuda_runtime'].append(other)
        profile['assets'].append(copy.deepcopy(other))
        with self.assertRaises(EvidenceError):backend.validate_cuda_assets(profile)

    def test_large_v3_missing_preprocessor_pin_rejects_before_model_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            profile={'id':'clipline-best-cuda-large-v3-v1','asr_model_name':'large-v3',
                     'asr_model':str(root),'alignment_dir':str(root),'packages':str(root),
                     'ffmpeg':str(root/'ffmpeg.exe'),'assets':[]}
            required={'asr_model':root/'model.bin','asr_config':root/'config.json',
                      'asr_tokenizer':root/'tokenizer.json','asr_vocabulary':root/'vocabulary.json',
                      'alignment_model':root/'wav2vec2_fairseq_base_ls960_asr_ls960.pth',
                      'vad_model':root/'faster_whisper/assets/silero_vad_v6.onnx',
                      'ffmpeg':root/'ffmpeg.exe','python':Path(backend.sys.executable)}
            profile['assets']=[{'role':role,'path':str(path),'sha256':'a'*64} for role,path in required.items()]
            encoded=json.dumps(profile).encode()
            manifest=root/'profile.json'
            manifest.write_bytes(encoded)
            with patch.object(backend.sys,'version_info',(3,12)):
                with self.assertRaisesRegex(EvidenceError,'omits or misbinds'):
                    backend.preflight({'profile_path':str(manifest),'profile_sha256':hashlib.sha256(encoded).hexdigest()})

if __name__=='__main__':unittest.main()
