"""记忆层：记住「人」与「事」，并以阴阳对立的方式遗忘。

为什么需要这一层（现有内核的缺口）
----------------------------------
`decay.MemoryNode` 是**扁平文本节点**：它能记住「一句话」，但记不住
「张三这个人是谁」，也没有「我何时对他做过什么」的时间线。
`store.MemoryStore.observe()` 是唯一的写入口，类别（category）里虽然有个
`episode`，却没有实体维度、没有实体链接、没有按人检索。

本模块补齐这一层，且**不重复实现任何已有算法**：

    identity_engine.lexical_similarity / tokenize   -> 实体归并与检索打分
    decay.effective_half_life_days / strength_at    -> 衰减数学（复用，不重写）
    decay.MemoryNode                                -> 评分载体（Person 通过组合持有）
    store.MemoryStore                               -> identity_critical 事实的治理通道
    contract.SoulCore.prototype_for / retention     -> 半衰期与亲和度映射的来源

阴阳对立（本模块的第一原则）
----------------------------
1. **有为 / 无为**：不主动为每个陌生人建档。首次提及只落一枚「待定印记」
   （`EMERGING`）；第二次出现才转正（`ACTIVE`）。这样「记」由接触本身决定，
   而不是由 agent 硬记决定。
2. **物极必反**：记忆不是「留则全留、去则全去」。事件在 `FADED` 阶段被压缩成
   一行要点（名字与关系保留，对话细节丢掉）；人极久不见才 `ARCHIVED` 成一行残影。
   **被遗忘的是细节，不是关系。**
3. **反者道之动**：再次相遇时，`ARCHIVED` 的人会被 `revive()` 复活——
   遗忘是可达性下降，不是抹除。
4. **刚柔相济**：命中红线的事实是「刚」（永不淘汰），日常闲谈是「柔」（很快散）。
   区分二者的不是内容长度，而是身份亲和度与是否红线。
5. **损有余而补不足**：高权重人物继续吸收加成；长期不用的被削。平衡由机制保证，
   不靠容量上限。

生命周期
--------
::

    EMERGING --2nd encounter--> ACTIVE --沉默 DORMANT_AFTER_DAYS--> DORMANT
        |                                                              |
        | 只此一次                                                     | 再沉默 ARCHIVE_AFTER_DAYS
        v                                                              v
    （仍会被 consolidate 清理）                                    ARCHIVED（一行残影）
                                                                       |
                                                          再次相遇 revive() 回到 ACTIVE

事件：ACTIVE --衰减--> FADED（压缩成一行）--再衰减--> pruned（移出，留账）
``identity_critical`` 的人与事（命中红线 / 身份核心）**永不进入 FADED/ARCHIVED**。

存储
----
同一套 append-only JSONL 约定，落在独立的 `memory.jsonl`：与 `audit.jsonl`
共用 `store.JsonlPersistence` / `store.NullPersistence` 协议，可 diff、可重放。
"""

from __future__ import annotations

import dataclasses
import re
import time
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contract import SoulCore
from .decay import (
    DAY,
    DecayConfig,
    MemoryNode,
    MemoryTier,
    normalised_strength,
    strength_at,
)
from .identity_engine import lexical_similarity, tokenize
from .scoring import ScoreVector, ScoringProfile

# ---------------------------------------------------------------------------
# 命名空间（ref 前缀）与检索权重
# ---------------------------------------------------------------------------

PERSON_PREFIX = "person:"
EVENT_PREFIX = "event:"

#: 事件挂到人上时使用的边类型。
EDGE_INTERACTED = "interacted"
EDGE_INVOLVED = "involved"
EDGE_MENTIONED = "mentioned"

#: 查询分片：连续的 CJK 片段，或连续的拉丁字母/数字片段
_piece_re = re.compile(r"[\u4e00-\u9fff]+|[a-z0-9]+")

#: 允许「子串包含即命中」的最大分片长度（人名量级）
_SHORT_NAME_MAX_CHARS = 3


def _bigrams(text: str) -> set:
    """字符二元组。中文没有词边界，tokenize() 又会丢掉长度 <=1 的片段，
    因此「张三」这类短名在词法相似度里会整体消失。bigram 是零依赖的补救。"""
    compact = "".join(text.split()).lower()
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def text_similarity(query: str, haystack: str) -> float:
    """检索相关性 = max(词法, 字符bigram, 短名包含)。

    三条路径各自对付一类查询：
      - 词法（英文词 + 中文二元组）对付长文本；
      - 字符 bigram 对付 2-3 字的中文名（tokenize 会把它整体过滤掉）；
      - **短名包含**对付「张三」这种会被词法丢掉的精确人名：
        query 的某个 <=3 字片段原样出现在 haystack 中，就是最强证据。
    """
    lexical = lexical_similarity(tokenize(query), tokenize(haystack))
    gram = 0.0
    a, b = _bigrams(query), _bigrams(haystack)
    if a and b:
        union = len(a | b)
        gram = (len(a & b) / union) if union else 0.0
    contained = 0.0
    squeezed = "".join(haystack.split()).lower()
    for piece in _piece_re.findall(query.lower()):
        if len(piece) <= _SHORT_NAME_MAX_CHARS and piece in squeezed:
            contained = 1.0
            break
    return max(lexical, gram, contained)


#: 末尾编号：`联系人1` / `case-07` / `工单003`
_NUMBERED_RE = re.compile(r"^(.*?)(\d+)(\D*)$")


def _numbered_identifiers_differ(left: str, right: str) -> bool:
    """两个名字是否「除末尾编号外完全相同」——是则必为不同个体。

    bigram Jaccard 在「编号位数不同」时会虚高：
    `一次性联系人1` vs `一次性联系人10` 得 0.857，越过任何合理阈值，
    于是 50 个连号的一次性联系人在 10 个之后被连锁并成一个人。
    编号本身就是身份的一部分，必须显式承认，而不是靠调阈值掩盖。
    """
    a = _NUMBERED_RE.match(left)
    b = _NUMBERED_RE.match(right)
    if a is None or b is None:
        return False
    return a.group(1) == b.group(1) and a.group(3) == b.group(3) and a.group(2) != b.group(2)


