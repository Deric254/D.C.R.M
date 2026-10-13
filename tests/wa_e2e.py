"""Real processes end to end: the server starts the WhatsApp worker, which opens a real (headed) Chrome on a mock WhatsApp page.
Needs a display: on Linux run it as  xvfb-run -a python tests/wa_e2e.py"""
"""Server -> Sender spawns the real worker process -> real (headed) Chrome -> mock WhatsApp Web -> recorded."""
import os, subprocess, sys, tempfile, time
from pathlib import Path
ROOT=str(Path(__file__).resolve().parent.parent); sys.path.insert(0, ROOT+"/tests")
import authkit
import mock_whatsapp as mw
mock=mw.serve(); mw.MODE.update(mode="ok", tick_ms=300)
PORT=8795; BASE=f"http://127.0.0.1:{PORT}"; DATA=tempfile.mkdtemp()
env=dict(os.environ, DERICBI_WA_URL=f"http://127.0.0.1:{mock.server_port}")
S=authkit.Session(BASE)
def call(m,p,b=None):
    return S.call(m,p,b)
srv=subprocess.Popen([sys.executable, ROOT+"/backend/main.py","--port",str(PORT),"--data-dir",DATA],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
try:
    for _ in range(60):
        try: call("GET","/health"); break
        except Exception: time.sleep(.25)
    S.sign_in()
    call("PUT","/settings",{"send_window_enabled":False,"whatsapp_delay_sec":20})
    # link: the worker opens WhatsApp Web, sees the chat list, remembers it
    call("POST","/whatsapp/link"); 
    for _ in range(40):
        time.sleep(1)
        if call("GET","/whatsapp/status")["linked"]: break
    st=call("GET","/whatsapp/status"); print("linked:", st["linked"], st["link_log"][-1:] ); assert st["linked"]
    ids=[call("POST","/leads",{"name":n,"sector":"Pharmacy","town":"Meru","phone":p})["id"] for n,p in (("Meru Chemist","0700000501"),("Embu Chemist","0700000502"),("Wrong Number","0700000999"))]
    c=call("POST","/campaigns",{"name":"E2E","channel":"whatsapp","style":"ready","body":"","launch":True,"filters":{"lead_ids":ids},"followups":[]})
    for _ in range(90):
        time.sleep(1)
        cs=[x for x in call("GET","/campaigns") if x["id"]==c["id"]][0]
        if cs["status"]=="done": break
    print("campaign:", cs["status"], "sent", cs["sent"], "skipped", cs["skipped"]); print("page received:", [(p,t[:40]) for p,t in mw.SENT])
    assert cs["status"]=="done" and cs["sent"]==2 and cs["skipped"]==1 and len(mw.SENT)==2
    lead=call("GET",f"/leads/{ids[0]}"); print("lead:", lead["status"], [(m["channel"],m["status"]) for m in lead["messages"]], "| outreach:", lead["outreach"]["kind"])
    assert lead["status"]=="contacted" and lead["outreach"]["kind"]=="too_soon"
    print("WA E2E OK")
finally:
    srv.terminate(); mock.shutdown()
