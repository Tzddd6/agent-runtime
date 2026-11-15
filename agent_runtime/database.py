"""Private SQLite metadata, bounded telemetry and audit, never provider credentials."""
import json
import os
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager


class RuntimeDatabase:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.root/'runtime.sqlite3'
        if self.path.is_symlink():raise ValueError('数据库不能是符号链接')
        descriptor=os.open(self.path,os.O_RDWR|os.O_CREAT,0o600);os.close(descriptor)
        if os.name=='posix':os.chmod(self.path,0o600)
        with self.connect() as db:
            db.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,conversation TEXT NOT NULL,name TEXT NOT NULL,
                    media TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,width INTEGER,height INTEGER,created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS attachments_owner ON attachments(conversation);
                CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY,job TEXT NOT NULL,created REAL NOT NULL,data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,job TEXT NOT NULL,action TEXT NOT NULL,decision TEXT NOT NULL,created REAL NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=3);db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=3000')
            with db:yield db
        finally:db.close()

    def attachment(self,key,conversation):
        with self.connect() as db:row=db.execute('SELECT * FROM attachments WHERE id=? AND conversation=?',(key,conversation)).fetchone()
        if row is None:raise ValueError('附件不存在或不属于当前会话')
        return dict(row)

    def attachments_for(self,conversation):
        with self.connect() as db:rows=db.execute('SELECT id,name,media,size,width,height FROM attachments WHERE conversation=?',(conversation,)).fetchall()
        return [dict(row) for row in rows]

    def save_attachment(self,row):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            count,size=db.execute('SELECT count(*),coalesce(sum(size),0) FROM attachments').fetchone()
            if count>=100 or size+row['size']>128*1024**2:raise ValueError('附件存储达到100个或128MiB上限，请先归档')
            db.execute('INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,?)',tuple(row[k] for k in
                ('id','conversation','name','media','size','sha256','width','height','created')))

    def observe(self,job,sample):
        data=json.dumps({'reserved':sample.get('reserved'),'budget':sample.get('budget'),
            'slots_used':sample.get('slots_used'),'sequence':sample.get('sample_sequence')},allow_nan=False)
        with self.connect() as db:
            db.execute('INSERT INTO observations(job,created,data) VALUES(?,?,?)',(job,time.time(),data))
            db.execute('DELETE FROM observations WHERE id <= (SELECT coalesce(max(id),0)-10000 FROM observations)')

    def audit(self,job,action,decision):
        # Identifiers only, never arbitrary tool parameters, filenames or output.
        if len(action)>64 or len(decision)>32:raise ValueError('审计字段超过限制')
        with self.connect() as db:
            db.execute('INSERT INTO audit(job,action,decision,created) VALUES(?,?,?,?)',(job,action,decision,time.time()))
            db.execute('DELETE FROM audit WHERE id <= (SELECT coalesce(max(id),0)-10000 FROM audit)')

    def status(self):
        with self.connect() as db:
            return {'engine':'sqlite','attachments':db.execute('SELECT count(*) FROM attachments').fetchone()[0],
                'observations':db.execute('SELECT count(*) FROM observations').fetchone()[0],
                'audit_events':db.execute('SELECT count(*) FROM audit').fetchone()[0]}
