#!/usr/bin/env python3
"""Local real-UI preview. Same store/dispatcher, isolated fixtures, no Codex writes."""
import argparse
import json
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN = ROOT / 'plugins' / 'chat-tree'
sys.path.insert(0, str(PLUGIN))
from server import Dispatcher
from store import Store, TreeError


class PreviewHistory:
    def create(self, *args, **kwargs):
        raise RuntimeError('Preview cannot create chats. The operation is saved only in test data.')

    def request(self, *args):
        raise RuntimeError('Chat management is available only in Codex')


def seed(store):
    view = store.view('preview-current')
    if view['roots']:
        return
    root = store.apply({'action':'create','title':'Account dashboard update',
        'description':'Break changes into focused branches while preserving decisions and results.',
        'common_context':'Work incrementally. Review workflows before changing the interface. Preserve existing user data.',
        'cwd':str(ROOT),'items':[
            {'title':'Review workflows','description':'Review common tasks and confusing steps.','context':'Use current support requests as evidence.'},
            {'title':'Build the interface','description':'Navigation, forms and clear states.','context':'Start with desktop, then narrow screens.'},
            {'title':'Verify migration','description':'Verify that settings and history are preserved.','context':'Validate using a copy of the data.'},
            {'title':'Release the update','description':'Readiness checks, release and monitoring.'}]}, {'source':'preview_fixture'}, 'preview-current')
    nested = store.apply({'action':'add','parent_id':root['item_ids'][1],'items':[
        {'title':'Search and navigation','description':'Check long titles and returning to the current branch.'},
        {'title':'Context and results','description':'Preserve decisions and display results.'},
        {'title':'Empty, loading and error states'}]}, {'source':'preview_fixture'}, 'preview-current')
    store.apply({'action':'add','parent_id':nested['item_ids'][0],'items':[
        {'title':'Keyboard navigation in the outline'},
        {'title':'A deliberately long item title that checks how text wraps inside the narrow panel without clipping'}]},
        {'source':'preview_fixture'}, 'preview-current')
    other = store.apply({'action':'create','title':'Billing migration','common_context':'Move invoices to the new provider.',
        'cwd':str(ROOT),'items':[{'title':'Export invoices'},{'title':'Map customer IDs'},{'title':'Switch webhooks'}]},
        {'source':'preview_fixture'}, 'preview-other')
    with store.transaction() as db:
        started = [root['item_ids'][0], root['item_ids'][1], root['item_ids'][2], *nested['item_ids'][:2], other['item_ids'][0]]
        for i, node_id in enumerate(started):
            db.execute('UPDATE nodes SET chat_id=? WHERE id=?',('preview-chat-'+str(i),node_id))
        for node_id in (root['item_ids'][0], other['item_ids'][0]):
            db.execute("UPDATE nodes SET state='done',summary='Reviewed the main workflows. Found two places that need a return to the previous step. Decisions saved for interface work.' WHERE id=?",(node_id,))
        db.execute('UPDATE nodes SET stale=1 WHERE id=?',(root['item_ids'][2],))
    for i in range(len(started)):
        store.hook('preview-chat-'+str(i),'Stop')
    store.hook('preview-chat-4','UserPromptSubmit','preview-turn','Continue')
    store.hook('preview-other','Stop')
    store.propose({'action':'add','parent_id':root['item_ids'][3],'items':[
        {'title':'Prepare release notes','description':'Summarize visible changes for users.'},
        {'title':'Monitor errors for 48 hours'}]}, 'preview-current')


