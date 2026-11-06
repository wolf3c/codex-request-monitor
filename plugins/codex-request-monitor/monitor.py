#!/usr/bin/env python3
import json, os, re, sqlite3, subprocess, sys, time, webbrowser
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
CODEX = Path.home() / '.codex'
LOG_DB = CODEX / 'logs_2.sqlite'
STATE_DB = CODEX / 'state_5.sqlite'
SIZE = re.compile(r'pre_compression_bytes=(\d+) post_compression_bytes=(\d+)')
REASON = re.compile(r'reason="([a-z_]+)"')
TURN_ID = re.compile(r'\bturn\.id="([a-fA-F0-9-]{36})"')
MONITOR_ID = b'codex-request-monitor'

def connect(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    db.execute('PRAGMA query_only=ON')
    return db

def collect(log_db=LOG_DB, state_db=STATE_DB, now=None):
    now = time.time() if now is None else now
    with closing(connect(state_db)) as db:
        index = {id: (name, source, thread_source) for id, name, source, thread_source in db.execute('SELECT id, name, source, thread_source FROM threads')}
    threads, alerts, nonincremental_events = {}, [], []
    pending_http = {}
    retry_requests, retry_websockets, retry_archives = {}, {}, {}
    ws_alert_resets = set()
    with closing(connect(log_db)) as db:
        rows = db.execute('''SELECT id, ts, thread_id, target, feedback_log_body FROM logs
            WHERE ts >= ? AND thread_id IS NOT NULL AND
            (target IN ('codex_http_client::request', 'feedback_tags', 'codex_core::client') OR
             (target = 'codex_app_server::thread_state' AND feedback_log_body LIKE '%rpc.method="thread/archive"%'))
            ORDER BY ts DESC, ts_nanos DESC, id DESC''', (int(now - 86400),))
        for id, ts, thread_id, target, body in rows:
            body = body or ''
            if target == 'codex_app_server::thread_state':
                if ('rpc.method="thread/archive"' in body and
                        'clearing thread listener during thread-state teardown' in body):
                    retry_archives.setdefault(thread_id, {
                        'time': ts, 'request': retry_requests.get(thread_id),
                        'websocket_time': retry_websockets.get(thread_id),
                    })
                continue
            transport = 'WebSocket' if 'transport="responses_websocket"' in body else 'HTTP' if 'transport="responses_http"' in body else None
            size = SIZE.search(body) if target == 'codex_http_client::request' and transport == 'HTTP' and 'api.path="/responses"' in body and 'Compressed request body' in body else None
            nonincremental = target == 'codex_core::client' and transport == 'WebSocket' and 'websocket.warmup=false' in body and 'non-incremental request' in body
            request_observed = size or nonincremental or (
                target == 'feedback_tags' and 'api.path="/responses"' in body and
                (transport == 'HTTP' or (transport == 'WebSocket' and 'websocket.warmup=false' in body))
            )
            # Only explicit generation evidence clears older alerts. Socket reuse alone
            # does not prove that the request carried an incremental input.
            incremental = (
                target == 'codex_core::client' and transport == 'WebSocket' and
                'codex.websocket.continuation' in body and 'mode="incremental"' in body and
                'phase="generation"' in body
            )
            if (request_observed and transport == 'HTTP') or incremental:
                ws_alert_resets.add(thread_id)
            if request_observed:
                if thread_id in retry_archives:
                    retry_archives[thread_id].setdefault('previous_transport', transport)
                else:
                    retry_requests.setdefault(thread_id, {'transport': transport, 'time': ts})
                    if transport == 'WebSocket':
                        retry_websockets.setdefault(thread_id, ts)
            if not transport and not size and not nonincremental:
                continue
            entry = index.get(thread_id)
            name, source, kind = entry if entry else (None, None, None)
            internal = kind in ('subagent', 'guardian_review') or (source or '').startswith('{"subagent"')
            is_chat = bool(entry) and not internal and (source in ('vscode', 'cli') or kind in ('user', 'automation', 'agent_created_thread', 'agent_forked_thread', 'chatgpt_handoff'))
            classification = 'chat' if is_chat else 'internal' if internal else 'unmatched' if not entry else 'unclassified'
            row = threads.setdefault(thread_id, {'id':thread_id,'title':name or ('未命名聊天' if is_chat else '来源未确认'),'group':'chat' if is_chat else 'other','classification':classification,'transport':None,'transport_time':None,'size':None,'latest_request_size':None,'nonincremental':None})
            latest_transport = transport and row['transport'] is None
            if transport and row['transport'] is None:
                row.update(transport=transport, transport_time=ts)
            # HTTP feedback is logged after the attempt. Only pair it with the immediately
            # preceding request record for this thread, and require the same turn and endpoint.
            pending_turn = pending_http.pop(thread_id, None)
            turn = TURN_ID.search(body) if size or (latest_transport and transport == 'HTTP') else None
            if latest_transport and target == 'feedback_tags' and turn and 'api.path="/responses"' in body:
                pending_http[thread_id] = turn.group(1)
            if size:
                event = {'id':id, 'time':ts, 'raw':int(size.group(1)), 'compressed':int(size.group(2))}
                if row['size'] is None:
                    row['size'] = event
                if latest_transport or (pending_turn is not None and turn and pending_turn == turn.group(1)):
                    row['latest_request_size'] = event
                alerts.append({'thread':thread_id, 'title':row['title'], **event})
            if nonincremental:
                match = REASON.search(body)
                event = {'id':id, 'time':ts, 'reason':match.group(1) if match else 'unknown',
                         'cleared':thread_id in ws_alert_resets}
                if row['nonincremental'] is None:
                    row['nonincremental'] = event
                nonincremental_events.append({'thread':thread_id, 'title':row['title'], **event})
    for thread_id, retry in retry_archives.items():
        row = threads.get(thread_id)
        if row and row['group'] == 'chat' and retry.get('previous_transport') == 'HTTP':
            row['retry'] = retry
    return {'now':int(now),'threads':sorted(threads.values(), key=lambda x:max(x['transport_time'] or 0, (x['size'] or {}).get('time',0), (x['nonincremental'] or {}).get('time',0)), reverse=True), 'sizes':alerts, 'nonincremental_events':nonincremental_events, 'max_event_id':max((e['id'] for e in alerts + nonincremental_events), default=0)}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get('Host','')
        if host not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
            self.send_error(403); return
        path = urlparse(self.path).path
        if path == '/':
            data = (ROOT / 'index.html').read_bytes(); kind = 'text/html; charset=utf-8'
        elif path == '/health':
            data = MONITOR_ID; kind = 'text/plain; charset=utf-8'
        elif path == '/api/status':
            try:
                data = json.dumps(collect(), ensure_ascii=False).encode(); kind = 'application/json; charset=utf-8'
            except (sqlite3.Error, OSError) as error:
                self.send_response(503); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'error':type(error).__name__}).encode()); return
        else:
            self.send_error(404); return
        self.send_response(200); self.send_header('Content-Type',kind); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *args): pass

