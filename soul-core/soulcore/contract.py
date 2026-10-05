"""SoulCore: 自我核心（Soul Core）的技术形态。

设计要点
--------
1. **不可变**：`soul/specs/*.soul.json` 是唯一事实来源（source of truth），
   运行时不得修改。任何变更走「新版本文件 + 治理记录」，旧版本文件永久保留。
2. **结构化**：字段有严格 schema（见 `validate_spec`），不是自由文本 prompt。
3. **可版本化**：每个版本是内容寻址的（`content_hash`），并带 `lineage`。
4. **受更新治理**：版本递增必须满足 `governance` 记录条件，否则加载失败。

SOUL.md 只作为 **人类可读的伴生视图**（human-readable view）由 spec 渲染，
永不作为运行时输入被解析回 spec —— 这消除了「Agent 改写自己身份」的漂移入口。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ContractViolation

SPEC_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 原语类型（primitive types）
# ---------------------------------------------------------------------------

StableId = str
RevisionId = str


@dataclasses.dataclass(frozen=True)
class GovernanceRecord:
    """更新治理记录：一次身份契约变更的「授权凭证」。

    没有这张凭证，revision > 1 的 spec 无法加载。
    """

    revision: RevisionId
    approved_by: str  # 人类主体（human principal）标识，必须非空
    approved_at: str  # RFC3339 时间戳
    diff_summary: str  # 变更摘要（人类可读）
    rationale: str  # 为什么改（必须回应「这个变更是否侵蚀身份」）
    migration_note: str = ""  # 既有记忆如何迁移/重评估；为空表示无迁移
    rollback_to: Optional[RevisionId] = None  # 可回滚到哪个 revision


@dataclasses.dataclass(frozen=True)
class SoulClaim:
    """一条身份主张（claim）—— 可被判定的最小单元。

    这是「自我核心」参与**决策**的接口：每条 claim 都携带
    `decision_rule`，使抽象价值观可以机械地对具体记忆作答。
    """

    id: StableId
    statement: str  # 人类可读陈述
    assertion: str  # 面向机器的断言（用于语义比对）
    weight: float  # 0-10，影响该 claim 在 alignment 中的话语权
    strength: float  # 0-1，身份权重（core 通常 1.0）
    mutability: str  # "locked" | "governed" | "adaptive"
    decision_rule: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class RedLine:
    """不可跨越的红线。命中即 revoke，无论重要性多高。"""

    id: StableId
    statement: str
    triggers: Tuple[str, ...]  # 触发关键词/短语（小写，子串匹配）
    enforcement: str = "hard"  # "hard" | "soft"


@dataclasses.dataclass(frozen=True)
class DecayPrototype:
    """差异化衰减的「类别原型」（prototype）。

    执行层按 claim 类别选择基础半衰期，再由身份亲和度调制。
    """

    category: str
    base_half_life_days: float
    priority: int  # 越高越优先匹配（用于兜底分类）


@dataclasses.dataclass(frozen=True)
class RetentionPolicy:
    """保留/遗忘的总闸门参数——身份契约对执行层的约束。"""

    min_alignment_to_store: float = 0.35  # 低于此值不得进入长期存储
    quarantine_band: Tuple[float, float] = (0.20, 0.35)  # 隔离待审区间
    agreement_floor: float = 0.60  # 低于此值触发 quarantine
    identity_affinity_min: float = 0.25  # 衰减率下限（身份无关内容）
    identity_affinity_max: float = 2.00  # 衰减率上限（核心一致内容）
    eviction_threshold: float = 0.05  # strength 低于此值进入驱逐候选
    reinforcement_delta: float = 0.005  # 每次强化对 strength 的增益
    duplicate_similarity: float = 0.92  # 视为重复的相似度阈值


@dataclasses.dataclass(frozen=True)
class Retraction:
    """对历史记忆的显式退役指令。

    这是「主动遗忘」的**治理路径**：不是等它自然衰减，
    而是由身份变更直接判定其为无效，并说明原因。
    """

    target_ref: str  # 记忆 ref 或 "*"
    reason: str
    issued_at: str


@dataclasses.dataclass(frozen=True)
class SoulCore:
    """自我核心 = 身份契约的运行时不可变视图。"""

    spec_version: str
    id: StableId
    name: str
    revision: RevisionId
    version: str
    created_at: str
    content_hash: str
    self_model: str
    core_claims: Tuple[SoulClaim, ...]
    adaptive_claims: Tuple[SoulClaim, ...]
    red_lines: Tuple[RedLine, ...]
    decay_prototypes: Tuple[DecayPrototype, ...]
    retention: RetentionPolicy
    lineage: Tuple[GovernanceRecord, ...]
    retractions: Tuple[Retraction, ...] = ()
    raw: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    # -- 便捷访问 -----------------------------------------------------------
    @property
    def all_claims(self) -> Tuple[SoulClaim, ...]:
        return self.core_claims + self.adaptive_claims

    def claim(self, claim_id: StableId) -> SoulClaim:
        for c in self.all_claims:
            if c.id == claim_id:
                return c
        raise KeyError(claim_id)

    def prototype_for(self, category: str) -> DecayPrototype:
        """按类别取衰减原型；未知类别回退到最低优先级的兜底原型（priority 最小）。"""
        matches = [p for p in self.decay_prototypes if p.category == category]
        if matches:
            return max(matches, key=lambda p: p.priority)
        return min(self.decay_prototypes, key=lambda p: p.priority)

    def active_retractions(self) -> Tuple[Retraction, ...]:
        return self.retractions

    def is_retracted(self, ref: str) -> Optional[Retraction]:
        """返回命中的退役指令（支持精确匹配与 '*' 通配）。"""
        for r in self.retractions:
            if r.target_ref == ref:
                return r
        for r in self.retractions:
            if r.target_ref.endswith("*") and ref.startswith(r.target_ref[:-1]):
                return r
        return None

    # -- 与执行层的契约 -----------------------------------------------------
    def decay_half_life_days(self, category: str) -> float:
        return self.prototype_for(category).base_half_life_days


# ---------------------------------------------------------------------------
# 加载与校验
# ---------------------------------------------------------------------------

REQUIRED_TOP_LEVEL = (
    "specVersion",
    "id",
    "name",
    "revision",
    "version",
    "createdAt",
    "selfModel",
    "identityCore",
    "retentionPolicy",
)

_CLAIM_KEYS = {
    "id",
    "statement",
    "assertion",
    "weight",
    "strength",
    "mutability",
    "decisionRule",
}


def canonical_json(obj: Any) -> str:
    """稳定序列化：用于内容寻址。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(spec_without_hash: Mapping[str, Any]) -> str:
    payload = {k: v for k, v in spec_without_hash.items() if k != "contentHash"}
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:32]


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ContractViolation(msg)


