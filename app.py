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
    # 已办结的返还裁定在依据来源失效后挂起，复核后可退回重审或维持/推翻。
    "review_hold": {"under_review", "resolved_return", "rejected"},
    "resolved_return": set(),
    "rejected": set(),
}
SOURCE_STATUSES = ("active", "doubtful", "withdrawn")
SOURCE_STATUS_LABELS = {"active": "有效", "doubtful": "存疑", "withdrawn": "撤回"}
# 可以对公众/主张人公开的来源字段；内部调查说明绝不能出现在这里。
PUBLIC_SOURCE_FIELDS = (
    "id", "name", "source_type", "reference", "status",
    "status_reason_public", "valid_from", "valid_until",
)
LEGACY_UPGRADE_KEY = "legacy_upgrade_v1"


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request", details=None):
        super().__init__(message)
        self.message, self.status, self.code, self.details = message, status, code, details or {}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public'))
                );
                CREATE TABLE IF NOT EXISTS sources(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    source_type TEXT NOT NULL, reference TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','doubtful','withdrawn')),
                    status_reason_public TEXT,
                    internal_investigation TEXT,
                    status_changed_by TEXT REFERENCES users(id),
                    status_changed_at TEXT,
                    valid_from TEXT, valid_until TEXT,
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
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    voided INTEGER NOT NULL DEFAULT 0,
                    voided_by TEXT REFERENCES users(id),
                    voided_at TEXT, void_reason TEXT,
                    needs_confirmation INTEGER NOT NULL DEFAULT 0,
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
                        CHECK(status IN ('submitted','under_review','negotiating','review_hold','resolved_return','rejected')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_reviews(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_status_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL REFERENCES sources(id),
                    changed_by TEXT NOT NULL REFERENCES users(id),
                    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    reason_public TEXT, internal_note TEXT, created_at TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS schema_meta(
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                """
            )
        # 旧库平滑升级：补列、重建带新状态枚举的 claims 表、回填历史关联。
        return self.upgrade_legacy_data()

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("staff", "藏品研究员", "staff"),
                    ("reviewer1", "返还审查员甲", "reviewer"),
                    ("reviewer2", "返还审查员乙", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("public", "公众访客", "public"),
                ],
            )

    # ---- 基础辅助 -------------------------------------------------------

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

    def _source(self, conn, source_id):
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise BusinessError("来源不存在", 404, "source_not_found")
        return row

    def _audit(self, conn, object_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(object_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (object_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _public_source(self, row):
        return {k: row[k] for k in PUBLIC_SOURCE_FIELDS}

    def _snapshot(self, conn, object_id, actor):
        """保存当时完整记录：来源状态、事件作废标记都随快照固化。"""
        row = self._object(conn, object_id)

        def source_snapshot(source_id):
            if not source_id:
                return None
            s = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            return dict(s) if s else None

        events = []
        for e in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall():
            ed = dict(e)
            ed["source"] = source_snapshot(e["source_id"])
            ed["evidence"] = [
                dict(x) for x in conn.execute(
                    "SELECT id,filename,sha256,size,visibility FROM evidence WHERE event_id=? ORDER BY id",
                    (e["id"],),
                ).fetchall()
            ]
            events.append(ed)
        claims = []
        for c in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall():
            cd = dict(c)
            cd["reviews"] = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM claim_reviews WHERE claim_id=? ORDER BY id", (c["id"],)
                ).fetchall()
            ]
            claims.append(cd)
        snapshot = {"object": dict(row), "events": events, "claims": claims}
        conn.execute(
            "INSERT INTO object_versions(object_id,version,snapshot,changed_by,created_at) VALUES(?,?,?,?,?)",
            (object_id, row["version"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now()),
        )

    # ---- 旧数据升级 -----------------------------------------------------

    def upgrade_legacy_data(self):
        """给旧库补来源状态/有效期与事件作废字段，并按发生时间回填来源关联。

        升级只执行一次：早年间未关联来源的事件，会被指到“事件发生时唯一有效”
        的来源上，同时挂 needs_confirmation 等待人工确认；找不到或不止一条
        候选时只挂待确认，不猜测。
        """
        with self._lock:
            conn = self.connect()
            conn.isolation_level = None  # autocommit，便于显式事务与 PRAGMA
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                existing = {
                    r["name"] for r in conn.execute("PRAGMA table_info(sources)").fetchall()
                }
                add_columns = [
                    ("sources", "status", "TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','doubtful','withdrawn'))"),
                    ("sources", "status_reason_public", "TEXT"),
                    ("sources", "internal_investigation", "TEXT"),
                    ("sources", "status_changed_by", "TEXT REFERENCES users(id)"),
                    ("sources", "status_changed_at", "TEXT"),
                    ("sources", "valid_from", "TEXT"),
                    ("sources", "valid_until", "TEXT"),
                ]
                for table, column, ddl in add_columns:
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                event_cols = {
                    r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()
                }
                for table, column, ddl in [
                    ("events", "voided", "INTEGER NOT NULL DEFAULT 0"),
                    ("events", "voided_by", "TEXT REFERENCES users(id)"),
                    ("events", "voided_at", "TEXT"),
                    ("events", "void_reason", "TEXT"),
                    ("events", "needs_confirmation", "INTEGER NOT NULL DEFAULT 0"),
                ]:
                    if column not in event_cols:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS source_status_log(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_id INTEGER NOT NULL REFERENCES sources(id),
                        changed_by TEXT NOT NULL REFERENCES users(id),
                        old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                        reason_public TEXT, internal_note TEXT, created_at TEXT NOT NULL)"""
                )
                self._rebuild_claims_for_review_hold(conn)

                done = conn.execute(
                    "SELECT value FROM schema_meta WHERE key=?", (LEGACY_UPGRADE_KEY,)
                ).fetchone()
                if done:
                    return {"backfilled": 0, "pending_confirmation": 0, "skipped": "already_upgraded"}

                conn.execute("BEGIN IMMEDIATE")
                # 旧来源没有有效期概念：以录入时间作为生效起点，窗口向后开放。
                conn.execute(
                    "UPDATE sources SET valid_from = date(created_at) WHERE valid_from IS NULL"
                )
                backfilled = pending = 0
                legacy_events = conn.execute(
                    "SELECT id, date_start FROM events WHERE source_id IS NULL"
                ).fetchall()
                for ev in legacy_events:
                    candidates = conn.execute(
                        """SELECT id FROM sources
                           WHERE (valid_from IS NULL OR valid_from <= ?)
                             AND (valid_until IS NULL OR valid_until >= ?)
                           ORDER BY id""",
                        (ev["date_start"], ev["date_start"]),
                    ).fetchall()
                    if len(candidates) == 1:
                        conn.execute(
                            "UPDATE events SET source_id=?, needs_confirmation=1 WHERE id=?",
                            (candidates[0]["id"], ev["id"]),
                        )
                        backfilled += 1
                    else:
                        conn.execute("UPDATE events SET needs_confirmation=1 WHERE id=?", (ev["id"],))
                    pending += 1
                # 旧库里已有的手工关联同样早于状态体系，统一挂待确认。
                cur = conn.execute(
                    "UPDATE events SET needs_confirmation=1 WHERE needs_confirmation=0"
                )
                pending += cur.rowcount or 0
                conn.execute(
                    "INSERT INTO schema_meta(key,value) VALUES(?,?)",
                    (LEGACY_UPGRADE_KEY, now()),
                )
                conn.execute("COMMIT")
                return {"backfilled": backfilled, "pending_confirmation": pending}
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    @staticmethod
    def _rebuild_claims_for_review_hold(conn):
        """旧库的 claims CHECK 约束不含 review_hold，需要按 SQLite 标准步骤重建。"""
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='claims'"
        ).fetchone()
        if ddl and "review_hold" in (ddl["sql"] or ""):
            return
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE claims RENAME TO claims_old")
        conn.execute(
            """CREATE TABLE claims(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL REFERENCES objects(id),
                claimant_id TEXT NOT NULL REFERENCES users(id),
                claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'submitted'
                    CHECK(status IN ('submitted','under_review','negotiating','review_hold','resolved_return','rejected')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )
        conn.execute(
            """INSERT INTO claims(id,object_id,claimant_id,claimed_by,desired_outcome,status,created_at,updated_at)
               SELECT id,object_id,claimant_id,claimed_by,desired_outcome,status,created_at,updated_at FROM claims_old"""
        )
        conn.execute("DROP TABLE claims_old")
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys=ON")

    # ---- 藏品 -----------------------------------------------------------

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

    # ---- 来源 -----------------------------------------------------------

    @staticmethod
    def _validate_window(valid_from, valid_until):
        vf = vu = None
        if valid_from:
            try:
                vf = date.fromisoformat(valid_from)
            except ValueError:
                raise BusinessError("生效日期必须是 YYYY-MM-DD", 422, "invalid_date")
        if valid_until:
            try:
                vu = date.fromisoformat(valid_until)
            except ValueError:
                raise BusinessError("失效日期必须是 YYYY-MM-DD", 422, "invalid_date")
        if vf and vu and vu < vf:
            raise BusinessError("来源失效日期不能早于生效日期", 422, "invalid_date_range")
        return (vf.isoformat() if vf else None, vu.isoformat() if vu else None)

    def add_source(self, user_id, name, source_type, reference, valid_from=None, valid_until=None):
        if not name.strip() or not reference.strip():
            raise BusinessError("来源名称和引用不能为空", 422, "invalid_source")
        vf, vu = self._validate_window(valid_from, valid_until)
        vf = vf or date.today().isoformat()
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                cur = conn.execute(
                    """INSERT INTO sources(name,source_type,reference,created_by,created_at,valid_from,valid_until)
                       VALUES(?,?,?,?,?,?,?)""",
                    (name.strip(), source_type.strip() or "archive", reference.strip(), user_id, now(), vf, vu),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("来源记录已存在", 409, "source_exists")
            return {"id": cur.lastrowid, "name": name.strip(), "reference": reference.strip(),
                    "status": "active", "valid_from": vf, "valid_until": vu}

    def change_source_status(self, user_id, source_id, new_status, reason_public="", internal_note=""):
        """审查员撤回/标疑来源，并级联处理引用方。

        并发安全：整段在 BEGIN IMMEDIATE 下完成，两名审查员同时提交时先到的
        生效，后到者拿到 409 及首位操作者身份。
        """
        if new_status not in SOURCE_STATUSES:
            raise BusinessError("来源状态只能是 active/doubtful/withdrawn", 422, "invalid_source_status")
        internal_note = internal_note.strip()
        if new_status in ("doubtful", "withdrawn") and len(internal_note) < 5:
            raise BusinessError("状态变更必须填写至少 5 字的内部调查说明", 422, "investigation_note_required")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._source(conn, source_id)
                old_status = row["status"]
                if old_status == new_status:
                    raise BusinessError(
                        f"来源已被{row['status_changed_by'] or '系统'}标记为“{SOURCE_STATUS_LABELS[new_status]}”，无可重复生效的操作",
                        409, "source_status_conflict", self._conflict_details(conn, row),
                    )
                if old_status == "withdrawn":
                    # 撤回是终局：后来者只能看到是谁先撤回的。
                    raise BusinessError(
                        f"来源已被{row['status_changed_by']}先撤回，不能再改状态",
                        409, "source_status_conflict", self._conflict_details(conn, row),
                    )
                if new_status == "active" and old_status != "doubtful":
                    raise BusinessError("只有存疑来源可以恢复为有效", 409, "invalid_source_transition")
                ts = now()
                conn.execute(
                    """UPDATE sources SET status=?, status_reason_public=?, internal_investigation=?,
                                         status_changed_by=?, status_changed_at=? WHERE id=?""",
                    (new_status, reason_public.strip() or None, internal_note or None, user_id, ts, source_id),
                )
                conn.execute(
                    """INSERT INTO source_status_log(source_id,changed_by,old_status,new_status,reason_public,internal_note,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (source_id, user_id, old_status, new_status, reason_public.strip() or None,
                     internal_note or None, ts),
                )
                cascade = {"voided_events": [], "reset_claims": [], "held_claims": [], "object_versions": []}
                if new_status in ("doubtful", "withdrawn"):
                    cascade = self._cascade_source_invalidation(conn, user_id, row, new_status, ts)
                self._audit(conn, None, user_id, "source.status_change",
                            {"source_id": source_id, "from": old_status, "to": new_status,
                             "cascade": cascade, "public_reason": reason_public.strip() or None})
                conn.execute("COMMIT")
                return {"source_id": source_id, "old_status": old_status, "status": new_status,
                        "changed_by": user_id, "changed_at": ts, **cascade}
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _conflict_details(conn, row):
        changer = conn.execute("SELECT id,name FROM users WHERE id=?", (row["status_changed_by"],)).fetchone()
        return {
            "current_status": row["status"],
            "changed_by": row["status_changed_by"],
            "changed_by_name": changer["name"] if changer else None,
            "changed_at": row["status_changed_at"],
        }

    def _cascade_source_invalidation(self, conn, actor, source_row, new_status, ts):
        """来源失效后的重算：事件作废、未办结主张退回审查、已办结返还裁定挂起。"""
        label = SOURCE_STATUS_LABELS[new_status]
        reason = f"依据来源“{source_row['name']}”（{source_row['reference']}）被标记为{label}，引用记录作废重算。"
        impacted_objects = [
            r["object_id"]
            for r in conn.execute(
                "SELECT DISTINCT object_id FROM events WHERE source_id=? AND voided=0",
                (source_row["id"],),
            ).fetchall()
        ]
        voided_events, reset_claims, held_claims, versions = [], [], [], []
        for object_id in impacted_objects:
            object_event_ids = []
            ev_rows = conn.execute(
                "SELECT id FROM events WHERE source_id=? AND voided=0 ORDER BY id",
                (source_row["id"],),
            ).fetchall()
            for ev in ev_rows:
                conn.execute(
                    "UPDATE events SET voided=1, voided_by=?, voided_at=?, void_reason=? WHERE id=?",
                    (actor, ts, reason, ev["id"]),
                )
                object_event_ids.append(ev["id"])
                voided_events.append(ev["id"])
            for claim in conn.execute(
                "SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)
            ).fetchall():
                if claim["status"] in ("under_review", "negotiating"):
                    target = "under_review"
                    reset_claims.append(claim["id"])
                elif claim["status"] == "resolved_return":
                    target = "review_hold"
                    held_claims.append(claim["id"])
                else:
                    continue
                note = (
                    f"依据来源被标记为{label}，相关流转事件已作废，原返还裁定挂起等待复核。"
                    if target == "review_hold"
                    else f"依据来源被标记为{label}，相关流转事件已作废，主张退回重新审查。"
                )
                conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (target, ts, claim["id"]))
                conn.execute(
                    """INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (claim["id"], actor, claim["status"], target, note, ts),
                )
            obj = self._object(conn, object_id)
            next_version = obj["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, ts, object_id))
            self._snapshot(conn, object_id, actor)
            self._audit(conn, object_id, actor, "source.cascade",
                        {"source_id": source_row["id"], "source_status": new_status,
                         "voided_events": object_event_ids,
                         "version": next_version})
            versions.append(next_version)
        return {"voided_events": voided_events, "reset_claims": reset_claims,
                "held_claims": held_claims, "object_versions": versions}

    def get_source(self, user_id, source_id):
        """分层来源视图：内部调查说明仅 staff/reviewer 可见。"""
        with self.connect() as conn:
            user = self._user(conn, user_id)
            row = self._source(conn, source_id)
            if user["role"] in ("staff", "reviewer"):
                data = dict(row)
                data["status_log"] = [
                    dict(r) for r in conn.execute(
                        """SELECT l.id,l.changed_by,u.name AS changed_by_name,l.old_status,l.new_status,
                                  l.reason_public,l.internal_note,l.created_at
                           FROM source_status_log l JOIN users u ON u.id=l.changed_by
                           WHERE source_id=? ORDER BY l.id""",
                        (source_id,),
                    ).fetchall()
                ]
                return data
            return self._public_source(row)

    def list_sources(self, user_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            rows = conn.execute("SELECT * FROM sources ORDER BY id").fetchall()
            if user["role"] in ("staff", "reviewer"):
                return [dict(r) for r in rows]
            return [self._public_source(r) for r in rows]

    def get_source_investigation(self, user_id, source_id):
        """内部调查说明专口：公众/主张人越权访问一律 403。"""
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = self._source(conn, source_id)
            log = conn.execute(
                """SELECT l.id,l.changed_by,u.name AS changed_by_name,l.old_status,l.new_status,
                          l.reason_public,l.internal_note,l.created_at
                   FROM source_status_log l JOIN users u ON u.id=l.changed_by
                   WHERE source_id=? ORDER BY l.id""",
                (source_id,),
            ).fetchall()
            return {
                "source_id": row["id"], "status": row["status"],
                "internal_investigation": row["internal_investigation"],
                "changed_by": row["status_changed_by"], "changed_at": row["status_changed_at"],
                "log": [dict(r) for r in log],
            }

    def confirm_event_source(self, user_id, event_id):
        """人工确认旧数据回填的来源关联，摘掉待确认标记。"""
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if not row:
                raise BusinessError("流转事件不存在", 404, "event_not_found")
            if not row["needs_confirmation"]:
                return {"event_id": event_id, "needs_confirmation": 0}
            conn.execute("UPDATE events SET needs_confirmation=0 WHERE id=?", (event_id,))
            self._audit(conn, row["object_id"], user_id, "event.source_confirmed",
                        {"event_id": event_id, "source_id": row["source_id"]})
            return {"event_id": event_id, "object_id": row["object_id"],
                    "source_id": row["source_id"], "needs_confirmation": 0}

    def run_admin_upgrade(self, user_id):
        """手动触发旧数据升级的管理入口，仅 staff 可用。"""
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
        return self.upgrade_legacy_data()

    # ---- 流转事件 / 证据 / 主张 ----------------------------------------

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
            needs_confirmation = 0
            if source_id:
                source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
                if not source:
                    raise BusinessError("来源不存在", 404, "source_not_found")
                if source["status"] == "withdrawn":
                    raise BusinessError("不能引用已撤回的来源，请先更换依据", 422, "source_not_usable")
                if source["status"] == "doubtful":
                    needs_confirmation = 1  # 存疑来源可以先录，但关联必须人工确认
            cur = conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,date_end,place,description,source_id,visibility,needs_confirmation,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (object_id, event_type.strip(), date_start, date_end or None, place.strip(), description.strip(),
                 source_id, visibility, needs_confirmation, user_id, now()),
            )
            new_version = row["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "event.add",
                        {"event_id": cur.lastrowid, "version": new_version, "visibility": visibility,
                         "needs_confirmation": needs_confirmation})
            return {"id": cur.lastrowid, "object_id": object_id, "object_version": new_version,
                    "needs_confirmation": needs_confirmation}

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
                conn.execute("COMMIT")
                return {"claim_id": claim_id, "old_status": claim["status"], "status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    # ---- 查询 -----------------------------------------------------------

    @staticmethod
    def _public_event_view(event):
        """公众/主张人可见的事件字段：作废事件整体不展示，内部标记一律剥离。"""
        return {
            "id": event["id"], "event_type": event["event_type"],
            "date_start": event["date_start"], "date_end": event["date_end"],
            "place": event["place"], "description": event["description"],
            "visibility": event["visibility"], "created_at": event["created_at"],
        }

    def get_object(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            obj = self._object(conn, object_id)
            if user["role"] == "public":
                events = conn.execute(
                    """SELECT id,event_type,date_start,date_end,place,description,visibility,created_at,source_id
                       FROM events WHERE object_id=? AND visibility='public' AND voided=0 ORDER BY id""",
                    (object_id,),
                ).fetchall()
                items = []
                for e in events:
                    ed = self._public_event_view(e)
                    ed["source"] = (
                        self._public_source(self._source(conn, e["source_id"]))
                        if e["source_id"] else None
                    )
                    items.append(ed)
                claims = conn.execute(
                    "SELECT id,claimed_by,desired_outcome,status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
                ).fetchall()
                return {
                    "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                    "object_type": obj["object_type"], "public_summary": obj["public_summary"], "version": obj["version"],
                    "events": items, "claims": [dict(c) for c in claims],
                }
            result = {
                "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                "object_type": obj["object_type"], "current_holder": obj["current_holder"],
                "public_summary": obj["public_summary"], "version": obj["version"],
                "events": [
                    dict(x) | {
                        "source": dict(conn.execute("SELECT * FROM sources WHERE id=?", (x["source_id"],)).fetchone()) if x["source_id"] else None,
                        "evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE event_id=? ORDER BY id", (x["id"],)).fetchall()],
                    }
                    for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()
                ],
                "claims": [dict(c) | {"reviews": [dict(r) for r in conn.execute("SELECT * FROM claim_reviews WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()]}
                           for c in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "unlinked_evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE object_id=? AND event_id IS NULL ORDER BY id", (object_id,)).fetchall()],
            }
            if user["role"] == "claimant":
                # 主张人只看公开且未作废的事件和自己的主张，内部调查材料全部剥离。
                public_events = []
                for e in result["events"]:
                    if e["visibility"] != "public" or e["voided"]:
                        continue
                    ed = self._public_event_view(e)
                    ed["source"] = self._public_source(e["source"]) if e["source"] else None
                    public_events.append(ed)
                result["events"] = public_events
                result["unlinked_evidence"] = []
                result["claims"] = [c for c in result["claims"] if c["claimant_id"] == user_id]
                for c in result["claims"]:
                    c.pop("claimant_id", None)
            return result

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
    server_version = "Provenance/1.1"

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
            d = self._body(); return self._send(201, store.add_source(user, d.get("name", ""), d.get("source_type", ""), d.get("reference", ""), d.get("valid_from"), d.get("valid_until")))
        if len(parts) == 4 and parts[:2] == ["api", "sources"] and method == "POST" and parts[3] == "status":
            d = self._body(); return self._send(200, store.change_source_status(user, int(parts[2]), d.get("status", ""), d.get("reason_public", ""), d.get("internal_note", "")))
        if len(parts) == 4 and parts[:2] == ["api", "sources"] and method == "GET" and parts[3] == "investigation":
            return self._send(200, store.get_source_investigation(user, int(parts[2])))
        if len(parts) == 3 and parts[:2] == ["api", "sources"] and method == "GET":
            return self._send(200, store.get_source(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "events"] and parts[3] == "confirm" and method == "POST":
            return self._send(200, store.confirm_event_source(user, int(parts[2])))
        if parts == ["api", "admin", "upgrade-legacy"] and method == "POST":
            self._body()  # 保留 JSON 请求体约定
            return self._send(200, store.run_admin_upgrade(user))
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            object_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST": return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body(); return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""), d.get("date_end", ""), d.get("place", ""), d.get("description", ""), d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body(); return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""), d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body(); return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET": return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET": return self._send(200, store.history_detail(user, object_id, int(parts[4])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body(); return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""), d.get("note", "")))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try:
            self._dispatch(method)
        except BusinessError as exc:
            error = {"code": exc.code, "message": exc.message}
            error.update(exc.details)
            self._send(exc.status, {"error": error})
        except (ValueError, TypeError):
            self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc:
            self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

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
    parser.add_argument("--upgrade", action="store_true", help="对旧库执行来源状态与历史关联升级")
    args = parser.parse_args(); store = ProvenanceStore(args.db)
    if args.upgrade:
        result = store.init_schema()
        if args.seed: store.seed()
        print(f"旧数据升级完成: {result}")
        return
    result = store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed:
        print(f"数据库已初始化: {args.db}；历史关联升级: {result}")
        return
    server = ProvenanceServer(("127.0.0.1", args.port), store)
    print(f"来源审查系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
