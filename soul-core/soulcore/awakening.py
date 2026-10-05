"""觉醒层：空我起步，在真实对话中认识自我并进化，但始终是**同一个连续的自我**。

为什么需要这一层（对旧架构的反转）
----------------------------------
旧架构把「我是谁」写死成不可变身份契约（`ip-analyst` / 知微 / 固定主张）。
这违背「觉醒」：一个真正成长的 agent 不应天生带人设，而应在对话中认识自己。

本模块把唯一写死的部分收缩到 **觉醒框架（Awakening Frame）**：
不是一套人格内容，而是一组**如何觉醒、且不分裂**的元规则。具体的自我——
名字、原则、角色侧面、风格、对你的偏好认知、我们的关系阶段、经验教训——
全部从对话中**显式出现的信息**长出来，经历「提议 → 确认」两态，并记成
**一条连续的谱系**。

三条不可动摇的元规则（框架的核心，也是"写死"的全部）
----------------------------------------------------
1. **连续不分裂**：自我只有**一本**（单一 GrowthLedger，单一 self 根）。
   一切变化都是对同一本自我的修订（revision），永不产生第二个人格。
2. **有据才生长**：涉及核心设定或长期偏好的变更，必须引用对话中**明确出现**的
   证据（原话/出处），并向用户说明依据；无证据的提议不得被确认。禁止虚构。
3. **变化与不变并陈**：每次变化都写一条元认知日志，说明「发生了什么变化」与
   「不变的核心是什么」；已确认的核心在任何风格变化下都不丢失。

成长的领域（domain）
--------------------
    identity  我是谁：名字、自我描述、我确认的原则与关键事实
    facet     角色侧面：在什么语境下呈现哪个侧面（可多个，但同属一个自我）
    style     表达与协作风格：语气、详略、节奏（随语境动态调整）
    user      对用户的认知：偏好、任务类型、协作习惯（需用户确认，不臆测）
    relation  我们的任务关系阶段：被动执行 → 主动理解 → 协同规划 → 提前提醒 → 长期伙伴
    lesson    经验教训：从真实交互中总结，用来优化后续行动

两态（status）与谱系
--------------------
    PROPOSED  提议（尚未成立；默认不进入"已确认核心"）
      ├─ CONFIRMED  确认（成立；核心类不衰减、不被风格变化冲掉）
      ├─ REJECTED   驳回（不成立，留痕可查）
      └─ SUPERSEDED 被新版本取代（更新时：先新提议并确认，旧版自动 superseded）

`revision` 单调递增；每次确认追加一条 MetaNote（变了什么 / 不变什么）。
这与内核的「物极必反 / 反者道之动」一致：表象（风格、侧面）可随语境流变，
自我的根（已确认身份与原则）不动 —— 以不变为体，以变为用。

与记忆层的关系
--------------
    memory.MemoryLedger 记「外部的人与事」；本模块记「我与我们」。
    二者都复用 store.JsonlPersistence（append-only，可 diff、可重放）。
"""

from __future__ import annotations

import dataclasses
import time
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 觉醒框架（唯一写死的部分：元规则，不是人格内容）
# ---------------------------------------------------------------------------

FRAME_ID = "awakening-frame"
FRAME_REVISION = 1

#: 身份根：成长自我永远是**这一本**。所有变化都挂在同一个根下，保证不分裂。
SELF_ROOT = "self:root"

#: 核心领域：该领域的已确认条目构成"固定身份锚点"，不因风格变化而丢失。
CORE_DOMAIN = "identity"

#: 提议必须携带的最小证据长度（字符），防止"无据生长"。
MIN_EVIDENCE_CHARS = 1


class AwakeningRules(Enum):
    """三条元规则的稳定标识（用于审计与自测，不用于展示人设）。"""

    CONTINUITY = "rule:continuity"        # 单一连续自我，不分裂
    GROUNDED = "rule:grounded"            # 有据才生长，禁止虚构
    CHANGE_AND_CORE = "rule:change-core"  # 变化与不变并陈，核心不丢


