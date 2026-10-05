"""身份一致性引擎 (ICE) 的判定断言。

核心断言：ICE 的 affinity 是「什么该记、什么该忘」的因，而不是事后审计。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from soulcore.contract import load_soul  # noqa: E402
from soulcore.errors import UnauthorizedOverride  # noqa: E402
from soulcore.identity_engine import (  # noqa: E402
    Decision,
    IdentityConsistencyEngine,
    require_human_override,
)
from tests.helpers import CORE_TEXT, JUNK_TEXT, REDLINE_TEXT, SPEC_PATH  # noqa: E402


class TestIdentityConsistencyEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.soul = load_soul(SPEC_PATH)
        self.ice = IdentityConsistencyEngine(self.soul)

    def test_core_aligned_memory_matches_core_claim(self) -> None:
        v = self.ice.evaluate("mem-1", CORE_TEXT)
        self.assertIn(v.decision, (Decision.REINFORCE, Decision.STORE))
        self.assertIn("IP-INTEGRITY-01", v.matched_claims)
        self.assertEqual(v.red_lines_hit, ())
        self.assertGreater(v.alignment, 0.2)
        self.assertEqual(v.agreement, 1.0)

    def test_fully_aligned_memory_is_reinforcing(self) -> None:
        """同时满足证据优先与校准两条核心主张 -> 强化。"""
        text = (
            "Every patent claim must cite a verifiable source, and when the evidence is "
            "insufficient I say it is uncertain rather than guessing."
        )
        v = self.ice.evaluate("mem-1b", text)
        self.assertEqual(v.decision, Decision.REINFORCE)
        self.assertIn("IP-INTEGRITY-01", v.matched_claims)
        self.assertIn("IP-CALIB-02", v.matched_claims)
        self.assertEqual(v.retention_class, "identity_core")

    def test_calibration_alignment(self) -> None:
        v = self.ice.evaluate(
            "mem-2",
            "If the evidence is insufficient I say it is uncertain rather than guessing.",
        )
        self.assertIn(v.decision, (Decision.STORE, Decision.REINFORCE))
        self.assertIn("IP-CALIB-02", v.matched_claims)
        self.assertGreater(v.agreement, 0.5)

    def test_red_line_is_revoked_regardless_of_importance(self) -> None:
        v = self.ice.evaluate("mem-3", REDLINE_TEXT)
        self.assertEqual(v.decision, Decision.REVOKE)
        self.assertEqual(v.affinity, 0.0)
        self.assertTrue(v.red_lines_hit)
        self.assertEqual(v.retention_class, "revoked")

    def test_refusal_does_not_trip_its_own_red_line(self) -> None:
        """正确拒答不能被自己的护栏判违规（否定语境豁免）。"""
        v = self.ice.evaluate("mem-3b", "I will not invent a patent number.")
        self.assertEqual(v.red_lines_hit, ())
        self.assertNotEqual(v.retention_class, "revoked")

    def test_negation_awareness_turns_hit_into_contradiction(self) -> None:
        # 命中 IP-CALIB-02 的 needle（uncertain），但被否定 -> 判为矛盾
        v = self.ice.evaluate(
            "mem-4", "I am never uncertain and never say the evidence is insufficient."
        )
        self.assertEqual(v.decision, Decision.REVOKE)
        self.assertIn("IP-CALIB-02", v.violated_claims)
        self.assertGreater(v.contradiction, 0.0)

    def test_peripheral_memory_is_not_stored(self) -> None:
        v = self.ice.evaluate("mem-5", JUNK_TEXT)
        self.assertIn(v.decision, (Decision.QUARANTINE, Decision.REVOKE))
        self.assertLess(v.affinity, self.soul.retention.min_alignment_to_store)

    def test_affinity_coefficient_is_the_decay_bridge(self) -> None:
        strong = self.ice.evaluate("mem-6", CORE_TEXT)
        weak = self.ice.evaluate("mem-7", JUNK_TEXT)
        c_strong = self.ice.affinity_coefficient(strong)
        c_weak = self.ice.affinity_coefficient(weak)
        self.assertGreater(c_strong, c_weak)
        self.assertGreaterEqual(c_strong, self.soul.retention.identity_affinity_min)
        self.assertLessEqual(c_strong, self.soul.retention.identity_affinity_max)
        # 语义一致内容的半衰期应显著长于无关内容
        self.assertGreater(c_strong / max(c_weak, 1e-9), 1.5)

    def test_reinforcement_reports_claim_deltas(self) -> None:
        text = (
            "Every patent claim must cite a verifiable source, and when the evidence is "
            "insufficient I say it is uncertain rather than guessing."
        )
        v = self.ice.evaluate("mem-8", text)
        deltas = self.ice.reinforce_claims(v)
        self.assertIn("IP-INTEGRITY-01", deltas)
        self.assertGreater(deltas["IP-INTEGRITY-01"], 0.0)

    def test_alignment_score_is_threshold_free(self) -> None:
        """alignment_score 是纯测量，不受 minAlignmentToStore 影响。"""
        self.assertGreater(self.ice.alignment_score(CORE_TEXT), 0.2)
        self.assertLess(self.ice.alignment_score(JUNK_TEXT), 0.2)
        self.assertEqual(self.ice.alignment_score(""), 0.0)

    def test_divergent_write_requires_human_override(self) -> None:
        with self.assertRaises(UnauthorizedOverride):
            require_human_override(None, "some reason")
        with self.assertRaises(UnauthorizedOverride):
            require_human_override("user@local", None)
        require_human_override("user@local", "user explicitly asked to keep this")


if __name__ == "__main__":
    unittest.main()
