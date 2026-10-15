"""Durable review state. Redis is a delivery queue, never the history database."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator
from uuid import uuid4

TERMINAL = {"completed", "partial", "failed", "cancelled"}
JSON_FIELDS = {"files", "artifacts", "warnings", "documents", "review_options", "review_requirements"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            with db:
                yield db
        finally:
            db.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS owners (
                    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
                    idempotency_key TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'queued', progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 1, issue_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT, expires_at TEXT,
                    error TEXT, files TEXT NOT NULL, artifacts TEXT NOT NULL DEFAULT '[]',
                    warnings TEXT NOT NULL DEFAULT '[]', documents TEXT NOT NULL DEFAULT '[]',
                    cancel_requested INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 1,
                    rq_job_id TEXT, UNIQUE(owner_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS reviews_owner_created ON reviews(owner_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS issues (
                    review_id TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
                    id TEXT NOT NULL, ordinal INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(review_id, id)
                );
            """)
            # Initialization runs in both web and worker. Serialize migrations
            # and never reset the version of an existing database.
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("Database schema is newer than this application")
            columns = {row[1] for row in db.execute("PRAGMA table_info(reviews)")}
            if "review_options" not in columns:
                db.execute("""ALTER TABLE reviews ADD COLUMN review_options TEXT NOT NULL
                              DEFAULT '{"custom_points":[],"review_notes":""}'""")
            if "review_requirements" not in columns:
                db.execute("ALTER TABLE reviews ADD COLUMN review_requirements TEXT NOT NULL DEFAULT '{}'")
            db.execute("PRAGMA user_version=2")

    def owner(self, token: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT id FROM owners WHERE token_hash=?", (token_hash(token),)).fetchone()
            return row[0] if row else None

    def add_owner(self, token: str) -> str:
        identifier = uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO owners VALUES (?, ?, ?)", (identifier, token_hash(token), now()))
        return identifier

    @staticmethod
    def decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in JSON_FIELDS:
            result[key] = json.loads(result[key])
        if not result["review_requirements"]:
            from .requirements import ReviewOptions, initial_requirements_state
            result["review_requirements"] = initial_requirements_state(
                ReviewOptions.model_validate(result["review_options"])).model_dump(mode="json")
        for file in result["files"]:
            file.pop("path", None)
        return result

    def get(self, review_id: str, owner_id: str | None = None, *, private: bool = False) -> dict | None:
        with self.connect() as db:
            sql = "SELECT * FROM reviews WHERE id=?"
            args = [review_id]
            if owner_id is not None:
                sql += " AND owner_id=?"
                args.append(owner_id)
            row = db.execute(sql, args).fetchone()
            if not row:
                return None
            if private:
                data = dict(row)
                for key in JSON_FIELDS:
                    data[key] = json.loads(data[key])
                return data
            data = self.decode(row)
            data["documents"] = [{"id": doc["id"], "filename": doc["filename"], "format": doc["format"],
                                  "block_count": len(doc.get("blocks", [])), "warnings": doc.get("warnings", [])}
                                 for doc in data["documents"]]
            data["cancel_requested"] = bool(data["cancel_requested"])
            for key in ("owner_id", "idempotency_key", "rq_job_id"):
                data.pop(key, None)
            for artifact in data["artifacts"]:
                artifact.pop("path", None)
            return data

    def find_key(self, owner_id: str, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT id FROM reviews WHERE owner_id=? AND idempotency_key=?", (owner_id, key)).fetchone()
        return self.get(row[0], owner_id) if row else None

    def create(self, identifier: str, owner: str, key: str, files: list[dict], review_options: dict | None = None) -> bool:
        from .requirements import ReviewOptions, initial_requirements_state
        options = ReviewOptions.model_validate(review_options or {})
        stamp = now()
        with self.connect() as db:
            try:
                db.execute("""INSERT INTO reviews
                    (id,owner_id,idempotency_key,title,status,created_at,updated_at,files,rq_job_id,review_options,review_requirements)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (identifier, owner, key, files[0]["filename"], "queued", stamp, stamp,
                     json.dumps(files, ensure_ascii=False), f"{identifier}-1",
                     options.model_dump_json(), initial_requirements_state(options).model_dump_json()))
            except sqlite3.IntegrityError:
                return False
        return True

    def update(self, identifier: str, **fields: Any) -> None:
        allowed = {"status", "stage", "progress_current", "progress_total", "issue_count", "finished_at",
                   "expires_at", "error", "files", "artifacts", "warnings", "documents", "cancel_requested",
                   "attempt", "rq_job_id", "review_options", "review_requirements"}
        if not fields.keys() <= allowed:
            raise ValueError("Invalid review update")
        fields["updated_at"] = now()
        values = [json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS else v for k, v in fields.items()]
        with self.connect() as db:
            db.execute("UPDATE reviews SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?", [*values, identifier])

    def finish(self, identifier: str, status: str, retention_days: int, **fields: Any) -> None:
        if status not in TERMINAL:
            raise ValueError("Invalid terminal status")
        ended = datetime.now(timezone.utc)
        self.update(identifier, status=status, stage=status, finished_at=ended.isoformat(),
                    expires_at=(ended + timedelta(days=retention_days)).isoformat(), **fields)

    def list(self, owner: str, page: int, page_size: int) -> dict:
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM reviews WHERE owner_id=?", (owner,)).fetchone()[0]
            ids = db.execute("SELECT id FROM reviews WHERE owner_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                             (owner, page_size, (page - 1) * page_size)).fetchall()
        items = [item for row in ids if (item := self.get(row[0], owner)) is not None]
        return {"items": items, "total": count, "page": page, "page_size": page_size}

    def save_issues(self, identifier: str, issues: list[dict]) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM issues WHERE review_id=?", (identifier,))
            db.executemany("INSERT INTO issues VALUES (?,?,?,?)",
                           [(identifier, issue["id"], i, json.dumps(issue, ensure_ascii=False)) for i, issue in enumerate(issues)])
            db.execute("UPDATE reviews SET issue_count=?,updated_at=? WHERE id=?", (len(issues), now(), identifier))

    def issues(self, identifier: str, page: int = 1, page_size: int = 50) -> dict:
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM issues WHERE review_id=?", (identifier,)).fetchone()[0]
            rows = db.execute("SELECT payload FROM issues WHERE review_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
                              (identifier, page_size, (page - 1) * page_size)).fetchall()
        return {"items": [json.loads(row[0]) for row in rows], "total": total, "page": page, "page_size": page_size}

    def active(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id FROM reviews WHERE status IN ('queued','running')").fetchall()
        return [item for row in rows if (item := self.get(row[0], private=True)) is not None]

    def expired(self) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT id FROM reviews WHERE expires_at IS NOT NULL AND expires_at<=? AND status NOT IN ('queued','running')", (now(),))]

    def delete(self, identifier: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM reviews WHERE id=?", (identifier,))

    def delete_terminal(self, identifier: str, remove_files: Callable[[], None], *, expired_only: bool = False) -> bool:
        """Serialize deletion with retry's state transition, including filesystem work."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,expires_at FROM reviews WHERE id=?", (identifier,)).fetchone()
            if not row or row["status"] not in TERMINAL:
                return False
            if expired_only and (not row["expires_at"] or row["expires_at"] > now()):
                return False
            remove_files()
            db.execute("DELETE FROM reviews WHERE id=?", (identifier,))
            return True

    def finish_current(self, identifier: str, job_id: str, retention_days: int,
                       *, status: str | None = None, error: str | None = None,
                       expected_status: str | None = None) -> bool:
        """An old queue snapshot must never overwrite a newly retried attempt."""
        ended = datetime.now(timezone.utc)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,cancel_requested,rq_job_id FROM reviews WHERE id=?", (identifier,)).fetchone()
            if not row or row["status"] not in {"queued", "running"} or row["rq_job_id"] != job_id:
                return False
            if expected_status is not None and row["status"] != expected_status:
                return False
            final_status = status or ("cancelled" if row["cancel_requested"] else "failed")
            db.execute("""UPDATE reviews SET status=?,stage=?,finished_at=?,expires_at=?,updated_at=?,error=? WHERE id=?""",
                       (final_status, final_status, ended.isoformat(), (ended + timedelta(days=retention_days)).isoformat(),
                        ended.isoformat(), error if final_status != "cancelled" else None, identifier))
            return True
