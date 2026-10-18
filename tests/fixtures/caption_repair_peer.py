"""Protocol fixture: source-aligned metadata, no native caption render proof."""
import json
import socket
import sys
import os
from pathlib import Path
config = Path(sys.argv[1])
data = json.loads(config.read_text())
root = config.parent
if sys.stdin.buffer.readline(32) != b'start\n':
    raise SystemExit(2)
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(8)
(root / 'ready.json').write_text(json.dumps({'port': listener.getsockname()[1]}))
state = {'pin': data['pin'], 'input': data['projection'], 'revision': 12}

def persist():
    (root / 'state.json').write_text(json.dumps(state))
persist()
while True:
    conn, _ = listener.accept()
    with conn:
        stream = conn.makefile('rwb')
        line = stream.readline(4 * 1024 ** 2)
        if not line:
            continue
        request = json.loads(line)
        command = request['params']['command']
        params = request['params']['params']
        with (root / 'calls.jsonl').open('a', encoding='utf8') as log:
            log.write(json.dumps({'command': command, 'params': params}) + '\n')
        error = None
        if command == 'project.inspect':
            reply = {'path': data['working'], 'revision': state['revision'], 'dirty': True, 'root': {}}
        elif command == 'metadata.get' and state['pin'] is None:
            reply = {'item': 5, 'metadata': {}}
        elif command == 'metadata.get':
            reply = {
                'item': 5,
                'metadata': {
                    'clipline.acceptedCaptions.v1': json.dumps(
                        {'schema': 1, 'owner': 5, 'pin': state['pin'], 'input': state['input']},
                    ),
                },
            }
        elif command == 'clipline.validate':
            if 'expectedRevision' in params and params['expectedRevision'] != state['revision']:
                error = 'stale or invalid expectedRevision'
            reply = {
                'valid': True,
                'revision': state['revision'],
                'savedRevision': 12,
                'owners': [
                    {
                        'owner': 5,
                        'linksRevision': 2,
                        'units': [
                            {
                                'roles': [
                                    {
                                        'role': 'gameplay',
                                        'sourceItem': 1,
                                        'resolvedMediaItem': 1,
                                        'resolvedMediaPath': data['source'],
                                        'storedMediaIdentity': {'size': data['bytes'], 'fingerprint': 99},
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        elif command == 'clipline.acceptCaptions':
            if data['mode'] == 'reject':
                error = 'fixture native rejection; no zero-side-effect assertion'
            elif data['mode'] == 'drop-before':
                continue
            elif params['expectedAcceptedRevision'] != state['pin']:
                error = 'stale expected accepted revision'
            elif params['expectedRevision'] != state['revision']:
                error = 'stale expected native revision'
            else:
                accepted = params['projectionInput']['accepted']
                state['pin'] = {
                    'transcript_id': accepted['transcript_id'],
                    'revision': accepted['revision'],
                    'payload_sha256': 'd' * 64,
                }
                state['input'] = params['projectionInput']
                state['revision'] += 1
                persist()
                if data['mode'] == 'close-after':
                    continue
            reply = {'owner': 5, 'accepted': state['pin'], 'revision': state['revision'], 'captions': 3}
        else:
            error = 'unknown fixture command'
            reply = None
        response = {'id': request['id'], 'ok': False, 'error': error} if error else {'id': request['id'], 'ok': True, 'result': reply}
        stream.write((json.dumps(response) + '\n').encode())
        stream.flush()
