"""博物馆藏品来源与返还审查系统。"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "provenance.db"
CLAIM_TRANSITIONS = {
    "submitted": {"under_review"},
    "under_review": {"negotiating", "resolved_return", "rejected"},
    "negotiating": {"resolved_return", "rejected"},
    "resolved_return": set(),
    "rejected": set(),
}
# 来源状态：active 有效、disputed 存疑、withdrawn 已撤回（终态）
SOURCE_STATUSES = {"active", "disputed", "withdrawn"}
# 来源状态流转：撤回为终态；存疑可恢复为有效或升级为撤回
SOURCE_TRANSITIONS = {
    "active": {"disputed", "withdrawn"},
    "disputed": {"active", "withdrawn"},
    "withdrawn": set(),
}
# 返还裁定挂起后的复核决定
RESUME_DECISIONS = {"reopen", "uphold"}


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users(
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public'))
);
CREATE TABLE IF NOT EXISTS sources(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    source_type TEXT NOT NULL, reference TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','disputed','withdrawn')),
    internal_note TEXT NOT NULL DEFAULT '',
    status_changed_by TEXT REFERENCES users(id),
    status_changed_at TEXT,
    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
    UNIQUE(name,reference)
);
CREATE TABLE IF NOT EXISTS objects(
    id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_no TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL, object_type TEXT NOT NULL, current_holder TEXT NOT NULL,
    public_summary TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES objects(id),
    event_type TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT,
    place TEXT NOT NULL, description TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id),
    source_confirmed INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','void')),
    void_reason TEXT, voided_by TEXT REFERENCES users(id), voided_at TEXT,
    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES objects(id),
    event_id INTEGER REFERENCES events(id), filename TEXT NOT NULL,
    sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
    uploaded_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES objects(id),
    claimant_id TEXT NOT NULL REFERENCES users(id),
    claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK(status IN ('submitted','under_review','negotiating','resolved_return','rejected','suspended')),
    suspended_reason TEXT, suspended_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_reviews(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    reviewer_id TEXT NOT NULL REFERENCES users(id),
    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
    note TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS object_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES objects(id),
    version INTEGER NOT NULL, snapshot TEXT NOT NULL,
    changed_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
    UNIQUE(object_id,version)
);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER REFERENCES objects(id),
    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
    detail TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_status_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
    note TEXT NOT NULL, changed_by TEXT NOT NULL REFERENCES users(id),
    changed_at TEXT NOT NULL
);
"""


class ProvenanceStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self):
        with self._lock:
            conn = self.connect()
            try:
                conn.executescript(SCHEMA_SQL)
                self._migrate(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _columns(self, conn, table):
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _migrate(self, conn):
        """为旧库补齐来源撤回/存疑所需的列与表结构。"""
        source_cols = [
            ("status", "TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disputed','withdrawn'))"),
            ("internal_note", "TEXT NOT NULL DEFAULT ''"),
            ("status_changed_by", "TEXT REFERENCES users(id)"),
            ("status_changed_at", "TEXT"),
        ]
        for col, ddl in source_cols:
            if col not in self._columns(conn, "sources"):
                conn.execute(f"ALTER TABLE sources ADD COLUMN {col} {ddl}")
        event_cols = [
            ("source_confirmed", "INTEGER NOT NULL DEFAULT 1"),
            ("status", "TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','void'))"),
            ("void_reason", "TEXT"),
            ("voided_by", "TEXT REFERENCES users(id)"),
            ("voided_at", "TEXT"),
        ]
        for col, ddl in event_cols:
            if col not in self._columns(conn, "events"):
                conn.execute(f"ALTER TABLE events ADD COLUMN {col} {ddl}")
        # claims 旧表的 CHECK 不含 suspended，需要重建表
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='claims'").fetchone()
        if sql and "suspended" not in sql[0]:
            conn.commit()
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript(
                """
                CREATE TABLE claims_new(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    claimant_id TEXT NOT NULL REFERENCES users(id),
                    claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK(status IN ('submitted','under_review','negotiating','resolved_return','rejected','suspended')),
                    suspended_reason TEXT, suspended_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO claims_new(id,object_id,claimant_id,claimed_by,desired_outcome,status,created_at,updated_at)
                SELECT id,object_id,claimant_id,claimed_by,desired_outcome,status,created_at,updated_at FROM claims;
                DROP TABLE claims;
                ALTER TABLE claims_new RENAME TO claims;
                """
            )
            conn.execute("PRAGMA foreign_keys=ON")

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("staff", "藏品研究员", "staff"),
                    ("reviewer1", "返还审查员", "reviewer"),
                    ("reviewer2", "复核审查员", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("public", "公众访客", "public"),
                ],
            )

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _object(self, conn, object_id):
        row = conn.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if not row:
            raise BusinessError("藏品不存在", 404, "not_found")
        return row

    def _audit(self, conn, object_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(object_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (object_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _snapshot(self, conn, object_id, actor):
        row = self._object(conn, object_id)
        snapshot = {
            "object": dict(row),
            "events": [dict(x) for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
            "claims": [dict(x) for x in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
        }
        conn.execute(
            "INSERT INTO object_versions(object_id,version,snapshot,changed_by,created_at) VALUES(?,?,?,?,?)",
            (object_id, row["version"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now()),
        )

    def create_object(self, user_id, inventory_no, title, object_type, holder, public_summary):
        inventory_no, title = inventory_no.strip(), title.strip()
        if not inventory_no or len(title) < 2:
            raise BusinessError("库存号和标题不能为空", 422, "invalid_object")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            try:
                cur = conn.execute(
                    """INSERT INTO objects(inventory_no,title,object_type,current_holder,public_summary,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (inventory_no, title, object_type.strip() or "未分类", holder.strip() or "馆藏", public_summary.strip(), user_id, now(), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("库存号已存在", 409, "inventory_exists")
            object_id = cur.lastrowid
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.create", {"inventory_no": inventory_no})
            return {"id": object_id, "inventory_no": inventory_no, "version": 1}

    def update_object(self, user_id, object_id, changes):
        allowed = {"title", "object_type", "current_holder", "public_summary"}
        clean = {k: str(v).strip() for k, v in changes.items() if k in allowed and str(v).strip()}
        if not clean:
            raise BusinessError("没有可更新字段", 422, "empty_update")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            new_version = row["version"] + 1
            assignments = ",".join(f"{k}=?" for k in clean)
            conn.execute(
                f"UPDATE objects SET {assignments},version=?,updated_at=? WHERE id=?",
                (*clean.values(), new_version, now(), object_id),
            )
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.update", {"version": new_version, "changes": clean})
            return {"id": object_id, "version": new_version, "changes": clean}

    def add_source(self, user_id, name, source_type, reference):
        if not name.strip() or not reference.strip():
            raise BusinessError("来源名称和引用不能为空", 422, "invalid_source")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                cur = conn.execute(
                    "INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES(?,?,?,?,?)",
                    (name.strip(), source_type.strip() or "archive", reference.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("来源记录已存在", 409, "source_exists")
            return {"id": cur.lastrowid, "name": name.strip(), "reference": reference.strip(), "status": "active"}

    def list_sources(self, user_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_source(self, user_id, source_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not row:
                raise BusinessError("来源不存在", 404, "source_not_found")
            return dict(row)

    def update_source_status(self, user_id, source_id, new_status, note, expected_status=None):
        if new_status not in SOURCE_STATUSES:
            raise BusinessError("来源状态必须是 active、disputed 或 withdrawn", 422, "invalid_source_status")
        if len(note.strip()) < 5:
            raise BusinessError("状态说明至少 5 字", 422, "status_note_required")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
                if not source:
                    raise BusinessError("来源不存在", 404, "source_not_found")
                if expected_status is not None and source["status"] != expected_status:
                    raise BusinessError(
                        f"来源状态已变更为 {source['status']}，请刷新后重试", 409, "source_status_conflict"
                    )
                old_status = source["status"]
                if old_status == "withdrawn":
                    # 撤回是终态；后到的审查员会看到先撤回的人
                    who = conn.execute("SELECT name FROM users WHERE id=?", (source["status_changed_by"],)).fetchone()
                    name = who["name"] if who else source["status_changed_by"]
                    raise BusinessError(
                        f"来源已由 {name} 于 {source['status_changed_at']} 撤回，不能重复操作",
                        409, "source_already_withdrawn",
                    )
                if old_status == new_status:
                    raise BusinessError(f"来源已是 {new_status} 状态", 409, "source_status_unchanged")
                if new_status not in SOURCE_TRANSITIONS.get(old_status, set()):
                    raise BusinessError(
                        f"不能从 {old_status} 直接变更为 {new_status}", 409, "invalid_source_transition"
                    )
                conn.execute(
                    "UPDATE sources SET status=?, internal_note=?, status_changed_by=?, status_changed_at=? WHERE id=?",
                    (new_status, note.strip(), user_id, now(), source_id),
                )
                conn.execute(
                    "INSERT INTO source_status_log(source_id,old_status,new_status,note,changed_by,changed_at) VALUES(?,?,?,?,?,?)",
                    (source_id, old_status, new_status, note.strip(), user_id, now()),
                )
                affected = self._apply_source_status(conn, source_id, old_status, new_status, note, user_id)
                self._audit(conn, None, user_id, "source.status_update",
                            {"source_id": source_id, "from": old_status, "to": new_status})
                return {"id": source_id, "status": new_status, "old_status": old_status, **affected}
            except Exception:
                conn.rollback()
                raise

    def _apply_source_status(self, conn, source_id, old_status, new_status, note, actor_id):
        """来源状态联动：未了结流转事件作废重算，已办结返还裁定挂起等复核。"""
        affected_objects, voided_events, reinstated_events, suspended_claims = [], [], [], []
        if new_status == "active" and old_status == "disputed":
            objs = conn.execute(
                "SELECT DISTINCT object_id FROM events WHERE source_id=? AND status='void' AND void_reason=?",
                (source_id, f"source:{source_id}:disputed"),
            ).fetchall()
        else:
            objs = conn.execute(
                "SELECT DISTINCT object_id FROM events WHERE source_id=? AND status='active'", (source_id,)
            ).fetchall()
        for row in objs:
            oid = row["object_id"]
            obj = self._object(conn, oid)
            next_version = obj["version"] + 1
            if new_status in ("disputed", "withdrawn"):
                resolved = [r["id"] for r in conn.execute(
                    "SELECT id FROM claims WHERE object_id=? AND status='resolved_return'", (oid,)
                ).fetchall()]
                open_claims = conn.execute(
                    "SELECT 1 FROM claims WHERE object_id=? AND status IN ('submitted','under_review','negotiating','suspended')",
                    (oid,),
                ).fetchone()
                if resolved:
                    reason = f"来源 {source_id} 状态变更为 {new_status}，返还裁定挂起等复核：{note}"
                    conn.execute(
                        "UPDATE claims SET status='suspended', suspended_reason=?, suspended_at=?, updated_at=? "
                        "WHERE id=?",
                        (reason, now(), now(), resolved[0]),
                    )
                    # 同一藏品若有多条已办结裁定，全部挂起
                    if len(resolved) > 1:
                        conn.executemany(
                            "UPDATE claims SET status='suspended', suspended_reason=?, suspended_at=?, updated_at=? WHERE id=?",
                            [(reason, now(), now(), cid) for cid in resolved[1:]],
                        )
                    suspended_claims.extend(resolved)
                if not resolved or open_claims:
                    ids = [r["id"] for r in conn.execute(
                        "SELECT id FROM events WHERE source_id=? AND object_id=? AND status='active'", (source_id, oid)
                    ).fetchall()]
                    reason = f"source:{source_id}:{new_status}"
                    conn.execute(
                        "UPDATE events SET status='void', void_reason=?, voided_by=?, voided_at=? "
                        "WHERE source_id=? AND object_id=? AND status='active'",
                        (reason, actor_id, now(), source_id, oid),
                    )
                    voided_events.extend(ids)
            elif new_status == "active" and old_status == "disputed":
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM events WHERE source_id=? AND object_id=? AND status='void' AND void_reason=?",
                    (source_id, oid, f"source:{source_id}:disputed"),
                ).fetchall()]
                conn.execute(
                    "UPDATE events SET status='active', void_reason=NULL, voided_by=NULL, voided_at=NULL "
                    "WHERE source_id=? AND object_id=? AND status='void' AND void_reason=?",
                    (source_id, oid, f"source:{source_id}:disputed"),
                )
                reinstated_events.extend(ids)
            conn.execute("UPDATE objects SET version=?, updated_at=? WHERE id=?", (next_version, now(), oid))
            self._snapshot(conn, oid, actor_id)
            self._audit(conn, oid, actor_id, "source.affect_object", {"source_id": source_id, "to": new_status})
            affected_objects.append({"object_id": oid, "version": next_version})
        return {
            "affected_objects": affected_objects,
            "voided_events": voided_events,
            "reinstated_events": reinstated_events,
            "suspended_claims": suspended_claims,
        }

    def add_event(self, user_id, object_id, event_type, date_start, date_end, place, description, source_id=None, visibility="internal"):
        if not event_type.strip() or not description.strip() or not place.strip():
            raise BusinessError("事件类型、地点和说明不能为空", 422, "invalid_event")
        try:
            start = date.fromisoformat(date_start)
            end = date.fromisoformat(date_end) if date_end else start
        except ValueError:
            raise BusinessError("事件日期必须是 YYYY-MM-DD", 422, "invalid_date")
        if end < start:
            raise BusinessError("事件结束日期不能早于开始日期", 422, "invalid_date_range")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            if source_id:
                src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
                if not src:
                    raise BusinessError("来源不存在", 404, "source_not_found")
                if src["status"] == "withdrawn":
                    raise BusinessError("来源已撤回，不能引用", 409, "source_withdrawn")
            cur = conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,date_end,place,description,source_id,
                                     source_confirmed,status,visibility,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,1,'active',?,?,?)""",
                (object_id, event_type.strip(), date_start, date_end or None, place.strip(), description.strip(),
                 source_id, visibility, user_id, now()),
            )
            new_version = row["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "event.add", {"event_id": cur.lastrowid, "version": new_version, "visibility": visibility})
            return {"id": cur.lastrowid, "object_id": object_id, "object_version": new_version}

    def upload_evidence(self, user_id, object_id, filename, content_b64, visibility, event_id=None):
        if not filename.strip():
            raise BusinessError("文件名不能为空", 422, "invalid_filename")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            if event_id and not conn.execute("SELECT 1 FROM events WHERE id=? AND object_id=?", (event_id, object_id)).fetchone():
                raise BusinessError("证据关联的事件不存在", 404, "event_not_found")
            cur = conn.execute(
                """INSERT INTO evidence(object_id,event_id,filename,sha256,size,content,visibility,uploaded_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (object_id, event_id, filename.strip(), digest, len(content), content, visibility, user_id, now()),
            )
            self._audit(conn, object_id, user_id, "evidence.upload", {"evidence_id": cur.lastrowid, "sha256": digest, "visibility": visibility})
            return {"id": cur.lastrowid, "filename": filename.strip(), "sha256": digest, "size": len(content)}

    def create_claim(self, user_id, object_id, claimed_by, desired_outcome):
        if not claimed_by.strip() or not desired_outcome.strip():
            raise BusinessError("主张人和期望结果不能为空", 422, "invalid_claim")
        with self.connect() as conn:
            claimant = self._user(conn, user_id, {"claimant"})
            self._object(conn, object_id)
            cur = conn.execute(
                """INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (object_id, user_id, claimed_by.strip(), desired_outcome.strip(), now(), now()),
            )
            self._audit(conn, object_id, user_id, "claim.create", {"claim_id": cur.lastrowid})
            return {"id": cur.lastrowid, "object_id": object_id, "status": "submitted"}

    def transition_claim(self, user_id, claim_id, new_status, note):
        if len(note.strip()) < 5:
            raise BusinessError("阶段审查说明至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            reviewer = self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                if claim["status"] == "suspended":
                    raise BusinessError("返还裁定已挂起，需先复核恢复", 409, "claim_suspended")
                allowed = CLAIM_TRANSITIONS.get(claim["status"], set())
                if new_status not in allowed:
                    raise BusinessError(f"不能从 {claim['status']} 直接变更为 {new_status}", 409, "invalid_transition")
                conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (new_status, now(), claim_id))
                conn.execute(
                    "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, user_id, claim["status"], new_status, note.strip(), now()),
                )
                new_version = claim["object_id"]
                obj = self._object(conn, claim["object_id"])
                next_version = obj["version"] + 1
                conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, now(), claim["object_id"]))
                self._snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.transition", {"claim_id": claim_id, "from": claim["status"], "to": new_status})
                return {"claim_id": claim_id, "old_status": claim["status"], "status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def resume_claim(self, user_id, claim_id, decision, note):
        if decision not in RESUME_DECISIONS:
            raise BusinessError("复核决定必须是 reopen 或 uphold", 422, "invalid_resume_decision")
        if len(note.strip()) < 5:
            raise BusinessError("复核说明至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                if claim["status"] != "suspended":
                    raise BusinessError("权利主张未挂起，无需复核", 409, "claim_not_suspended")
                new_status = "under_review" if decision == "reopen" else "resolved_return"
                conn.execute(
                    "UPDATE claims SET status=?, suspended_reason=NULL, suspended_at=NULL, updated_at=? WHERE id=?",
                    (new_status, now(), claim_id),
                )
                conn.execute(
                    "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, user_id, "suspended", new_status, note.strip(), now()),
                )
                obj = self._object(conn, claim["object_id"])
                next_version = obj["version"] + 1
                conn.execute("UPDATE objects SET version=?, updated_at=? WHERE id=?", (next_version, now(), claim["object_id"]))
                self._snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.resume",
                            {"claim_id": claim_id, "decision": decision, "to": new_status})
                return {"claim_id": claim_id, "status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def confirm_event_source(self, user_id, object_id, event_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            ev = conn.execute("SELECT * FROM events WHERE id=? AND object_id=?", (event_id, object_id)).fetchone()
            if not ev:
                raise BusinessError("事件不存在", 404, "event_not_found")
            if not ev["source_id"]:
                raise BusinessError("事件未关联来源", 422, "event_without_source")
            conn.execute("UPDATE events SET source_confirmed=1 WHERE id=?", (event_id,))
            self._audit(conn, object_id, user_id, "event.confirm_source",
                        {"event_id": event_id, "source_id": ev["source_id"]})
            return {"event_id": event_id, "source_confirmed": 1}

    def upgrade(self, user_id="staff"):
        """旧数据升级：为缺来源的事件按发生时间回填当时有效的一条来源，挂待确认。"""
        with self._lock, self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            rows = conn.execute(
                "SELECT id, date_start FROM events WHERE source_id IS NULL AND status='active'"
            ).fetchall()
            backfilled = []
            for ev in rows:
                src = conn.execute(
                    """SELECT id FROM sources
                       WHERE date(created_at) <= date(?)
                         AND (status IN ('active','disputed')
                              OR (status='withdrawn' AND date(status_changed_at) > date(?)))
                       ORDER BY date(created_at) DESC, id DESC LIMIT 1""",
                    (ev["date_start"], ev["date_start"]),
                ).fetchone()
                if src:
                    conn.execute("UPDATE events SET source_id=?, source_confirmed=0 WHERE id=?", (src["id"], ev["id"]))
                    backfilled.append({"event_id": ev["id"], "source_id": src["id"]})
            if backfilled:
                self._audit(conn, None, user_id, "data.upgrade", {"backfilled": backfilled})
            return {"backfilled": backfilled}

    def get_object(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            obj = self._object(conn, object_id)
            if user["role"] == "public":
                events = conn.execute(
                    "SELECT id,event_type,date_start,date_end,place,description,visibility,created_at FROM events "
                    "WHERE object_id=? AND visibility='public' AND status='active' ORDER BY id",
                    (object_id,),
                ).fetchall()
                claims = conn.execute(
                    "SELECT id,claimed_by,desired_outcome,status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
                ).fetchall()
                return {
                    "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                    "object_type": obj["object_type"], "public_summary": obj["public_summary"], "version": obj["version"],
                    "events": [dict(e) for e in events], "claims": [dict(c) for c in claims],
                }
            result = {
                "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                "object_type": obj["object_type"], "current_holder": obj["current_holder"],
                "public_summary": obj["public_summary"], "version": obj["version"],
                "events": [dict(x) | {"source": self._source_view(conn, x["source_id"], user["role"]),
                                     "evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE event_id=? ORDER BY id", (x["id"],)).fetchall()]}
                            for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "claims": [dict(c) | {"reviews": [dict(r) for r in conn.execute("SELECT * FROM claim_reviews WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()]}
                           for c in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "unlinked_evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE object_id=? AND event_id IS NULL ORDER BY id", (object_id,)).fetchall()],
            }
            if user["role"] == "claimant":
                # 主张人只看到公开且未作废的来源事件和自己的主张，不能浏览内部调查材料。
                result["events"] = [e for e in result["events"] if e["visibility"] == "public" and e["status"] == "active"]
                result["unlinked_evidence"] = []
                result["claims"] = [c for c in result["claims"] if c["claimant_id"] == user_id]
                for c in result["claims"]:
                    c.pop("claimant_id", None)
            return result

    def _source_view(self, conn, source_id, role):
        if not source_id:
            return None
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        if role == "claimant":
            # 内部调查说明不对主张人公开
            data.pop("internal_note", None)
        return data

    def list_objects(self, user_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "public":
                rows = conn.execute("SELECT id,inventory_no,title,object_type,public_summary,version FROM objects ORDER BY id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def object_history(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            rows = conn.execute("SELECT id,version,changed_by,created_at FROM object_versions WHERE object_id=? ORDER BY version", (object_id,)).fetchall()
            return [dict(r) for r in rows]

    def history_detail(self, user_id, object_id, version):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM object_versions WHERE object_id=? AND version=?", (object_id, version)).fetchone()
            if not row:
                raise BusinessError("历史版本不存在", 404, "not_found")
            return dict(row) | {"snapshot": json.loads(row["snapshot"])}


class Handler(BaseHTTPRequestHandler):
    server_version = "Provenance/1.0"

    def _store(self): return self.server.store  # type: ignore[attr-defined]

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "GET" and path == "/health": return self._send(200, {"ok": True})
        store = self._store()
        if parts == ["api", "objects"] and method == "GET": return self._send(200, {"items": store.list_objects(user)})
        if parts == ["api", "objects"] and method == "POST":
            d = self._body(); return self._send(201, store.create_object(user, d.get("inventory_no", ""), d.get("title", ""), d.get("object_type", ""), d.get("current_holder", ""), d.get("public_summary", "")))
        if parts == ["api", "sources"] and method == "GET": return self._send(200, {"items": store.list_sources(user)})
        if parts == ["api", "sources"] and method == "POST":
            d = self._body(); return self._send(201, store.add_source(user, d.get("name", ""), d.get("source_type", ""), d.get("reference", "")))
        if len(parts) == 3 and parts[:2] == ["api", "sources"] and method == "GET":
            return self._send(200, store.get_source(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "sources"] and parts[3] == "status" and method == "POST":
            d = self._body(); return self._send(200, store.update_source_status(user, int(parts[2]), d.get("status", ""), d.get("note", ""), d.get("expected_status")))
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            object_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST": return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body(); return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""), d.get("date_end", ""), d.get("place", ""), d.get("description", ""), d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body(); return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""), d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 6 and parts[3] == "events" and parts[5] == "confirm-source" and method == "POST":
                return self._send(200, store.confirm_event_source(user, object_id, int(parts[4])))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body(); return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET": return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET": return self._send(200, store.history_detail(user, object_id, int(parts[4])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body(); return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""), d.get("note", "")))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "resume" and method == "POST":
            d = self._body(); return self._send(200, store.resume_claim(user, int(parts[2]), d.get("decision", ""), d.get("note", "")))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc: self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (ValueError, TypeError): self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc: self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class ProvenanceServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store): self.store = store; super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="博物馆藏品来源与返还审查")
    parser.add_argument("--db", default=str(DEFAULT_DB)); parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    parser.add_argument("--upgrade", action="store_true", help="升级旧数据：回填缺失来源并挂待确认")
    args = parser.parse_args(); store = ProvenanceStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.upgrade:
        store.seed()
        result = store.upgrade("staff")
        print(f"数据升级完成，回填 {len(result['backfilled'])} 条事件来源")
        return
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server = ProvenanceServer(("127.0.0.1", args.port), store)
    print(f"来源审查系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
