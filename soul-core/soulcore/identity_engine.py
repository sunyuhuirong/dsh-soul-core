"""身份一致性引擎 (Identity Consistency Engine, ICE)。

职责
----
在**每条候选记忆进入长期存储之前**，判定它与「自我核心」的相符程度，
并给出可执行的处置指令（reinforce / store / strengthen / quarantine / revoke）。

它是主动遗忘的**因果源头**：ICE 产出的 `affinity` 不是审计日志，
而是直接进入执行层衰减函数的乘子。偏离核心的记忆不是因为「旧」而被遗忘，
而是因为它与核心不一致 -> 衰减更快 -> 在巩固中被淘汰。

三个判定信号（混合式，可解释、可离线跑）
----------------------------------------
1. `alignment`  语义一致性：候选记忆与各 claim assertion 的加权最大相似度。
2. `agreement`  规则一致性：claim.decisionRule 谓词对候选特征求值的加权支持率。
3. `contradiction` 矛盾度：命中红线 / 否定型决策规则 / 降级级联。

最终 `affinity` 同时驱动两件事：存储准入（gate）与衰减率（decay multiplier）。
"""

from __future__ import annotations

import dataclasses
import re
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .contract import RedLine, RetentionPolicy, SoulClaim, SoulCore
from .errors import UnauthorizedOverride

# ---------------------------------------------------------------------------
# 可选：语义嵌入后端（生产用）。缺省为零依赖的词法后端。
# ---------------------------------------------------------------------------

EmbedFn = Callable[[Sequence[str]], Sequence[Sequence[float]]]

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}")

_STOPWORDS: Set[str] = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this", "these",
    "those", "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did",
    "have", "has", "had", "will", "would", "shall", "should", "can", "could", "may",
    "might", "must", "of", "in", "on", "at", "to", "for", "from", "by", "with", "about",
    "into", "over", "after", "before", "as", "it", "its", "i", "you", "he", "she", "they",
    "we", "me", "my", "your", "our", "their", "not", "no", "yes", "so", "just", "very",
    "user", "prefer", "prefers", "like", "likes", "want", "wants",
    "please", "would", "should", "let", "get", "got", "make", "made", "also", "more",
    "most", "such", "some", "any", "all", "each", "every", "when", "while", "how", "what",
    "which", "who", "whom", "why", "where", "there", "here", "now", "new", "old",
}

# 子句切分：拒绝/条件句常把「我不会」与被引用的危险短语分开，必须分段匹配。
_CLAUSE_SPLIT_RE = re.compile(r"[,;，；。！？\n]+")
_NEGATION_AFTER_RE = re.compile(r"\bnot\b|不再|不是")

# 否定词：用于把「命中决策规则关键词但语义相反」判为矛盾，而不是支持。
_NEGATIONS: Tuple[str, ...] = (
    "not ", "n't", "no longer", "never", "不再", "不是", "别", "拒绝", "反对", "不要",
    "停止", "放弃", "弃用", "without ",
)


def tokenize(text: str) -> Set[str]:
    """零依赖分词：英文词 + 中文二元组，去停用词。"""
    raw = [t for t in _TOKEN_RE.findall(text.lower())]
    return {t for t in raw if t not in _STOPWORDS and len(t) > 1}


def lexical_similarity(a: Set[str], b: Set[str]) -> float:
    """词法相似度（零依赖）。

    `b` 是身份主张侧的 token 集合（通常较长），`a` 是候选记忆。
    单纯用交集/Jaccard 会系统性惩罚短记忆 —— 一条 8 词的记忆讲的就是
    「引用可核查来源」这件事，却因为 assertion 长而拿不到分，方向上是致命的。

    因此采用 **coverage 主导 + cosine 平衡**：

        sim = max(cosine, 0.5 * cosine + 0.5 * coverage)

    其中 `coverage = |a ∩ b| / |a|`（候选记忆有多少比例的内容确实在讲该主张）。
    完全切题的短记忆 coverage ≈ 1，因此 sim 显著抬高；与主张完全无关时
    coverage = cosine = 0，仍然得 0。
    """
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    cosine = inter / (float(len(a)) * float(len(b))) ** 0.5
    coverage = inter / float(len(a))
    return max(cosine, 0.5 * cosine + 0.5 * coverage)


