import sqlite3, tempfile, time, unittest
from pathlib import Path
from contextlib import closing
from monitor import collect

class MonitorTest(unittest.TestCase):
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
if __name__=='__main__': unittest.main()