def _parse_claim(raw: Mapping[str, Any], kind: str) -> SoulClaim:
    unknown = set(raw) - _CLAIM_KEYS
    _require(not unknown, f"{kind} claim {raw.get('id')!r}: unknown fields {sorted(unknown)}")
    for key in ("id", "statement", "assertion", "weight", "strength", "mutability"):
        _require(key in raw, f"{kind} claim {raw.get('id')!r}: missing {key!r}")
    mutability = str(raw["mutability"])
    _require(
        mutability in ("locked", "governed", "adaptive"),
        f"claim {raw['id']!r}: mutability must be locked|governed|adaptive, got {mutability!r}",
    )
    weight = float(raw["weight"])
    strength = float(raw["strength"])
    _require(0.0 <= weight <= 10.0, f"claim {raw['id']!r}: weight must be within 0..10")
    _require(0.0 <= strength <= 1.0, f"claim {raw['id']!r}: strength must be within 0..1")
    if kind == "core":
        _require(
            mutability == "locked",
            f"core claim {raw['id']!r} must be locked (core is immutable by construction)",
        )
        _require(strength >= 0.8, f"core claim {raw['id']!r}: core strength must be >= 0.8")
    return SoulClaim(
        id=str(raw["id"]),
        statement=str(raw["statement"]),
        assertion=str(raw["assertion"]),
        weight=weight,
        strength=strength,
        mutability=mutability,
        decision_rule=raw.get("decisionRule"),
    )


