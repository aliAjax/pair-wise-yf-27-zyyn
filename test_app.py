import base64
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

from app import BusinessError, ProvenanceStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_case(self, claim_path=("under_review", "negotiating", "resolved_return")):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        for status, note in zip(
            claim_path,
            ["材料齐全，进入调查。", "双方开始协商返还安排。", "签署返还协议。"],
        ):
            self.store.transition_claim("reviewer1", claim["id"], status, note)
        current = self.store.get_object("staff", obj["id"])
        return source, {"id": obj["id"], "version": current["version"]}, event, claim

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        self.assertEqual(len(evidence["sha256"]), 64)
        updated = self.store.update_object("staff", obj["id"], {"public_summary": "已完成首轮来源整理。"})
        self.assertEqual(updated["version"], 3)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 6)

    def test_visibility_and_claim_stage_invariants(self):
        obj = self.store.create_object("staff", "M-2001-2", "手稿", "纸质", "资料室", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "归还手稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接结束。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertNotIn("claimant_id", self.store.get_object("public", obj["id"])["claims"][0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("public", obj["id"], "note", "2020-01-01", "", "馆内", "未授权事件", None, "public")
        self.assertEqual(ctx.exception.status, 403)

    # ---- 来源撤回/存疑 -------------------------------------------------

    def test_withdrawn_source_voids_events_and_holds_resolved_return(self):
        source, obj, event, claim = self._make_case()
        pre_withdraw_version = obj["version"]  # 撤回前最后一个历史版本

        result = self.store.change_source_status(
            "reviewer1", source["id"], "withdrawn",
            reason_public="该档案经鉴定系后人伪造。",
            internal_note="笔迹比对与印章核验均不支持其真实性，联系原出具单位后确认撤回。",
        )
        self.assertEqual(result["voided_events"], [event["id"]])
        self.assertEqual(result["held_claims"], [claim["id"]])

        detail = self.store.get_object("reviewer1", obj["id"])
        self.assertEqual(detail["events"][0]["voided"], 1)
        self.assertEqual(detail["events"][0]["source"]["status"], "withdrawn")
        self.assertEqual(detail["claims"][0]["status"], "review_hold")
        self.assertIn("挂起", detail["claims"][0]["reviews"][-1]["note"])

        # 公众页不再展示作废事件，持有人等内部字段依旧不可见；来源只给公开字段。
        public = self.store.get_object("public", obj["id"])
        self.assertEqual(public["events"], [])
        self.assertNotIn("current_holder", public)
        self.assertEqual(public["claims"][0]["status"], "review_hold")
        # 来源公开列表/详情不含内部调查说明，但能看到状态与公开原因。
        public_source = next(s for s in self.store.list_sources("public") if s["id"] == source["id"])
        self.assertEqual(public_source["status"], "withdrawn")
        self.assertIn("伪造", public_source["status_reason_public"])
        self.assertNotIn("笔迹", json.dumps(public_source))

        # 历史版本保留撤回当时之前的记录：事件未作废、来源仍为 active。
        old = self.store.history_detail("reviewer1", obj["id"], pre_withdraw_version)["snapshot"]
        self.assertEqual(old["events"][0]["voided"], 0)
        self.assertEqual(old["events"][0]["source"]["status"], "active")
        new = self.store.history_detail("reviewer1", obj["id"], pre_withdraw_version + 1)["snapshot"]
        self.assertEqual(new["events"][0]["voided"], 1)
        self.assertEqual(new["events"][0]["source"]["status"], "withdrawn")

        # 挂起的裁定复核后可以退回重审。
        review = self.store.transition_claim("reviewer2", claim["id"], "under_review", "依据已失效，重新审查。")
        self.assertEqual(review["old_status"], "review_hold")

    def test_doubtful_source_resets_open_claims_and_flags_new_events(self):
        source, obj, event, claim = self._make_case(claim_path=("under_review", "negotiating"))
        result = self.store.change_source_status(
            "reviewer1", source["id"], "doubtful",
            reason_public="该来源真实性正在核查。",
            internal_note="出具单位回函与档案编号对不上，需要进一步比对。",
        )
        self.assertEqual(result["voided_events"], [event["id"]])
        self.assertEqual(result["reset_claims"], [claim["id"]])
        self.assertEqual(result["held_claims"], [])
        self.assertEqual(self.store.get_object("staff", obj["id"])["claims"][0]["status"], "under_review")

        # 不能引用已撤回来源；引用存疑来源可以录入但挂待确认。
        other = self.store.add_source("staff", "补充记录", "ledger", "LD-2020-3")
        self.store.change_source_status("reviewer1", other["id"], "withdrawn",
                                        internal_note="发现与已知伪造批次同源，撤回。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("staff", obj["id"], "note", "2020-02-02", "", "馆内", "引用撤回来源", other["id"], "internal")
        self.assertEqual(ctx.exception.code, "source_not_usable")
        flagged = self.store.add_event("staff", obj["id"], "note", "2020-02-02", "", "馆内", "暂引存疑来源", source["id"], "internal")
        self.assertEqual(flagged["needs_confirmation"], 1)
        confirmed = self.store.confirm_event_source("reviewer1", flagged["id"])
        self.assertEqual(confirmed["needs_confirmation"], 0)

        # 存疑可以恢复有效，恢复不再触发级联。
        restored = self.store.change_source_status(
            "reviewer1", source["id"], "active", internal_note="核查完成，来源真实有效。"
        )
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["voided_events"], [])

    def test_investigation_notes_are_internal_only(self):
        source = self.store.add_source("staff", "机密档案", "archive", "SEC-1")
        self.store.change_source_status("reviewer1", source["id"], "doubtful",
                                        reason_public="核查中。", internal_note="内部线索：线人证词反复。")
        inv = self.store.get_source_investigation("reviewer1", source["id"])
        self.assertIn("线人", inv["internal_investigation"])
        full = self.store.get_source("staff", source["id"])
        self.assertIn("internal_investigation", full)
        self.assertIn("internal_note", json.dumps(full["status_log"][0]))

        for viewer in ("public", "claimant1"):
            with self.assertRaises(BusinessError) as ctx:
                self.store.get_source_investigation(viewer, source["id"])
            self.assertEqual(ctx.exception.status, 403)
            redacted = self.store.get_source(viewer, source["id"])
            self.assertNotIn("internal_investigation", redacted)
            self.assertNotIn("线人", json.dumps(redacted))

        # 只有审查员能撤回/标疑来源；staff、公众都不行。
        with self.assertRaises(BusinessError) as ctx:
            self.store.change_source_status("staff", source["id"], "withdrawn", internal_note="工作人员无权撤回。")
        self.assertEqual(ctx.exception.status, 403)

    def test_concurrent_withdrawal_first_wins(self):
        source = self.store.add_source("staff", "竞态档案", "archive", "RACE-1")
        barrier = threading.Barrier(2)
        outcomes = {}

        def withdraw(actor):
            barrier.wait()
            try:
                outcomes[actor] = ("ok", self.store.change_source_status(
                    actor, source["id"], "withdrawn", internal_note=f"{actor} 提交撤回决定。"))
            except BusinessError as exc:
                outcomes[actor] = ("conflict", exc)

        t1 = threading.Thread(target=withdraw, args=("reviewer1",))
        t2 = threading.Thread(target=withdraw, args=("reviewer2",))
        t1.start(); t2.start(); t1.join(); t2.join()

        winners = [a for a, v in outcomes.items() if v[0] == "ok"]
        losers = [a for a, v in outcomes.items() if v[0] == "conflict"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        loser_err = outcomes[losers[0]][1]
        self.assertEqual(loser_err.status, 409)
        self.assertEqual(loser_err.details["current_status"], "withdrawn")
        self.assertEqual(loser_err.details["changed_by"], winners[0])
        self.assertTrue(loser_err.details["changed_at"])

        # 已经撤回的来源不能再改任何状态。
        with self.assertRaises(BusinessError) as ctx:
            self.store.change_source_status("reviewer2", source["id"], "active", internal_note="试图翻案。")
        self.assertEqual(ctx.exception.code, "source_status_conflict")
        self.assertEqual(ctx.exception.details["changed_by"], winners[0])


class LegacyUpgradeTests(unittest.TestCase):
    """用旧版表结构建库，验证启动升级的回填与挂待确认逻辑。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE users(
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public')));
            CREATE TABLE sources(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                source_type TEXT NOT NULL, reference TEXT NOT NULL,
                created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                UNIQUE(name,reference));
            CREATE TABLE objects(
                id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_no TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL, object_type TEXT NOT NULL, current_holder TEXT NOT NULL,
                public_summary TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL);
            CREATE TABLE events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL REFERENCES objects(id),
                event_type TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT,
                place TEXT NOT NULL, description TEXT NOT NULL,
                source_id INTEGER REFERENCES sources(id),
                visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
            CREATE TABLE evidence(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL REFERENCES objects(id),
                event_id INTEGER REFERENCES events(id), filename TEXT NOT NULL,
                sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                uploaded_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL);
            CREATE TABLE claims(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL REFERENCES objects(id),
                claimant_id TEXT NOT NULL REFERENCES users(id),
                claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'submitted'
                    CHECK(status IN ('submitted','under_review','negotiating','resolved_return','rejected')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE claim_reviews(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id INTEGER NOT NULL REFERENCES claims(id),
                reviewer_id TEXT NOT NULL REFERENCES users(id),
                old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                note TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE object_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                object_id INTEGER NOT NULL REFERENCES objects(id),
                version INTEGER NOT NULL, snapshot TEXT NOT NULL,
                changed_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                UNIQUE(object_id,version));
            CREATE TABLE audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER REFERENCES objects(id),
                actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                detail TEXT NOT NULL, created_at TEXT NOT NULL);
            """
        )
        conn.executemany(
            "INSERT INTO users(id,name,role) VALUES(?,?,?)",
            [("staff", "藏品研究员", "staff"),
             ("reviewer1", "审查员甲", "reviewer"),
             ("reviewer2", "审查员乙", "reviewer"),
             ("claimant1", "主张人", "claimant"),
             ("public", "公众", "public")],
        )

        def iso(d):
            return f"{d.isoformat()}T00:00:00+00:00"

        # 三个来源在不同年份录入，升级时以录入时间作为生效起点。
        conn.execute("INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES('早期档案','archive','S1','staff',?)",
                     (iso(date(1990, 5, 1)),))
        conn.execute("INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES('中期档案','archive','S2','staff',?)",
                     (iso(date(2000, 1, 1)),))
        conn.execute("INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES('晚期档案','archive','S3','staff',?)",
                     (iso(date(2010, 3, 1)),))
        ts = iso(date(2015, 1, 1))
        conn.execute(
            """INSERT INTO objects(inventory_no,title,object_type,current_holder,public_summary,created_by,created_at,updated_at)
               VALUES('M-OLD-1','旧藏','器物','馆内','公开摘要','staff',?,?)""",
            (ts, ts),
        )
        # e1(1995)：唯一有效来源 S1 → 回填并挂待确认。
        # e2(2005)：S1、S2 同时有效 → 不猜，仅挂待确认。
        # e3(1985)：无有效来源 → 仅挂待确认。
        # e4：旧手工关联 S2 → 保留关联，挂待确认。
        for eid, ds, sid in [
            ("1995 购入", "1995-06-01", None),
            ("2005 出借", "2005-06-01", None),
            ("1985 著录", "1985-06-01", None),
            ("2012 展览", "2012-06-01", 2),
        ]:
            conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,place,description,source_id,visibility,created_by,created_at)
                   VALUES(1,'note',?,'本市',?,?,'internal','staff',?)""",
                (ds, eid, sid, ts),
            )
        conn.commit()
        conn.close()
        self.store = ProvenanceStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_backfill_and_pending_flags(self):
        self.store.init_schema()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = {r["description"]: r for r in conn.execute("SELECT description,source_id,needs_confirmation FROM events ORDER BY id")}
        self.assertEqual(rows["1995 购入"]["source_id"], 1)
        self.assertEqual(rows["1995 购入"]["needs_confirmation"], 1)
        self.assertIsNone(rows["2005 出借"]["source_id"])
        self.assertEqual(rows["2005 出借"]["needs_confirmation"], 1)
        self.assertIsNone(rows["1985 著录"]["source_id"])
        self.assertEqual(rows["1985 著录"]["needs_confirmation"], 1)
        self.assertEqual(rows["2012 展览"]["source_id"], 2)
        self.assertEqual(rows["2012 展览"]["needs_confirmation"], 1)
        s1 = conn.execute("SELECT valid_from,status FROM sources WHERE id=1").fetchone()
        self.assertEqual(s1["valid_from"], "1990-05-01")
        self.assertEqual(s1["status"], "active")
        # claims 表已重建，支持 review_hold 新状态。
        cols = conn.execute("SELECT sql FROM sqlite_master WHERE name='claims'").fetchone()[0]
        self.assertIn("review_hold", cols)
        conn.close()

        # 人工确认回填的关联后，待确认标记摘掉。
        confirmed = self.store.confirm_event_source("staff", 1)
        self.assertEqual(confirmed["needs_confirmation"], 0)
        self.assertEqual(confirmed["source_id"], 1)

        # 升级幂等：第二次运行不重复回填。
        again = self.store.run_admin_upgrade("staff")
        self.assertEqual(again["backfilled"], 0)
        self.assertEqual(again["skipped"], "already_upgraded")
        with self.assertRaises(BusinessError) as ctx:
            self.store.run_admin_upgrade("reviewer1")
        self.assertEqual(ctx.exception.status, 403)

        # 升级后撤回旧来源，级联同样适用于回填事件之外的正常流程。
        result = self.store.change_source_status(
            "reviewer1", 1, "withdrawn", internal_note="旧档案经鉴定系伪造，撤回全部引用。"
        )
        self.assertIn(1, result["voided_events"])


if __name__ == "__main__":
    unittest.main()