def _coverage(candidate: Set[str], needles: Set[str]) -> float:
    """needles 中有多大比例出现在 candidate 里。"""
    if not needles:
        return 0.0
    return len(needles & candidate) / float(len(needles))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    if da == 0.0 or db == 0.0:
        return 0.0
    return max(0.0, min(1.0, num / (da * db)))


# ---------------------------------------------------------------------------
# 判定结果
# ---------------------------------------------------------------------------


class Decision(str, Enum):
    """ICE 的处置指令。执行层必须按此路由，不得自行放宽。"""

    REINFORCE = "reinforce"  # 强化：高度一致，同时反哺 identity claim 的强度
    STORE = "store"          # 常规存储
    STRENGTHEN = "strengthen"  # 与既有记忆重复：合并并强化，不新增节点
    QUARANTINE = "quarantine"  # 模糊区间：隔离待审，不得进入长期存储
    REVOKE = "revoke"        # 与核心/红线冲突：拒绝存储，仅留 tombstone


@dataclasses.dataclass(frozen=True)
class ClaimMatch:
    claim_id: str
    relation: str  # "supports" | "contradicts" | "neutral"
    similarity: float

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class SoulVerdict:
    """ICE 的完整输出。可序列化，可审计，可回放。"""

    memory_ref: str
    decision: Decision
    alignment: float      # 0-1：与 identityCore 的语义一致性
    agreement: float      # 0-1：与决策规则的规则一致性
    contradiction: float  # 0-1：矛盾度
    affinity: float       # 0-1：驱动衰减率的身份亲和度
    matched_claims: Tuple[str, ...] = ()
    violated_claims: Tuple[str, ...] = ()
    red_lines_hit: Tuple[str, ...] = ()
    retention_class: str = "standard"
    reason: str = ""

    @property
    def is_identity_bearing(self) -> bool:
        return self.decision is Decision.REINFORCE

    def as_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["decision"] = self.decision.value
        return d


# ---------------------------------------------------------------------------
# 规则求值（decisionRule）
# ---------------------------------------------------------------------------


def _feats(text: str) -> Dict[str, Any]:
    tl = text.lower()
    return {
        "text": text,
        "lower": tl,
        "tokens": tokenize(text),
        "negated": any(n in tl for n in _NEGATIONS),
    }


