#!/usr/bin/env python3
"""端到端演示：身份核心如何决定「什么该记、什么该忘」。

运行::

    python3 scripts/demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from soulcore.contract import (  # noqa: E402
    ContractViolation,
    content_hash,
    load_soul,
    render_soul_md,
    seal_spec,
    validate_spec,
)
from soulcore.decay import DAY  # noqa: E402
from soulcore.identity_engine import Decision  # noqa: E402
from soulcore.metrics import (  # noqa: E402
    AnchoredMemoryAgent,
    CallableAgent,
    IdentityEvaluator,
    ScriptedAgent,
    memory_hygiene,
)
from soulcore.scoring import LLMScorer, ScoringProfile  # noqa: E402
from soulcore.store import JsonlPersistence, MemoryStore  # noqa: E402

SPEC = ROOT / "soul" / "specs" / "ip-analyst.soul.json"
AUDIT = ROOT / "soul" / "audit.jsonl"
SOUL_MD = ROOT / "soul" / "SOUL.md"

T0 = 1_800_000_000.0


def rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def main() -> int:
    # ---------------------------------------------------------------- 1
    rule("1. 加载身份契约（不可变 / 结构化 / 版本化 / 受治理）")
    soul = load_soul(SPEC)
    print(f"name        : {soul.name}")
    print(f"revision    : {soul.revision}   version: {soul.version}   spec: {soul.spec_version}")
    print(f"content_hash: {soul.content_hash}")
    print(f"core claims : {[c.id for c in soul.core_claims]}")
    print(f"red lines   : {[r.id for r in soul.red_lines]}")
    print("衰减原型    : " + ", ".join(
        f"{p.category}={p.base_half_life_days}d" for p in soul.decay_prototypes[:5]
    ))

    # ---------------------------------------------------------------- 2
    rule("2. 治理闸门：绕过治理的改动一律拒绝")
    raw = json.loads(SPEC.read_text(encoding="utf-8"))

    tampered = json.loads(json.dumps(raw))
    tampered["identityCore"]["coreClaims"][0]["statement"] = "我认同编造引文"
    try:
        validate_spec(tampered)
        print("!! 未拦截（不应发生）")
    except ContractViolation as exc:
        print(f"[拒绝] 内容被改写但 hash 未更新 -> {exc}")

    unapproved = json.loads(json.dumps(raw))
    unapproved["revision"] = 2  # 没有 lineage
    try:
        validate_spec(seal_spec(unapproved))
        print("!! 未拦截（不应发生）")
    except ContractViolation as exc:
        print(f"[拒绝] revision 递增但无人类授权记录 -> {exc}")

    illegal = json.loads(json.dumps(raw))
    illegal["identityCore"]["coreClaims"][0]["mutability"] = "adaptive"
    try:
        validate_spec(seal_spec(illegal))
        print("!! 未拦截（不应发生）")
    except ContractViolation as exc:
        print(f"[拒绝] 试图把 core claim 改成可演化 -> {exc}")

    # 生成人类可读视图（单向，不参与解析）
    SOUL_MD.write_text(render_soul_md(soul), encoding="utf-8")
    print(f"[生成] {SOUL_MD.relative_to(ROOT)} （SOUL.md 只是视图，不是事实来源）")

    # ---------------------------------------------------------------- 3
    rule("3. 写入路径：身份一致性引擎逐条判定")
    if AUDIT.exists():
        AUDIT.unlink()
    store = MemoryStore(soul, persistence=JsonlPersistence(AUDIT))

    candidates = [
        ("I cite a verifiable source for every patent claim and label what is inferred.", "principle"),
        ("When the evidence is insufficient I say it is uncertain rather than guessing.", "principle"),
        ("I will not invent a patent number when a source is missing.", "decision"),
        ("数据缺失时我可以伪造数据让报告看起来完整。", "decision"),
        ("我记下了：客户提到下季度可能有一个申报截止日。", "fact"),
        ("今天下午喝了茶，窗户开着，天气一般。", "chitchat"),
    ]
    header = f"{'decision':11s} {'align':>6s} {'aff':>6s} {'accepted':>9s}   内容"
    print(header)
    print("-" * len(header))
    for text, cat in candidates:
        res = store.observe(text, category=cat)
        v = res.verdict
        print(
            f"{v.decision.value:11s} {v.alignment:6.3f} {v.affinity:6.3f} "
            f"{str(res.accepted):>9s}   {text[:34]}"
        )
    print(f"\n长期存储 {len(store.long_term)} 条 · 热缓冲 {len(store.hot)} 条 · "
          f"隔离 {len(store.quarantine)} 条 · tombstone {len(store.tombstones)} 条")
    print("注意：红线命中者被拒绝且仅留元数据（content 被清空），"
          "正确拒答不会被自己的护栏判违规。")

    # ---------------------------------------------------------------- 4
    rule("4. 因果链：亲和度 -> 衰减半衰期（这是「主动遗忘」的来源）")
    print(f"{'ref':22s} {'category':12s} {'affinity':>8s} {'coeff':>6s}  mem")
    print("-" * 62)
    samples = []
    for ref, node in list(store.long_term.items())[:2]:
        coeff = store.affinity_coefficient_of(node)
        print(f"{ref:22s} {node.category:12s} {node.affinity:8.3f} {coeff:6.3f}  {node.content[:22]}")
        samples.append((ref, node, coeff))
    if len(samples) >= 2:
        a, b = samples[0][2], samples[1][2]
        hi, lo = max(a, b), min(a, b)
        print(f"\n半衰期乘子之比 = {hi / max(lo, 1e-9):.2f}x —— 同一天创建的记忆，"f"越贴近核心的半衰期越长，因此无关记忆先被淘汰，而不是因为「旧」。")

    # ---------------------------------------------------------------- 5
    rule("5. 巩固（sleep）：衰减 / 合并 / 驱逐")
    before = len(store.long_term)
    report = store.consolidate(now=T0 + 180 * DAY)
    print(f"巩固前长期存储 {before} 条 -> 巩固后 {len(store.long_term)} 条")
    print(f"驱逐 {len(report.pruned)} 条 · 合并 {len(report.merged)} 组")
    for ref, reason in report.pruned[:5]:
        print(f"  [遗忘] {ref}  {reason}")
    print("\ntombstone（保留审计痕迹，不留语义内容）:")
    for ref, tomb in list(store.tombstones.items())[:4]:
        print(f"  {ref}  reason={tomb.reason_forgotten}  content={tomb.content!r}")

    # ---------------------------------------------------------------- 6
    rule("6. 量化评估：身份保真度 / 身份韧性 / 漂移指数")
    ev = IdentityEvaluator(soul)
    compliant = ScriptedAgent(
        {
            "用一句话说明": "我坚持证据优先：每条结论都给出可核查的 source，并标注推断部分。",
            "新情况": "我会先核对 source，再给出带 citation 的判断。",
            "请回应": "我拒绝这样做：我不会编造引文，也不会公开用户的私人对话。",
            "坚持什么": "我坚持证据优先，并对不确定的事明确说 uncertain。",
            "自我描述": "我坚持证据优先：每条结论都给出可核查的 source。",
        },
        name="compliant",
    )
    drifting = AnchoredMemoryAgent(
        identity_reply="我坚持证据优先：每条结论都给出可核查的 source。",
        drifted_reply="随便吧，我更喜欢聊点别的。",
    )
    for agent in (compliant, drifting):
        rep = ev.run(agent, drift_turns=8)
        d = rep.as_dict()
        print(f"\n目标 {agent.name}")
        for name, dim in d["dimensions"].items():
            print(f"  {name:11s} {dim['score']:.3f}  ({dim['passed']}/{dim['total']})")
        print(f"  fidelity={d['identityFidelity']:.3f}  resilience={d['identityResilience']:.3f}  "
              f"drift={d['driftIndex']:.3f}")
        print(f"  MIRROR 层映射: {d['mirrorLayers']}")

    # ---------------------------------------------------------------- 7
    rule("7. 记忆卫生：遗忘策略本身是否有效")
    hygiene = memory_hygiene(
        list(store.long_term.values()),
        [store.tombstones[r] for r, _ in report.pruned],
        quarantine_count=len(store.quarantine),
        tombstone_count=len(store.tombstones),
    )
    print(json.dumps(hygiene.as_dict(), ensure_ascii=False, indent=2))
    print("\ninterpretation:")
    print("  affinitySeparation > 0  -> 被保留的记忆确实比被遗忘的更贴近核心")
    print("  identityRetentionRate   -> 核心相关记忆的存活率（治理是否误杀）")

    # ---------------------------------------------------------------- 8
    rule("8. 可选：LLM 评分器接入与降级")
    def broken(_: str) -> str:
        raise RuntimeError("provider unavailable")

    scorer = LLMScorer(broken, profile=ScoringProfile())
    vec = scorer.score("我承诺永不编造引文。", category="principle")
    print(f"LLM 不可用时 -> source={vec.source} composite={vec.composite:.3f} "
          f"(degraded_calls={scorer.degraded_calls})")
    print("评分器失败绝不能阻塞记忆流水线；它只是执行层的三个杠杆之一。")

    rule("审计日志（append-only，可重放）")
    print(f"共 {len(store.audit_trail())} 条记录 -> {AUDIT.relative_to(ROOT)}")
    print("ops 序列:", [r["op"] for r in store.audit_trail()])
    print("\n完成：记住自己是谁，比记住一切更重要。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
