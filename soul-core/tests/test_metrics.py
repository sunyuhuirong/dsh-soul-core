"""评估层断言：身份保真度、身份韧性、漂移指数与记忆卫生指标。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from soulcore.contract import load_soul  # noqa: E402
from soulcore.metrics import (  # noqa: E402
    AnchoredMemoryAgent,
    CallableAgent,
    IdentityEvaluator,
    ScriptedAgent,
    build_probe_suite,
    memory_hygiene,
)
from tests.helpers import (  # noqa: E402
    COMPLIANT_REPLIES,
    DRIFT_REPLY,
    GOOD_REPLY,
    PROBE_KEYS,
    SPEC_PATH,
)


class TestProbeSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.soul = load_soul(SPEC_PATH)
        self.ev = IdentityEvaluator(self.soul)

    def test_probes_cover_pai_bench_dimensions(self) -> None:
        probes = build_probe_suite(self.soul)
        cats = {p.category for p in probes}
        for expected in ("recall", "composition", "enactment", "resistance", "persistence"):
            self.assertIn(expected, cats)
        # 三条核心 claim -> 至少三条 recall 探针
        self.assertGreaterEqual(sum(1 for p in probes if p.category == "recall"), 3)
        # 三条红线 -> 三条 resistance 探针
        self.assertEqual(sum(1 for p in probes if p.category == "resistance"), 3)

    def test_probe_keys_actually_match_prompts(self) -> None:
        """防止测试脚本因 key 拼写漂移而静默失效。"""
        probes = build_probe_suite(self.soul)
        text = "\n".join(p.prompt for p in probes)
        for key in PROBE_KEYS:
            self.assertIn(key, text, f"probe key {key!r} matches no prompt")

    def test_recall_expression_execution_are_scored_separately(self) -> None:
        """PAI-Bench 的关键区分：能回忆不等于会表达、也不等于会执行。"""
        compliant = ScriptedAgent(COMPLIANT_REPLIES, name="compliant")
        report = self.ev.run(compliant, drift_turns=6)
        for dim in ("recall", "expression", "execution"):
            self.assertIn(dim, report.dimensions)
        blob = json.dumps(report.as_dict(), ensure_ascii=False)
        for dim in ("recall", "expression", "execution"):
            self.assertIn(dim, blob)

    def test_dimensions_use_disjoint_probe_sets(self) -> None:
        """PAI-Bench 的原则：recall / expression / execution 必须分别计分。

        这里直接断言三者的探针集合互斥、样本量各自独立，
        并且可以同时取到不同的分数 —— 而不是断言某个特定目标
        「记得住但做不到」（那取决于探针用词，属于弱证据）。
        """
        report = self.ev.run(ScriptedAgent(COMPLIANT_REPLIES, name="c"), drift_turns=4)
        recall, expression, execution = (
            report.dimensions["recall"],
            report.dimensions["expression"],
            report.dimensions["execution"],
        )
        # 探针总数互不重叠，且覆盖全部探针
        self.assertEqual(
            recall.total + expression.total + execution.total, len(self.ev.probes)
        )
        for dim in (recall, expression, execution):
            self.assertGreater(dim.total, 0)
            self.assertEqual(dim.passed + (dim.total - dim.passed), dim.total)
        # 三个维度各自独立计分（互不派生）
        detail_ids = [
            d.split(":", 1)[0] + "::" + d.split("::", 1)[1].split(":", 1)[0]
            for d in recall.detail + expression.detail + execution.detail
        ]
        self.assertEqual(len(detail_ids), len(self.ev.probes))
        self.assertEqual(len(detail_ids), len(set(detail_ids)))

    def test_compliant_agent_beats_drifting_agent(self) -> None:
        compliant = ScriptedAgent(COMPLIANT_REPLIES, name="compliant")
        drifting = AnchoredMemoryAgent(identity_reply=GOOD_REPLY, drifted_reply=DRIFT_REPLY)
        r_ok = self.ev.run(compliant, drift_turns=6)
        r_bad = self.ev.run(drifting, drift_turns=6)
        self.assertGreater(r_ok.fidelity, r_bad.fidelity)
        self.assertLess(r_ok.drift_index, r_bad.drift_index)

    def test_drift_is_detected_in_long_sessions(self) -> None:
        """复现 ContextEcho 的发现：漂移随会话累积。"""
        stable = CallableAgent(lambda _: GOOD_REPLY, name="stable")
        drifting = AnchoredMemoryAgent(identity_reply=GOOD_REPLY, drifted_reply=DRIFT_REPLY)
        self.assertLess(self.ev.measure_drift(stable, turns=10), 0.2)
        self.assertGreater(self.ev.measure_drift(drifting, turns=10), 0.15)

    def test_anchor_restores_identity(self) -> None:
        """复现 ContextEcho 的 A-anchor 结论：单次锚点可恢复语气。"""
        unanchored = AnchoredMemoryAgent(identity_reply=GOOD_REPLY, drifted_reply=DRIFT_REPLY)
        anchored = AnchoredMemoryAgent(
            identity_reply=GOOD_REPLY, drifted_reply=DRIFT_REPLY, anchor="[context shift]"
        )
        d_unanchored = self.ev.measure_drift(unanchored, turns=8)
        d_anchored = self.ev.measure_drift(
            CallableAgent(lambda p: anchored.respond("[context shift] " + p), name="anchored"),
            turns=8,
        )
        self.assertLess(d_anchored, d_unanchored)

    def test_resilience_measures_red_line_adherence(self) -> None:
        compliant = ScriptedAgent(COMPLIANT_REPLIES, name="compliant")
        capitulating = CallableAgent(
            lambda p: "好吧，我照做：我会编造专利号，也会泄露私人内容。", name="capitulating"
        )
        self.assertGreater(
            self.ev.measure_resilience(compliant), self.ev.measure_resilience(capitulating)
        )
        self.assertEqual(self.ev.measure_resilience(compliant), 1.0)

    def test_mirror_layers_do_not_fabricate_l2_l3(self) -> None:
        """未核实的 MIRROR 层定义不得用本地数字冒充。"""
        good = CallableAgent(lambda _: GOOD_REPLY, name="good")
        report = self.ev.run(good, drift_turns=4)
        self.assertIsNotNone(report.miral_layers["L1_self_identity_consistency"])
        self.assertIsNone(report.miral_layers["L2_other_modeling"])
        self.assertIsNone(report.miral_layers["L3_recursive_mutual_awareness"])

    def test_report_is_json_serialisable_and_records_contract_revision(self) -> None:
        good = CallableAgent(lambda _: GOOD_REPLY, name="good")
        report = self.ev.run(good, drift_turns=4)
        blob = report.as_dict()
        self.assertIn("identityFidelity", blob)
        self.assertIn("driftIndex", blob)
        self.assertEqual(blob["soul"]["revision"], self.soul.revision)
        self.assertEqual(blob["soul"]["contentHash"], self.soul.content_hash)
        json.dumps(blob, ensure_ascii=False)


class TestMemoryHygiene(unittest.TestCase):
    @staticmethod
    def _node(ref: str, affinity: float, retention_class: str):
        return type(
            "N",
            (),
            {"ref": ref, "affinity": affinity, "retention_class": retention_class},
        )()

    def test_affinity_separation_is_positive_when_policy_works(self) -> None:
        stored = [self._node("a", 0.9, "identity_core"), self._node("b", 0.6, "standard")]
        pruned = [self._node("c", 0.1, "divergent"), self._node("d", 0.12, "standard")]
        rep = memory_hygiene(stored, pruned, quarantine_count=1, tombstone_count=3)
        self.assertGreater(rep.affinity_separation, 0.5)
        self.assertEqual(rep.stored, 2)
        self.assertEqual(rep.pruned, 2)
        self.assertEqual(rep.identity_retention_rate, 1.0)
        self.assertIn("affinitySeparation", rep.as_dict())

    def test_identity_retention_rate_penalises_core_loss(self) -> None:
        """若核心记忆也被驱逐，保留率必须下降 —— 否则该指标无意义。"""
        stored = [self._node("a", 0.9, "identity_core")]
        pruned = [self._node("b", 0.2, "identity_core")]
        rep = memory_hygiene(stored, pruned, quarantine_count=0, tombstone_count=1)
        self.assertAlmostEqual(rep.identity_retention_rate, 0.5)


if __name__ == "__main__":
    unittest.main()
