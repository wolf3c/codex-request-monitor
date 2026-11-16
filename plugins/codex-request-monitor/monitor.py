#!/usr/bin/env python3
import json, os, re, secrets, sqlite3, subprocess, sys, threading, time, webbrowser
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from network_diagnostics import collect_network as read_network_configuration
from temporary_proxy import inspect_launch, launch as launch_temporary_proxy

ROOT = Path(__file__).resolve().parent
CODEX = Path.home() / '.codex'
LOG_DB = CODEX / 'logs_2.sqlite'
STATE_DB = CODEX / 'state_5.sqlite'
SIZE = re.compile(r'pre_compression_bytes=(\d+) post_compression_bytes=(\d+)')
REASON = re.compile(r'reason="([a-z_]+)"')
TURN_ID = re.compile(r'\bturn\.id="([a-fA-F0-9-]{36})"')
MODEL_ID = re.compile(
    r'(?:^|[\s{,])model=(?:"([A-Za-z0-9][A-Za-z0-9._:/-]{0,127})"'
    r'|([A-Za-z0-9][A-Za-z0-9._:/-]{0,127}))(?=[\s},]|$)'
)
MONITOR_ID = b'codex-request-monitor'
PROXY_MONITOR_ID = b'codex-request-monitor temporary-proxy-v1'

def collect_network():
    return {**read_network_configuration(), 'temporary_proxy': inspect_launch()}

class MonitorServer(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy_action_token = secrets.token_urlsafe(32)
        self.proxy_action_lock = threading.Lock()

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
    pending_incremental = {}
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
                'phase="generation"' in body and 'websocket.warmup=true' not in body
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
            row = threads.setdefault(thread_id, {'id':thread_id,'title':name or ('未命名聊天' if is_chat else '来源未确认'),'title_missing': not name,'group':'chat' if is_chat else 'other','classification':classification,'transport':None,'transport_time':None,'size':None,'latest_request_size':None,'nonincremental':None})
            if request_observed or incremental:
                evidence = True if incremental else False if nonincremental else None
                request_turn = TURN_ID.search(body)
                if 'latest_request_incremental' not in row:
                    row['latest_request_incremental'] = evidence
                    if target == 'feedback_tags' and transport == 'WebSocket' and request_turn:
                        pending_incremental[thread_id] = request_turn.group(1)
                else:
                    # An intervening request ends the match, even within the same turn.
                    pending = pending_incremental.pop(thread_id, None)
                    if pending and request_turn and request_turn.group(1) == pending and evidence is not None:
                        row['latest_request_incremental'] = evidence
            if (request_observed or incremental) and 'latest_request_model' not in row:
                # Keep a missing model on the newest request instead of showing an older one.
                models = {quoted or bare for quoted, bare in MODEL_ID.findall(body)}
                row['latest_request_model'] = models.pop() if len(models) == 1 else None
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
        elif path == '/health/temporary-proxy':
            data = PROXY_MONITOR_ID; kind = 'text/plain; charset=utf-8'
        elif path == '/api/status':
            try:
                data = json.dumps(collect(), ensure_ascii=False).encode(); kind = 'application/json; charset=utf-8'
            except (sqlite3.Error, OSError) as error:
                self.send_response(503); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'error':type(error).__name__}).encode()); return
        elif path == '/api/network':
            try:
                network = collect_network()
                if network.get('temporary_proxy', {}).get('ready'):
                    network['temporary_proxy'] = {**network['temporary_proxy'], 'action_token': self.server.proxy_action_token}
                data = json.dumps(network, ensure_ascii=False).encode(); kind = 'application/json; charset=utf-8'
            except (OSError, ValueError) as error:
                self.send_response(503); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'error':type(error).__name__}).encode()); return
        else:
            self.send_error(404); return
        self.send_response(200); self.send_header('Content-Type',kind); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff'); self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        host = self.headers.get('Host', '')
        if (host not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}') or
                self.headers.get('Origin') != 'http://' + host):
            self.send_error(403); return
        if self.path != '/api/temporary-proxy':
            self.send_error(404); return
        length = self.headers.get('Content-Length', '')
        if (self.headers.get('Content-Type') != 'application/json' or
                not length.isdigit() or not 2 <= int(length) <= 1024):
            self.send_error(400); return
        if not self.server.proxy_action_lock.acquire(blocking=False):
            self.send_error(409); return
        try:
            if not secrets.compare_digest(self.headers.get('X-Codex-Monitor-Action', '').encode(), self.server.proxy_action_token.encode()):
                self.send_error(403); return
            try:
                self.connection.settimeout(3)
                body = json.loads(self.rfile.read(int(length)))
            except (ValueError, UnicodeError, OSError):
                self.send_error(400); return
            if not isinstance(body, dict) or set(body) != {'proxy'} or not isinstance(body['proxy'], str):
                self.send_error(400); return
            self.server.proxy_action_token = secrets.token_urlsafe(32)
            result = launch_temporary_proxy(body['proxy'])
            data = json.dumps(result).encode()
            self.send_response(202 if result['reason'] == 'launch_requested' else 409)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)
        finally:
            self.server.proxy_action_lock.release()

    def log_message(self, *args): pass

def open_temporary_proxy_page():
    port = 49858
    ensure_monitor(port, '/health/temporary-proxy', PROXY_MONITOR_ID)
    subprocess.run(['/usr/bin/open', f'http://127.0.0.1:{port}/#temporaryProxy'],
                   check=True, timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def monitor_is_running(port, health_path='/health', identity=MONITOR_ID):
    with closing(HTTPConnection('127.0.0.1', port, timeout=1)) as connection:
        try:
            connection.request('GET', health_path)
        except ConnectionRefusedError:
            return False
        response = connection.getresponse()
        if response.status != 200 or response.read(len(identity) + 1) != identity:
            raise RuntimeError(f'Port {port} is in use by another service')
        return True

def ensure_monitor(port, health_path='/health', identity=MONITOR_ID):
    if not 1 <= port <= 65535:
        raise ValueError('The monitor port must be between 1 and 65535')
    if monitor_is_running(port, health_path, identity):
        return

    environment = {'HOME': str(Path.home()), 'CODEX_MONITOR_PORT': str(port)}
    if os.environ.get('CODEX_HOME'):
        environment['CODEX_HOME'] = os.environ['CODEX_HOME']
    process = subprocess.Popen(
        [sys.executable, str(ROOT / 'monitor.py'), '--serve-only'],
        env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 4
    try:
        while time.monotonic() < deadline:
            if monitor_is_running(port, health_path, identity):
                return
            time.sleep(0.05)
        raise RuntimeError(f'The monitor did not start on port {port}')
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

    server = MonitorServer(('127.0.0.1', int(os.environ.get('CODEX_MONITOR_PORT', '0'))), Handler)
    url = f'http://127.0.0.1:{server.server_port}/'
    print('Codex Request Monitor: ' + url, flush=True)
    print('Keep this window open. Press Control-C to stop.', flush=True)
    if sys.argv[1:] != ['--serve-only']:
        webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