def monitor_is_running(port):
    with closing(HTTPConnection('127.0.0.1', port, timeout=1)) as connection:
        try:
            connection.request('GET', '/health')
        except ConnectionRefusedError:
            return False
        response = connection.getresponse()
        if response.status != 200 or response.read(len(MONITOR_ID) + 1) != MONITOR_ID:
            raise RuntimeError(f'端口 {port} 已被其他服务占用')
        return True

def ensure_monitor(port):
    if not 1 <= port <= 65535:
        raise ValueError('监控端口必须在 1 到 65535 之间')
    if monitor_is_running(port):
        return

    process = subprocess.Popen(
        [sys.executable, str(ROOT / 'monitor.py'), '--serve-only'],
        env={'HOME': str(Path.home()), 'CODEX_MONITOR_PORT': str(port)},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 4
    try:
        while time.monotonic() < deadline:
            if monitor_is_running(port):
                return
            time.sleep(0.05)
        raise RuntimeError(f'监控服务未能在端口 {port} 启动')
    except (OSError, HTTPException, RuntimeError):
        process.terminate()
        process.wait()
        raise

if __name__ == '__main__':
    if sys.argv[1:] == ['--ensure-running']:
        try:
            port = int(os.environ.get('CODEX_MONITOR_PORT', '49857'))
            ensure_monitor(port)
        except (OSError, HTTPException, RuntimeError, ValueError) as error:
            sys.exit(str(error))
        print(f'http://127.0.0.1:{port}/')
        sys.exit(0)

    server = ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('CODEX_MONITOR_PORT', '0'))), Handler)
    url = f'http://127.0.0.1:{server.server_port}/'
    print('Codex 请求监控：' + url, flush=True)
    print('保持此窗口开启；按 Control-C 停止。', flush=True)
    if sys.argv[1:] != ['--serve-only']:
        webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
