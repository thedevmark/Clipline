"""Deliberately hostile worker modes for parent-protocol tests; never used for model inference."""
import json,os,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"native/services"))
from caption_worker.evidence import MAX_EVENT,MAX_LOG,make_hypothesis,make_alignment
mode=sys.argv[1]
if mode=="unread": time.sleep(30);raise SystemExit(0)
r=json.loads(sys.stdin.buffer.readline())
def send(kind,data):
    print(json.dumps({"event":kind,"run_id":r["run_id"],"data":data}),flush=True)
if mode=="malformed": print("not JSON",flush=True);raise SystemExit(0)
if mode=="truncated": sys.stdout.write('{"event":');sys.stdout.flush();raise SystemExit(0)
if mode=="flood": sys.stdout.write("x"*(MAX_EVENT+100));sys.stdout.flush();raise SystemExit(0)
if mode=="stderr": sys.stderr.write("x"*(MAX_LOG*2));sys.stderr.flush();raise SystemExit(0)
if mode=="crash": os._exit(7)
if mode=="sleep": time.sleep(30);raise SystemExit(0)
if mode=="phase": print("phase=waiting",file=sys.stderr,flush=True);time.sleep(30);raise SystemExit(0)
if mode=="child":
    child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
    Path(r["test_child_pid_path"]).write_text(str(child.pid),encoding="ascii")
    time.sleep(30);raise SystemExit(0)
if mode=="false_complete": send("terminal",{"state":"complete"});raise SystemExit(0)
segments=[]
if mode in {"reordered","overlap"}: segments=[{"text":"one two","start":0,"end":.009}]
if mode=="missing_gap": segments=[{"text":"one gap two","start":0,"end":.009}]
h=make_hypothesis(r,segments,{"test_fixture":True})
send("hypothesis",h)
if mode=="partial": os._exit(7)
if mode=="cancel_partial": time.sleep(30);raise SystemExit(0)
raw_words=[]
if mode in {"reordered","overlap","missing_gap"}:
    raw_words=[{"word":"one","start":0,"end":.004},{"word":"two","start":.005,"end":.009}]
    if mode=="missing_gap": raw_words.insert(1,{"word":"gap"})
a=make_alignment(r,h,{"word_segments":raw_words},{"test_fixture":True})
if mode=="reordered": a["words"].reverse()
if mode=="overlap": a["words"][1]["timing"].update(start_us=r["range"]["start_us"]+3000,reported_start_us=r["range"]["start_us"]+3000)
if mode=="bad_alignment": a["words"]=[{"word_id":"foreign","text":"foreign","timing":{"status":"missing","start_us":None,"end_us":None}}]
send("alignment",a)
if mode=="missing_terminal": raise SystemExit(0)
send("terminal",{"state":"complete","error_code":None})
if mode=="duplicate": send("terminal",{"state":"complete"})
