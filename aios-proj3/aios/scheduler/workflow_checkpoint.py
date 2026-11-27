"""Transactional workflow checkpoints with bounded delta replay and legacy reads."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import time


def _encode(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str)


class WorkflowCheckpoint:
    snapshot_interval = 128

    def __init__(self, directory, workflow_id):
        self.workflow_id = workflow_id
        self.path = Path(directory) / f'{workflow_id}_checkpoint.sqlite3'
        self.legacy = self.path.with_suffix('.json')

    def _load_legacy(self):
        if self.legacy.exists():
            return json.loads(self.legacy.read_text(encoding='utf-8'))
        return None

    def load(self):
        if not self.path.exists():
            return self._load_legacy()
        # One read transaction sees either side of an atomic compaction.
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN')
            if db.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone() is None:
                # A crash before the first commit can leave an empty database.
                return self._load_legacy()
            row = db.execute('SELECT payload FROM snapshot WHERE id = 1').fetchone()
            if row is None:
                raise ValueError('Workflow checkpoint has no snapshot')
            state = json.loads(row[0])
            for (payload,) in db.execute('SELECT payload FROM deltas ORDER BY id'):
                delta = json.loads(payload)
                for key in ('states', 'results', 'context_refs'):
                    state[key].update(delta[key])
                state['events'].extend(delta['events'])
                state['updated_at'] = delta['updated_at']
            return state

    @contextmanager
    def writer(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA synchronous=FULL')
            yield CheckpointWriter(db, self.workflow_id, self.snapshot_interval)

    def purge(self):
        self.path.unlink(missing_ok=True)
        self.path.with_suffix('.sqlite3-journal').unlink(missing_ok=True)
        self.legacy.unlink(missing_ok=True)


class CheckpointWriter:
    def __init__(self, db, workflow_id, interval):
        self.db = db
        self.workflow_id = workflow_id
        self.interval = interval
        self.events_saved = 0
        self.deltas_saved = 0
        self.plan_signature = None
        self.initialized = False

    def snapshot(self, states, results, refs, events):
        data = _encode({'workflow_id': self.workflow_id, 'plan_signature': self.plan_signature,
                        'updated_at': time.time(), 'states': states, 'results': results,
                        'context_refs': refs, 'events': events})
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if not self.initialized:
                self.db.execute('CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS deltas (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
            self.db.execute('INSERT OR REPLACE INTO snapshot VALUES (1, ?)', (data,))
            self.db.execute('DELETE FROM deltas')
        self.initialized = True
        self.events_saved = len(events)
        self.deltas_saved = 0

    def append(self, states, results, refs, events, changed):
        if self.deltas_saved + 1 >= self.interval:
            self.snapshot(states, results, refs, events)
            return
        data = _encode({'updated_at': time.time(),
                        'states': {key: states[key] for key in changed},
                        'results': {key: results[key] for key in changed if key in results},
                        'context_refs': {key: refs[key] for key in changed if key in refs},
                        'events': events[self.events_saved:]})
        with self.db:
            self.db.execute('INSERT INTO deltas (payload) VALUES (?)', (data,))
        self.events_saved = len(events)
        self.deltas_saved += 1