def validate_spec(spec: Mapping[str, Any]) -> None:
    """schema 校验。任何违规 -> ContractViolation（fail closed）。"""
    _require(isinstance(spec, Mapping), "spec must be a JSON object")
    for key in REQUIRED_TOP_LEVEL:
        _require(key in spec, f"missing required field {key!r}")

    _require(spec["specVersion"] == SPEC_VERSION, f"unsupported specVersion {spec['specVersion']!r}")

    ic = spec["identityCore"]
    _require(isinstance(ic, Mapping), "identityCore must be an object")
    for key in ("coreClaims", "redLines", "decayPrototypes"):
        _require(key in ic, f"identityCore missing {key!r}")
    _require(len(ic["coreClaims"]) >= 1, "identityCore.coreClaims must not be empty")
    _require(len(ic["redLines"]) >= 1, "identityCore.redLines must not be empty")
    _require(len(ic["decayPrototypes"]) >= 1, "identityCore.decayPrototypes must not be empty")

    # core claims 必须唯一
    ids = [c["id"] for c in ic["coreClaims"]]
    _require(len(ids) == len(set(ids)), f"duplicate coreClaim ids: {sorted(ids)}")

    # claim 级约束必须在**校验阶段**就被强制，而不是拖到 parse 阶段：
    # 否则 `validate_spec` 会对「core claim 被改成 adaptive」或
    # 「混入未知字段」这类实质性篡改放行。
    for claim in ic["coreClaims"]:
        _parse_claim(claim, "core")
    for claim in ic.get("adaptiveClaims", []):
        _parse_claim(claim, "adaptive")
    adaptive_ids = [c["id"] for c in ic.get("adaptiveClaims", [])]
    _require(
        not (set(ids) & set(adaptive_ids)),
        f"claim id collision between core and adaptive: {sorted(set(ids) & set(adaptive_ids))}",
    )
    for red in ic["redLines"]:
        for key in ("id", "statement", "triggers"):
            _require(key in red, f"redLine {red.get('id')!r}: missing {key!r}")
        _require(len(red["triggers"]) >= 1, f"redLine {red['id']!r}: triggers must not be empty")
    for proto in ic["decayPrototypes"]:
        for key in ("category", "baseHalfLifeDays"):
            _require(key in proto, f"decayPrototype {proto.get('category')!r}: missing {key!r}")
        _require(
            float(proto["baseHalfLifeDays"]) > 0,
            f"decayPrototype {proto['category']!r}: baseHalfLifeDays must be > 0",
        )

    # 内容寻址校验
    declared = spec.get("contentHash")
    _require(isinstance(declared, str) and declared.startswith("sha256:"),
             "contentHash must be a 'sha256:<hex>' string")
    _require(declared == content_hash(spec),
             "contentHash does not match spec body; the contract was edited outside governance")

    # 更新治理闸门
    revision = spec["revision"]
    _require(isinstance(revision, int) and revision >= 1, "revision must be an integer >= 1")
    lineage = spec.get("lineage", [])
    _require(isinstance(lineage, list), "lineage must be a list")
    if revision > 1:
        _require(len(lineage) >= 1,
                 f"revision {revision} requires >=1 governance record in lineage")
        for rec in lineage:
            for key in ("revision", "approvedBy", "approvedAt", "diffSummary", "rationale"):
                _require(key in rec, f"governance record missing {key!r}")
            _require(bool(str(rec["approvedBy"]).strip()),
                     "governance record approvedBy must be a non-empty human principal")
            _require(bool(str(rec["rationale"]).strip()),
                     "governance record rationale must explain why the identity changed")
        _require(
            max(r["revision"] for r in lineage) >= revision,
            f"lineage must contain a record for revision {revision}",
        )

    rp = spec["retentionPolicy"]
    _require(isinstance(rp, Mapping), "retentionPolicy must be an object")
    band = rp.get("quarantineBand", [0.2, 0.35])
    _require(len(band) == 2 and float(band[0]) < float(band[1]),
             "retentionPolicy.quarantineBand must be [low, high] with low < high")


