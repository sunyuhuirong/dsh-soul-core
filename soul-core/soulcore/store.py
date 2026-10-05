"""执行层杠杆 2/4：双缓冲巩固 (dual-buffer consolidation)。

    observe()  ->  热缓冲区 (HOT)
                    1. 身份一致性判定 (ICE)
                    2. 校验 / 去重
                    3. 多维重要性评分
                    4. 通过后 promote() -> 长期存储 (LONG_TERM)
                    consolidate() -> 衰减 / 合并 / 驱逐 -> tombstone

关键因果链（本设计的核心，不可裁剪）：

    ICE verdict.affinity --(affinity_coefficient)--> effective_half_life
                                                        |
                                            偏离核心 -> 半衰期短 -> 先被驱逐

即「什么该记、什么该忘」由身份契约决定，而不是由时间或容量单方面决定。

持久化：JSONL 操作日志（append-only，可重放）+ 内存索引。
生产替换为 SQLite/Postgres 只需实现 `Persistence` 协议，算法不变。
"""

from __future__ import annotations

import dataclasses
import json
import math
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from .contract import SoulCore
from .decay import (
    DAY,
    DecayConfig,
    EvictionCandidate,
    MemoryNode,
    MemoryTier,
    eviction_candidates,
    merge_node,
    normalised_strength,
    strength_at,
)
from .errors import UnauthorizedOverride
from .identity_engine import (
    Decision,
    IdentityConsistencyEngine,
    SoulVerdict,
    tokenize,
    lexical_similarity,
)
from .scoring import HeuristicScorer, Scorer, ScoreVector, ScoringProfile


class Persistence(Protocol):
    def append(self, record: Mapping[str, Any]) -> None:  # pragma: no cover
        ...

    def load(self) -> List[Mapping[str, Any]]:  # pragma: no cover
        ...


class NullPersistence:
    """内存态持久化（测试用）。"""

    def __init__(self) -> None:
        self.records: List[Mapping[str, Any]] = []

    def append(self, record: Mapping[str, Any]) -> None:
        self.records.append(record)

    def load(self) -> List[Mapping[str, Any]]:
        return list(self.records)


