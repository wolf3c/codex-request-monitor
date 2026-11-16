import sqlite3, tempfile, time, unittest
from pathlib import Path
from contextlib import closing
from monitor import collect

class MonitorTest(unittest.TestCase):
 def test_incremental_evidence_matches_only_the_latest_generation(self):
  with tempfile.TemporaryDirectory() as temp:
   logs, state = Path(temp) / 'logs', Path(temp) / 'state'
   now = int(time.time())
   with closing(sqlite3.connect(state)) as db:
    db.execute('CREATE TABLE threads(id TEXT, name TEXT, source TEXT, thread_source TEXT)')
   turn = ' turn.id="00000000-0000-0000-0000-000000000001"'
   other_turn = ' turn.id="00000000-0000-0000-0000-000000000002"'
   generation = ' api.path="/responses" transport="responses_websocket" websocket.warmup=false'
   full = ' non-incremental request reason="connection_closed"'
   delta = ' codex.websocket.continuation mode="incremental" phase="generation"'
   records = [
    ('full', 'codex_core::client', generation + turn + full),
    ('full', 'feedback_tags', generation + turn),
    ('delta', 'codex_core::client', generation + turn + delta),
    ('delta', 'feedback_tags', generation + turn),
    ('older_same_turn', 'codex_core::client', generation + turn + full),
    ('older_same_turn', 'feedback_tags', generation + turn),
    ('older_same_turn', 'feedback_tags', generation + turn),
    ('other_turn', 'codex_core::client', generation + turn + full),
    ('other_turn', 'feedback_tags', generation + other_turn),
    ('no_turn', 'codex_core::client', generation + full),
    ('no_turn', 'feedback_tags', generation),
    ('direct', 'codex_core::client', generation + turn + full),
    ('direct', 'codex_core::client', generation.replace('warmup=false', 'warmup=true') + turn + delta),
    ('http', 'codex_core::client', generation + turn + full),
    ('http', 'feedback_tags', 'api.path="/responses" transport="responses_http"' + turn),
   ]
   with closing(sqlite3.connect(logs)) as db:
    db.execute('CREATE TABLE logs(id INTEGER, ts INTEGER, ts_nanos INTEGER, thread_id TEXT, target TEXT, feedback_log_body TEXT)')
    db.executemany('INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?)', [
     (i, now, i, thread, target, body)
     for i, (thread, target, body) in enumerate(records, 1)
    ])
    db.commit()
   states = {row['id']: row.get('latest_request_incremental') for row in collect(logs, state, now)['threads']}
   self.assertEqual(states, {
    'full': False, 'delta': True, 'older_same_turn': None, 'other_turn': None,
    'no_turn': None, 'direct': False, 'http': None,
   })

 def test_latest_request_model_excludes_warmups_and_older_requests(self):
  with tempfile.TemporaryDirectory() as temp:
   logs, state = Path(temp) / 'logs', Path(temp) / 'state'
   now = int(time.time())
   with closing(sqlite3.connect(state)) as db:
    db.execute('CREATE TABLE threads(id TEXT, name TEXT, source TEXT, thread_source TEXT)')
   http = 'api.path="/responses" transport="responses_http"'
   websocket = 'api.path="/responses" transport="responses_websocket"'
   records = [
    ('switch', http + ' model="gpt-6.1-sol"'),
    ('switch', websocket + ' websocket.warmup=false model=gpt-6-luna'),
    ('switch', websocket + ' websocket.warmup=true model=gpt-6.1-sol'),
    ('missing', http + ' model="gpt-6.1-sol"'),
    ('missing', http),
    ('quoted', http + ' model="gpt-6.1-sol"'),
    ('ambiguous', http + ' model=gpt-6.1-sol model=gpt-6-luna'),
   ]
   with closing(sqlite3.connect(logs)) as db:
    db.execute('CREATE TABLE logs(id INTEGER, ts INTEGER, ts_nanos INTEGER, thread_id TEXT, target TEXT, feedback_log_body TEXT)')
    db.executemany('INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?)', [
     (i, now, i, thread, 'feedback_tags', body)
     for i, (thread, body) in enumerate(records, 1)
    ])
    db.commit()
   models = {row['id']: row['latest_request_model'] for row in collect(logs, state, now)['threads']}
   self.assertEqual(models, {
    'switch': 'gpt-6-luna', 'missing': None, 'quoted': 'gpt-6.1-sol', 'ambiguous': None,
   })

 def test_known_events_and_separate_times(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t); logs=p/'logs'; state=p/'state'; now=int(time.time())
   with closing(sqlite3.connect(state)) as db:
    db.execute('create table threads(id text,name text,source text,thread_source text)');db.execute('insert into threads values(?,?,?,?)',('t','会话','vscode','user'));db.commit()
   with closing(sqlite3.connect(logs)) as db:
    db.execute('create table logs(id integer,ts integer,ts_nanos integer,thread_id text,target text,feedback_log_body text)')
    db.executemany('insert into logs values(?,?,?,?,?,?)',[(1,now-20,0,'t','codex_http_client::request','transport="responses_http" api.path="/responses" Compressed request body pre_compression_bytes=220 post_compression_bytes=110'),(2,now-10,0,'t','codex_core::client','transport="responses_websocket" websocket.warmup=false non-incremental request reason="connection_closed"'),(3,now-5,0,'t','codex_core::client','transport="responses_websocket" websocket.warmup=true non-incremental request reason="no_previous_request"'),(4,now-1,0,'t','codex_http_client::request','api.path="/other" Compressed request body pre_compression_bytes=999 post_compression_bytes=999')]);db.commit()
   r=collect(logs,state,now); x=r['threads'][0];self.assertEqual(x['group'],'chat');self.assertEqual(x['size']['compressed'],110);self.assertEqual(x['nonincremental']['reason'],'connection_closed');self.assertEqual(len(r['nonincremental_events']),1);self.assertEqual(r['max_event_id'],2);self.assertNotEqual(x['size']['time'],x['nonincremental']['time'])
 def test_separate_unknown_and_internal_keep_alerts(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);logs=p/'logs';state=p/'state';now=int(time.time())
   with closing(sqlite3.connect(state)) as db:
    db.execute('create table threads(id text,name text,source text,thread_source text)');db.executemany('insert into threads values(?,?,?,?)', [('u','用户聊天','vscode','user'),('g','内部任务','vscode','subagent'),('m',None,'vscode',None)]);db.commit()
   with closing(sqlite3.connect(logs)) as db:
    db.execute('create table logs(id integer,ts integer,ts_nanos integer,thread_id text,target text,feedback_log_body text)')
    for i,ident in enumerate(['u','g','m','missing'],1):db.execute('insert into logs values(?,?,?,?,?,?)',(i,now,0,ident,'codex_http_client::request','transport="responses_http" api.path="/responses" Compressed request body pre_compression_bytes=200 post_compression_bytes=100'))
    db.commit()
   r=collect(logs,state,now); rows={x['id']:x for x in r['threads']};self.assertEqual(rows['u']['group'],'chat');self.assertEqual(rows['g']['group'],'other');self.assertEqual(rows['m']['group'],'chat');self.assertEqual(rows['missing']['group'],'other');self.assertEqual(rows['missing']['classification'],'unmatched');self.assertEqual(len(r['sizes']),4)
   self.assertEqual(rows['u']['title'], '用户聊天')
   self.assertFalse(rows['u']['title_missing'])
   self.assertTrue(rows['m']['title_missing'])
   self.assertTrue(rows['missing']['title_missing'])
if __name__=='__main__': unittest.main()