def serve(path, port=0):
    store=Store(path)
    seed(store)
    store.hook('preview-current','Stop')
    dispatcher=Dispatcher(store, PreviewHistory())
    token=secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def respond(self,code,content,mime='application/json'):
            body=content.encode() if isinstance(content,str) else json.dumps(content,ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type',mime+'; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path=='/component':
                return self.respond(200,(PLUGIN/'assets/tree.html').read_text(),'text/html')
            if self.path.split('?')[0]!='/':
                return self.respond(404,{'error':'not_found'})
            page='''<!doctype html><html lang="en"><meta charset="utf-8"><title>Chat Tree Preview</title>
<style>body{margin:0;font:13px system-ui;background:#16181c;color:#eee}.bar{padding:12px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}button{padding:7px 10px;border:1px solid #777;border-radius:6px;color:inherit;background:transparent}iframe{display:block;border:1px solid #777;width:min(1050px,100%);height:850px;margin:0 18px;max-width:calc(100% - 36px);border-radius:10px}</style>
<div class="bar"><span>Preview · changes affect test data only</span><button id="wide">1050 px</button><button id="narrow">390 px</button><button id="dark">Dark</button><button id="light">Light</button><label><input id="keep-mode" type="checkbox"> Keep current mode</label><span id="status" role="status"></span></div>
<iframe id="app" title="Chat Tree" src="/component"></iframe>
<script>const frame=document.getElementById('app');let theme='dark',displayMode='fullscreen';
window.addEventListener('message',async e=>{if(e.source!==frame.contentWindow||e.data?.jsonrpc!=='2.0')return;const{id,method,params}=e.data;if(id===undefined)return;try{let result;if(method==='ui/initialize')result={protocolVersion:'2026-01-26',hostInfo:{name:'preview',version:'0.1.0'},hostCapabilities:{},hostContext:{theme,displayMode}};else if(method==='ui/request-display-mode'){if(!['inline','fullscreen'].includes(params.mode))throw{message:'Unsupported display mode'};if(!document.getElementById('keep-mode').checked)displayMode=params.mode;result={mode:displayMode};document.getElementById('status').textContent='Display mode: '+displayMode;frame.contentWindow.postMessage({jsonrpc:'2.0',method:'ui/notifications/host-context-changed',params:{displayMode}},location.origin);}else if(method==='tools/call'){const r=await fetch('/rpc',{method:'POST',headers:{'Content-Type':'application/json','X-Preview-Token':__TOKEN__,'X-Preview-Chat':new URLSearchParams(location.search).get('chat')||'preview-current'},body:JSON.stringify({jsonrpc:'2.0',id,method,params})});const v=await r.json();if(v.error)throw v.error;result=v.result;}else if(method==='ui/message'){document.getElementById('status').textContent='Agent request saved. Execution is available in Codex.';throw{message:'Preview cannot message agents. The operation is saved only in test data.'};}else if(method==='ui/open-link'){document.getElementById('status').textContent='Chat navigation is available in Codex';result={preview:true};}else result={};frame.contentWindow.postMessage({jsonrpc:'2.0',id,result},location.origin);}catch(error){frame.contentWindow.postMessage({jsonrpc:'2.0',id,error:{code:-32000,message:error.message||String(error)}},location.origin);}});
document.getElementById('wide').onclick=()=>frame.style.width='min(1050px,100%)';document.getElementById('narrow').onclick=()=>frame.style.width='390px';function setTheme(value){theme=value;frame.contentWindow.postMessage({jsonrpc:'2.0',method:'ui/notifications/host-context-changed',params:{theme}},location.origin)}document.getElementById('dark').onclick=()=>setTheme('dark');document.getElementById('light').onclick=()=>setTheme('light');</script></html>'''.replace('__TOKEN__',json.dumps(token))
            self.respond(200,page,'text/html')

        def do_POST(self):
            if self.path!='/rpc' or self.headers.get('X-Preview-Token')!=token:
                return self.respond(403,{'error':'denied'})
            if self.headers.get('Origin') not in (None,origin):
                return self.respond(403,{'error':'origin'})
            length=int(self.headers.get('Content-Length','0'))
            if length>262144:
                return self.respond(413,{'error':'too_large'})
            try:
                req=json.loads(self.rfile.read(length))
                chat=self.headers.get('X-Preview-Chat') or 'preview-current'
                req.setdefault('params',{})['_meta']={'threadId':chat[:80]}
                result=dispatcher.handle(req['method'],req['params'])
                self.respond(200,{'jsonrpc':'2.0','id':req['id'],'result':result})
            except (ValueError,KeyError,TreeError) as error:
                self.respond(400,{'error':{'message':str(error)}})

    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    origin='http://127.0.0.1:'+str(server.server_port)
    print(origin+'/',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',default=str(ROOT/'preview.sqlite3'))
    parser.add_argument('--port',type=int,default=0)
    args=parser.parse_args()
    serve(Path(args.db),args.port)
