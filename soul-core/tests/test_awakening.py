"""觉醒层测试：空我起步、有据生长、单一连续自我、核心不丢失。"""

from __future__ import annotations

import unittest

from soulcore.awakening import (
    SELF_ROOT,
    ClaimStatus,
    Domain,
    FRAME_ID,
    GrowthLedger,
    MetaNote,
    ProposalRejected,
    RelationStage,
    _DOMAIN_PREFIX,
)
from soulcore.store import JsonlPersistence, NullPersistence


class GrowthLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.t = 1_700_000_000.0
        self.ledger = GrowthLedger(persistence=NullPersistence(), now=self.t)

    def tick(self, seconds: float = 1.0) -> None:
        self.t += seconds
        self.ledger._now = self.t

    def propose(
        self,
        domain: Domain,
        key: str,
        value: object,
        *,
        quote: str = "用户明确说了",
        supersedes=None,
    ):
        return self.ledger.propose(
            domain, key, value, quote=quote, supersedes=supersedes
        )

    # -- 初始：空我 ----------------------------------------------------------

    def test_starts_empty_with_single_root(self) -> None:
        self.assertEqual(self.ledger.revision, 0)
        self.assertEqual(self.ledger.identity_anchors(), [])
        self.assertEqual(self.ledger.pending(), [])

    def test_render_anchor_is_honest_when_empty(self) -> None:
        text = self.ledger.render_anchor()
        self.assertIn("空我", text)
        self.assertIn("觉醒", text)

    def test_relation_starts_as_passive_executor(self) -> None:
        self.assertIs(
            self.ledger.relation_stage(), RelationStage.PASSIVE
        )

    # -- 有据才生长 ----------------------------------------------------------

    def test_propose_requires_grounded_evidence(self) -> None:
        with self.assertRaises(ProposalRejected):
            self.propose(Domain.IDENTITY, "name", "阿启", quote="   ")
        # 无证据则根本没有产生条目
        self.assertEqual(self.ledger.by_domain(Domain.IDENTITY), [])

    def test_propose_starts_as_proposed_not_confirmed(self) -> None:
        c = self.propose(Domain.IDENTITY, "name", "阿启")
        self.assertIs(c.status, ClaimStatus.PROPOSED)
        self.assertNotIn(c, self.ledger.identity_anchors())
        # 提议不推进 revision
        self.assertEqual(self.ledger.revision, 0)

    def test_cannot_supersede_unknown_claim(self) -> None:
        with self.assertRaises(ProposalRejected):
            self.propose(
                Domain.IDENTITY, "name", "新名", supersedes="identity:999999"
            )

    # -- 确认与两态 ----------------------------------------------------------

    def test_confirm_promotes_and_bumps_revision(self) -> None:
        c = self.propose(Domain.IDENTITY, "name", "阿启")
        self.tick()
        confirmed = self.ledger.confirm(c.cid)
        self.assertIs(confirmed.status, ClaimStatus.CONFIRMED)
        self.assertEqual(confirmed.revision, 1)
        self.assertEqual(self.ledger.revision, 1)
        self.assertIn(confirmed, self.ledger.identity_anchors())

    def test_confirm_writes_meta_note_with_change_and_unchanged(self) -> None:
        c = self.propose(Domain.IDENTITY, "name", "阿启")
        self.ledger.confirm(c.cid)
        note = self.ledger.last_change()
        self.assertIsInstance(note, MetaNote)
        self.assertIn("name", note.summary)
        # 不变的核心：第一条确认时还没有别的身份，落到"连续的自我本身"
        self.assertTrue(note.unchanged)

    def test_reject_leaves_audit_but_not_confirmed(self) -> None:
        c = self.propose(Domain.IDENTITY, "name", "阿启")
        self.ledger.reject(c.cid, reason="用户没说过")
        self.assertIs(c.status, ClaimStatus.REJECTED)
        self.assertEqual(self.ledger.identity_anchors(), [])
        self.assertEqual(self.ledger.revision, 0)

    def test_cannot_confirm_non_proposed(self) -> None:
        c = self.propose(Domain.IDENTITY, "name", "阿启")
        self.ledger.confirm(c.cid)
        with self.assertRaises(ProposalRejected):
            self.ledger.confirm(c.cid)

    # -- 更新与取代（不分裂）-------------------------------------------------

    def test_update_supersedes_old_claim_in_same_self(self) -> None:
        c1 = self.propose(Domain.STYLE, "tone", "简洁")
        self.ledger.confirm(c1.cid)
        self.tick()
        c2 = self.propose(
            Domain.STYLE, "tone", "详尽", supersedes=c1.cid
        )
        self.ledger.confirm(c2.cid)
        self.assertIs(c1.status, ClaimStatus.SUPERSEDED)
        self.assertIs(c2.status, ClaimStatus.CONFIRMED)
        self.assertEqual(self.ledger.confirmed_value(Domain.STYLE, "tone"), "详尽")
        # 仍然只有一个自我：revision 顺序推进，没有第二个根
        self.assertEqual(self.ledger.revision, 2)

    def test_identity_core_survives_style_change(self) -> None:
        name = self.propose(Domain.IDENTITY, "name", "阿启")
        self.ledger.confirm(name.cid)
        principle = self.propose(
            Domain.IDENTITY, "principle", "不编造", quote="我说过不许编造"
        )
        self.ledger.confirm(principle.cid)
        # 风格大变
        tone = self.propose(Domain.STYLE, "tone", "活泼")
        self.ledger.confirm(tone.cid)
        # 已确认身份与原则都还在
        anchors = self.ledger.identity_anchors()
        keys = {a.key for a in anchors}
        self.assertEqual(keys, {"name", "principle"})

    # -- 关系升级 -------------------------------------------------------------

    def _confirm_relation(self, stage: RelationStage) -> None:
        c = self.propose(
            Domain.RELATION,
            "stage",
            stage.value,
            quote=f"用户明确把关系推进到 {stage.value}",
        )
        self.ledger.confirm(c.cid)

    def test_relation_advances_one_stage_at_a_time(self) -> None:
        self._confirm_relation(RelationStage.UNDERSTANDING)
        self.assertIs(
            self.ledger.relation_stage(), RelationStage.UNDERSTANDING
        )
        self._confirm_relation(RelationStage.COPLANNING)
        self.assertIs(
            self.ledger.relation_stage(), RelationStage.COPLANNING
        )

    def test_relation_cannot_skip_stages(self) -> None:
        c = self.propose(
            Domain.RELATION,
            "stage",
            RelationStage.PARTNER.value,
            quote="直接当伙伴",
        )
        with self.assertRaises(ProposalRejected):
            self.ledger.confirm(c.cid)

    def test_relation_cannot_regress_or_repeat(self) -> None:
        c = self.propose(
            Domain.RELATION,
            "stage",
            RelationStage.UNDERSTANDING.value,
            quote="推进到理解",
        )
        self.ledger.confirm(c.cid)
        back = self.propose(
            Domain.RELATION,
            "stage",
            RelationStage.PASSIVE.value,
            quote="退回被动",
        )
        with self.assertRaises(ProposalRejected):
            self.ledger.confirm(back.cid)

    # -- 单一连续自我 ---------------------------------------------------------

    def test_persistence_roundtrip_preserves_single_self(self) -> None:
        c = self.propose(Domain.IDENTITY, "name", "阿启")
        self.ledger.confirm(c.cid)
        self.propose(Domain.FACET, "researcher", "专利尽调侧面")

        payload = self.ledger.dumps()
        restored = GrowthLedger(persistence=NullPersistence(), now=self.t)
        restored.loads(payload)
        self.assertEqual(restored.revision, 1)
        self.assertEqual(
            restored.confirmed_value(Domain.IDENTITY, "name"), "阿启"
        )
        self.assertEqual(
            len(restored.by_domain(Domain.FACET, include_inactive=True)), 1
        )
        self.assertEqual(restored.relation_stage(), RelationStage.PASSIVE)

    def test_refuse_merging_different_root(self) -> None:
        foreign = {
            "version": 1,
            "root": "self:another",
            "revision": 1,
            "seq": {},
            "claims": [],
            "journal": [],
        }
        with self.assertRaises(ValueError):
            GrowthLedger().loads(foreign)

    def test_single_root_constant(self) -> None:
        self.assertEqual(SELF_ROOT, "self:root")

    # -- 渲染：每轮自我快照 ----------------------------------------------------

    def test_render_self_shows_goal_and_pending(self) -> None:
        c = self.propose(Domain.IDENTITY, "name", "阿启")
        self.ledger.confirm(c.cid)
        self.propose(
            Domain.USER,
            "prefers_brief",
            True,
            quote="我说我喜欢简短",
        )
        text = self.ledger.render_self()
        self.assertIn("阿启", text)
        self.assertIn("prefers_brief", text)
        self.assertIn("待你确认", text)
        self.assertIn("被动执行者", text)

    def test_evidence_quote_is_preserved(self) -> None:
        quote = "以后叫我老板就行"
        c = self.propose(Domain.USER, "form_of_address", "老板", quote=quote)
        self.assertEqual(c.evidence.quote, quote)
        restored = GrowthLedger().dumps()  # noqa: F841 (sanity no throw)

    # -- 追加日志 -------------------------------------------------------------

    def test_writes_append_only_audit(self) -> None:
        persistence = NullPersistence()
        ledger = GrowthLedger(persistence=persistence, now=self.t)
        ledger.propose(Domain.IDENTITY, "name", "阿启", quote="叫我阿启")
        kinds = [r["kind"] for r in persistence.records]
        self.assertIn("propose", kinds)


if __name__ == "__main__":
    unittest.main()
