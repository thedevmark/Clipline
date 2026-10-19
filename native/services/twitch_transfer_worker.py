"""One owned own-channel transfer. No secret leaves this process's memory."""
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request

MAX_BYTES = 2 * 1024**3
MAX_SECONDS = 300
MAX_REDIRECTS = 5
CHUNK = 65536


class TransferFailure(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def transport_url(url):
    try:
        value = urllib.parse.urlsplit(url)
        if (not isinstance(url, str) or len(url) > 32768 or value.scheme != 'https'
                or not value.hostname or value.port not in (None, 443) or value.username or value.password
                or value.fragment or not value.path or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
            raise ValueError()
    except (ValueError, TypeError):
        raise TransferFailure('INVALID_TRANSPORT') from None
    return url


class BoundedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise TransferFailure('REDIRECT_LIMIT')
        # Construct a fresh request: never forward cookies/authentication.
        return urllib.request.Request(transport_url(newurl), headers={'Accept-Encoding': 'identity'})


def transfer(url, path, *, limit, deadline, opener=None, clock=time.monotonic):
    """Basic bounded provider transfer; opener injection is for local doubles."""
    if type(limit) is not int or not 0 < limit <= MAX_BYTES:
        raise TransferFailure('INVALID_LIMIT')
    if clock() >= deadline:
        raise TransferFailure('DEADLINE')
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), BoundedRedirect())
    request = urllib.request.Request(transport_url(url), headers={'Accept-Encoding': 'identity'})
    try:
        with opener.open(request, timeout=min(15, max(.1, deadline-clock()))) as response:
            if getattr(response, 'status', 200) != 200:
                raise TransferFailure('HTTP_STATUS')
            lengths = response.headers.get_all('Content-Length', [])
            if len(lengths) > 1 or (lengths and (not lengths[0].isdigit() or len(lengths[0]) > 20)):
                raise TransferFailure('INVALID_LENGTH')
            length = int(lengths[0]) if lengths else None
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise TransferFailure('ENCODING')
            if lengths and response.headers.get('Transfer-Encoding'):
                raise TransferFailure('INVALID_LENGTH')
            if length is not None and not 0 < length <= limit:
                raise TransferFailure('BYTE_LIMIT')
            total = 0
            with Path(path).open('xb') as target:
                while True:
                    if clock() >= deadline:
                        raise TransferFailure('DEADLINE')
                    block = response.read(min(CHUNK, limit-total+1))
                    if not isinstance(block, bytes):
                        raise TransferFailure('INVALID_BODY')
                    if not block:
                        break
                    total += len(block)
                    if total > limit or length is not None and total > length:
                        raise TransferFailure('BYTE_LIMIT')
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            if not total or length is not None and total != length:
                raise TransferFailure('TRUNCATED')
            if clock() >= deadline:
                raise TransferFailure('DEADLINE')
            return total
    except TransferFailure:
        raise
    except BaseException:
        raise TransferFailure('TRANSFER_FAILED') from None


def run(request):
    if (not isinstance(request, dict) or set(request) != {'schema', 'code_root', 'clip', 'target', 'client_id', 'max_bytes', 'seconds'}
            or type(request['schema']) is not int or request['schema'] != 1
            or type(request['seconds']) not in (int, float) or not 0 < request['seconds'] <= MAX_SECONDS
            or type(request['max_bytes']) is not int or not 0 < request['max_bytes'] <= MAX_BYTES):
        raise TransferFailure('INVALID_REQUEST')
    root = Path(request['code_root']).resolve(strict=True)
    target = Path(request['target'])
    if not root.is_absolute() or not target.is_absolute() or target.name != 'download.part' or target.exists():
        raise TransferFailure('INVALID_REQUEST')
    # Only after the parent has attached the process and released safe stdin.
    sys.path.insert(0, str(root))
    from native.core.twitch_inbox import TwitchClip
    from native.services import twitch_auth, twitch_api
    selected = TwitchClip(**request['clip'])
    if selected.duration_us > 180_000_000:
        raise TransferFailure('DURATION_LIMIT')
    try:
        connection = twitch_auth.validated_intake_connection(expected_broadcaster_id=selected.broadcaster_id,
                                                             bound_client_id=request['client_id'])
    except Exception:
        raise TransferFailure('AUTH_REQUIRED') from None
    try:
        temporary = twitch_api.own_clip_download(selected, connection=connection)
    except twitch_api.TwitchAuthError:
        raise TransferFailure('AUTH_REQUIRED') from None
    except Exception:
        raise TransferFailure('SOURCE_UNAVAILABLE') from None
    count = transfer(temporary.landscape_url, target, limit=request['max_bytes'],
                     deadline=time.monotonic()+request['seconds'])
    return {'event': 'result', 'state': 'completed', 'bytes': count}


def main():
    code = 1
    try:
        line = sys.stdin.buffer.readline(65537)
        if not line or len(line) > 65536:
            raise TransferFailure('INVALID_REQUEST')
        def invalid(_): raise TransferFailure('INVALID_REQUEST')
        value = run(json.loads(line, parse_constant=invalid))
        code = 0
    except TransferFailure as error:
        value = {'event': 'result', 'state': 'failed', 'error_code': error.code}
    except BaseException:
        value = {'event': 'result', 'state': 'failed', 'error_code': 'WORKER_FAILED'}
    try:
        sys.stdout.write(json.dumps(value, separators=(',', ':'))+'\n')
        sys.stdout.flush()
    except BaseException:
        code = 1
    return code


if __name__ == '__main__':
    raise SystemExit(main())
