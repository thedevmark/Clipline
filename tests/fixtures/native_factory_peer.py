"""Protocol fixture only: Python runs this as the local `instantiate` script."""
import hashlib
import json
from pathlib import Path
import sys
import time

assert sys.argv[0].endswith('instantiate')
root = Path.cwd()
config = json.loads((root / 'fixture-config.json').read_text(encoding='utf8'))
request_path, output = map(Path, sys.argv[1:])
raw = request_path.read_bytes()
request = json.loads(raw)
(root / 'fixture-consumed.json').write_text(json.dumps(request), encoding='utf8')
mode = config.get('mode', 'success')
if mode in ('hold', 'hang', 'failure'):
    output.write_bytes(b'partial opaque fixture output')
    (root / 'fixture-ready').write_text('ready', encoding='utf8')
if mode == 'failure':
    print('fixture failure', file=sys.stderr)
    raise SystemExit(23)
if mode in ('hold', 'hang'):
    deadline = time.monotonic() + 30
    while mode == 'hang' or not (root / 'fixture-proceed').exists():
        if time.monotonic() > deadline:
            raise SystemExit(24)
        time.sleep(.01)
if mode == 'stdout-limit':
    print('x' * (2 * 1024**2 + 8192))
    raise SystemExit(0)
if mode == 'stderr-limit':
    print('x' * (128 * 1024 + 8192), file=sys.stderr)
    raise SystemExit(0)
output.write_bytes(b'OPAQUE FIXTURE DOCUMENT\0' + hashlib.sha256(raw).digest())
profile = next(profile for profile in json.loads(request['kit']['descriptor_json'])['layout_profiles']
               if profile['id'] == request['profile_id'])
role_names = [role for role in ('gameplay', 'facecam') if role in profile['regions']]
sequences = {'shared': 5, 'landscape': 13, 'portrait': 21}
compositions = []
for index, (name, sequence) in enumerate(sequences.items()):
    slots = [{'role': role, 'video_track': 100 + index * 10 + ri,
              'prototype_clip': 200 + index * 10 + ri,
              'audio_track': 300 + index if role == 'gameplay' else None,
              'prototype_audio': 400 + index if role == 'gameplay' else None}
             for ri, role in enumerate(role_names)]
    compositions.append({'name': name, 'sequence': sequence, 'width': 1080 if name == 'portrait' else 1920,
                         'height': 1920 if name == 'portrait' else 1080, 'slots': slots})
roles = [{'role': role, 'audio': 'inherit' if role == 'gameplay' else 'none',
          'clips': {name: 1000 + index * 10 + ri for index, name in enumerate(sequences)},
          'nativeAudioClips': {name: [2000 + index] if role == 'gameplay' else []
                               for index, name in enumerate(sequences)},
          'sourceItem': 50, 'resolvedMediaItem': 50,
          'resolvedMediaPath': request['source']['path'],
          'storedMediaIdentity': {'size': request['source']['bytes'], 'fingerprint': 'a' * 16}}
         for ri, role in enumerate(role_names)]
moment = request['moment']
receipt = {
    'status': 'pass', 'project': str(output), 'projectBytes': output.stat().st_size,
    'projectSha256': hashlib.sha256(output.read_bytes()).hexdigest(),
    'momentId': moment['id'], 'momentRevision': moment['revision'],
    'requestedSourceRangeUs': {'start': moment['source_in_us'], 'end': moment['source_out_us']},
    'profileId': request['profile_id'], 'sceneKey': request['scene_key'],
    'requestSha256': hashlib.sha256(raw).hexdigest(),
    'kit': {'id': request['kit']['id'], 'version': request['kit']['version'],
            'descriptorSha256': request['kit']['descriptor_sha256']},
    'sourceAsset': request['source']['id'], 'templateAsset': request['template']['id'],
    'mediaCopies': 0, 'nativeImportCalls': 1, 'exactSaveReopen': True,
    'nativeEffectsAndAudioPreserved': True,
    'nativeInterval': {'sourceInTicks': 1000000, 'durationTicks': 2000000,
                       'frames': 120, 'frameRate': {'num': 60, 'den': 1}},
    'compositions': compositions,
    'validation': {'valid': True, 'revision': 10, 'savedRevision': 10,
                   'owners': [{'owner': 5, 'schema': 2, 'linksRevision': 0,
                               'sequences': sequences,
                               'units': [{'id': moment['id'], 'parent': None, 'roles': roles}]}]},
}
for field, value in config.get('tamper', {}).items():
    target = receipt
    parts = field.split('.')
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    if isinstance(target, list):
        target[int(parts[-1])] = value
    else:
        target[parts[-1]] = value
print(json.dumps(receipt), flush=True)