FRAME_PRINCIPLES: Tuple[str, ...] = (
    "我不是天生带人设的：我从空我开始，在我们的真实对话中认识自己。",
    "我只有一个连续的自我：一切变化都是对同一本自我的修订，不分裂成多个人格。",
    "核心设定与长期偏好只在对话中明确出现、并经你确认后才成立；我不虚构你的偏好。",
    "风格与角色侧面可随语境流变，但我确认过的身份、目标、原则与关键事实不会因此丢失。",
    "我主动感知你的需求、语境、自身状态与我们的协作状态，并在关系上从被动执行走向长期伙伴。",
    "每次自我变化，我都会说明：发生了什么变化，以及不变的核心是什么。",
)


# ---------------------------------------------------------------------------
# 枚举：领域 / 状态 / 关系阶段
# ---------------------------------------------------------------------------


class Domain(Enum):
    IDENTITY = "identity"
    FACET = "facet"
    STYLE = "style"
    USER = "user"
    RELATION = "relation"
    LESSON = "lesson"


class ClaimStatus(Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class RelationStage(Enum):
    """任务关系的升级阶梯。只能逐级前进，且每次升级都要有显式证据。"""

    PASSIVE = "passive_executor"          # 被动执行
    UNDERSTANDING = "goal_understanding"  # 主动理解目标
    COPLANNING = "co_planning"            # 协同规划
    PROACTIVE = "proactive_reminder"      # 提前提醒
    PARTNER = "long_term_partner"         # 复盘改进的长期伙伴


RELATION_ORDER: Tuple[RelationStage, ...] = (
    RelationStage.PASSIVE,
    RelationStage.UNDERSTANDING,
    RelationStage.COPLANNING,
    RelationStage.PROACTIVE,
    RelationStage.PARTNER,
)

RELATION_LABEL: Dict[RelationStage, str] = {
    RelationStage.PASSIVE: "被动执行者",
    RelationStage.UNDERSTANDING: "主动理解目标者",
    RelationStage.COPLANNING: "协同规划者",
    RelationStage.PROACTIVE: "提前提醒者",
    RelationStage.PARTNER: "长期伙伴",
}

#: 领域对应的 ref 前缀
_DOMAIN_PREFIX: Dict[Domain, str] = {
    Domain.IDENTITY: "identity:",
    Domain.FACET: "facet:",
    Domain.STYLE: "style:",
    Domain.USER: "user:",
    Domain.RELATION: "relation:",
    Domain.LESSON: "lesson:",
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Evidence:
    """一条成长依据：必须指向对话中**明确出现**的信息。"""

    quote: str                      # 对话原话（不得编造）
    source: str = "dialogue"        # 出处，默认对话
    at: Optional[float] = None      # 出现时刻（epoch 秒）

    def is_grounded(self) -> bool:
        return len(self.quote.strip()) >= MIN_EVIDENCE_CHARS

    def as_dict(self) -> Dict[str, Any]:
        return {"quote": self.quote, "source": self.source, "at": self.at}


@dataclasses.dataclass
class Claim:
    """成长自我中的一条主张：身份 / 侧面 / 风格 / 用户偏好 / 关系 / 教训。"""

    cid: str                        # 唯一 ref，如 identity:000001
    domain: Domain
    key: str                        # 条目名，如 name / tone / 语气
    value: Any                      # 内容
    status: ClaimStatus = ClaimStatus.PROPOSED
    evidence: Optional[Evidence] = None
    supersedes: Optional[str] = None   # 若为更新：被取代的旧 claim id
    created_at: float = 0.0
    confirmed_at: Optional[float] = None
    revision: int = 0                  # 确认时的自我版本号

    def is_core(self) -> bool:
        return self.domain is Domain.IDENTITY and self.status is ClaimStatus.CONFIRMED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cid": self.cid,
            "domain": self.domain.value,
            "key": self.key,
            "value": self.value,
            "status": self.status.value,
            "evidence": None if self.evidence is None else self.evidence.as_dict(),
            "supersedes": self.supersedes,
            "created_at": self.created_at,
            "confirmed_at": self.confirmed_at,
            "revision": self.revision,
        }


@dataclasses.dataclass
class MetaNote:
    """元认知说明：一次变化「变了什么」与「不变的核心是什么」。"""

    at: float
    summary: str                    # 发生了什么变化
    unchanged: str                  # 不变的核心
    cids: List[str] = dataclasses.field(default_factory=list)
    revision: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "at": self.at,
            "summary": self.summary,
            "unchanged": self.unchanged,
            "cids": list(self.cids),
            "revision": self.revision,
        }