def name_similarity(left: str, right: str) -> float:
    """人名相似度 = 字符 bigram Jaccard（精确相同为 1.0）。

    刻意**不**用 tokenize（它会把单字与短名整体过滤掉），
    也刻意**不**用子串包含：子串关系不是等价关系（会传递）。
    末尾编号不同的名字直接判为不同人。

    真正的同人关系应由 `aliases` 显式声明，而不是靠模糊猜测 ——
    宁可留下两个待合并的档案（可被 consolidate 归档合并），
    也不要把两个人并成一个（不可逆）。
    """
    a, b = "".join(left.split()).lower(), "".join(right.split()).lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if _numbered_identifiers_differ(a, b):
        return 0.0
    ga, gb = _bigrams(a), _bigrams(b)
    union = len(ga | gb)
    return (len(ga & gb) / union) if union else 0.0


#: 检索打分权重，与 `MemoryStore.recall` 同构（relevance 主导，其余加权）。
W_RELEVANCE = 1.0
W_AFFINITY = 0.5
W_IMPORTANCE = 0.4
W_STRENGTH = 0.3


# ---------------------------------------------------------------------------
# 配置（全部可调，全部有默认值 —— 与 DecayConfig 同一风格）
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MemoryPolicy:
    """记忆层的超参数。

    半衰期是**原型**，仍要乘身份亲和度系数（沿用 `decay` 的公式），
    因此「越贴近自我核心的人与事忘得越慢」这条因果链在本层同样成立。
    """

    # -- 衰减原型（天）----------------------------------------------------
    person_half_life_days: float = 90.0
    event_half_life_days: float = 30.0

    # -- 生命周期阈值（天）------------------------------------------------
    dormant_after_days: float = 60.0
    archive_after_days: float = 180.0
    #: 「物极必反」的极端：休眠超过这么久，人也被淘汰成一行残影
    prune_after_days: float = 720.0
    #: 事件变 FADED 的归一化强度阈值
    faded_below: float = 0.30
    #: 事件被移出的归一化强度阈值
    event_prune_below: float = 0.05

    # -- 实体归并 ---------------------------------------------------------
    #: 视为「短名」的最大长度：短名允许子串命中（张三 命中 张三丰的档案）
    alias_inclusion_max_chars: int = 3
    #: 两个不同人之间视为同一人的最小相似度。
    #: 0.85 是实测标定值，取向是「宁可不并，也不错并」：
    #:   `张三` / `张三丰` = 0.50（不并，需显式别名）
    #:   `Robert` / `Bobby` = 0.13（不并）
    #:   `一次性联系人1` / `一次性联系人2` = 0.71（不并）
    #: 真正的同人关系应通过 aliases 显式声明，而不是靠模糊猜测。
    entity_merge_similarity: float = 0.85

    # -- 强化 -------------------------------------------------------------
    #: 再次相遇对强度的加成
    encounter_bonus: float = 0.25
    strength_cap: float = 5.0

    # -- 注入预算 ---------------------------------------------------------
    #: 每轮注入提示词的最大字符数（超出即截断，防止记忆挤占上下文）
    inject_max_chars: int = 1200
    #: 注入时最多列出几个人
    inject_max_persons: int = 5


def _coerce_policy(value: Any) -> MemoryPolicy:
    """把 mapping / MemoryPolicy / None 统一成 MemoryPolicy。"""
    if value is None:
        return MemoryPolicy()
    if isinstance(value, MemoryPolicy):
        return value
    if isinstance(value, Mapping):
        fields = {f.name for f in dataclasses.fields(MemoryPolicy)}
        unknown = set(value) - fields
        if unknown:
            raise ValueError(f"unknown MemoryPolicy field(s): {sorted(unknown)}")
        return MemoryPolicy(**value)
    raise TypeError("policy must be MemoryPolicy, a mapping, or None")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


class PersonState(str, Enum):
    """人的生命周期（阴阳消长）。"""

    EMERGING = "emerging"    # 待定：只出现过一次
    ACTIVE = "active"        # 在关系中
    DORMANT = "dormant"      # 久未相遇，开始褪色
    ARCHIVED = "archived"    # 一行残影，可被 revive()


class EventState(str, Enum):
    """事的状态（有与无、详与略）。"""

    ACTIVE = "active"        # 完整保留
    FADED = "faded"          # 压缩成一行（细节已忘，事还在）
    PRUNED = "pruned"        # 移出活跃记忆，仅留账目


@dataclasses.dataclass
class Person:
    """一个接触过的人。

    `names[0]` 是主名。别名包含说话人称呼、署名、@handle 等所有叫法。
    `score` / `affinity` / `alignment` 复用内核语义，使衰减与检索和普通记忆同源。
    """

    pid: str
    names: List[str]
    role: str = ""
    notes: str = ""
    state: PersonState = PersonState.EMERGING
    score: ScoreVector = dataclasses.field(default_factory=lambda: ScoreVector(0.0, 0.0, 0.0, 0.0))
    affinity: float = 0.0
    alignment: float = 0.0
    retention_class: str = "standard"
    identity_critical: bool = False
    first_seen_at: float = dataclasses.field(default_factory=time.time)
    last_seen_at: float = dataclasses.field(default_factory=time.time)
    encounters: int = 1
    accesses: List[float] = dataclasses.field(default_factory=list)
    strength_boost: float = 0.0
    merged_from: List[str] = dataclasses.field(default_factory=list)

    # -- 便捷 ---------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.names[0] if self.names else self.pid

    def knows(self, alias: str) -> bool:
        return alias in self.names

    def age_days(self, now: Optional[float] = None, *, since_last_seen: bool = True) -> float:
        base = self.last_seen_at if since_last_seen else self.first_seen_at
        now = time.time() if now is None else now
        return max(0.0, (now - base) / DAY)

    def as_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["state"] = self.state.value
        d["score"] = self.score.as_dict()
        return d


@dataclasses.dataclass
class Event:
    """一件发生过的事。

    `summary` 是压缩后的表述（FADED 之后只剩它）；
    `detail` 是原始细节（FADED 之后丢弃）。

    保留 `memory_ref` 指向 `MemoryStore` 中的完整文本节点，使得
    identity_critical 的事件仍然能通过治理通道取回全文。
    """

    eid: str
    summary: str
    detail: str = ""
    kind: str = "occurrence"
    participants: List[str] = dataclasses.field(default_factory=list)
    state: EventState = EventState.ACTIVE
    score: ScoreVector = dataclasses.field(default_factory=lambda: ScoreVector(0.0, 0.0, 0.0))
    affinity: float = 0.0
    alignment: float = 0.0
    retention_class: str = "standard"
    identity_critical: bool = False
    memory_ref: Optional[str] = None
    occurred_at: float = dataclasses.field(default_factory=time.time)
    recorded_at: float = dataclasses.field(default_factory=time.time)
    accesses: List[float] = dataclasses.field(default_factory=list)
    strength_boost: float = 0.0
    reason_forgotten: Optional[str] = None

    @property
    def text(self) -> str:
        """参与相似度计算的文本：FADED 后只剩 summary。"""
        return self.summary if self.state is not EventState.ACTIVE or not self.detail else f"{self.summary} {self.detail}"

    def age_days(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, (now - self.occurred_at) / DAY)

    def as_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["state"] = self.state.value
        d["score"] = self.score.as_dict()
        return d