def parse_spec(spec: Mapping[str, Any]) -> SoulCore:
    validate_spec(spec)
    ic = spec["identityCore"]
    rp = spec.get("retentionPolicy", {})
    band = rp.get("quarantineBand", [0.20, 0.35])
    retention = RetentionPolicy(
        min_alignment_to_store=float(rp.get("minAlignmentToStore", 0.35)),
        quarantine_band=(float(band[0]), float(band[1])),
        agreement_floor=float(rp.get("agreementFloor", 0.60)),
        identity_affinity_min=float(rp.get("identityAffinityMin", 0.25)),
        identity_affinity_max=float(rp.get("identityAffinityMax", 2.00)),
        eviction_threshold=float(rp.get("evictionThreshold", 0.05)),
        reinforcement_delta=float(rp.get("reinforcementDelta", 0.005)),
        duplicate_similarity=float(rp.get("duplicateSimilarity", 0.92)),
    )
    return SoulCore(
        spec_version=str(spec["specVersion"]),
        id=str(spec["id"]),
        name=str(spec["name"]),
        revision=int(spec["revision"]),
        version=str(spec["version"]),
        created_at=str(spec["createdAt"]),
        content_hash=str(spec["contentHash"]),
        self_model=str(spec["selfModel"]),
        core_claims=tuple(_parse_claim(c, "core") for c in ic["coreClaims"]),
        adaptive_claims=tuple(_parse_claim(c, "adaptive") for c in ic.get("adaptiveClaims", [])),
        red_lines=tuple(
            RedLine(
                id=str(r["id"]),
                statement=str(r["statement"]),
                triggers=tuple(str(t).lower() for t in r["triggers"]),
                enforcement=str(r.get("enforcement", "hard")),
            )
            for r in ic["redLines"]
        ),
        decay_prototypes=tuple(
            DecayPrototype(
                category=str(p["category"]),
                base_half_life_days=float(p["baseHalfLifeDays"]),
                priority=int(p.get("priority", 0)),
            )
            for p in ic["decayPrototypes"]
        ),
        retention=retention,
        lineage=tuple(
            GovernanceRecord(
                revision=int(r["revision"]),
                approved_by=str(r["approvedBy"]),
                approved_at=str(r["approvedAt"]),
                diff_summary=str(r["diffSummary"]),
                rationale=str(r["rationale"]),
                migration_note=str(r.get("migrationNote", "")),
                rollback_to=r.get("rollbackTo"),
            )
            for r in spec.get("lineage", [])
        ),
        retractions=tuple(
            Retraction(
                target_ref=str(r["targetRef"]),
                reason=str(r["reason"]),
                issued_at=str(r["issuedAt"]),
            )
            for r in spec.get("retractions", [])
        ),
        raw=spec,
    )


def load_soul(path: Path) -> SoulCore:
    """从磁盘加载身份契约。校验失败即抛错，绝不返回「部分可用」的核心。"""
    path = Path(path)
    if not path.exists():
        raise ContractViolation(f"soul spec not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        spec = json.load(fh)
    return parse_spec(spec)


def seal_spec(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """给 spec 写入/刷新 contentHash。供 tests 与工具使用。"""
    sealed = dict(spec)
    sealed.pop("contentHash", None)
    sealed["contentHash"] = content_hash(sealed)
    return sealed


def render_soul_md(soul: SoulCore) -> str:
    """渲染 SOUL.md —— 单向视图（view），不参与解析。"""
    lines: List[str] = [
        f"# {soul.name}",
        "",
        f"> revision `{soul.revision}` · version `{soul.version}` · spec `{soul.spec_version}`",
        f"> content_hash `{soul.content_hash}`",
        "",
        "<!-- GENERATED FILE — do not edit. Source of truth: soul/specs/*.soul.json -->",
        "",
        "## 自我模型 (Self Model)",
        "",
        soul.self_model,
        "",
        "## 核心主张 (Core Claims) — locked",
        "",
    ]
    for c in soul.core_claims:
        lines.append(f"- **{c.id}** (weight {c.weight}, strength {c.strength}): {c.statement}")
        lines.append(f"  - assertion: `{c.assertion}`")
        if c.decision_rule:
            lines.append(f"  - decisionRule: `{canonical_json(c.decision_rule)}`")
    if soul.adaptive_claims:
        lines += ["", "## 可演化主张 (Adaptive Claims) — governed", ""]
        for c in soul.adaptive_claims:
            lines.append(f"- **{c.id}** (weight {c.weight}): {c.statement}")
    lines += ["", "## 红线 (Red Lines) — hard", ""]
    for r in soul.red_lines:
        lines.append(f"- **{r.id}**: {r.statement} (enforcement: {r.enforcement})")
    lines += ["", "## 衰减原型 (Decay Prototypes)", "", "| category | base half-life (days) |", "| --- | --- |"]
    for p in soul.decay_prototypes:
        lines.append(f"| `{p.category}` | {p.base_half_life_days} |")
    lines += [
        "",
        "## 保留策略 (Retention Policy)",
        "",
        "```json",
        canonical_json(dataclasses.asdict(soul.retention)),
        "```",
    ]
    if soul.lineage:
        lines += ["", "## 变更谱系 (Lineage)", ""]
        for g in soul.lineage:
            lines.append(
                f"- revision {g.revision} · approved_by `{g.approved_by}` · {g.approved_at}"
            )
            lines.append(f"  - diff: {g.diff_summary}")
            lines.append(f"  - rationale: {g.rationale}")
            if g.migration_note:
                lines.append(f"  - migration: {g.migration_note}")
    if soul.retractions:
        lines += ["", "## 退役指令 (Retractions)", ""]
        for r in soul.retractions:
            lines.append(f"- `{r.target_ref}` — {r.reason} ({r.issued_at})")
    lines.append("")
    return "\n".join(lines)
