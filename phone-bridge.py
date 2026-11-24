#!/usr/bin/env python3
"""Phone bridge: mobile web UI <-> local ACP agent session.

Run:  python3 phone-bridge.py [--port 8790] [--cwd DIR] [--token SECRET]
Open http://<mac-ip>:8790 on your phone (same Wi-Fi).

Drives an Agent Client Protocol agent over stdio JSON-RPC. Defaults to
`devin acp` (Devin CLI), authenticating with the windsurf_api_key from
~/.local/share/devin/credentials.toml.
"""
import argparse, json, os, queue, re, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEVIN = os.path.expanduser("~/.local/bin/devin")
CRED = os.path.expanduser("~/.local/share/devin/credentials.toml")
API_SERVER = "https://server.codeium.com"


def load_key():
    if not os.path.exists(CRED):
        sys.exit("no credentials file at %s" % CRED)
    m = re.search(r'windsurf_api_key\s*=\s*"([^"]+)"', open(CRED).read())
    if not m:
        sys.exit("no windsurf_api_key in credentials.toml")
    return m.group(1)


class Acp:
    def __init__(self, cwd, devin=DEVIN):
        self.cwd = cwd
        self.devin = devin
        self.session_id = None
        self._start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _start(self):
        """(Re)spawn the ACP agent, init and authenticate."""
        errlog = open("/tmp/devin-acp-stderr.log", "ab")
        env = dict(os.environ, WINDSURF_API_KEY=load_key())
        self.p = subprocess.Popen(
            [self.devin, "acp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=errlog, env=env, text=True, bufsize=1)
        self.q = queue.Queue()
        self._id = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()
        self._rpc("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": False},
            "clientInfo": {"name": "phone-bridge", "version": "0.2"}})
        key = load_key()
        self._rpc("authenticate", {
            "methodId": "windsurf-api-key",
            "_meta": {"api_key": key, "api_server_url": API_SERVER}})

    def _watchdog(self):
        """Respawn the agent if it dies; try to reload the active session."""
        while True:
            time.sleep(2)
            if self.p.poll() is None:
                continue
            print("devin acp died (rc=%s); respawning..." % self.p.returncode,
                  file=sys.stderr, flush=True)
            time.sleep(1)
            for attempt in range(10):
                try:
                    sid = self.session_id
                    self._start()
                    if sid:
                        try:
                            self.load_session(sid)
                            print("reloaded session %s" % sid, file=sys.stderr,
                                  flush=True)
                        except Exception as e:
                            self.session_id = None
                            print("session reload failed: %s" % e,
                                  file=sys.stderr, flush=True)
                    break
                except Exception as e:
                    print("respawn attempt %d failed: %s" % (attempt, e),
                          file=sys.stderr, flush=True)
                    time.sleep(3)

    def _reader(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except Exception:
                pass

    def _rpc(self, method, params=None, timeout=120, on_notify=None):
        """Send request; collect notifications via on_notify until reply."""
        with self.lock:
            self._id += 1
            rid = self._id
            self.p.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method,
                 "params": params or {}}) + "\n")
            self.p.stdin.flush()
        import time
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(json.dumps(msg["error"]))
                return msg.get("result", {})
            # agent -> client requests (e.g. permission) get auto-allowed
            if "id" in msg and "method" in msg:
                self._respond(msg)
            elif on_notify and msg.get("method"):
                on_notify(msg)
        raise TimeoutError(method)

    def _respond(self, req):
        result = None
        if req.get("method") == "session/request_permission":
            opts = req.get("params", {}).get("options", [])
            allow = next((o for o in opts if "allow" in str(o.get("kind", "")).lower()), opts[0] if opts else None)
            result = {"outcome": {"outcome": "selected", "optionId": allow["optionId"]}} if allow else {"outcome": {"outcome": "cancelled"}}
        self.p.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": req["id"], "result": result or {}}) + "\n")
        self.p.stdin.flush()

    def new_session(self):
        r = self._rpc("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=60)
        self.session_id = r["sessionId"]
        return self.session_id

    def list_sessions(self):
        r = self._rpc("session/list", {"cwd": self.cwd}, timeout=30)
        return r.get("sessions", [])

    def load_session(self, sid):
        r = self._rpc("session/load", {
            "sessionId": sid, "cwd": self.cwd, "mcpServers": []}, timeout=120)
        self.session_id = sid
        return sid

    def unload(self):
        self.session_id = None

    def prompt(self, text, on_chunk):
        def note(msg):
            u = msg.get("params", {}).get("update", {})
            if u.get("sessionUpdate") == "agent_message_chunk":
                c = u.get("content", {})
                if c.get("type") == "text":
                    on_chunk(c.get("text", ""))
        return self._rpc("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}]},
            timeout=1800, on_notify=note)


PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Devin Bridge</title>
<style>
body{font-family:-apple-system,sans-serif;margin:0;display:flex;flex-direction:column;height:100vh;background:#0d1117;color:#e6edf3}
#log{flex:1;overflow-y:auto;padding:12px}
.msg{margin:6px 0;padding:8px 12px;border-radius:12px;white-space:pre-wrap;word-break:break-word;max-width:85%}
.me{background:#1f6feb;margin-left:auto}
.ai{background:#21262d}
.sys{color:#8b949e;font-size:12px;text-align:center}
form{display:flex;gap:8px;padding:10px;background:#161b22}
input{flex:1;padding:12px;border-radius:8px;border:1px solid #30363d;background:#0d1117;color:#e6edf3;font-size:16px}
button{padding:12px 20px;border:0;border-radius:8px;background:#1f6feb;color:#fff;font-size:16px}
</style>
<div id=log></div>
<form id=f><input id=i placeholder="发消息给 Devin..." autocomplete=off><button>发送</button></form>
<div style="display:flex;gap:8px;margin:0 10px 10px">
<select id=sess style="flex:1;padding:8px;border-radius:8px;border:1px solid #30363d;background:#21262d;color:#8b949e;font-size:13px"></select>
<button id=nw style="padding:8px 12px;border-radius:8px;border:1px solid #30363d;background:#21262d;color:#8b949e">新会话</button>
</div>
<script>
const K=new URLSearchParams(location.search).get('k')||'';
const H={'Content-Type':'application/json','X-Bridge-Token':K};
const log=document.getElementById('log');
function add(cls,txt){const d=document.createElement('div');d.className='msg '+cls;d.textContent=txt;log.appendChild(d);log.scrollTop=log.scrollHeight;return d}
add('sys','已连接本地 Devin agent');
async function send(t){
 add('me',t); add('sys','思考中...');
 const r=await fetch('/api/chat',{method:'POST',headers:H,body:JSON.stringify({text:t})});
 if(!r.ok){let m='';try{m=(await r.json()).error||''}catch(e){}add('sys','发送失败: '+m);return}
 const ai=add('ai',''); let buf='';
 const rd=r.body.getReader(),dec=new TextDecoder();
 while(true){const{done,value}=await rd.read();if(done)break;
  buf+=dec.decode(value,{stream:true});
  const lines=buf.split('\\n');buf=lines.pop();
  for(const l of lines){if(!l.startsWith('data:'))continue;
   try{const d=JSON.parse(l.slice(5));if(d.t==='chunk'){ai.textContent+=d.v;log.scrollTop=log.scrollHeight}
    else if(d.t==='done'){ai.textContent=d.full||ai.textContent;ai.textContent+='\\n[结束]'}
    else if(d.t==='error'){ai.textContent='[错误] '+d.v}}catch(e){}}
 }
}
document.getElementById('f').onsubmit=e=>{e.preventDefault();const i=document.getElementById('i');if(i.value.trim()){send(i.value.trim());i.value=''}};
document.getElementById('nw').onclick=async()=>{await fetch('/api/new',{method:'POST',headers:H});log.innerHTML='';add('sys','已开启新会话');loadSessions()};
const sel=document.getElementById('sess');
async function loadSessions(){
 try{const r=await fetch('/api/sessions',{headers:H});const d=await r.json();
  sel.innerHTML='<option value="">-- 选择历史会话 --</option>';
  for(const s of d.sessions||[]){
   const o=document.createElement('option');o.value=s.sessionId;
   const lock=s._meta&&s._meta.isLocked?' 🔒':'';
   o.textContent=(s.title||s.sessionId).slice(0,30)+' · '+s.sessionId+lock;
   sel.appendChild(o);}
 }catch(e){}
}
sel.onchange=async()=>{if(!sel.value)return;
 const r=await fetch('/api/load',{method:'POST',headers:H,body:JSON.stringify({sessionId:sel.value})});
 const d=await r.json();
 if(d.session){log.innerHTML='';add('sys','已恢复会话 '+d.session+'（历史记录不在此显示，直接发消息即可继续）')}
 else add('sys','恢复失败: '+(d.error||''))};
loadSessions();
</script>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--devin", default=DEVIN, help="path to devin CLI")
    ap.add_argument("--token", default="", help="optional access token (?k=TOKEN)")
    a = ap.parse_args()
    token = a.token

    acp = Acp(a.cwd, devin=a.devin)
    acp.cwd = a.cwd
    acp.new_session()
    busy = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *x): pass

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _authed(self):
            if not token:
                return True
            return self.headers.get("X-Bridge-Token") == token

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                b = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif path == "/api/status":
                if not self._authed():
                    return self._json(401, {"error": "unauthorized"})
                self._json(200, {"session": acp.session_id})
            elif path == "/api/sessions":
                if not self._authed():
                    return self._json(401, {"error": "unauthorized"})
                try:
                    self._json(200, {"sessions": acp.list_sessions()})
                except Exception as e:
                    self._json(500, {"error": str(e)})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                n = int(self.headers.get("Content-Length", 0))
                self.rfile.read(n)
                return self._json(401, {"error": "unauthorized"})
            path = self.path.split("?", 1)[0]
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json(400, {"error": "bad json"})
            if path == "/api/new":
                try:
                    acp.new_session()
                    self._json(200, {"session": acp.session_id})
                except Exception as e:
                    self._json(500, {"error": str(e)})
            elif path == "/api/load":
                sid = body.get("sessionId")
                if not sid:
                    return self._json(400, {"error": "sessionId required"})
                try:
                    acp.load_session(sid)
                    self._json(200, {"session": acp.session_id})
                except Exception as e:
                    acp.unload()
                    self._json(500, {"error": str(e)})
            elif path == "/api/chat":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._json(400, {"error": "empty"})
                if acp.session_id is None:
                    return self._json(409, {"error": "no session loaded - pick one from the list"})
                if acp.p.poll() is not None:
                    return self._json(500, {"error": "agent process died, restart phone-bridge.py"})
                if not busy.acquire(blocking=False):
                    return self._json(409, {"error": "busy"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                full = []
                def chunk(t):
                    full.append(t)
                    self.wfile.write(b"data: " + json.dumps({"t": "chunk", "v": t}).encode() + b"\n")
                    self.wfile.flush()
                try:
                    acp.prompt(text, chunk)
                    self.wfile.write(b"data: " + json.dumps({"t": "done", "full": "".join(full)}).encode() + b"\n")
                except Exception as e:
                    self.wfile.write(b"data: " + json.dumps({"t": "error", "v": str(e)}).encode() + b"\n")
                finally:
                    self.wfile.write(b"data: {\"t\":\"eof\"}\n")
                    self.wfile.flush()
                    busy.release()
            else:
                self._json(404, {"error": "not found"})

    import socket
    ip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ip.connect(("8.8.8.8", 80)); lan = ip.getsockname()[0]
    except Exception:
        lan = "127.0.0.1"
    finally:
        ip.close()
    hint = f"?k={token}" if token else ""
    print(f"Phone bridge on http://{lan}:{a.port}/{hint}  (session {acp.session_id}, cwd {a.cwd})")
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
