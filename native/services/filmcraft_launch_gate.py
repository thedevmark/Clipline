"""Wait for parent Job Object attachment before launching the native process."""
import json, os, subprocess, sys
if sys.stdin.buffer.readline(32) != b"start\n":
    raise SystemExit(71)
with open(sys.argv[1], encoding="utf8") as stream:
    request = json.load(stream)
child = subprocess.Popen(request["argv"], stdin=subprocess.DEVNULL,
                         stdout=sys.stdout, stderr=sys.stderr,
                         creationflags=subprocess.CREATE_NO_WINDOW)
# The private file is single-writer. A partial write is never a ready signal.
target = request["child_receipt"]
with open(target + ".pending", "x", encoding="utf8") as stream:
    json.dump({"pid": child.pid}, stream); stream.flush(); os.fsync(stream.fileno())
os.replace(target + ".pending", target)
raise SystemExit(child.wait())
