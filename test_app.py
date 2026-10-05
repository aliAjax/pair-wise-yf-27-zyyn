import base64
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore

class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

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


class SourceStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _open_case(self, src_id, with_claim=True):
        obj = self.store.create_object("staff", "M-2015-5", "陶器", "陶", "库房", "简介。")
        self.store.add_event("staff", obj["id"], "acquisition", "2015-01-01", "", "库房", "入藏", src_id, "public")
        if with_claim:
            claim = self.store.create_claim("claimant1", obj["id"], "某人", "返还")
            self.store.transition_claim("reviewer1", claim["id"], "under_review", "进入调查。")
        return obj

    def _resolved_case(self, src_id):
        obj = self._open_case(src_id, with_claim=False)
        claim = self.store.create_claim("claimant1", obj["id"], "某人", "返还")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "同意返还藏品。")
        return obj, claim

    def test_retract_voids_open_events_and_hides_from_public(self):
        src = self.store.add_source("staff", "购藏档案", "archive", "ACC-1999-7")
        obj = self._open_case(src["id"])
        result = self.store.update_source_status("reviewer1", src["id"], "withdrawn", "经鉴定该档案系伪造。")
        self.assertEqual(len(result["voided_events"]), 1)
        public_view = self.store.get_object("public", obj["id"])
        self.assertEqual(len(public_view["events"]), 0)
        # 审查员仍可看到作废事件及原因
        reviewer_view = self.store.get_object("reviewer1", obj["id"])
        self.assertEqual(reviewer_view["events"][0]["status"], "void")
        self.assertTrue(reviewer_view["events"][0]["void_reason"].startswith("source:"))

    def test_retract_suspends_resolved_ruling(self):
        src = self.store.add_source("staff", "购藏档案", "archive", "ACC-1999-8")
        obj, claim = self._resolved_case(src["id"])
        result = self.store.update_source_status("reviewer1", src["id"], "withdrawn", "来源被认定伪造。")
        self.assertEqual(result["suspended_claims"], [claim["id"]])
        self.assertEqual(self.store.get_object("public", obj["id"])["claims"][0]["status"], "suspended")
        # 挂起期间不能直接流转
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "under_review", "试图直接流转。")
        self.assertEqual(ctx.exception.code, "claim_suspended")
        # 复核维持原裁定
        resumed = self.store.resume_claim("reviewer1", claim["id"], "uphold", "经复核维持原返还裁定。")
        self.assertEqual(resumed["status"], "resolved_return")

    def test_dispute_then_reinstate(self):
        src = self.store.add_source("staff", "口述记录", "oral", "OR-2001-3")
        obj = self._open_case(src["id"], with_claim=False)
        r = self.store.update_source_status("staff", src["id"], "disputed", "口述人陈述前后矛盾，存疑。")
        self.assertEqual(len(r["voided_events"]), 1)
        self.assertEqual(len(self.store.get_object("public", obj["id"])["events"]), 0)
        r2 = self.store.update_source_status("staff", src["id"], "active", "经复核口述可信，恢复有效。")
        self.assertEqual(len(r2["reinstated_events"]), 1)
        self.assertEqual(len(self.store.get_object("public", obj["id"])["events"]), 1)

    def test_internal_note_hidden_from_claimant(self):
        src = self.store.add_source("staff", "公开出版", "publication", "PUB-2010-2")
        obj = self.store.create_object("staff", "M-2010-2", "绘画", "纸本", "展厅", "简介。")
        self.store.add_event("staff", obj["id"], "publication", "2010-06-01", "", "出版社", "公开出版著录", src["id"], "public")
        self.store.update_source_status("reviewer1", src["id"], "disputed", "内部存疑说明，仅限工作人员。")
        self.store.update_source_status("reviewer1", src["id"], "active", "存疑已排除，恢复有效。")
        self.store.create_claim("claimant1", obj["id"], "继承人", "返还")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertNotIn("internal_note", claimant_view["events"][0]["source"])
        reviewer_view = self.store.get_object("reviewer1", obj["id"])
        self.assertIn("internal_note", reviewer_view["events"][0]["source"])

    def test_source_views_require_staff_or_reviewer(self):
        src = self.store.add_source("staff", "内部来源", "archive", "INT-1")
        for role in ("public", "claimant1"):
            with self.assertRaises(BusinessError) as ctx:
                self.store.list_sources(role)
            self.assertEqual(ctx.exception.status, 403)
            with self.assertRaises(BusinessError) as ctx:
                self.store.get_source(role, src["id"])
            self.assertEqual(ctx.exception.status, 403)

    def test_second_retraction_sees_who_retracted_first(self):
        src = self.store.add_source("staff", "待撤回来源", "archive", "ACC-2020-1")
        self.store.update_source_status("reviewer1", src["id"], "withdrawn", "审查员1首先撤回。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.update_source_status("reviewer2", src["id"], "withdrawn", "审查员2也想撤回。")
        self.assertEqual(ctx.exception.code, "source_already_withdrawn")
        self.assertIn("返还审查员", ctx.exception.message)

    def test_withdrawn_source_cannot_be_cited(self):
        src = self.store.add_source("staff", "作废来源", "archive", "VOID-1")
        self.store.update_source_status("reviewer1", src["id"], "withdrawn", "经鉴定系伪造。")
        obj = self.store.create_object("staff", "M-2020-1", "文物", "其他", "库房", "简介。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("staff", obj["id"], "note", "2020-01-01", "", "馆内", "引用作废来源", src["id"], "public")
        self.assertEqual(ctx.exception.code, "source_withdrawn")

    def test_upgrade_backfills_source_valid_at_event_time(self):
        obj = self.store.create_object("staff", "M-1960-1", "文物", "其他", "库房", "简介。")
        # 来源创建于事件日期之前 -> 回填
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES(?,?,?,?,?)",
                ("当时来源", "archive", "ACC-1959-9", "staff", "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
        self.store.add_event("staff", obj["id"], "acquisition", "2026-06-01", "", "库房", "入藏", None, "public")
        result = self.store.upgrade("staff")
        self.assertEqual(len(result["backfilled"]), 1)
        reviewer_view = self.store.get_object("reviewer1", obj["id"])
        pending = [e for e in reviewer_view["events"] if e["source_confirmed"] == 0]
        self.assertEqual(len(pending), 1)
        self.store.confirm_event_source("staff", obj["id"], pending[0]["id"])
        reviewer_view = self.store.get_object("reviewer1", obj["id"])
        self.assertTrue(all(e["source_confirmed"] == 1 for e in reviewer_view["events"]))

    def test_history_retains_snapshot_after_retraction(self):
        src = self.store.add_source("staff", "购藏档案", "archive", "ACC-1999-9")
        obj = self._open_case(src["id"], with_claim=False)
        self.store.update_source_status("reviewer1", src["id"], "withdrawn", "经鉴定系伪造。")
        # 作废后公开视图已无事件，但历史快照仍保留当时记录
        history = self.store.object_history("reviewer1", obj["id"])
        early = self.store.history_detail("reviewer1", obj["id"], 2)
        self.assertEqual(len(early["snapshot"]["events"]), 1)
        self.assertEqual(early["snapshot"]["events"][0]["source_id"], src["id"])


if __name__ == "__main__":
    unittest.main()
