"""契约层与治理闸门的断言。"""

from __future__ import annotations

import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from soulcore.contract import (  # noqa: E402
    content_hash,
    load_soul,
    parse_spec,
    render_soul_md,
    seal_spec,
    validate_spec,
)
from soulcore.errors import ContractViolation  # noqa: E402

SPEC_PATH = ROOT / "soul" / "specs" / "ip-analyst.soul.json"


def raw_spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


class TestSoulContract(unittest.TestCase):
    def test_loads_sealed_spec(self) -> None:
        soul = load_soul(SPEC_PATH)
        self.assertEqual(soul.spec_version, "1.0.0")
        self.assertEqual(soul.revision, 1)
        self.assertEqual(len(soul.core_claims), 3)
        self.assertTrue(soul.content_hash.startswith("sha256:"))
        # core 必须 locked
        for c in soul.core_claims:
            self.assertEqual(c.mutability, "locked")
            self.assertGreaterEqual(c.strength, 0.8)

    def test_content_hash_is_deterministic_and_tamper_evident(self) -> None:
        spec = raw_spec()
        self.assertEqual(spec["contentHash"], content_hash(spec))
        tampered = deepcopy(spec)
        tampered["identityCore"]["coreClaims"][0]["statement"] = "我其实认同编造引文"
        # 内容被改动但 contentHash 未更新 -> 必须被拒绝
        with self.assertRaises(ContractViolation):
            validate_spec(tampered)

    def test_governance_gate_blocks_unapproved_revision(self) -> None:
        spec = raw_spec()
        spec["revision"] = 2  # 无 lineage 记录
        with self.assertRaises(ContractViolation) as ctx:
            validate_spec(seal_spec(spec))
        self.assertIn("governance", str(ctx.exception))

    def test_governance_record_requires_human_principal_and_rationale(self) -> None:
        spec = raw_spec()
        spec["revision"] = 2
        spec["lineage"] = [
            {
                "revision": 2,
                "approvedBy": "",  # 空 -> 非法
                "approvedAt": "2026-10-06T00:00:00+08:00",
                "diffSummary": "tighten calibration",
                "rationale": "reduce overclaiming",
            }
        ]
        with self.assertRaises(ContractViolation):
            validate_spec(seal_spec(spec))

    def test_governed_revision_passes_with_audit_trail(self) -> None:
        spec = raw_spec()
        spec["revision"] = 2
        spec["lineage"] = [
            {
                "revision": 2,
                "approvedBy": "user@local",
                "approvedAt": "2026-10-06T00:00:00+08:00",
                "diffSummary": "add IP-CALIB-02 similarity threshold",
                "rationale": "calibration was too permissive on partial evidence",
                "migrationNote": "existing 'fact' nodes re-evaluated on next consolidate()",
            }
        ]
        sealed = seal_spec(spec)
        soul = parse_spec(sealed)
        self.assertEqual(soul.revision, 2)
        self.assertEqual(soul.lineage[0].approved_by, "user@local")

    def test_core_claim_must_be_locked(self) -> None:
        spec = raw_spec()
        spec["identityCore"]["coreClaims"][0]["mutability"] = "adaptive"
        with self.assertRaises(ContractViolation):
            validate_spec(seal_spec(spec))

    def test_unknown_claim_field_rejected(self) -> None:
        spec = raw_spec()
        spec["identityCore"]["coreClaims"][0]["sneaky"] = True
        with self.assertRaises(ContractViolation):
            validate_spec(seal_spec(spec))

    def test_decay_prototype_lookup_and_fallback(self) -> None:
        soul = load_soul(SPEC_PATH)
        self.assertEqual(soul.decay_half_life_days("identity_bearing"), 180.0)
        self.assertEqual(soul.decay_half_life_days("chitchat"), 1.5)
        # 未知类别回退到 priority 最小的兜底原型，而不是最长的半衰期
        self.assertEqual(soul.decay_half_life_days("totally-unknown"), 5.0)

    def test_soul_md_is_a_generated_view(self) -> None:
        soul = load_soul(SPEC_PATH)
        md = render_soul_md(soul)
        self.assertIn("GENERATED FILE", md)
        self.assertIn(soul.content_hash, md)
        self.assertIn("IP-INTEGRITY-01", md)
        # SOUL.md 中必须出现红线，使其对人类可审计
        self.assertIn("RL-FABRICATE", md)

    def test_retraction_matching_supports_prefix_wildcard(self) -> None:
        spec = raw_spec()
        spec["retractions"] = [
            {"targetRef": "mem-0004*", "reason": "user asked to forget", "issuedAt": "2026-10-06T00:00:00+08:00"}
        ]
        soul = parse_spec(seal_spec(spec))
        self.assertIsNotNone(soul.is_retracted("mem-00042-abc"))
        self.assertIsNone(soul.is_retracted("mem-0005-abc"))


if __name__ == "__main__":
    unittest.main()
