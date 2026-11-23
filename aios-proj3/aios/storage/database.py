"""Private SQLite WAL metadata, bounded telemetry, and security audit store for AIOS Kernel."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List


class RuntimeDatabase:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "aios_runtime.sqlite3"
        if self.path.is_symlink():
            raise ValueError("数据库不能是符号链接")
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS attachments(
                    id TEXT PRIMARY KEY,
                    conversation TEXT NOT NULL,
                    name TEXT NOT NULL,
                    media TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    width INTEGER,
                    height INTEGER,
                    created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attachments_owner ON attachments(conversation);
                CREATE TABLE IF NOT EXISTS attachment_text(
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    note TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations(
                    id INTEGER PRIMARY KEY,
                    job TEXT NOT NULL,
                    created REAL NOT NULL,
                    data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY,
                    job TEXT NOT NULL,
                    action TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    created REAL NOT NULL
                );
                """
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=3000")
            with db:
                yield db
        finally:
            db.close()

    def attachment(self, key: str, conversation: str) -> Dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT a.*, t.text AS extracted_text, t.sha256 AS extracted_sha256, t.note AS extraction_note
                FROM attachments a
                LEFT JOIN attachment_text t ON t.id = a.id
                WHERE a.id = ? AND a.conversation = ?
                """,
                (key, conversation),
            ).fetchone()
        if row is None:
            raise ValueError("附件不存在或不属于当前会话")
        return dict(row)

    def attachments_for(self, conversation: str) -> List[Dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,name,media,size,width,height FROM attachments WHERE conversation=?",
                (conversation,),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_attachment(self, row: Dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count, size = db.execute("SELECT count(*), coalesce(sum(size),0) FROM attachments").fetchone()
            if count >= 100 or size + row["size"] > 128 * 1024**2:
                raise ValueError("附件存储达到100个或128MiB上限，请先归档")
            db.execute(
                "INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,?)",
                tuple(
                    row[k]
                    for k in ("id", "conversation", "name", "media", "size", "sha256", "width", "height", "created")
                ),
            )
            if "extracted_text" in row:
                db.execute(
                    "INSERT INTO attachment_text VALUES(?,?,?,?)",
                    (row["id"], row["extracted_text"], row["extracted_sha256"], row["extraction_note"]),
                )

    def observe(self, job: str, sample: Dict[str, Any]) -> None:
        data = json.dumps(sample, ensure_ascii=False, default=str)
        with self.connect() as db:
            db.execute("INSERT INTO observations(job,created,data) VALUES(?,?,?)", (job, time.time(), data))
            db.execute("DELETE FROM observations WHERE id <= (SELECT coalesce(max(id),0)-5000 FROM observations)")

    def audit(self, job: str, action: str, decision: str) -> None:
        if len(action) > 64 or len(decision) > 32:
            raise ValueError("审计字段超过限制")
        with self.connect() as db:
            db.execute("INSERT INTO audit(job,action,decision,created) VALUES(?,?,?,?)", (job, action, decision, time.time()))
            db.execute("DELETE FROM audit WHERE id <= (SELECT coalesce(max(id),0)-5000 FROM audit)")

    def recent_audits(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT job,action,decision,created FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def status(self) -> Dict[str, Any]:
        with self.connect() as db:
            return {
                "engine": "sqlite_wal",
                "attachments": db.execute("SELECT count(*) FROM attachments").fetchone()[0],
                "observations": db.execute("SELECT count(*) FROM observations").fetchone()[0],
                "audit_events": db.execute("SELECT count(*) FROM audit").fetchone()[0],
            }
