"""Protocol-only child process, never a native GUI acceptance fixture."""
import argparse,json,os,socket,subprocess,sys,time
p=argparse.ArgumentParser();p.add_argument("mode");p.add_argument("--control",type=int);p.add_argument("--data-dir");p.add_argument("--no-recover",action="store_true");p.add_argument("project");a=p.parse_args()
if a.mode=="exit":raise SystemExit(7)
if a.mode=="hang":time.sleep(120);raise SystemExit(0)
if a.mode=="descendant":
    cmd=[sys.executable,__file__,"normal","--control",str(a.control),"--data-dir",a.data_dir,"--no-recover",a.project]
    child=subprocess.Popen(cmd);time.sleep(120);raise SystemExit(0)
s=socket.socket();s.bind(("127.0.0.1",a.control));s.listen(8)
while True:
    conn,_=s.accept()
    with conn:
        stream=conn.makefile("rwb");line=stream.readline(4*1024*1024)
        if not line:continue
        open(os.path.join(a.data_dir,"requests.fixture"),"ab").write(b"request\n")
        req=json.loads(line);method=req["method"]
        if a.mode=="slow":time.sleep(2)
        if method=="ui.inspect":result={"window":[1280,720],"ui":{},"dialog":None}
        elif method=="engine.execute":
            command=req["params"]["command"]
            if command=="clipline.validate":result={"valid":True,"projectPath":a.project+".wrong" if a.mode=="wrong" else a.project,"revision":1,"owners":[]}
            elif command=="clipline.saveSnapshot":
                params=req["params"]["params"]
                assert params["expectedProjectPath"]==a.project and params["expectedRevision"]==1 and params["expectedLinksRevisions"]==[]
                open(params["path"],"xb").write(open(a.project,"rb").read()+b" retained opaque snapshot")
                result={"path":params["path"],"validation":{"valid":True,"revision":1,"projectPath":a.project,"owners":[]}}
            elif command=="evaluation.markDirty":
                open(os.path.join(a.data_dir,"retained-recovery.fixture"),"wb").write(b"unsaved fixture data")
                result={"dirty":True}
            elif command=="project.inspect":result={"fixtureOnly":True}
            else:result={}
        else:result={}
        stream.write((json.dumps({"id":req["id"],"ok":True,"result":result})+"\n").encode());stream.flush()
