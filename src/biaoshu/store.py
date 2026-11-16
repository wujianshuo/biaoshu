"""Tenant-scoped transactions and append-only project revisions."""

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


class ConflictError(ValueError):
    """The supplied revision or lifecycle transition no longer applies."""


class NotFoundError(ValueError):
    """No such record exists inside the caller's scope."""


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def json_text(data) -> str:
    return json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _project(row) -> dict | None:
    if row is None:
        return None
    value = dict(row)
    value["profile"] = json.loads(value.pop("profile_json"))
    return value


def _record(row) -> dict | None:
    if row is None:
        return None
    return {
        "project_id": row["project_id"], "kind": row["kind"], "key": row["record_key"],
        "revision": row["revision"], "data": json.loads(row["payload_json"]),
        "sha256": row["payload_sha256"], "created_at": row["created_at"], "created_by": row["created_by"],
    }


class Store:
    def __init__(self, dsn: str, *, schema_dir: Path | None = None):
        self.dsn = dsn
        self.sqlite = dsn.startswith("sqlite:///")
        if not self.sqlite and not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL 需以 postgresql:// 或 sqlite:/// 开头。")
        self.schema_dir = schema_dir or Path(__file__).resolve().parents[2] / "db"
        if self.sqlite:
            self.database_path = Path(dsn.removeprefix("sqlite:///"))
            if str(self.database_path) in {".", ":memory:"}:
                raise ValueError("SQLite 冒烟测试请使用一个实际文件路径。")
            self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def transaction(self, *, write=False):
        if self.sqlite:
            connection = sqlite3.connect(self.database_path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
        else:
            import psycopg
            from psycopg.rows import dict_row

            connection = psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5, prepare_threshold=None)
        try:
            if self.sqlite:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def execute(self, conn, statement: str, params=()):
        return conn.execute(statement if self.sqlite else statement.replace("?", "%s"), params)

    @property
    def for_update(self) -> str:
        return "" if self.sqlite else " FOR UPDATE"

    def init(self) -> None:
        schema = "\n".join((self.schema_dir / name).read_text(encoding="utf-8")
                           for name in ("001_schema.sql", "002_seed.sql"))
        if self.sqlite:
            connection = sqlite3.connect(self.database_path, timeout=10)
            try:
                connection.executescript("PRAGMA foreign_keys = ON; BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
            finally:
                connection.close()
        else:
            with self.transaction(write=True) as conn:
                conn.execute("SELECT pg_advisory_xact_lock(116778172)")
                conn.execute(schema, prepare=False)

    def add_audit(self, conn, tenant, actor, event_type, target_key, *, project_id=None, metadata=None):
        self.execute(conn, """
            INSERT INTO audit_events
              (event_id, tenant_id, project_id, actor, event_type, target_key, metadata_json, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(uuid4()), tenant, project_id, actor, event_type, target_key, json_text(metadata or {}), timestamp()))

    def audit_event(self, tenant, actor, event_type, target_key, *, project_id=None, metadata=None):
        with self.transaction(write=True) as conn:
            if project_id:
                self.require_project(conn, tenant, project_id)
            self.add_audit(conn, tenant, actor, event_type, target_key, project_id=project_id, metadata=metadata)

    def require_project(self, conn, tenant, project_id, *, lock=False):
        query = "SELECT * FROM projects WHERE tenant_id = ? AND project_id = ?"
        row = self.execute(conn, query + (self.for_update if lock else ""), (tenant, project_id)).fetchone()
        if row is None:
            raise NotFoundError("当前账户下找不到此项目。")
        return row

    def create_project(self, tenant, name, *, actor, profile=None, project_id=None) -> dict:
        label = name.strip()
        if not label or len(label) > 300:
            raise ValueError("项目名称需为 1 到 300 字。")
        if profile is not None and not isinstance(profile, dict):
            raise ValueError("项目属性需为 JSON 对象。")
        project_id = project_id or str(uuid4())
        profile_json = json_text(profile if profile is not None else {})
        with self.transaction(write=True) as conn:
            result = self.execute(conn, """
                INSERT INTO projects (tenant_id, project_id, name, profile_json, created_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (tenant_id, project_id) DO NOTHING
            """, (tenant, project_id, label, profile_json, timestamp(), actor))
            row = self.require_project(conn, tenant, project_id)
            if result.rowcount:
                self.add_audit(conn, tenant, actor, "project.created", project_id, project_id=project_id)
            elif row["name"] != label or row["profile_json"] != profile_json:
                raise ConflictError("该项目编号已用于不同的项目资料。")
            return _project(row)

    def get_project(self, tenant, project_id) -> dict | None:
        with self.transaction() as conn:
            row = self.execute(conn, "SELECT * FROM projects WHERE tenant_id = ? AND project_id = ?",
                               (tenant, project_id)).fetchone()
            return _project(row)

    def list_projects(self, tenant, *, limit=200) -> list[dict]:
        with self.transaction() as conn:
            rows = self.execute(conn, "SELECT * FROM projects WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
                                (tenant, min(200, max(1, limit)))).fetchall()
            return [_project(row) for row in rows]

    def _fetch_record(self, conn, tenant, project_id, kind, key, revision=None):
        query = """SELECT * FROM project_records
                   WHERE tenant_id = ? AND project_id = ? AND kind = ? AND record_key = ?"""
        params = (tenant, project_id, kind, key)
        if revision is not None:
            return self.execute(conn, query + " AND revision = ?", (*params, revision)).fetchone()
        return self.execute(conn, query + " ORDER BY revision DESC LIMIT 1", params).fetchone()

    def get_record(self, tenant, project_id, kind, key, *, revision=None) -> dict | None:
        with self.transaction() as conn:
            return _record(self._fetch_record(conn, tenant, project_id, kind, key, revision))

    def list_records(self, tenant, project_id, kind) -> list[dict]:
        with self.transaction() as conn:
            rows = self.execute(conn, """
                SELECT * FROM (
                    SELECT *, row_number() OVER (PARTITION BY record_key ORDER BY revision DESC) AS row_rank
                    FROM project_records WHERE tenant_id = ? AND project_id = ? AND kind = ?
                ) AS latest WHERE row_rank = 1 ORDER BY record_key
            """, (tenant, project_id, kind)).fetchall()
            return [_record(row) for row in rows]

    def list_revisions(self, tenant, project_id, kind, key) -> list[dict]:
        with self.transaction() as conn:
            rows = self.execute(conn, """SELECT * FROM project_records
                WHERE tenant_id = ? AND project_id = ? AND kind = ? AND record_key = ? ORDER BY revision
            """, (tenant, project_id, kind, key)).fetchall()
            return [_record(row) for row in rows]

    def put_record(self, tenant, project_id, kind, key, payload, *, actor, expected_revision=None) -> dict:
        entry = {"kind": kind, "key": key, "data": payload, "expected_revision": expected_revision}
        return self.put_records(tenant, project_id, [entry], actor=actor)[0]

    def put_records(self, tenant, project_id, entries, *, actor, preconditions=()) -> list[dict]:
        """Persist one WF step; an upstream revision mismatch aborts the whole step."""
        with self.transaction(write=True) as conn:
            self.require_project(conn, tenant, project_id, lock=True)
            for premise in preconditions:
                row = self._fetch_record(conn, tenant, project_id, premise["kind"], premise["key"])
                active = row["revision"] if row else 0
                if active != premise["revision"]:
                    raise ConflictError("上游资料已经更新，请重新加载后继续。")

            results = []
            for entry in entries:
                kind, key, payload = entry["kind"], entry["key"], entry["data"]
                if not isinstance(kind, str) or not isinstance(key, str) or not kind.strip() or not key.strip():
                    raise ValueError("记录类型及编号不能为空。")
                if not isinstance(payload, dict):
                    raise ValueError("项目记录需为 JSON 对象。")
                saved = self._fetch_record(conn, tenant, project_id, kind, key)
                current = saved["revision"] if saved else 0
                expected = entry.get("expected_revision")
                if expected is not None and expected != current:
                    raise ConflictError("该记录版本已变化，请重新加载后提交。")
                payload_json = json_text(payload)
                content_hash = sha256(payload_json.encode()).hexdigest()
                if saved and content_hash == saved["payload_sha256"]:
                    results.append(_record(saved))
                    continue
                self.execute(conn, """
                    INSERT INTO project_records (tenant_id, project_id, kind, record_key, revision,
                        payload_json, payload_sha256, created_at, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (tenant, project_id, kind, key, current + 1, payload_json, content_hash, timestamp(), actor))
                self.add_audit(conn, tenant, actor, "record.saved", key, project_id=project_id,
                               metadata={"kind": kind, "revision": current + 1, "sha256": content_hash})
                results.append(_record(self._fetch_record(conn, tenant, project_id, kind, key, current + 1)))
            return results