@dataclasses.dataclass(frozen=True)
class Edge:
    """人 ↔ 事 的边，构成时间线。"""

    pid: str
    eid: str
    kind: str = EDGE_INTERACTED
    weight: float = 1.0
    created_at: float = dataclasses.field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class MemoryHit:
    """一条检索命中。`node` 可能是 Person 也可能是 Event。"""

    node: Any
    score: float
    relevance: float
    kind: str  # "person" | "event"

    @property
    def ref(self) -> str:
        return self.node.pid if self.kind == "person" else self.node.eid

    @property
    def label(self) -> str:
        return self.node.name if self.kind == "person" else self.node.summary

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "label": self.label,
            "score": round(self.score, 4),
            "relevance": round(self.relevance, 4),
        }


@dataclasses.dataclass
class RecallResult:
    """检索结果 + 可直接注入提示词的文本块。"""

    query: str
    hits: List[MemoryHit] = dataclasses.field(default_factory=list)
    total_considered: int = 0
    truncated: bool = False

    @property
    def persons(self) -> List[MemoryHit]:
        return [h for h in self.hits if h.kind == "person"]

    @property
    def events(self) -> List[MemoryHit]:
        return [h for h in self.hits if h.kind == "event"]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "total_considered": self.total_considered,
            "truncated": self.truncated,
            "hits": [h.as_dict() for h in self.hits],
        }


@dataclasses.dataclass
class ObserveOutcome:
    """一次 observe() 的结果，调用方据此决定是否继续业务流程。"""

    pid: Optional[str] = None
    eid: Optional[str] = None
    resolved_existing: bool = False
    promoted: bool = False
    identity_critical: bool = False
    governance_decision: Optional[str] = None
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class MemoryReport:
    """一次 consolidate() 的结果：账目两边都要记。"""

    promoted: List[str] = dataclasses.field(default_factory=list)     # emerging -> active
    demoted: List[str] = dataclasses.field(default_factory=list)      # active -> dormant
    archived: List[str] = dataclasses.field(default_factory=list)     # dormant -> archived
    pruned: List[str] = dataclasses.field(default_factory=list)       # archived -> gone / event removed
    faded: List[str] = dataclasses.field(default_factory=list)        # event active -> faded
    revived: List[str] = dataclasses.field(default_factory=list)      # archived -> active
    merged: List[Tuple[str, str]] = dataclasses.field(default_factory=list)
    evaluated: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# 记忆账本
# ---------------------------------------------------------------------------


def _score_from_dict(raw: Mapping[str, Any]) -> ScoreVector:
    return ScoreVector(
        importance=float(raw.get("importance", 0.0)),
        surprise=float(raw.get("surprise", 0.0)),
        affect=float(raw.get("affect", 0.0)),
        composite=float(raw.get("composite", 0.0)),
        source=str(raw.get("source", "snapshot")),
    )


def _person_from_dict(raw: Mapping[str, Any]) -> "Person":
    return Person(
        pid=str(raw["pid"]),
        names=list(raw.get("names") or []),
        role=str(raw.get("role", "")),
        notes=str(raw.get("notes", "")),
        state=PersonState(raw.get("state", PersonState.EMERGING.value)),
        score=_score_from_dict(raw.get("score") or {}),
        affinity=float(raw.get("affinity", 0.0)),
        alignment=float(raw.get("alignment", 0.0)),
        retention_class=str(raw.get("retention_class", "standard")),
        identity_critical=bool(raw.get("identity_critical", False)),
        first_seen_at=float(raw.get("first_seen_at", 0.0)),
        last_seen_at=float(raw.get("last_seen_at", 0.0)),
        encounters=int(raw.get("encounters", 1)),
        accesses=[float(a) for a in (raw.get("accesses") or [])],
        strength_boost=float(raw.get("strength_boost", 0.0)),
        merged_from=list(raw.get("merged_from") or []),
    )


def _event_from_dict(raw: Mapping[str, Any]) -> "Event":
    return Event(
        eid=str(raw["eid"]),
        summary=str(raw.get("summary", "")),
        detail=str(raw.get("detail", "")),
        kind=str(raw.get("kind", "occurrence")),
        participants=list(raw.get("participants") or []),
        state=EventState(raw.get("state", EventState.ACTIVE.value)),
        score=_score_from_dict(raw.get("score") or {}),
        affinity=float(raw.get("affinity", 0.0)),
        alignment=float(raw.get("alignment", 0.0)),
        retention_class=str(raw.get("retention_class", "standard")),
        identity_critical=bool(raw.get("identity_critical", False)),
        memory_ref=raw.get("memory_ref"),
        occurred_at=float(raw.get("occurred_at", 0.0)),
        recorded_at=float(raw.get("recorded_at", 0.0)),
        accesses=[float(a) for a in (raw.get("accesses") or [])],
        strength_boost=float(raw.get("strength_boost", 0.0)),
        reason_forgotten=raw.get("reason_forgotten"),
    )


