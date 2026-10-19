"""Read only the argv frame; leave the remaining stdin for the requested worker."""
import json
import os
import subprocess
import sys


def main():
    frame = bytearray()
    while len(frame) < 65536:
        byte = os.read(sys.stdin.fileno(), 1)
        if not byte:
            return 125
        frame.extend(byte)
        if byte == b'\n':
            break
    if not frame.endswith(b'\n'):
        return 125
    try:
        argv = json.loads(frame.decode('utf8'))
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(arg, str) or not arg or '\0' in arg for arg in argv)):
            return 125
        child = subprocess.Popen(argv, stdin=sys.stdin.buffer,
                                 stdout=sys.stdout.buffer, stderr=sys.stderr.buffer)
        return child.wait()
    except (OSError, ValueError, UnicodeError, RecursionError):
        return 125


if __name__ == '__main__':
    raise SystemExit(main())
