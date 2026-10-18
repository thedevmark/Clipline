import json,os,re,subprocess,sys,time,wave
from pathlib import Path
config=json.loads(Path(__file__).with_name("mode.json").read_text())
mode=config["mode"]
if mode=="blocked":time.sleep(3600)
argv=json.loads(sys.stdin.buffer.readline())
if "-version" in argv:
    print("test fake FFmpeg boundary tool; not a real decoder");raise SystemExit(0)
if "-show_streams" in argv:
    if mode=="malformed":print("[");raise SystemExit(0)
    if mode=="stdout_flood":sys.stdout.write("x"*(5*1024**2));sys.stdout.flush();time.sleep(20)
    if mode=="stderr_flood":sys.stderr.write("x"*200000);sys.stderr.flush();time.sleep(20)
    stream={"index":0,"codec_type":"audio","channels":2,"sample_rate":"16000","start_time":"1" if mode=="offset" else "0","duration":"4"}
    if mode in ("unknown_offset","hostile_shift","valid_priming"):stream.pop("start_time")
    print(json.dumps({"streams":[] if mode=="missing" else [stream],"format":{"duration":"4"}}));raise SystemExit(0)
if "-show_packets" in argv:
    packets=[{"pts_time":"0","duration_time":"4"}] if mode!="gap" else [{"pts_time":"0","duration_time":"1"},{"pts_time":"2","duration_time":"2"}]
    if mode in ("hostile_shift","valid_priming"):packets=[{"pts_time":"-1","duration_time":"1"},{"pts_time":"0","duration_time":"4"}]
    if mode=="unknown_offset":packets=[{"pts_time":"1","duration_time":"3"}]
    print(json.dumps({"packets":packets}));raise SystemExit(0)
output=Path(argv[-1]);output.write_bytes(b"partial")
if mode!="missing_decoded_clock" and ("ashowinfo" in argv[argv.index("-af")+1] or mode in ("hostile_shift","valid_priming")):
    pts=-16000 if mode=="hostile_shift" else 0
    for stage in ("origin","clock"):
        if mode=="subsample_origin" and stage=="origin":
            print("[ashowinfo@clipline_origin @ fake] n:0 pts:1 pts_time:0.00001 rate:16000 nb_samples:64000",file=sys.stderr,flush=True);continue
        if mode=="decoded_gap":
            for n,position in ((0,0),(1,33000)):
                print("[ashowinfo@clipline_"+stage+" @ fake] n:"+str(n)+" pts:"+str(position)+" pts_time:"+str(position/16000)+" rate:16000 nb_samples:32000",file=sys.stderr,flush=True)
            continue
        print("[ashowinfo@clipline_"+stage+" @ fake] n:0 pts:"+str(pts)+" pts_time:"+str(pts/16000)+" rate:16000 nb_samples:64000",file=sys.stderr,flush=True)
if mode in ("hang","descendant"):
    if mode=="descendant":
        child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(3600)"])
        pid_path=Path(__file__).with_name("descendant.pid")
        pending_pid=pid_path.with_name("descendant.pid.pending")
        pending_pid.write_text(str(child.pid))
        os.replace(pending_pid,pid_path)
    print("extraction started",flush=True)
    time.sleep(3600)
if mode=="crash":raise SystemExit(7)
filters=argv[argv.index("-af")+1]
first=int(re.search(r"start_sample=(\d+)",filters).group(1));last=int(re.search(r"end_sample=(\d+)",filters).group(1))
with wave.open(str(output),"wb") as w:
    w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000)
    w.writeframes(b"\0\0"*(last-first-(1 if mode=="short" else 0)))
if mode=="changed":
    source=Path(argv[argv.index("-i")+1]);source.write_bytes(source.read_bytes()+b"changed")