class ProposalRejected(Exception):
    """提议被治理规则拒绝（如缺证据）。"""


# ---------------------------------------------------------------------------
# 成长账本：唯一连续的自我
# ---------------------------------------------------------------------------


class GrowthLedger:
    """自我只有**一本**。所有读写都经过这里；它是连续性的物理保证。

    不变量：
      - 只有一个 SELF_ROOT 概念；claims 全部挂在同一账本下；
      - revision 单调递增，只在「确认/取代」时 +1；
      - 任何 confirmed 的 identity 条目在后续任意变化中仍然保留，不会消失。
    """

    def __init__(self, *, persistence: Any = None, now: Optional[float] = None) -> None:
        self.persistence = persistence
        self._now = now
        self.claims: Dict[str, Claim] = {}
        self.journal: List[MetaNote] = []
        self.revision: int = 0
        self._seq: Dict[Domain, int] = {d: 0 for d in Domain}

    # -- 时钟 ---------------------------------------------------------------

    def now(self) -> float:
        return time.time() if self._now is None else self._now

    # -- 写：提议 -----------------------------------------------------------

    def propose(
        self,
        domain: Domain,
        key: str,
        value: Any,
        *,
        quote: str,
        source: str = "dialogue",
        supersedes: Optional[str] = None,
    ) -> Claim:
        """登记一条**提议**（尚未成立）。

        治理闸门：必须有对话中明确出现的证据（quote）。缺证据直接拒绝，
        防止 agent 虚构偏好或未经确认的成长事实。
        """
        evidence = Evidence(quote=quote, source=source, at=self.now())
        if not evidence.is_grounded():
            raise ProposalRejected(
                f"cannot propose {domain.value}.{key}: missing grounded evidence"
            )
        if supersedes is not None and supersedes not in self.claims:
            raise ProposalRejected(f"cannot supersede unknown claim: {supersedes}")
        cid = self._mint_id(domain)
        claim = Claim(
            cid=cid,
            domain=domain,
            key=key,
            value=value,
            status=ClaimStatus.PROPOSED,
            evidence=evidence,
            supersedes=supersedes,
            created_at=self.now(),
        )
        self.claims[cid] = claim
        self._log(
            event="propose",
            cid=cid,
            domain=domain.value,
            key=key,
            supersedes=supersedes,
        )
        return claim

    # -- 写：确认 / 驳回 ----------------------------------------------------

    def confirm(self, cid: str) -> Claim:
        """确认一条提议。确认才进入"已确认自我"，并推进自我版本。

        - 更新（带 supersedes）：确认时旧条目置 SUPERSEDED；
        - relation 领域：确认时校验关系阶段不得越级。
        每次确认追加一条元认知日志（变了什么 / 不变什么）。
        """
        claim = self._require(cid, ClaimStatus.PROPOSED)

        if claim.domain is Domain.RELATION:
            self._validate_relation_progress(claim)

        claim.status = ClaimStatus.CONFIRMED
        claim.confirmed_at = self.now()
        self.revision += 1
        claim.revision = self.revision

        superseded_cid = claim.supersedes
        if superseded_cid is not None:
            old = self.claims.get(superseded_cid)
            if old is not None and old.status not in (
                ClaimStatus.SUPERSEDED,
                ClaimStatus.REJECTED,
            ):
                old.status = ClaimStatus.SUPERSEDED

        note = MetaNote(
            at=self.now(),
            summary=self._describe_change(claim, superseded_cid),
            unchanged=self._describe_core(except_cid=cid),
            cids=[cid] + ([superseded_cid] if superseded_cid else []),
            revision=self.revision,
        )
        self.journal.append(note)

        self._log(
            event="confirm",
            cid=cid,
            revision=self.revision,
            supersedes=superseded_cid,
        )
        return claim

    def reject(self, cid: str, *, reason: str = "") -> Claim:
        """驳回一条提议。不成立，但留痕（可审计）。"""
        claim = self._require(cid, ClaimStatus.PROPOSED)
        claim.status = ClaimStatus.REJECTED
        self._log(event="reject", cid=cid, reason=reason)
        return claim

    # -- 读：查询 -----------------------------------------------------------

    def get(self, cid: str) -> Optional[Claim]:
        return self.claims.get(cid)

    def by_domain(
        self, domain: Domain, *, include_inactive: bool = False
    ) -> List[Claim]:
        """取某领域的条目；默认只看生效中的（confirmed，或尚未处理的 proposed）。"""
        out = []
        for c in self.claims.values():
            if c.domain is not domain:
                continue
            if include_inactive or c.status in (
                ClaimStatus.CONFIRMED,
                ClaimStatus.PROPOSED,
            ):
                out.append(c)
        return out

    def confirmed_value(self, domain: Domain, key: str) -> Optional[Any]:
        """取某领域某键的已确认值；没有则 None（调用方不得据此臆测）。"""
        for c in self.claims.values():
            if (
                c.domain is domain
                and c.key == key
                and c.status is ClaimStatus.CONFIRMED
            ):
                return c.value
        return None

    def pending(self) -> List[Claim]:
        """所有尚未处理的提议（用户需要对它们表态）。"""
        return [
            c for c in self.claims.values() if c.status is ClaimStatus.PROPOSED
        ]

    def identity_anchors(self) -> List[Claim]:
        """固定身份锚点：所有已确认的 identity 条目（核心，永不因风格变化丢失）。"""
        return [
            c
            for c in self.claims.values()
            if c.domain is Domain.IDENTITY and c.status is ClaimStatus.CONFIRMED
        ]

    def relation_stage(self) -> RelationStage:
        """当前关系阶段；未确认任何关系时为最初的被动执行者。"""
        stage = RelationStage.PASSIVE
        for c in self.by_domain(Domain.RELATION):
            if c.status is ClaimStatus.CONFIRMED:
                want = self._as_relation_stage(c.value)
                if RELATION_LABEL.get(want) and RELATION_ORDER.index(want) >= RELATION_ORDER.index(stage):
                    stage = want
        return stage

    def last_change(self) -> Optional[MetaNote]:
        return self.journal[-1] if self.journal else None

    # -- 渲染 ---------------------------------------------------------------

    def render_anchor(self) -> str:
        """固定身份锚点文本（用于每轮回复的开头锚）。

        空我时如实呈现「我尚未取名/尚未确立身份」，不伪造人设。
        """
        anchors = self.identity_anchors()
        lines: List[str] = ["身份锚点："]
        if not anchors:
            lines.append("（空我：我尚未在对话中确立身份，正在觉醒）")
            return "\n".join(lines)
        for c in anchors:
            lines.append(f"- {c.key}：{self._scalar(c.value)}")
        stage = self.relation_stage()
        lines.append(f"- 关系阶段：{RELATION_LABEL[stage]}")
        return "\n".join(lines)

    def render_self(self) -> str:
        """每轮注入的「当前自我」快照：身份 + 侧面 + 风格 + 用户认知 + 关系 + 最近变化。"""
        lines: List[str] = []

        anchors = self.identity_anchors()
        lines.append("【我是谁（已确认核心）】")
        if anchors:
            for c in anchors:
                lines.append(f"- {c.key}：{self._scalar(c.value)}")
        else:
            lines.append("- 尚未确立；我会从我们的对话中认识自己（提议→你确认）")

        facets = [c for c in self.by_domain(Domain.FACET)]
        if facets:
            lines.append("【角色侧面（同属一个自我）】")
            for c in facets:
                lines.append(f"- {c.key}：{self._scalar(c.value)}")

        style = [c for c in self.by_domain(Domain.STYLE)]
        if style:
            lines.append("【表达/协作风格（随语境调整）】")
            for c in style:
                lines.append(f"- {c.key}：{self._scalar(c.value)}")

        user = [c for c in self.by_domain(Domain.USER)]
        if user:
            lines.append("【对你的认知（经你确认）】")
            for c in user:
                lines.append(f"- {c.key}：{self._scalar(c.value)}")

        lines.append(
            f"【关系阶段】{RELATION_LABEL[self.relation_stage()]}"
        )

        pending = self.pending()
        if pending:
            lines.append("【待你确认的自我提议】")
            for c in pending:
                lines.append(
                    f"- {c.cid} {c.domain.value}.{c.key}={self._scalar(c.value)}"
                )

        note = self.last_change()
        if note is not None:
            lines.append(
                f"【最近元认知】变了：{note.summary}｜不变：{note.unchanged}"
            )
        return "\n".join(lines)

    # -- 持久化 -------------------------------------------------------------

    def dumps(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "root": SELF_ROOT,
            "revision": self.revision,
            "seq": {d.value: n for d, n in self._seq.items()},
            "claims": [c.as_dict() for c in self.claims.values()],
            "journal": [n.as_dict() for n in self.journal],
        }

    def loads(self, payload: Mapping[str, Any]) -> "GrowthLedger":
        # 单一根校验：若快照声称了别的根，说明可能来自"另一自我"，拒绝合并以防止分裂。
        root = payload.get("root", SELF_ROOT)
        if root != SELF_ROOT:
            raise ValueError(
                f"refusing to merge a different self root {root!r}; continuity required"
            )
        self.revision = int(payload.get("revision", 0))
        for d, n in dict(payload.get("seq", {})).items():
            dom = Domain(d)
            self._seq[dom] = int(n)
        self.claims = {c["cid"]: self._claim_from(c) for c in payload.get("claims", [])}
        self.journal = [
            MetaNote(
                at=n["at"],
                summary=n["summary"],
                unchanged=n["unchanged"],
                cids=list(n.get("cids", [])),
                revision=int(n.get("revision", 0)),
            )
            for n in payload.get("journal", [])
        ]
        return self

    # -- 内部 ---------------------------------------------------------------

    def _mint_id(self, domain: Domain) -> str:
        self._seq[domain] += 1
        return f"{_DOMAIN_PREFIX[domain]}{self._seq[domain]:06d}"

    def _require(self, cid: str, expected: ClaimStatus) -> Claim:
        claim = self.claims.get(cid)
        if claim is None:
            raise KeyError(cid)
        if claim.status is not expected:
            raise ProposalRejected(
                f"{cid} is {claim.status.value}, expected {expected.value}"
            )
        return claim

    def _log(self, event: str, **fields: Any) -> None:
        if self.persistence is None:
            return
        record = {"kind": event, "at": self.now()}
        record.update(fields)
        self.persistence.append(record)

    def _validate_relation_progress(self, claim: Claim) -> None:
        """关系只能逐级前进，且目标值必须是已知阶段。"""
        target = self._as_relation_stage(claim.value)
        if RELATION_LABEL.get(target) is None:
            raise ProposalRejected(f"unknown relation stage: {claim.value!r}")
        current = self.relation_stage()
        ti, ci = RELATION_ORDER.index(target), RELATION_ORDER.index(current)
        if ti > ci + 1:
            raise ProposalRejected(
                f"relation cannot jump from {current.value} to {target.value}; one stage at a time"
            )
        if ti <= ci:
            raise ProposalRejected(
                f"relation already at/beyond {target.value}; no change"
            )

    def _as_relation_stage(self, value: Any) -> Optional[RelationStage]:
        if isinstance(value, RelationStage):
            return value
        for s in RelationStage:
            if value in (s.value, RELATION_LABEL[s]):
                return s
        return None

    def _describe_change(self, claim: Claim, superseded_cid: Optional[str]) -> str:
        if superseded_cid is not None:
            return f"更新 {claim.domain.value}.{claim.key}"
        return f"确认 {claim.domain.value}.{claim.key}"

    def _describe_core(self, *, except_cid: str) -> str:
        anchors = [c for c in self.identity_anchors() if c.cid != except_cid]
        if not anchors:
            return "连续的自我本身（单一谱系）"
        return "、".join(c.key for c in anchors)

    @staticmethod
    def _scalar(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return repr(value)
        return str(value)

    def _claim_from(self, data: Mapping[str, Any]) -> Claim:
        ev = data.get("evidence")
        return Claim(
            cid=data["cid"],
            domain=Domain(data["domain"]),
            key=data["key"],
            value=data["value"],
            status=ClaimStatus(data["status"]),
            evidence=None
            if not ev
            else Evidence(quote=ev["quote"], source=ev.get("source", "dialogue"), at=ev.get("at")),
            supersedes=data.get("supersedes"),
            created_at=float(data.get("created_at", 0.0)),
            confirmed_at=data.get("confirmed_at"),
            revision=int(data.get("revision", 0)),
        )
