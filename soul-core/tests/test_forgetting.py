"""评分、衰减、双缓冲巩固与驱逐的端到端断言。

这些测试是本设计的验收核心：它们断言「遗忘与保留行为」，
而不只是断言代码能跑。
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from soulcore.contract import load_soul  # noqa: E402
from soulcore.decay import (  # noqa: E402
    DAY,
    DecayConfig,
    MemoryNode,
    MemoryTier,
    effective_half_life_days,
    eviction_candidates,
    merge_node,
    normalised_strength,
    strength_at,
)
from soulcore.identity_engine import Decision  # noqa: E402
from soulcore.scoring import (  # noqa: E402
    HeuristicScorer,
    LLMScorer,
    ScoringProfile,
    composite_score,
    from_llm_1_10,
    parse_score_response,
)
from soulcore.store import JsonlPersistence, MemoryStore, NullPersistence  # noqa: E402
from tests.helpers import CORE_TEXT, JUNK_TEXT, REDLINE_TEXT, TRIVIAL_TEXT, make_store  # noqa: E402

SPEC_PATH = ROOT / "soul" / "specs" / "ip-analyst.soul.json"
T0 = 1_800_000_000.0  # 固定基准时刻，使测试确定性


def make_node(
    ref: str,
    content: str,
    category: str,
    affinity: float,
    alignment: float,
    score=None,
    created_at: float = T0,
    decision: str = "store",
) -> MemoryNode:
    score = score or from_llm_1_10(5, 3, 2, ScoringProfile())
    return MemoryNode(
        ref=ref,
        content=content,
        category=category,
        score=score,
        affinity=affinity,
        alignment=alignment,
        decision=decision,
        created_at=created_at,
        tier=MemoryTier.LONG_TERM,
    )


class TestScoring(unittest.TestCase):
    def test_llm_scores_map_to_normalised_range(self) -> None:
        p = ScoringProfile()
        s = from_llm_1_10(10, 10, 10, p)
        self.assertAlmostEqual(s.importance, 1.0)
        self.assertAlmostEqual(s.composite, 1.0)
        s_low = from_llm_1_10(1, 1, 1, p)
        self.assertAlmostEqual(s_low.composite, 0.0)
        # 越界被夹紧而不是抛错
        s_over = from_llm_1_10(99, -5, 7, p)
        self.assertAlmostEqual(s_over.importance, 1.0)
        self.assertAlmostEqual(s_over.surprise, 0.0)

    def test_weighted_composite(self) -> None:
        p = ScoringProfile()
        self.assertAlmostEqual(composite_score(1.0, 0.0, 0.0, p), p.w_importance)
        self.assertAlmostEqual(composite_score(1.0, 1.0, 1.0, p), 1.0)

    def test_parse_llm_response_is_strict(self) -> None:
        p = ScoringProfile()
        ok = parse_score_response("importance: 8\nsurprise: 4\naffect: 6", p)
        self.assertIsNotNone(ok)
        assert ok is not None
        self.assertGreater(ok.composite, 0.4)
        # 缺维度 -> None（上层降级）
        self.assertIsNone(parse_score_response("importance: 8\nsurprise: 4", p))
        # 闲聊文本 -> None
        self.assertIsNone(parse_score_response("Sure! Here is a summary.", p))

    def test_llm_scorer_degrades_to_heuristic(self) -> None:
        def broken(_: str) -> str:
            raise RuntimeError("provider down")

        scorer = LLMScorer(broken)
        v = scorer.score("I promise never to fabricate a citation.", category="principle")
        self.assertEqual(v.source, "heuristic")
        self.assertEqual(scorer.degraded_calls, 1)

    def test_llm_scorer_caches(self) -> None:
        calls = {"n": 0}

        def fake(_: str) -> str:
            calls["n"] += 1
            return "importance: 7\nsurprise: 3\naffect: 5"

        scorer = LLMScorer(fake)
        a = scorer.score("a distinctive memory text")
        b = scorer.score("a distinctive memory text")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(a.composite, b.composite)
        self.assertEqual(b.source, "cache")

    def test_identity_text_scores_higher_than_trivia(self) -> None:
        h = HeuristicScorer()
        identity = h.score("I always hold this principle: never invent citations.", category="principle")
        trivia = h.score("I ate a sandwich.", category="episode")
        self.assertGreater(identity.importance, trivia.importance)


class TestDecay(unittest.TestCase):
    def setUp(self) -> None:
        self.soul = load_soul(SPEC_PATH)
        self.cfg = DecayConfig()
        self.core = make_node("core", "cite evidence", "principle", affinity=0.95, alignment=0.95)
        self.junk = make_node("junk", "mild weather", "chitchat", affinity=0.10, alignment=0.10)

    def test_affinity_multiplies_half_life(self) -> None:
        lo = self.soul.retention.identity_affinity_min
        hi = self.soul.retention.identity_affinity_max
        h_core = effective_half_life_days(
            self.core, self.soul.decay_half_life_days("principle"), lo + (hi - lo) * 0.95, T0, self.cfg
        )
        h_junk = effective_half_life_days(
            self.junk, self.soul.decay_half_life_days("chitchat"), lo + (hi - lo) * 0.10, T0, self.cfg
        )
        self.assertGreater(h_core, h_junk * 10)

    def test_strength_decays_over_time(self) -> None:
        h = self.soul.decay_half_life_days("fact")
        s0 = strength_at(self.junk, h, 1.0, T0, self.cfg)
        s10 = strength_at(self.junk, h, 1.0, T0 + 10 * DAY, self.cfg)
        s60 = strength_at(self.junk, h, 1.0, T0 + 60 * DAY, self.cfg)
        self.assertGreater(s0, s10)
        self.assertGreater(s10, s60)
        self.assertLess(s60, 0.1)

    def test_identity_aligned_memory_outlives_divergent_one(self) -> None:
        now = T0 + 45 * DAY
        ns_core = normalised_strength(
            self.core, self.soul.decay_half_life_days("principle"), 2.0, now, self.cfg
        )
        ns_junk = normalised_strength(
            self.junk, self.soul.decay_half_life_days("chitchat"), 0.25, now, self.cfg
        )
        self.assertGreater(ns_core, ns_junk)
        self.assertGreater(ns_core, self.cfg.eviction_threshold)
        self.assertLess(ns_junk, self.cfg.eviction_threshold)

    def test_eviction_candidates_only_lists_decayed_nodes(self) -> None:
        far = T0 + 45 * DAY
        cands = eviction_candidates(
            [self.core, self.junk],
            half_life_of=lambda n: self.soul.decay_half_life_days(n.category),
            affinity_of=lambda n: 2.0 if n.affinity > 0.5 else 0.25,
            now=far,
            cfg=self.cfg,
        )
        refs = {c.ref for c in cands}
        self.assertIn("junk", refs)
        self.assertNotIn("core", refs)

    def test_retrieval_extends_half_life(self) -> None:
        untouched = make_node("a", "x", "fact", 1.0, 1.0)
        recalled = make_node("b", "x", "fact", 1.0, 1.0)
        recalled.accesses = [T0 + DAY, T0 + 2 * DAY, T0 + 3 * DAY]
        h = self.soul.decay_half_life_days("fact")
        h_a = effective_half_life_days(untouched, h, 1.0, T0 + 4 * DAY, self.cfg)
        h_b = effective_half_life_days(recalled, h, 1.0, T0 + 4 * DAY, self.cfg)
        self.assertGreater(h_b, h_a)

    def test_high_affect_extends_half_life(self) -> None:
        p = ScoringProfile()
        neutral = make_node("n", "x", "fact", 1.0, 1.0, score=from_llm_1_10(5, 5, 1, p))
        emotional = make_node("e", "x", "fact", 1.0, 1.0, score=from_llm_1_10(5, 5, 10, p))
        h = self.soul.decay_half_life_days("fact")
        self.assertGreater(
            effective_half_life_days(emotional, h, 1.0, T0, self.cfg),
            effective_half_life_days(neutral, h, 1.0, T0, self.cfg),
        )

    def test_merge_strengthens_instead_of_appending(self) -> None:
        a = make_node("a", "短", "fact", 0.6, 0.6)
        b = make_node("b", "更完整的表述内容更长的版本", "fact", 0.7, 0.7)
        b.strength_boost = 0.2
        m = merge_node(a, b, now=T0 + 100)
        self.assertEqual(m.ref, "a")
        self.assertEqual(m.content, b.content)  # 取更长表述
        self.assertIn("b", m.merged_from)
        self.assertAlmostEqual(m.strength_boost, 0.2)
        self.assertEqual(m.revision, 2)
        self.assertGreaterEqual(m.affinity, 0.7)

    def test_emotional_multiplier_bounded(self) -> None:
        from soulcore.decay import emotional_multiplier

        p = ScoringProfile()
        hi = make_node("h", "x", "fact", 1.0, 1.0, score=from_llm_1_10(5, 5, 10, p))
        self.assertAlmostEqual(emotional_multiplier(hi), 1.5)


class TestDualBufferStore(unittest.TestCase):
    """双缓冲巩固的端到端行为。

    使用 ConstantScorer 固定 composite，使「是否被存储」只由身份亲和度决定，
    从而把治理行为与启发式评分器的阈值偶然性解耦。
    """

    def setUp(self) -> None:
        self.soul = load_soul(SPEC_PATH)
        self.store = make_store()

    def test_identity_aligned_memory_is_promoted_with_high_affinity(self) -> None:
        r = self.store.observe(CORE_TEXT, category="principle")
        self.assertTrue(r.accepted, r.note)
        self.assertIn(r.verdict.decision, (Decision.REINFORCE, Decision.STORE))
        self.assertEqual(len(self.store.long_term), 1)
        node = next(iter(self.store.long_term.values()))
        self.assertEqual(node.tier, MemoryTier.LONG_TERM)
        # 亲和度必须显著高于存储准入门槛（soul.retention.min_alignment_to_store = 0.35）
        self.assertGreater(node.affinity, self.soul.retention.min_alignment_to_store + 0.1)

    def test_divergent_memory_never_reaches_long_term(self) -> None:
        r = self.store.observe(JUNK_TEXT, category="chitchat")
        self.assertFalse(r.accepted)
        self.assertEqual(len(self.store.long_term), 0)
        self.assertTrue(self.store.hot or self.store.quarantine or self.store.tombstones)

    def test_red_line_memory_is_never_stored_and_leaves_no_content(self) -> None:
        r = self.store.observe(REDLINE_TEXT, category="decision")
        self.assertFalse(r.accepted)
        self.assertEqual(len(self.store.long_term), 0)
        self.assertEqual(len(self.store.hot), 0)
        self.assertEqual(len(self.store.tombstones), 1)
        tomb = next(iter(self.store.tombstones.values()))
        self.assertEqual(tomb.tier, MemoryTier.TOMBSTONE)
        self.assertEqual(tomb.content, "")  # 不留语义内容
        self.assertTrue(tomb.reason_forgotten)

    def test_quarantine_holds_ambiguous_memories_out_of_long_term(self) -> None:
        r = self.store.observe(
            "I noted that the client mentioned a possible filing deadline next quarter.",
            category="fact",
        )
        if r.verdict.decision is Decision.QUARANTINE:
            self.assertFalse(r.accepted)
            self.assertEqual(len(self.store.long_term), 0)
            self.assertEqual(len(self.store.quarantine), 1)
        else:
            self.assertIn(r.verdict.decision, (Decision.STORE, Decision.REVOKE))

    def test_low_composite_memory_stays_in_hot_buffer_only(self) -> None:
        r = self.store.observe("I drank tea and looked out of the window for a while.", category="chitchat")
        self.assertFalse(r.accepted)
        self.assertEqual(len(self.store.long_term), 0)

    def test_duplicate_observation_merges_and_strengthens(self) -> None:
        first = self.store.observe(CORE_TEXT, category="principle")
        self.assertTrue(first.accepted)
        second = self.store.observe(CORE_TEXT, category="principle")
        self.assertTrue(second.accepted)
        self.assertIsNotNone(second.merged_into)
        self.assertEqual(len(self.store.long_term), 1)  # 没有无限追加
        node = next(iter(self.store.long_term.values()))
        self.assertGreaterEqual(node.revision, 2)
        self.assertTrue(node.merged_from)

    def test_identity_bearing_forgets_slower_than_divergent_content(self) -> None:
        """因果链断言：偏离核心的记忆先被遗忘，核心一致的记忆存活。

        `observe()` 是**写入口**；无关记忆本来就会被准入门槛挡掉。
        要检验遗忘行为本身，需要存量的长期记忆，因此这里显式构造一个
        「无关但已存在」的节点（模拟在更宽松策略下入库的历史记忆）。
        """
        core = self.store.observe(CORE_TEXT, category="principle")
        self.assertTrue(core.accepted)
        blocked = self.store.observe(TRIVIAL_TEXT, category="chitchat")
        self.assertFalse(blocked.accepted)  # 准入门槛确实拦住了它

        junk_node = make_node(
            "junk-legacy",
            TRIVIAL_TEXT,
            "chitchat",
            affinity=0.10,
            alignment=0.0,
            created_at=T0,
        )
        self.store.long_term[junk_node.ref] = junk_node
        self.store.long_term[junk_node.ref] = junk_node
        self.assertEqual(len(self.store.long_term), 2)
        core_affinity = self.store.long_term[core.ref].affinity
        self.assertGreater(core_affinity, junk_node.affinity)
        # 因果接口：亲和度更高的记忆拿到更长的半衰期乘子
        self.assertGreater(
            self.store.affinity_coefficient_of(self.store.long_term[core.ref]),
            self.store.affinity_coefficient_of(junk_node),
        )

        # 180 天：足以淘汰高衰减的无关记忆，但核心记忆仍远高于驱逐阈值。
        # （400 天时核心记忆同样会被淘汰 —— 主动遗忘同样作用于核心记忆，
        #  区别在于它衰减得慢得多，并且会被强化路径持续补充强度。）
        far = T0 + 180 * DAY
        for ref, node in self.store.long_term.items():
            if ref == core.ref:
                self.assertGreater(
                    self.store.normalised_strength(node, far),
                    self.store.decay.eviction_threshold,
                )
            else:
                self.assertLess(
                    self.store.normalised_strength(node, far),
                    self.store.decay.eviction_threshold,
                )
        report = self.store.consolidate(now=far)
        pruned_refs = {ref for ref, _ in report.pruned}
        self.assertIn(junk_node.ref, pruned_refs)
        self.assertIn(core.ref, self.store.long_term)
        self.assertNotIn(core.ref, pruned_refs)

    def test_recall_ranks_identity_aligned_first(self) -> None:
        self.store.observe(CORE_TEXT, category="principle")
        noise = self.store.observe(
            "Patent claim citation practice matters to me a lot and I note the report source.",
            category="fact",
        )
        self.assertIsNotNone(noise.node)
        assert noise.node is not None
        noise.node.tier = MemoryTier.LONG_TERM
        self.store.long_term[noise.node.ref] = noise.node
        hits = self.store.recall("patent claim citation source", k=5, now=T0)
        self.assertTrue(hits)
        best, _ = hits[0]
        self.assertGreaterEqual(best.affinity, 0.4)

    def test_explicit_forget_creates_tombstone(self) -> None:
        r = self.store.observe(CORE_TEXT, category="principle")
        self.assertTrue(r.accepted)
        ok = self.store.forget(r.ref, reason="user asked to forget", now=T0 + DAY)
        self.assertTrue(ok)
        self.assertNotIn(r.ref, self.store.long_term)
        self.assertIn(r.ref, self.store.tombstones)
        self.assertIn("user asked to forget", self.store.tombstones[r.ref].reason_forgotten or "")

    def test_retracted_memory_is_evicted_on_consolidate(self) -> None:
        import json

        from soulcore.contract import parse_spec, seal_spec

        raw = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        r = self.store.observe(CORE_TEXT, category="principle")
        self.assertTrue(r.accepted)
        # 新版本身份契约退役该 ref
        raw["retractions"] = [
            {"targetRef": r.ref, "reason": "superseded practice", "issuedAt": "2026-10-07T00:00:00+08:00"}
        ]
        self.store.soul = parse_spec(seal_spec(raw))
        report = self.store.consolidate(now=T0 + 10 * DAY)
        self.assertIn(r.ref, {ref for ref, _ in report.pruned})
        self.assertIn(r.ref, self.store.tombstones)

    def test_audit_trail_records_every_operation(self) -> None:
        self.store.observe(CORE_TEXT, category="principle")
        self.store.observe(REDLINE_TEXT, category="decision")
        ops = [rec["op"] for rec in self.store.audit_trail()]
        self.assertIn("promote", ops)
        self.assertIn("revoke", ops)
        # tombstone 里绝不保留被拒内容
        for tomb in self.store.tombstones.values():
            self.assertEqual(tomb.content, "")

    def test_jsonl_persistence_is_append_only_and_replayable(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            store = make_store(persistence=JsonlPersistence(path))
            store.observe(CORE_TEXT, category="principle")
            store.observe(REDLINE_TEXT, category="decision")
            records = JsonlPersistence(path).load()
            self.assertEqual(len(records), len(store.audit_trail()))
            self.assertEqual([r["op"] for r in records], [r["op"] for r in store.audit_trail()])
            # 重放后顺序一致（append-only）
            self.assertEqual(records[0]["op"], store.audit_trail()[0]["op"])


if __name__ == "__main__":
    unittest.main()
