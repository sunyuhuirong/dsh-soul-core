"""执行层杠杆 2-4/4：数据结构与衰减/合并/驱逐算法。

覆盖四项杠杆：
1. 重要性      -> `scoring.py`（三维 1-10，LLM 打分，阈值准入）
2. 合并        -> `merge_node()`（重复检测 -> 强化既有节点，而非无限追加）
3. 衰减        -> `strength_at()`（差异化半衰期，由身份亲和度/类别/访问频率调制）
4. 驱逐        -> `evict()`（strength 低于阈值 -> tombstone，保留审计痕迹）
"""

from __future__ import annotations

import dataclasses
import math
import time
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .scoring import ScoreVector

DAY = 86400.0


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DecayConfig:
    """衰减与巩固的超参数。全部可调，全部有默认值。"""

    default_half_life_days: float = 10.0
    base_strength: float = 0.6              # 新记忆的初始强度（持久性需要争取）
    emotional_bonus: float = 0.4            # 情感强度对初始强度的加成上限
    retrieval_half_life_bonus_days: float = 2.0  # 每次被回忆延长的半衰期（对齐 hippo 做法）
    recall_gain: float = 0.15               # 每次回忆的强度增益
    strength_cap: float = 5.0
    reinforce_bonus: float = 0.25           # 身份核心一致记忆的初始强度加成
    eviction_threshold: float = 0.05        # 归一化后的驱逐阈值
    tombstone_ttl_days: float = 90.0        # tombstone 保留期（审计需要）
    # 驱逐时使用的「归一化强度」：消除 base_strength 尺度影响，使阈值可解释
    normalise_by_base: bool = True


class MemoryTier(str, Enum):
    HOT = "hot"          # 热缓冲区：未验证、未晋升
    LONG_TERM = "long_term"
    TOMBSTONE = "tombstone"  # 已遗忘，仅保留审计元数据


@dataclasses.dataclass
class MemoryNode:
    """长期存储中的一个记忆节点。

    `accesses` 保存历史访问时刻（epoch 秒），使「访问频率动态调节衰减率」
    与「时间模式」可计算，而不是只存一个 counter。
    """

    ref: str
    content: str
    category: str
    score: ScoreVector
    affinity: float
    alignment: float
    retention_class: str = "standard"
    decision: str = "store"
    created_at: float = dataclasses.field(default_factory=time.time)
    accesses: List[float] = dataclasses.field(default_factory=list)
    strength_boost: float = 0.0
    merged_from: List[str] = dataclasses.field(default_factory=list)
    superseded_by: Optional[str] = None
    reason_forgotten: Optional[str] = None
    tombstoned_at: Optional[float] = None
    tier: MemoryTier = MemoryTier.HOT
    revision: int = 1

    # -- 便捷 ---------------------------------------------------------------
    @property
    def access_count(self) -> int:
        return len(self.accesses)

    def last_access_at(self) -> float:
        return max(self.accesses) if self.accesses else self.created_at

    def age_days(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, (now - self.created_at) / DAY)

    def as_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["tier"] = self.tier.value
        d["score"] = self.score.as_dict()
        return d


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------


def merge_node(target: MemoryNode, incoming: MemoryNode, now: Optional[float] = None) -> MemoryNode:
    """把 incoming 合并进既有节点 target —— 就地强化，不新增节点。

    合并策略（与「只追加日志」相对）：
    - 取更强的三维分数与更高的亲和度
    - 累加强化增益
    - 短内容被更长、更完整的表述取代
    - 记录 lineage（merged_from）
    """
    now = time.time() if now is None else now
    merged = dataclasses.replace(
        target,
        score=ScoreVector(
            importance=max(target.score.importance, incoming.score.importance),
            surprise=max(target.score.surprise, incoming.score.surprise),
            affect=max(target.score.affect, incoming.score.affect),
            composite=max(target.score.composite, incoming.score.composite),
            source="merged",
        ),
        affinity=max(target.affinity, incoming.affinity),
        alignment=max(target.alignment, incoming.alignment),
        content=target.content if len(target.content) >= len(incoming.content) else incoming.content,
        strength_boost=target.strength_boost + incoming.strength_boost,
        merged_from=target.merged_from + [incoming.ref] + incoming.merged_from,
        accesses=target.accesses + [now],
        revision=target.revision + 1,
    )
    return merged


# ---------------------------------------------------------------------------
# 衰减
# ---------------------------------------------------------------------------


def emotional_multiplier(node: MemoryNode) -> float:
    """情感强度延长寿命：affect=1.0 -> 1.5x 半衰期。

    与 hippo-memory 的 emotional_multiplier 同构（那里为 1.0/1.3/1.5/2.0）。
    """
    return 1.0 + 0.5 * max(0.0, min(1.0, node.score.affect))


def retrieval_multiplier(node: MemoryNode, cfg: DecayConfig) -> float:
    """访问频率延长寿命：每次回忆 +2 天半衰期（对齐 hippo-memory）。"""
    if not node.accesses:
        return 1.0
    base = max(1e-6, cfg.default_half_life_days)
    return (base + cfg.retrieval_half_life_bonus_days * node.access_count) / base