class JsonlPersistence:
    """append-only JSONL 日志：可 diff、可重放、可审计。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: Mapping[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load(self) -> List[Mapping[str, Any]]:
        if not self.path.exists():
            return []
        out: List[Mapping[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


@dataclasses.dataclass
class ObservationResult:
    """observe() 的返回值：调用方据此决定是否继续业务流程。"""

    ref: str
    accepted: bool
    verdict: SoulVerdict
    node: Optional[MemoryNode] = None
    merged_into: Optional[str] = None
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "accepted": self.accepted,
            "verdict": self.verdict.as_dict(),
            "merged_into": self.merged_into,
            "note": self.note,
        }


@dataclasses.dataclass
class ConsolidationReport:
    """一次巩固（sleep）的结果，用于监控与测试断言。"""

    pruned: List[Tuple[str, str]] = dataclasses.field(default_factory=list)
    merged: List[Tuple[str, str]] = dataclasses.field(default_factory=list)
    promoted: List[str] = dataclasses.field(default_factory=list)
    reinforced_claims: Dict[str, float] = dataclasses.field(default_factory=dict)
    evaluated: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _similarity(a: str, b: str) -> float:
    return lexical_similarity(tokenize(a), tokenize(b))


class MemoryStore:
    """双缓冲记忆存储。ICE 是它的治理前端，衰减器是它的清理后端。"""

    def __init__(
        self,
        soul: SoulCore,
        ice: Optional[IdentityConsistencyEngine] = None,
        scorer: Optional[Scorer] = None,
        profile: Optional[ScoringProfile] = None,
        decay: Optional[DecayConfig] = None,
        persistence: Optional[Persistence] = None,
    ) -> None:
        self.soul = soul
        self.profile = (profile or ScoringProfile()).normalise()
        self.ice = ice or IdentityConsistencyEngine(soul)
        self.scorer: Scorer = scorer or HeuristicScorer(self.profile)
        self.decay = decay or DecayConfig(
            eviction_threshold=soul.retention.eviction_threshold
        )
        self.persistence: Persistence = persistence or NullPersistence()

        self.hot: Dict[str, MemoryNode] = {}
        self.long_term: Dict[str, MemoryNode] = {}
        self.tombstones: Dict[str, MemoryNode] = {}
        self.verdicts: Dict[str, SoulVerdict] = {}
        self.quarantine: Dict[str, Tuple[MemoryNode, SoulVerdict]] = {}
        self.audit: List[Dict[str, Any]] = []
        self._seq = 0

    # -- 内部工具 -----------------------------------------------------------
    def _next_ref(self, prefix: str = "mem") -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:05d}-{uuid.uuid4().hex[:6]}"

    def _log(self, op: str, **payload: Any) -> None:
        rec = {"op": op, "at": time.time(), **payload}
        self.audit.append(rec)
        self.persistence.append(rec)

    def half_life_of(self, node: MemoryNode) -> float:
        return self.soul.decay_half_life_days(node.category)

    def affinity_coefficient_of(self, node: MemoryNode) -> float:
        """把节点亲和度映射为半衰期乘子（与 ICE 使用同一公式）。"""
        lo = self.soul.retention.identity_affinity_min
        hi = self.soul.retention.identity_affinity_max
        return lo + (hi - lo) * max(0.0, min(1.0, node.affinity))

    def strength(self, node: MemoryNode, now: Optional[float] = None) -> float:
        return strength_at(
            node, self.half_life_of(node), self.affinity_coefficient_of(node), now, self.decay
        )

    def normalised_strength(self, node: MemoryNode, now: Optional[float] = None) -> float:
        return normalised_strength(
            node, self.half_life_of(node), self.affinity_coefficient_of(node), now, self.decay
        )

    # -- 主入口 -------------------------------------------------------------
    def observe(
        self,
        text: str,
        category: str = "fact",
        approved_by: Optional[str] = None,
        rationale: Optional[str] = None,
        now: Optional[float] = None,
    ) -> ObservationResult:
        """接收一条候选记忆。这是执行层唯一的写入口。"""
        now = time.time() if now is None else now
        ref = self._next_ref()
        verdict = self.ice.evaluate(ref, text)
        self.verdicts[ref] = verdict

        node = MemoryNode(
            ref=ref,
            content=text,
            category=category,
            score=self.scorer.score(text, category=category),
            affinity=verdict.affinity,
            alignment=verdict.alignment,
            retention_class=verdict.retention_class,
            decision=verdict.decision.value,
            created_at=now,
            tier=MemoryTier.HOT,
            reason_forgotten=None,
        )

        # 1) revoke：拒绝进入任何缓冲区（除 tombstone 元数据）
        if verdict.decision is Decision.REVOKE:
            self._ensure_override(verdict, approved_by, rationale)
            tomb = dataclasses.replace(
                node,
                tier=MemoryTier.TOMBSTONE,
                reason_forgotten=verdict.reason,
                tombstoned_at=now,
                content="",  # 不留内容，避免偏离语义被后续检索
            )
            self.tombstones[ref] = tomb
            self._log("revoke", ref=ref, reason=verdict.reason, affinity=verdict.affinity)
            return ObservationResult(ref=ref, accepted=False, verdict=verdict, note=verdict.reason)

        # 2) 全部进入热缓冲区接受验证（双缓冲的第一道缓冲）
        self.hot[ref] = node

        # 3) 隔离：与核心模糊相关的内容不得进入长期存储
        if verdict.decision is Decision.QUARANTINE:
            self.hot.pop(ref, None)
            self.quarantine[ref] = (node, verdict)
            self._log("quarantine", ref=ref, affinity=verdict.affinity)
            return ObservationResult(
                ref=ref, accepted=False, verdict=verdict, node=node, note=verdict.reason
            )

        # 4) 重要性阈值：低价值内容只留在热缓冲区，等待自然过期
        #
        # 注意判定优先级 —— **身份优先于重要性**：
        # 与核心高度一致的记忆即使「不意外、不情绪化」（composite 偏低），
        # 也必须被记住；否则「我坚持证据优先」这种最该记住的东西
        # 会因为「毫无意外性」而被判为低价值，这正是要防的失效模式。
        identity_critical = (
            verdict.decision is Decision.REINFORCE
            or node.affinity >= self.soul.retention.min_alignment_to_store
            or node.retention_class == "identity_core"
        )
        if node.score.composite < self.profile.store_threshold and not identity_critical:
            self._log(
                "hot_only",
                ref=ref,
                composite=node.score.composite,
                threshold=self.profile.store_threshold,
                affinity=node.affinity,
            )
            return ObservationResult(
                ref=ref,
                accepted=False,
                verdict=verdict,
                node=node,
                note=f"composite {node.score.composite:.3f} < store_threshold "
                     f"and affinity {node.affinity:.3f} below identity floor",
            )
        if node.score.composite < self.profile.store_threshold:
            self._log(
                "identity_exempt",
                ref=ref,
                composite=node.score.composite,
                affinity=node.affinity,
                reason="identity_critical overrides importance threshold",
            )

        # 4) 去重 / 合并（在热缓冲区与长期区同时查找）
        dup = self._find_duplicate(text, category, exclude_ref=ref)
        if dup is not None:
            merged = merge_node(dup, node, now=now)
            merged.tier = MemoryTier.LONG_TERM
            self.long_term[merged.ref] = merged
            self.hot.pop(merged.ref, None)
            self.hot.pop(ref, None)
            self.long_term.pop(ref, None)
            self._log(
                "merge",
                ref=ref,
                into=merged.ref,
                revision=merged.revision,
                similarity_hit=True,
            )
            return ObservationResult(
                ref=ref,
                accepted=True,
                verdict=verdict,
                node=merged,
                merged_into=merged.ref,
                note="merged into existing node",
            )

        # 5) 通过热缓冲验证 -> 晋升长期存储
        promoted = self.promote(node)
        self._log(
            "promote",
            ref=promoted.ref,
            affinity=promoted.affinity,
            composite=promoted.score.composite,
        )
        return ObservationResult(ref=promoted.ref, accepted=True, verdict=verdict, node=promoted)

    def _ensure_override(
        self, verdict: SoulVerdict, approved_by: Optional[str], rationale: Optional[str]
    ) -> None:
        """红线/矛盾记忆默认拒收；只有人类授权才能留档（且仍需 rationale）。"""
        if verdict.red_lines_hit:
            # 红线不可被任何授权绕过
            return
        if approved_by or rationale:
            if not approved_by or not str(approved_by).strip():
                raise UnauthorizedOverride("approved_by must be a non-empty human principal")
            if not rationale or not str(rationale).strip():
                raise UnauthorizedOverride("rationale must be non-empty")

    def _find_duplicate(
        self, text: str, category: str, exclude_ref: Optional[str] = None
    ) -> Optional[MemoryNode]:
        best: Optional[MemoryNode] = None
        best_sim = 0.0
        for pool in (self.long_term, self.hot):
            for node in pool.values():
                # 排除候选自身：调用方可能已把它放进热缓冲区
                # （否则相似度恒为 1.0，任何记忆都会「与自身重复」并被合并掉）
                if node.ref == exclude_ref:
                    continue
                if node.tier is MemoryTier.TOMBSTONE or node.category != category:
                    continue
                sim = _similarity(text, node.content)
                if sim > best_sim:
                    best, best_sim = node, sim
        if best is not None and best_sim >= self.soul.retention.duplicate_similarity:
            return best
        return None

    def promote(self, node: MemoryNode) -> MemoryNode:
        """热缓冲区 -> 长期存储。"""
        promoted = dataclasses.replace(node, tier=MemoryTier.LONG_TERM)
        self.long_term[promoted.ref] = promoted
        self.hot.pop(node.ref, None)
        return promoted

    # -- 检索（身份感知加权）------------------------------------------------
    def recall(
        self,
        query: str,
        k: int = 5,
        now: Optional[float] = None,
        w_relevance: float = 1.0,
        w_affinity: float = 0.5,
        w_importance: float = 0.4,
        w_strength: float = 0.3,
    ) -> List[Tuple[MemoryNode, float]]:
        """检索加权：relevance x strength x identity affinity。

        与纯相似度检索的差别：偏离核心的记忆即使语义接近，也排在后面。
        """
        now = time.time() if now is None else now
        scored: List[Tuple[MemoryNode, float]] = []
        for node in self.long_term.values():
            if node.tier is not MemoryTier.LONG_TERM or node.superseded_by is not None:
                continue
            rel = _similarity(query, node.content)
            if rel <= 0.0:
                continue
            st = self.normalised_strength(node, now)
            total = (
                w_relevance * rel
                + w_affinity * node.affinity
                + w_importance * node.score.composite
                + w_strength * st
            )
            scored.append((node, total))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]

    def touch(self, ref: str, now: Optional[float] = None) -> Optional[MemoryNode]:
        """标记一次「被回忆」，延长半衰期并增加强度。"""
        node = self.long_term.get(ref)
        if node is None:
            return None
        node.accesses.append(time.time() if now is None else now)
        self._log("recall", ref=ref, count=node.access_count)
        return node

    # -- 巩固（sleep）-------------------------------------------------------
    def consolidate(self, now: Optional[float] = None) -> ConsolidationReport:
        """周期性巩固：衰减 -> 合并 -> 驱逐。

        这是「主动遗忘」真正发生的地方。
        """
        now = time.time() if now is None else now
        report = ConsolidationReport()

        # a) 治理退役：身份契约可以事后撤回某条记忆（按 ref 或 ref 前缀）
        for ref, node in list(self.long_term.items()):
            retraction = self.soul.is_retracted(node.ref)
            if retraction is not None:
                self._evict(node, now, reason=f"retracted: {retraction.reason}", report=report)

        # b) 衰减 + 驱逐
        candidates: List[EvictionCandidate] = eviction_candidates(
            list(self.long_term.values()),
            half_life_of=self.half_life_of,
            affinity_of=self.affinity_coefficient_of,
            now=now,
            cfg=self.decay,
        )
        for cand in candidates:
            node = self.long_term.get(cand.ref)
            if node is not None:
                self._evict(node, now, reason=cand.reason, report=report)

        # c) 合并同类低强度节点（等价于「把冗余压缩成更强的表征」）
        merged_pairs = self._merge_within_categories(now)
        report.merged.extend(merged_pairs)

        # d) 热缓冲区清理：超过 2 个半衰期仍未晋级者丢弃
        for ref, node in list(self.hot.items()):
            report.evaluated += 1
            if node.age_days(now) > 2.0 * self.half_life_of(node):
                self.hot.pop(ref, None)
                self._log("hot_expired", ref=ref, age_days=round(node.age_days(now), 2))

        self._log(
            "consolidate",
            pruned=len(report.pruned),
            merged=len(report.merged),
            evaluated=report.evaluated,
        )
        return report

    def _evict(
        self,
        node: MemoryNode,
        now: float,
        reason: str,
        report: ConsolidationReport,
    ) -> None:
        """驱逐 = 移出长期存储 + 转 tombstone（保留审计痕迹，不留语义内容）。"""
        self.long_term.pop(node.ref, None)
        tomb = dataclasses.replace(
            node,
            tier=MemoryTier.TOMBSTONE,
            reason_forgotten=reason,
            tombstoned_at=now,
            content="",
        )
        self.tombstones[node.ref] = tomb
        report.pruned.append((node.ref, reason))
        self._log("evict", ref=node.ref, reason=reason, affinity=node.affinity)

    def _merge_within_categories(self, now: float) -> List[Tuple[str, str]]:
        """把同一 category 内低强度的相近节点压进高强度节点。"""
        pairs: List[Tuple[str, str]] = []
        by_cat: Dict[str, List[MemoryNode]] = {}
        for node in list(self.long_term.values()):
            by_cat.setdefault(node.category, []).append(node)
        for cat, nodes in by_cat.items():
            if len(nodes) < 2:
                continue
            nodes.sort(key=lambda n: self.normalised_strength(n, now), reverse=True)
            keepers: List[MemoryNode] = []
            for node in nodes:
                target = next(
                    (k for k in keepers if _similarity(k.content, node.content) >= 0.5), None
                )
                if target is None:
                    keepers.append(node)
                    continue
                merged = merge_node(target, node, now=now)
                merged.tier = MemoryTier.LONG_TERM
                self.long_term[merged.ref] = merged
                self.long_term.pop(node.ref, None)
                tomb = dataclasses.replace(
                    node,
                    tier=MemoryTier.TOMBSTONE,
                    reason_forgotten=f"merged into {merged.ref}",
                    tombstoned_at=now,
                    content="",
                )
                self.tombstones[node.ref] = tomb
                # 更新 keepers 中的引用，避免留下悬空节点
                keepers = [merged if k.ref == target.ref else k for k in keepers]
                pairs.append((node.ref, merged.ref))
                self._log("merge_consolidate", ref=node.ref, into=merged.ref)
        return pairs

    # -- 审计 ---------------------------------------------------------------
    def audit_trail(self) -> List[Dict[str, Any]]:
        return list(self.audit)

    def forget(self, ref: str, reason: str, now: Optional[float] = None) -> bool:
        """显式遗忘单条记忆（用户「请忘记这件事」的路径）。"""
        now = time.time() if now is None else now
        node = self.long_term.get(ref)
        if node is None:
            return False
        self._evict(node, now, reason=f"explicit user forget: {reason}", report=ConsolidationReport())
        return True
