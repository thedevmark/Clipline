"""Wait for parent Job Object attachment before starting the native tool."""
import json,subprocess,sys
line=sys.stdin.buffer.readline(65537)
if len(line)>65536:raise SystemExit(125)
try:
    argv=json.loads(line)
    if not isinstance(argv,list) or not argv or any(not isinstance(x,str) or not x for x in argv):raise ValueError()
    result=subprocess.run(argv,stdin=subprocess.DEVNULL)
except (OSError,ValueError):raise SystemExit(125)
raise SystemExit(result.returncode)