def recency_of_access_multiplier(node: MemoryNode, now: float, cfg: DecayConfig) -> float:
    """时间模式调制：刚被回忆过的记忆更「新鲜」，衰减更慢。

    对数衰减的空间分布（spaced-repetition 的粗近似），上限 1.5x。
    """
    elapsed_days = max(0.0, (now - node.last_access_at()) / DAY)
    return 1.0 + 0.5 * math.exp(-elapsed_days / max(1.0, cfg.default_half_life_days))


def effective_half_life_days(
    node: MemoryNode,
    prototype_half_life_days: float,
    affinity_coefficient: float,
    now: float,
    cfg: DecayConfig,
) -> float:
    """差异化衰减率的核心公式。

        H_eff = H_prototype
                x affinity_coefficient      <- 身份亲和度（本题最关键的乘子）
                x emotional_multiplier      <- 情感强度
                x retrieval_multiplier      <- 访问频率
                x recency_of_access_mult     <- 时间模式
    """
    h = max(0.05, prototype_half_life_days)
    h *= max(0.0, affinity_coefficient)
    h *= emotional_multiplier(node)
    h *= retrieval_multiplier(node, cfg)
    h *= recency_of_access_multiplier(node, now, cfg)
    return max(0.01, h)


def initial_strength(node: MemoryNode, cfg: DecayConfig, affinity_coefficient: float) -> float:
    """新记忆的初始强度 = 基准 x 重要性 x 身份亲和度 + 情感加成 + 强化加成。"""
    value = cfg.base_strength * (0.4 + 0.6 * node.score.importance)
    value *= affinity_coefficient
    value += cfg.emotional_bonus * node.score.affect
    if node.decision == "reinforce":
        value += cfg.reinforce_bonus
    return max(1e-6, value)


def strength_at(
    node: MemoryNode,
    prototype_half_life_days: float,
    affinity_coefficient: float,
    now: Optional[float] = None,
    cfg: Optional[DecayConfig] = None,
) -> float:
    """计算 node 在 now 时刻的强度（指数衰减 + 回忆增益）。

    采用 Ebbinghaus 形式的指数衰减；`MemoryBank` (Zhong et al., 2024) 使用
    其简化版 `Importance = exp(-Δt / S)`，本实现把 S 扩展为多因子乘子。
    """
    cfg = cfg or DecayConfig()
    now = time.time() if now is None else now
    h_eff = effective_half_life_days(node, prototype_half_life_days, affinity_coefficient, now, cfg)
    try:
        elapsed_days = max(0.0, (now - node.last_access_at()) / DAY)
    except OverflowError:
        elapsed_days = float("inf")
    decayed = math.exp(-elapsed_days / h_eff) if math.isfinite(elapsed_days) else 0.0
    value = initial_strength(node, cfg, affinity_coefficient) * decayed
    value += cfg.recall_gain * node.access_count
    value += node.strength_boost
    return min(cfg.strength_cap, max(0.0, value))


def normalised_strength(
    node: MemoryNode,
    prototype_half_life_days: float,
    affinity_coefficient: float,
    now: Optional[float] = None,
    cfg: Optional[DecayConfig] = None,
) -> float:
    """归一化强度：用于驱逐阈值判定。

    除以「刚创建时的强度」，使阈值退化为「相对保留率」，
    从而不受 base_strength / 情感加成等尺度差异影响。
    """
    cfg = cfg or DecayConfig()
    now = time.time() if now is None else now
    s = strength_at(node, prototype_half_life_days, affinity_coefficient, now, cfg)
    fresh = dataclasses.replace(node, accesses=[], strength_boost=0.0)
    s0 = initial_strength(fresh, cfg, affinity_coefficient)
    if s0 <= 0:
        return 0.0
    return max(0.0, min(1.0, s / s0)) if cfg.normalise_by_base else s


# ---------------------------------------------------------------------------
# 驱逐
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class EvictionCandidate:
    ref: str
    normalised_strength: float
    reason: str


def eviction_candidates(
    nodes: Sequence[MemoryNode],
    half_life_of: Any,
    affinity_of: Any,
    now: float,
    cfg: DecayConfig,
) -> List[EvictionCandidate]:
    """列出当前应被遗忘的节点。

    `half_life_of(node) -> float` 与 `affinity_of(node) -> float` 由存储层注入，
    避免执行层直接依赖身份契约。
    """
    out: List[EvictionCandidate] = []
    for n in nodes:
        if n.tier is not MemoryTier.LONG_TERM or n.superseded_by is not None:
            continue
        ns = normalised_strength(n, half_life_of(n), affinity_of(n), now, cfg)
        if ns < cfg.eviction_threshold:
            out.append(
                EvictionCandidate(
                    ref=n.ref,
                    normalised_strength=ns,
                    reason=f"normalised strength {ns:.4f} < {cfg.eviction_threshold}",
                )
            )
    return out