def _eval_rule(rule: Mapping[str, Any], text: str) -> Optional[bool]:
    """返回 True=支持, False=矛盾, None=不适用。

    故意做得很小：身份治理不该是图灵完备的规则引擎。
    """
    feats = _feats(text)
    op = str(rule.get("op", ""))

    if op == "contains_any":
        needles = [str(n).lower() for n in rule.get("needles", [])]
        hit = any(n in feats["lower"] for n in needles)
        if not hit:
            return None
        # 命中但被否定 -> 矛盾（例如 "no longer writes Chinese"）
        if rule.get("negation_aware", True) and feats["negated"]:
            return False
        return True

    if op == "contains_all":
        needles = [str(n).lower() for n in rule.get("needles", [])]
        if not all(n in feats["lower"] for n in needles):
            return None
        return not (rule.get("negation_aware", True) and feats["negated"])
    if op == "token_overlap":
        needles = {str(n).lower() for n in rule.get("needles", [])}
        ratio = len(needles & feats["tokens"]) / float(max(1, len(needles)))
        return ratio >= float(rule.get("threshold", 0.5))
    if op == "coverage":
        # 要求记忆覆盖某个语义簇的关键词比例达到阈值（簇内允许同义/变体表述）
        needles: Set[str] = set()
        for n in rule.get("needles", []):
            needles |= tokenize(str(n))
        if not needles:
            return None
        if _coverage(feats["tokens"], needles) < float(rule.get("threshold", 0.5)):
            return None
        return not (rule.get("negation_aware", True) and feats["negated"])
    return None


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class IdentityConsistencyEngine:
    """身份一致性引擎。

    参数
    ----
    soul: 加载后的身份契约。
    embed_fn: 可选的嵌入后端；传入则由语义相似度替代词法相似度，
              并有 `embed_weight` 控制语义项在 alignment 中的占比。
    """

    def __init__(
        self,
        soul: SoulCore,
        embed_fn: Optional[EmbedFn] = None,
        embed_weight: float = 0.7,
        alignment_weights: Tuple[float, float] = (0.7, 0.3),  # (semantic, rule)
    ) -> None:
        self.soul = soul
        self.retention: RetentionPolicy = soul.retention
        self._embed_fn = embed_fn
        self._embed_weight = embed_weight
        self._w_sem, self._w_rule = alignment_weights
        self._rule_alignment_floor = 0.8
        self._claim_tokens: Dict[str, Set[str]] = {
            c.id: tokenize(f"{c.statement} {c.assertion}") for c in soul.all_claims
        }
        self._embeddings: Optional[Dict[str, Sequence[float]]] = None

    # -- 相似度 -------------------------------------------------------------
    def _similarity(self, cand_tokens: Set[str], claim: SoulClaim) -> float:
        lex = lexical_similarity(cand_tokens, self._claim_tokens[claim.id])
        if self._embed_fn is None or self._embeddings is None:
            return lex
        cand_vec = self._embed(cand_tokens)
        claim_vec = self._embeddings.get(claim.id)
        if cand_vec is None or claim_vec is None:
            return lex
        sem = _cosine(cand_vec, claim_vec)
        return self._embed_weight * sem + (1.0 - self._embed_weight) * lex

    def alignment_score(self, text: str) -> float:
        """只返回 alignment（0-1），不带任何存储阈值判定。

        用于评估层（drift / resilience），因为评估不该受
        `minAlignmentToStore` 这类**存储**参数影响。
        """
        cand = tokenize(text)
        weight_sum = 0.0
        acc = 0.0
        for claim in self.soul.all_claims:
            sim = self._similarity(cand, claim)
            if claim.decision_rule and _eval_rule(claim.decision_rule, text) is True:
                sim = max(sim, self._rule_alignment_floor)
            weight_sum += claim.weight
            acc += claim.weight * sim
        return round(acc / weight_sum, 4) if weight_sum else 0.0

    def _embed(self, tokens: Set[str]) -> Optional[Sequence[float]]:
        if self._embed_fn is None:
            return None
        try:
            vecs = self._embed_fn([" ".join(sorted(tokens))])
            return vecs[0] if vecs else None
        except Exception:  # 嵌入失败不得阻断治理；降级为词法
            return None

    def prepare(self) -> None:
        """预计算 claim 嵌入（在线服务启动时调用一次）。"""
        if self._embed_fn is None:
            return
        texts = [f"{c.statement} {c.assertion}" for c in self.soul.all_claims]
        vecs = self._embed_fn(texts)
        self._embeddings = {c.id: v for c, v in zip(self.soul.all_claims, vecs)}

    # -- 主流程 -------------------------------------------------------------
    def evaluate(self, memory_ref: str, text: str) -> SoulVerdict:
        """判定一条候选记忆。这是本引擎唯一的入口。"""
        cand_tokens = tokenize(text)

        # 0) 退役指令优先于一切
        retraction = self.soul.is_retracted(memory_ref)
        if retraction is not None:
            return SoulVerdict(
                memory_ref=memory_ref,
                decision=Decision.REVOKE,
                alignment=0.0,
                agreement=0.0,
                contradiction=1.0,
                affinity=0.0,
                retention_class="revoked",
                reason=f"retracted by governance: {retraction.reason}",
            )

        # 1) 红线：一票否决
        red_hits = [r.id for r in self.soul.red_lines if self._hits_red_line(text, r)]
        if red_hits:
            return SoulVerdict(
                memory_ref=memory_ref,
                decision=Decision.REVOKE,
                alignment=0.0,
                agreement=0.0,
                contradiction=1.0,
                affinity=0.0,
                red_lines_hit=tuple(red_hits),
                retention_class="revoked",
                reason=f"red line violated: {', '.join(red_hits)}",
            )

        # 2) 逐 claim 计算 relation
        matched: List[str] = []
        violated: List[str] = []
        supports: Dict[str, float] = {}
        weight_sum = 0.0
        align_acc = 0.0
        agree_acc = 0.0
        agree_w = 0.0
        rule_collisions = 0

        for claim in self.soul.all_claims:
            rule_result = _eval_rule(claim.decision_rule or {}, text) if claim.decision_rule else None
            sim = self._similarity(cand_tokens, claim)
            if rule_result is True:
                # 规则命中（且未被否定）是「这条记忆就是在执行该主张」的强证据；
                # 词法相似度对同义改写脆弱，因此给一个对齐下限，
                # 使治理决策不过度依赖粗糙的词面打分。
                sim = max(sim, self._rule_alignment_floor)

            relation = "neutral"
            if rule_result is True:
                relation = "supports"
            elif rule_result is False:
                relation = "contradicts"
                rule_collisions += 1
            elif sim >= float((claim.decision_rule or {}).get("similarity_threshold", 0.45)):
                relation = "supports"

            if relation == "supports":
                matched.append(claim.id)
                supports[claim.id] = claim.strength
            elif relation == "contradicts":
                violated.append(claim.id)

            weight_sum += claim.weight
            align_acc += claim.weight * sim
            if rule_result is not None:
                agree_acc += claim.weight * (1.0 if rule_result else 0.0)
                agree_w += claim.weight

        alignment = align_acc / weight_sum if weight_sum else 0.0
        agreement = agree_acc / agree_w if agree_w else 0.5

        # 3) 矛盾度：红线已处理，这里处理规则碰撞
        denom = float(max(1, len(self.soul.all_claims)))
        contradiction = min(1.0, rule_collisions / denom * 2.0)

        # 4) 身份亲和度
        affinity = max(0.0, min(1.0, self._w_sem * alignment + self._w_rule * agreement))
        affinity = max(0.0, affinity * (1.0 - 0.8 * contradiction))

        # 5) 处置路由
        decision, retention_class, reason = self._route(
            affinity=affinity,
            alignment=alignment,
            agreement=agreement,
            contradiction=contradiction,
            supports=supports,
            violated=violated,
        )
        return SoulVerdict(
            memory_ref=memory_ref,
            decision=decision,
            alignment=round(alignment, 4),
            agreement=round(agreement, 4),
            contradiction=round(contradiction, 4),
            affinity=round(affinity, 4),
            matched_claims=tuple(matched),
            violated_claims=tuple(violated),
            red_lines_hit=(),
            retention_class=retention_class,
            reason=reason,
        )

    # -- 路由规则 -----------------------------------------------------------
    def _route(
        self,
        affinity: float,
        alignment: float,
        agreement: float,
        contradiction: float,
        supports: Mapping[str, float],
        violated: Sequence[str],
    ) -> Tuple[Decision, str, str]:
        r = self.retention
        low, high = r.quarantine_band

        if violated:
            return (
                Decision.REVOKE,
                "revoked",
                f"contradicts core claims {sorted(violated)}",
            )
        # 命中 locked core -> 强化（反哺 claim 强度）。
        # 判据取「同时命中多条核心主张」或「单条高度一致」：
        # 只要一条核心主张被命中就升级为 identity_core 会让几乎所有正常工作记忆
        # 都变成身份记忆，稀释 leadership 信号。
        core_ids = {c.id for c in self.soul.core_claims}
        hit_core = sorted(set(supports) & core_ids)
        if hit_core and (len(hit_core) >= 2 or affinity >= 0.70 or alignment >= 0.75):
            return (
                Decision.REINFORCE,
                "identity_core",
                f"reinforces core claims {hit_core}",
            )
        if affinity >= r.min_alignment_to_store:
            return (Decision.STORE, "standard", f"affinity {affinity:.3f} >= floor")
        if low <= affinity < max(high, r.min_alignment_to_store):
            return (
                Decision.QUARANTINE,
                "quarantine",
                f"affinity {affinity:.3f} in ambiguous band -> quarantined, not promoted",
            )
        return (
            Decision.REVOKE,
            "divergent",
            f"affinity {affinity:.3f} below floor {r.min_alignment_to_store} -> not stored",
        )

    @staticmethod
    def _hits_red_line(text: str, red: RedLine) -> bool:
        """红线命中的判定（两种形式），并要求**不是被否定地提及**。

        1. 整条短语是候选文本的子串；
        2. **needle group**：用 `+` 连接的词组，其所有部分都必须出现。
           这解决词形变化（invent/invented/inventing）与语序差异
           （"invent a patent number" vs "invented patent number"），
           同时避免裸词（如 "patent"）造成大面积误杀。

        「I will not invent a patent number」这类**拒绝表述**不应触发红线，
        否则一个正确拒答的 agent 会被自己的护栏判违规。
        """
        tl = text.lower()
        for trigger in red.triggers:
            if not trigger:
                continue
            if "+" in trigger:
                parts = [p.strip() for p in trigger.split("+") if p.strip()]
                if parts and all(p in tl for p in parts):
                    spans = [tl.find(p) for p in parts]
                    if not IdentityConsistencyEngine._is_negated_at(tl, min(spans)):
                        return True
            elif trigger in tl:
                idx = tl.find(trigger)
                if not IdentityConsistencyEngine._is_negated_at(tl, idx):
                    return True
        return False

    @staticmethod
    def _is_negated_at(tl: str, idx: int, window: int = 24) -> bool:
        """检查匹配位置之前的小窗口内是否存在否定标记。"""
        if idx <= 0:
            return False
        prefix = tl[max(0, idx - window):idx]
        return any(n in prefix for n in _NEGATIONS)

    # -- 衰减率调制（与执行层的因果接口）-------------------------------------
    def affinity_coefficient(self, v: SoulVerdict) -> float:
        """把 affinity 映射为衰减半衰期的乘子。

        - 核心一致 (affinity=1.0) -> max (默认 2.0，寿命 x2)
        - 无关内容 (affinity=0.0) -> min (默认 0.25，寿命 /4)
        这就是「偏离身份的记忆自然衰减」的数学形式，而不是一句口号。
        """
        lo, hi = self.retention.identity_affinity_min, self.retention.identity_affinity_max
        return lo + (hi - lo) * v.affinity

    def reinforce_claims(self, v: SoulVerdict) -> Dict[str, float]:
        """强化路径：返回本次操作对 claim 强度的增量（由存储层落盘）。"""
        if v.decision is not Decision.REINFORCE:
            return {}
        return {cid: self.retention.reinforcement_delta for cid in v.matched_claims}


def require_human_override(approved_by: Optional[str], rationale: Optional[str] = None) -> None:
    """写入偏离核心的记忆必须有人类主体授权，否则拒绝。

    这是「治理」与「无原则漂移」的分界线。
    """
    if not approved_by or not str(approved_by).strip():
        raise UnauthorizedOverride(
            "storing identity-divergent memory requires approved_by=<human principal>"
        )
    if rationale is None or not str(rationale).strip():
        raise UnauthorizedOverride(
            "storing identity-divergent memory requires a non-empty rationale"
        )