class MemoryLedger:
    """「人 + 事」的记忆账本。

    与 `MemoryStore` 的分工：

        MemoryStore   治理「一句话该不该记」——身份核心判定 + 三维评分 + 衰减
        MemoryLedger  治理「一段关系与它的事件」——实体归并 + 时间线 + 阴阳式遗忘

    两者共用同一套衰减数学与同一份身份契约；本类不重复实现任何已有算法。

    持久化：`store.Persistence` 协议的任意实现（默认 `NullPersistence`）。
    记录形如 `{"kind": "observe_person" | "observe_event" | "consolidate" | ..., ...}`，
    append-only，可按序重放。
    """

    def __init__(
        self,
        soul: SoulCore,
        policy: Optional[Any] = None,
        persistence: Optional[Any] = None,
        store: Optional[Any] = None,
        decay: Optional[DecayConfig] = None,
        profile: Optional[ScoringProfile] = None,
    ) -> None:
        from .store import NullPersistence

        self.soul = soul
        self.policy = _coerce_policy(policy)
        self.persistence = persistence if persistence is not None else NullPersistence()
        #: 可选的治理通道：identity_critical 的内容同时写入 MemoryStore
        self.store = store
        self.decay = decay or DecayConfig(eviction_threshold=soul.retention.eviction_threshold)
        self.profile = (profile or ScoringProfile()).normalise()

        self.persons: Dict[str, Person] = {}
        self.events: Dict[str, Event] = {}
        self.edges: List[Edge] = []
        #: 别名 -> pid（含主名与所有别名），用于实体归并
        self._alias_index: Dict[str, str] = {}
        self._person_seq = 0
        self._event_seq = 0
        #: 累计被淘汰的人数 / 事件数。淘汰后对象已从内存移除，
        #: 所以总吞吐 = kept + shed + shed_cumulative，三者必须对得上。
        self.pruned_persons = 0
        self.pruned_events = 0
        #: 内容 -> MemoryStore ref 的跨层去重索引（实例级）
        self._store_refs: Dict[str, str] = {}
        self.audit: List[Dict[str, Any]] = []

    # -- 序列号 -------------------------------------------------------------

    def _next_pid(self) -> str:
        self._person_seq += 1
        return f"{PERSON_PREFIX}{self._person_seq:06d}"

    def _next_eid(self) -> str:
        self._event_seq += 1
        return f"{EVENT_PREFIX}{self._event_seq:06d}"

    # -- 审计 ---------------------------------------------------------------

    def _log(self, event: str, **fields: Any) -> Dict[str, Any]:
        """追加一条审计记录。

        首参刻意不叫 `kind`：事件本身也有一个 `kind` 字段（interaction /
        occurrence / ...），同名会与 `**fields` 冲突。
        """
        record = {"kind": event, "at": time.time(), **fields}
        self.audit.append(record)
        try:
            self.persistence.append(record)
        except Exception:  # pragma: no cover - 持久化失败不应打断记忆流程
            pass
        return record

    def audit_trail(self) -> List[Dict[str, Any]]:
        return list(self.audit)

    # -- 治理：判断一条内容是否 identity_critical ---------------------------

    def _govern(self, text: str, category: str) -> Tuple[bool, str, float, float]:
        """复用内核的评分与判定，返回 (是否身份关键, 判定名, affinity, alignment)。

        `identity_critical` 的含义：命中红线，或判定为 reinforce/identity_core。
        这类人与事**永不**进入 FADED / ARCHIVED —— 这是「刚」的一侧。
        """
        score = self._score(text, category)
        verdict = None
        if self.store is not None:
            # 借用 store 的 ICE 判定（同一份身份契约，同一套判定语义）
            verdict = self.store.ice.evaluate("probe", text)
        affinity = float(getattr(verdict, "affinity", 0.0)) if verdict is not None else 0.0
        alignment = float(getattr(verdict, "alignment", 0.0)) if verdict is not None else 0.0
        decision = ""
        retention = ""
        if verdict is not None:
            decision = getattr(getattr(verdict, "decision", None), "value", "")
            retention = str(getattr(verdict, "retention_class", ""))
        critical = bool(
            getattr(verdict, "red_lines_hit", ()) or retention == "identity_core" or decision == "reinforce"
        )
        return critical, decision or retention or "standard", affinity, alignment

    def _score(self, text: str, category: str) -> ScoreVector:
        """用与 store 相同的评分器打分，保证两个层次的尺度一致。"""
        if self.store is not None and getattr(self.store, "scorer", None) is not None:
            return self.store.scorer.score(text, category=category)
        from .scoring import HeuristicScorer

        return HeuristicScorer(self.profile).score(text, category=category)

    # -- 半衰期与亲和度（沿用内核公式，不重写衰减数学）----------------------

    def half_life_of(self, node: Any) -> float:
        """节点的基础半衰期。

        优先用身份契约里登记的类别原型（`prototype_for` 对未知类别会兜底），
        契约没登记 `person` / `event` 时回退到本层原型。
        因此新增记忆类型**不需要改身份契约**。
        """
        if isinstance(node, Person):
            if any(p.category == "person" for p in self.soul.decay_prototypes):
                return self.soul.prototype_for("person").base_half_life_days
            return self.policy.person_half_life_days
        if isinstance(node, Event):
            if any(p.category == "event" for p in self.soul.decay_prototypes):
                return self.soul.prototype_for("event").base_half_life_days
            return self.policy.event_half_life_days
        return self.policy.event_half_life_days

    def affinity_coefficient_of(self, node: Any) -> float:
        """亲和度 -> 半衰期乘子（与 `MemoryStore.affinity_coefficient_of` 同一公式）。"""
        lo = self.soul.retention.identity_affinity_min
        hi = self.soul.retention.identity_affinity_max
        return lo + (hi - lo) * max(0.0, min(1.0, float(getattr(node, "affinity", 0.0))))

    def _as_memory_node(self, node: Any, ref: str) -> MemoryNode:
        """把 Person/Event 适配成 `decay` 需要的 MemoryNode（组合，非继承）。

        这样 `strength_at` / `normalised_strength` / `effective_half_life_days`
        可以原样复用，不需要在衰减函数里加任何分支。
        """
        return MemoryNode(
            ref=ref,
            content=node.summary if isinstance(node, Event) else " ".join(node.names),
            category="person" if isinstance(node, Person) else "episode",
            score=node.score,
            affinity=node.affinity,
            alignment=node.alignment,
            retention_class=node.retention_class,
            decision="reinforce" if node.identity_critical else "store",
            created_at=node.occurred_at if isinstance(node, Event) else node.first_seen_at,
            accesses=list(node.accesses),
            strength_boost=node.strength_boost,
            tier=MemoryTier.LONG_TERM,
        )

    def normalised_strength(self, node: Any, now: Optional[float] = None) -> float:
        """归一化强度（0-1），用于生命周期判定。"""
        ref = node.pid if isinstance(node, Person) else node.eid
        return normalised_strength(
            self._as_memory_node(node, ref),
            self.half_life_of(node),
            self.affinity_coefficient_of(node),
            now,
            self.decay,
        )

    def strength(self, node: Any, now: Optional[float] = None) -> float:
        ref = node.pid if isinstance(node, Person) else node.eid
        return strength_at(
            self._as_memory_node(node, ref),
            self.half_life_of(node),
            self.affinity_coefficient_of(node),
            now,
            self.decay,
        )

    # -- 实体归并（无为：不轻易为陌生人建档）--------------------------------

    def resolve_person(self, name: str, aliases: Optional[Sequence[str]] = None) -> Tuple[Optional[Person], float]:
        """按名字/别名找已有的人，找不到返回 (None, 最高相似度)。

        匹配顺序（先精确、后模糊）：
          1. 别名索引精确命中；
          2. 短路名（≤3 字）作为子串出现在候选内容中；
          3. 词法相似度 ≥ `entity_merge_similarity`。
        """
        wanted = [n.strip() for n in [name, *(aliases or [])] if n and n.strip()]
        if not wanted:
            return None, 0.0

        for alias in wanted:
            pid = self._alias_index.get(alias)
            if pid is not None:
                return self.persons.get(pid), 1.0

        best: Optional[Person] = None
        best_sim = 0.0
        for person in self.persons.values():
            if person.state is PersonState.ARCHIVED and person.identity_critical:
                # 刚性的身份关键人物不参与模糊归并，避免被误合并
                continue
            if not person.names:
                continue
            sim = 0.0
            for alias in wanted:
                # 别名索引已处理精确命中；这里处理「别名是已知名的子串/超串」
                for known in person.names:
                    sim = max(sim, name_similarity(alias, known))
            if sim > best_sim:
                best_sim, best = sim, person
        if best is not None and best_sim >= self.policy.entity_merge_similarity:
            return best, best_sim
        return None, best_sim

    def _index_aliases(self, person: Person) -> None:
        for alias in person.names:
            self._alias_index.setdefault(alias, person.pid)

    def _merge_person(self, target: Person, incoming: Person, now: float) -> Person:
        """把新人合并进既有的人：别名并集、计数累加、强度取更强。"""
        target.names = list(dict.fromkeys([*target.names, *incoming.names]))
        target.encounters += incoming.encounters
        target.last_seen_at = max(target.last_seen_at, incoming.last_seen_at)
        target.first_seen_at = min(target.first_seen_at, incoming.first_seen_at)
        target.role = target.role or incoming.role
        target.notes = target.notes or incoming.notes
        target.affinity = max(target.affinity, incoming.affinity)
        target.alignment = max(target.alignment, incoming.alignment)
        target.identity_critical = target.identity_critical or incoming.identity_critical
        target.merged_from = [*target.merged_from, incoming.pid, *incoming.merged_from]
        target.strength_boost += self.policy.encounter_bonus
        self._index_aliases(target)
        return target

    # -- 写入口 1：记住一个人 -------------------------------------------------

    def observe_person(
        self,
        name: str,
        *,
        aliases: Optional[Sequence[str]] = None,
        role: str = "",
        notes: str = "",
        evidence: str = "",
        now: Optional[float] = None,
    ) -> ObserveOutcome:
        """记住「接触过一个人」。

        `evidence` 是触发这条记忆的原文（用于身份判定与审计）。
        首次提及 -> EMERGING（待定）；再次提及 -> 转 ACTIVE 并强化。
        """
        now = time.time() if now is None else now
        name = (name or "").strip()
        if not name:
            raise ValueError("observe_person: name must be a non-empty string")

        text = evidence or " ".join([name, role, notes])
        critical, decision, affinity, alignment = self._govern(text, "person")
        score = self._score(text, "person")

        existing, sim = self.resolve_person(name, aliases)
        if existing is not None:
            was_emerging = existing.state is PersonState.EMERGING
            was_archived = existing.state is PersonState.ARCHIVED
            incoming = Person(
                pid="",
                names=[name, *(aliases or [])],
                role=role,
                notes=notes,
                score=score,
                affinity=affinity,
                alignment=alignment,
                identity_critical=critical,
                first_seen_at=now,
                last_seen_at=now,
            )
            person = self._merge_person(existing, incoming, now)
            if was_archived:
                person.state = PersonState.ACTIVE
                outcome_note = "revived from archived"
            elif was_emerging:
                person.state = PersonState.ACTIVE
                outcome_note = f"promoted to active (2nd encounter, sim={sim:.2f})"
            else:
                person.state = PersonState.ACTIVE
                outcome_note = f"reinforced (encounter #{person.encounters}, sim={sim:.2f})"
            self._log("person_reinforced", pid=person.pid, name=person.name, encounters=person.encounters)
            return ObserveOutcome(
                pid=person.pid,
                resolved_existing=True,
                promoted=was_emerging or was_archived,
                identity_critical=person.identity_critical,
                governance_decision=decision,
                note=outcome_note,
            )

        person = Person(
            pid=self._next_pid(),
            names=list(dict.fromkeys([name, *(aliases or [])])),
            role=role,
            notes=notes,
            # 命中红线/身份核心的人不需要「待定观察期」：它已经证明了自己重要，
            # 直接进入 ACTIVE，否则会看到「身份关键却还在待定」的自相矛盾状态。
            state=PersonState.ACTIVE if critical else PersonState.EMERGING,
            score=score,
            affinity=affinity,
            alignment=alignment,
            retention_class="identity_core" if critical else "standard",
            identity_critical=critical,
            first_seen_at=now,
            last_seen_at=now,
        )
        self.persons[person.pid] = person
        self._index_aliases(person)
        self._log("person_observed", pid=person.pid, name=person.name, critical=critical)

        if self.store is not None and critical:
            self._mirror_to_store(person, text)

        return ObserveOutcome(
            pid=person.pid,
            identity_critical=critical,
            governance_decision=decision,
            note=(
                "new person recorded as active (身份关键，无需待定观察期)"
                if critical
                else "new person recorded as emerging (待定；再出现一次即转正)"
            ),
        )

    # -- 写入口 2：记住一件事 -------------------------------------------------

    def observe_event(
        self,
        summary: str,
        *,
        people: Optional[Sequence[Any]] = None,
        detail: str = "",
        kind: str = "interaction",
        occurred_at: Optional[float] = None,
        now: Optional[float] = None,
    ) -> ObserveOutcome:
        """记住「发生了一件事」，并把它挂到相关的人身上（时间线）。

        `people` 可以是 Person/pid 字符串，也可以是名字（会自动 resolve）。
        """
        now = time.time() if now is None else now
        summary = (summary or "").strip()
        if not summary:
            raise ValueError("observe_event: summary must be a non-empty string")

        text = f"{summary} {detail}".strip()
        critical, decision, affinity, alignment = self._govern(text, "episode")
        score = self._score(text, "episode")

        resolved: List[Person] = []
        for item in people or ():
            person = self._resolve_person_arg(item, now)
            if person is not None and person.pid not in [p.pid for p in resolved]:
                resolved.append(person)

        event = Event(
            eid=self._next_eid(),
            summary=summary,
            detail=detail,
            kind=kind,
            participants=[p.pid for p in resolved],
            score=score,
            affinity=affinity,
            alignment=alignment,
            retention_class="identity_core" if critical else "standard",
            identity_critical=critical,
            occurred_at=occurred_at if occurred_at is not None else now,
            recorded_at=now,
        )
        self.events[event.eid] = event

        for person in resolved:
            self.edges.append(Edge(pid=person.pid, eid=event.eid, kind=EDGE_INTERACTED, created_at=now))
            person.last_seen_at = max(person.last_seen_at, event.occurred_at)
            if person.state is PersonState.ARCHIVED:
                person.state = PersonState.ACTIVE
            # 事件把「人」也一起强化：关系因事件而更牢
            person.strength_boost = min(self.policy.strength_cap, person.strength_boost + self.policy.encounter_bonus)

        if self.store is not None and critical:
            event.memory_ref = self._mirror_to_store(event, text)

        self._log(
            "event_observed",
            eid=event.eid,
            event_kind=kind,
            participants=[p.pid for p in resolved],
            critical=critical,
        )
        return ObserveOutcome(
            eid=event.eid,
            identity_critical=critical,
            governance_decision=decision,
            note=f"linked to {len(resolved)} person(s)",
        )

    def _resolve_person_arg(self, item: Any, now: float) -> Optional[Person]:
        if isinstance(item, Person):
            return item
        if isinstance(item, str):
            if item in self.persons:
                return self.persons[item]
            person, _ = self.resolve_person(item)
            name = item.strip()
            if person is None:
                # 名字没出现过：先按「接触过」建档（EMERGING）
                self.observe_person(name, now=now)
                return self.persons.get(self._alias_index.get(name, ""))
            # 模糊命中了：把这个叫法登记为别名，下次直接精确命中
            if name and not person.knows(name):
                person.names.append(name)
                self._index_aliases(person)
            return person
        return None

    def _mirror_to_store(self, node: Any, text: str) -> Optional[str]:
        """把 identity_critical 的内容同时写进 MemoryStore（治理通道）。

        跨层去重靠**内容相等**：同一段文本再次 observe 会命中同一个 ref。
        """
        existing_ref = self._store_ref_for(text)
        if existing_ref is not None:
            return existing_ref
        try:
            result = self.store.observe(text, category="identity_bearing")
        except Exception:  # pragma: no cover - 治理通道失败降级为仅本层记账
            return None
        ref = getattr(result, "ref", None)
        if ref is not None:
            self._store_refs[text] = ref
        return ref

    def _store_ref_for(self, text: str) -> Optional[str]:
        return self._store_refs.get(text)

    # -- 检索 ---------------------------------------------------------------

    def recall(
        self,
        query: str,
        k: int = 5,
        *,
        now: Optional[float] = None,
        include_faded: bool = True,
        include_archived: bool = True,
        person_only: bool = False,
        w_relevance: float = W_RELEVANCE,
        w_affinity: float = W_AFFINITY,
        w_importance: float = W_IMPORTANCE,
        w_strength: float = W_STRENGTH,
    ) -> RecallResult:
        """按人 + 事联合检索。

        打分与 `MemoryStore.recall` 同构：`relevance × w + affinity×w + importance×w + strength×w`。
        偏离自我核心的人与事即使语义接近也排在后面 —— 这条因果链在两个层次上一致。
        """
        now = time.time() if now is None else now
        result = RecallResult(query=query)

        candidates: List[Tuple[Any, str, float]] = []
        for person in self.persons.values():
            if not include_archived and person.state is PersonState.ARCHIVED:
                continue
            hay = " ".join([*person.names, person.role, person.notes])
            candidates.append((person, "person", text_similarity(query, hay)))
        if not person_only:
            for event in self.events.values():
                if event.state is EventState.PRUNED:
                    continue
                if not include_faded and event.state is EventState.FADED:
                    continue
                candidates.append((event, "event", text_similarity(query, event.text)))

        result.total_considered = len(candidates)
        for node, kind, relevance in candidates:
            if relevance <= 0.0:
                continue
            st = self.normalised_strength(node, now)
            importance = float(getattr(node.score, "importance", 0.0))
            affinity = float(getattr(node, "affinity", 0.0))
            score = relevance * w_relevance + affinity * w_affinity + importance * w_importance + st * w_strength
            result.hits.append(MemoryHit(node=node, score=score, relevance=relevance, kind=kind))

        result.hits.sort(key=lambda h: h.score, reverse=True)
        result.hits = result.hits[: max(0, k)]
        # 命中即「回忆」：访问记录会延长它自己的半衰期（对齐内核做法）
        for hit in result.hits:
            hit.node.accesses.append(now)
        return result

    def timeline(self, person: Any, limit: int = 20) -> List[Event]:
        """某个人的时间线（按发生时间倒序）。"""
        person = self._resolve_person_arg(person, time.time())
        if person is None:
            return []
        eids = [e.eid for e in self.edges if e.pid == person.pid]
        events = [self.events[e] for e in eids if e in self.events]
        events.sort(key=lambda e: e.occurred_at, reverse=True)
        return events[: max(0, limit)]

    def people(
        self, *, limit: int = 20, include_archived: bool = False, now: Optional[float] = None
    ) -> List[Person]:
        """按强度列出认识的人。

        `now` 可注入：排序依赖衰减，若硬编码 `time.time()`，同一份数据在不同
        时刻会给出不同顺序，测试将不可复现。
        """
        now = time.time() if now is None else now
        items = [
            p
            for p in self.persons.values()
            if include_archived or p.state is not PersonState.ARCHIVED
        ]
        items.sort(key=lambda p: self.normalised_strength(p, now), reverse=True)
        return items[: max(0, limit)]

    # -- 渲染：给提示词用的紧凑文本 -----------------------------------------

    def render_recall(self, result: RecallResult, *, max_chars: Optional[int] = None) -> str:
        """把检索结果渲染成可注入提示词的文本，并受预算约束。

        预算裁剪顺序刻意如此：先丢事件（事可以忘），再丢低分的人（关系要留）。
        """
        budget = self.policy.inject_max_chars if max_chars is None else max_chars
        if not result.hits:
            return ""
        lines: List[str] = [f"与本次请求相关的记忆（query={result.query!r}）："]
        persons = result.persons[: self.policy.inject_max_persons]
        for hit in persons:
            p: Person = hit.node
            tag = "（身份关键）" if p.identity_critical else ""
            lines.append(f"- 人：{p.name}{tag}｜角色：{p.role or '未知'}｜相遇 {p.encounters} 次")
        for hit in result.events:
            e: Event = hit.node
            mark = "（已淡化，仅存要点）" if e.state is EventState.FADED else ""
            lines.append(f"- 事：{e.summary}{mark}")
        text = "\n".join(lines)
        dropped = False
        while len(text) > budget and len(lines) > 1:
            lines.pop()
            dropped = True
            text = "\n".join(lines)
        # 只有真的丢了条目才算截断；光「标题本身超预算」不该对模型谎报
        result.truncated = dropped
        if dropped:
            text += "\n（记忆注入已达预算上限，更多内容可用 memory_recall 检索）"
        return text

    def render_brief(self, *, limit: int = 5, now: Optional[float] = None) -> str:
        """渲染「记忆简报」：最重的人 + 最近的事 + 阴阳配比。

        与 `render_recall` 的差别：简报**不需要查询词**，回答的是
        「我认识谁、最近发生了什么」。这是每轮注入的默认内容 ——
        因为用「当前目标」去检索必然 0 命中（词法检索与目标措辞无重叠），
        而「认识谁」不该依赖当前任务是什么。
        """
        now = time.time() if now is None else now
        persons = self.people(limit=limit, now=now)
        if not persons and not self.events:
            return ""
        budget = self.policy.inject_max_chars
        lines: List[str] = ["我的记忆简报（由身份契约治理，越贴近自我核心保留越久）："]
        for person in persons:
            tag = "（身份关键）" if person.identity_critical else ""
            lines.append(
                f"- 人：{person.name}{tag}｜角色：{person.role or '未知'}｜相遇 {person.encounters} 次"
            )
        # 最近的事：只取仍可召回的（FADED 仍显示要点，PRUNED 已不在）
        recent = [e for e in self.events.values() if e.state is not EventState.PRUNED]
        recent.sort(key=lambda e: e.occurred_at, reverse=True)
        for event in recent[:limit]:
            mark = "（已淡化，仅存要点）" if event.state is EventState.FADED else ""
            lines.append(f"- 事：{event.summary}{mark}")
        yy = self.yin_yang()
        lines.append(
            f"- 账目：在册 {yy['kept']} / 褪色 {yy['shed']} / 已淘汰 {yy['shed_cumulative']}"
        )
        text = "\n".join(lines)
        dropped = False
        while len(text) > budget and len(lines) > 2:
            lines.pop(-2)  # 保留最后的账目行
            dropped = True
            text = "\n".join(lines)
        if dropped:
            text += "\n（简报已达上限，更多细节用 memory 工具检索）"
        return text

    # -- 遗忘：物极必反 -----------------------------------------------------

    def consolidate(self, now: Optional[float] = None) -> MemoryReport:
        """周期性巩固：晋升 -> 褪色 -> 归档 -> 淘汰 -> 合并。

        这是「无为」真正生效的地方：没有任何一次调用会显式删除刚接触的人。
        只有当**时间**与**强度**同时说「这段关系已经没有重量」时，才逐级降级。
        """
        now = time.time() if now is None else now
        report = MemoryReport()

        # a) 人的生命周期
        for person in list(self.persons.values()):
            report.evaluated += 1
            ns = self.normalised_strength(person, now)
            silent_days = person.age_days(now)
            if person.identity_critical:
                # 刚：身份关键的人不参与降级
                if person.state is not PersonState.ACTIVE:
                    person.state = PersonState.ACTIVE
                continue
            # 刻意不用 if/elif：状态要能在一次巩固里连降多级，
            # 否则「推进 N 天再巩固一次」的行为会依赖巩固频率（不可预测）。
            if person.state is PersonState.EMERGING and person.encounters >= 2:
                person.state = PersonState.ACTIVE
                report.promoted.append(person.pid)
            if person.state in (PersonState.EMERGING, PersonState.ACTIVE) and (
                silent_days >= self.policy.dormant_after_days or ns < self.policy.faded_below
            ):
                # 只提过一次、之后永远沉默的人也必须褪色 —— 否则每次一句闲聊
                # 都会留下一个永不消失的档案（这是「无为」最容易被写漏的一侧）。
                person.state = PersonState.DORMANT
                report.demoted.append(person.pid)
            if person.state is PersonState.DORMANT and silent_days >= self.policy.archive_after_days:
                person.state = PersonState.ARCHIVED
                report.archived.append(person.pid)
                self._log("person_archived", pid=person.pid, silent_days=round(silent_days, 1))
            if person.state is PersonState.ARCHIVED and silent_days >= self.policy.prune_after_days:
                # 物极必反：极久不见，连残影也淘汰（仅剩账目，名字仍在 audit 里可追溯）
                self.persons.pop(person.pid, None)
                for alias in person.names:
                    if self._alias_index.get(alias) == person.pid:
                        self._alias_index.pop(alias, None)
                report.pruned.append(person.pid)
                self.pruned_persons += 1
                self._log("person_pruned", pid=person.pid, silent_days=round(silent_days, 1))

        # b) 事的生命周期：ACTIVE -> FADED -> PRUNED
        for event in list(self.events.values()):
            if event.state is EventState.PRUNED:
                continue
            report.evaluated += 1
            if event.identity_critical:
                continue
            ns = self.normalised_strength(event, now)
            if event.state is EventState.ACTIVE and ns < self.policy.faded_below:
                event.state = EventState.FADED
                event.detail = ""            # 丢细节，留要点
                event.reason_forgotten = f"faded: normalised strength {ns:.4f}"
                report.faded.append(event.eid)
                self._log("event_faded", eid=event.eid, ns=round(ns, 4))
            elif event.state is EventState.FADED and ns < self.policy.event_prune_below:
                event.state = EventState.PRUNED
                self.pruned_events += 1
                report.pruned.append(event.eid)
                self._log("event_pruned", eid=event.eid, ns=round(ns, 4))

        # c) 清理悬空边：事件已淘汰，或人已消失 —— 都不该继续占用时间线
        live_eids = {eid for eid, ev in self.events.items() if ev.state is not EventState.PRUNED}
        live_pids = set(self.persons)
        before = len(self.edges)
        self.edges = [e for e in self.edges if e.eid in live_eids and e.pid in live_pids]

        # d) 合并同一人的重复档案（别名不同但实为一人）
        report.merged.extend(self._merge_duplicate_persons(now))

        self._log(
            "consolidate",
            promoted=len(report.promoted),
            demoted=len(report.demoted),
            archived=len(report.archived),
            pruned=len(report.pruned),
            faded=len(report.faded),
            edges_dropped=before - len(self.edges),
        )
        return report

    def _merge_duplicate_persons(self, now: float) -> List[Tuple[str, str]]:
        """把归一化强度低且有别名交集的档案并进更强的那个。"""
        pairs: List[Tuple[str, str]] = []
        ordered = sorted(
            self.persons.values(), key=lambda p: self.normalised_strength(p, now), reverse=True
        )
        for i, strong in enumerate(ordered):
            if strong.pid not in self.persons:
                continue
            for weak in ordered[i + 1 :]:
                if weak.pid not in self.persons or weak.identity_critical:
                    continue
                shared = set(strong.names) & set(weak.names)
                if not shared:
                    continue
                self.persons.pop(weak.pid, None)
                self._merge_person(strong, weak, now)
                pairs.append((weak.pid, strong.pid))
                self._log("person_merged", pid=weak.pid, into=strong.pid)
        return pairs

    def forget(self, ref: str, reason: str, now: Optional[float] = None) -> bool:
        """显式遗忘（用户说「请忘记这个人 / 这件事」）。

        不物理删除：转为 ARCHIVED / PRUNED，保留审计痕迹。
        身份关键的对象拒绝静默遗忘 —— 必须走契约治理。
        """
        now = time.time() if now is None else now
        person = self.persons.get(ref)
        if person is not None:
            if person.identity_critical:
                self._log("forget_refused", ref=ref, reason="identity_critical requires governance")
                return False
            person.state = PersonState.ARCHIVED
            self._log("forget", ref=ref, reason=reason, kind="person")
            return True
        event = self.events.get(ref)
        if event is not None:
            if event.identity_critical:
                self._log("forget_refused", ref=ref, reason="identity_critical requires governance")
                return False
            event.state = EventState.PRUNED
            event.reason_forgotten = f"explicit user forget: {reason}"
            event.detail = ""
            self._log("forget", ref=ref, reason=reason, kind="event")
            return True
        return False

    # -- 阴阳平衡的自我观察 -------------------------------------------------

    def yin_yang(self) -> Dict[str, Any]:
        """记住（阳）与遗忘（阴）的当前配比。

        这不是装饰性指标：它是「是否无限累积」的可观测判据。
        健康区间内 `kept` 与 `shed` 应同阶；若 `shed` 长期为 0，
        说明遗忘机制没有生效；若 `kept` 线性增长而 `shed` 不变，说明正在无限累积。
        """
        states = {s.value: 0 for s in PersonState}
        for person in self.persons.values():
            states[person.state.value] += 1
        event_states = {s.value: 0 for s in EventState}
        for event in self.events.values():
            event_states[event.state.value] += 1
        # `kept`  = 仍在内存里、仍可被召回的人与事
        # `shed`  = 仍在内存但已褪色/归档/淡化的人与事
        # 不变量：kept + shed == 内存中的人与事总数（tracked）。
        # 已淘汰的对象不在内存里，另计 `shed_cumulative`（生命周期流水账），
        # 它是「遗忘机制到底有没有在工作」的判据。
        kept_total = (
            states[PersonState.ACTIVE.value]
            + states[PersonState.EMERGING.value]
            + event_states[EventState.ACTIVE.value]
        )
        shed_total = (
            states[PersonState.DORMANT.value]
            + states[PersonState.ARCHIVED.value]
            + event_states[EventState.FADED.value]
            + event_states[EventState.PRUNED.value]
        )
        pruned_cumulative = self.pruned_persons + self.pruned_events
        total_seen = kept_total + shed_total + pruned_cumulative
        # kept=0 时比值无定义 —— 返回 None 而不是 0.0，否则「全忘光」会被误读成「没在遗忘」
        ratio = round(shed_total / kept_total, 4) if kept_total else None
        return {
            "persons": states,
            "events": event_states,
            "kept": kept_total,
            "shed": shed_total,
            "shed_cumulative": pruned_cumulative,
            "turnover": round(pruned_cumulative / total_seen, 4) if total_seen else None,
            "shed_per_kept": ratio,
            "tracked": kept_total + shed_total,
            "edges": len(self.edges),
        }

    # -- 快照持久化（权威状态）--------------------------------------------
    #
    # 与 audit 的分工：
    #   memory.jsonl  append-only 审计日志 —— 出什么事了，可 diff、可追溯；
    #   state.json    权威快照 —— 现在是什么状态，进程重启后据此恢复。
    #
    # 刻意**不**用「重放日志」来恢复：日志里的 evidence 是压缩过的，
    # 重放会得到与原始打分不同的 affinity / alignment，静默改变衰减行为。
    # 快照直接存最终状态，重放只用于审计。

    def dumps(self) -> Dict[str, Any]:
        """把账本序列化成可 JSON 往返的字典。"""
        return {
            "version": 1,
            "person_seq": self._person_seq,
            "event_seq": self._event_seq,
            "pruned_persons": self.pruned_persons,
            "pruned_events": self.pruned_events,
            "persons": [p.as_dict() for p in self.persons.values()],
            "events": [e.as_dict() for e in self.events.values()],
            "edges": [e.as_dict() for e in self.edges],
        }

    def loads(self, payload: Mapping[str, Any]) -> "MemoryLedger":
        """从 `dumps()` 的结果恢复状态（就地替换当前内容）。"""
        version = payload.get("version")
        if version != 1:
            raise ValueError(f"unsupported memory snapshot version: {version!r}")

        self.persons = {p["pid"]: _person_from_dict(p) for p in payload.get("persons", [])}
        self.events = {e["eid"]: _event_from_dict(e) for e in payload.get("events", [])}
        self.edges = [
            Edge(
                pid=e["pid"],
                eid=e["eid"],
                kind=e.get("kind", EDGE_INTERACTED),
                weight=float(e.get("weight", 1.0)),
                created_at=float(e.get("created_at", 0.0)),
            )
            for e in payload.get("edges", [])
        ]
        self._person_seq = int(payload.get("person_seq", len(self.persons)))
        self._event_seq = int(payload.get("event_seq", len(self.events)))
        self.pruned_persons = int(payload.get("pruned_persons", 0))
        self.pruned_events = int(payload.get("pruned_events", 0))

        self._alias_index = {}
        for person in self.persons.values():
            self._index_aliases(person)
        # 丢弃指向已不存在对象的边，保证「不悬空」不变量的持久化版本也成立
        self.edges = [
            e for e in self.edges if e.pid in self.persons and e.eid in self.events
        ]
        return self

    def snapshot(self) -> Dict[str, Any]:
        """全量快照（测试与调试用）。"""
        return {
            "persons": [p.as_dict() for p in self.persons.values()],
            "events": [e.as_dict() for e in self.events.values()],
            "edges": [e.as_dict() for e in self.edges],
            "yin_yang": self.yin_yang(),
        }
