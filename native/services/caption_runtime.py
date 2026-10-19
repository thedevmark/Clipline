"""Explicit, optional local caption runtime. No discovery, download or quality default."""
from dataclasses import dataclass
import json
from pathlib import Path
from native.services.caption_worker.evidence import hash_file
from native.services.editor_runtime import _object


@dataclass(frozen=True)
class CaptionRuntime:
    display_name: str
    profile: tuple[str, str]
    python: tuple[str, str]
    worker: tuple[str, str]
    ffmpeg: tuple[str, str]
    ffprobe: tuple[str, str]
    projection_json: str
    assets_json: str

    @property
    def projection(self):
        return json.loads(self.projection_json)

    def verify(self):
        """Background admission before tool use; the existing worker also pins its profile."""
        for path, pin in [self.profile, self.python, self.worker, self.ffmpeg, self.ffprobe,
                          *[(x['path'], x['sha256']) for x in json.loads(self.assets_json)]]:
            if hash_file(path) != pin:
                raise ValueError('A configured caption runtime input changed: ' + Path(path).name)


def load_caption_runtime(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('Caption runtime exceeds 64 KiB')
    value = json.loads(raw, object_pairs_hook=_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Finite JSON required')))
    required = {'schema', 'purpose', 'display_name', 'profile', 'python', 'worker', 'ffmpeg', 'ffprobe',
                'caption_projection_profile', 'assets_verified', 'observed_utc', 'limits'}
    if not isinstance(value, dict) or set(value) != required or type(value['schema']) is not int or value['schema'] != 1:
        raise ValueError('Unsupported explicit caption runtime manifest')
    def program(record):
        path, pin = record['path'], record['sha256']
        if not isinstance(path, str) or not Path(path).is_absolute() or path.startswith(('//', '\\\\')):
            raise ValueError('Local absolute caption paths required')
        if not isinstance(pin, str) or len(pin) != 64 or any(c not in '0123456789abcdef' for c in pin):
            raise ValueError('Explicit caption SHA256 required')
        return path, pin
    programs = [program(value[name]) for name in ['profile', 'python', 'worker', 'ffmpeg', 'ffprobe']]
    assets = value['assets_verified']
    if not isinstance(assets, list) or not 1 <= len(assets) <= 128:
        raise ValueError('Bounded caption asset pins required')
    for item in assets:
        program(item)
    title = value['display_name']
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise ValueError('A human caption profile name is required')
    projection = value['caption_projection_profile']
    rules = projection['rules']
    bounds = {'maxChars': (1,512), 'lines': (1,4), 'minDurationUs': (0,10000000),
              'maxDurationUs': (1,60000000), 'gapFrames': (0,60), 'breakPauseUs': (0,10000000)}
    if set(rules) != set(bounds) or any(type(rules[k]) is not int or not lo <= rules[k] <= hi for k,(lo,hi) in bounds.items()):
        raise ValueError('Explicit bounded caption wrapping rules required')
    settings = projection['defaultSettings']
    if rules['minDurationUs'] > rules['maxDurationUs'] or not isinstance(settings,str) or len(settings)>1024 or any(ord(c)<32 for c in settings):
        raise ValueError('Invalid explicit caption projection settings')
    return CaptionRuntime(title,*programs,json.dumps(projection,allow_nan=False),json.dumps(assets,allow_nan=False))
