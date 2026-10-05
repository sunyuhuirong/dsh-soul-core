"""执行层杠杆 1/4：多维重要性评分 (importance / surprise / affect)。

对齐已有工作：
- `Generative Agents` (Park et al., 2023) 用 LLM 对记忆的 poignancy 打 1-10 分，
  该分数在检索时加权，并作为反思（reflection）的触发阈值。
- 一篇基于 Ebbinghaus 的 LLM 记忆管理框架（ACM, 10.1145/3803291.3803294）
  把「内在价值」拆成 emotional intensity / novelty / repetition frequency 三维。

本实现取三维：importance（重要性）、surprise（意外性）、affect（情感强度），
LLM 打 1-10 分，加权合成为 composite score；仅超过阈值者才允许长期存储。
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Sequence, Tuple

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ScoringProfile:
    """评分权重与阈值。所有权重之和应为 1.0。"""

    w_importance: float = 0.55
    w_surprise: float = 0.25
    w_affect: float = 0.20
    store_threshold: float = 0.45      # composite >= 此值才可长期存储
    reflect_threshold: float = 0.60    # composite >= 此值触发反思/合并
    scale_max: int = 10                # LLM 打分上限

    def normalise(self) -> "ScoringProfile":
        total = self.w_importance + self.w_surprise + self.w_affect
        if abs(total - 1.0) < 1e-9:
            return self
        return dataclasses.replace(
            self,
            w_importance=self.w_importance / total,
            w_surprise=self.w_surprise / total,
            w_affect=self.w_affect / total,
        )


@dataclasses.dataclass(frozen=True)
class ScoreVector:
    """单条记忆的三维分数（均为 0-1 归一化）+ 合成分。"""

    importance: float
    surprise: float
    affect: float
    composite: float
    source: str = "llm"  # "llm" | "heuristic" | "cache"

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


class Scorer(Protocol):
    """评分器协议。实现可以是 LLM、启发式规则或缓存包装。"""

    def score(self, text: str, *, category: str = "fact") -> ScoreVector:  # pragma: no cover
        ...


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def composite_score(
    importance: float,
    surprise: float,
    affect: float,
    profile: ScoringProfile,
) -> float:
    """加权合成。输入假定已是 0-1。"""
    p = profile.normalise()
    return _clamp01(p.w_importance * importance + p.w_surprise * surprise + p.w_affect * affect)


def from_llm_1_10(
    importance: int,
    surprise: int,
    affect: int,
    profile: ScoringProfile,
) -> ScoreVector:
    """把 LLM 的 1-10 原始分转为 ScoreVector。越界值被夹紧，不抛错。"""
    hi = float(profile.scale_max)
    lo = 1.0

    def norm(v: int) -> float:
        return _clamp01((float(v) - lo) / (hi - lo))

    i, s, a = norm(importance), norm(surprise), norm(affect)
    return ScoreVector(
        importance=i,
        surprise=s,
        affect=a,
        composite=composite_score(i, s, a, profile),
        source="llm",
    )


# ---------------------------------------------------------------------------
# LLM 评分器
# ---------------------------------------------------------------------------

SCORE_PROMPT = """You are a memory scorer for a personal agent.

Rate the candidate memory on three dimensions, each an integer from 1 to 10:

- importance: how much this matters to who the agent (and its user) is and to future decisions.
  1 = purely mundane (e.g. brushing teeth); 10 = identity-defining or irreversible.
- surprise: how unexpected or novel this is relative to what is already known.
  1 = entirely expected / already known; 10 = contradicts or upends prior understanding.
- affect: emotional intensity carried by the memory.
  1 = emotionally flat; 10 = intense (grief, joy, conflict, shame, love).

Candidate memory:
\"\"\"{text}\"\"\"

Answer with exactly three lines and nothing else:
importance: <int>
surprise: <int>
affect: <int>
"""

_LINE_RE = re.compile(r"^\s*(importance|surprise|affect)\s*[:=]\s*(-?\d+)", re.IGNORECASE | re.MULTILINE)


def parse_score_response(text: str, profile: ScoringProfile) -> Optional[ScoreVector]:
    """严格解析 LLM 输出。三个维度必须齐全，否则返回 None（上层降级）。"""
    found: Dict[str, int] = {}
    for name, raw in _LINE_RE.findall(text):
        found[name.lower()] = int(raw)
    if set(found) != {"importance", "surprise", "affect"}:
        return None
    return from_llm_1_10(found["importance"], found["surprise"], found["affect"], profile)


class LLMScorer:
    """把任意 `call_llm(prompt) -> str` 适配成 Scorer。

    失败、超时或解析不到时**自动降级**为启发式评分，绝不阻塞记忆流水线。
    """

    def __init__(
        self,
        call_llm: Callable[[str], str],
        profile: Optional[ScoringProfile] = None,
        fallback: Optional[Scorer] = None,
        cache: Optional[Dict[str, ScoreVector]] = None,
    ) -> None:
        self._call = call_llm
        self.profile = (profile or ScoringProfile()).normalise()
        self._fallback = fallback or HeuristicScorer(self.profile)
        self._cache = cache if cache is not None else {}
        self.degraded_calls = 0

    def score(self, text: str, *, category: str = "fact") -> ScoreVector:
        key = f"{category}::{text}"
        if key in self._cache:
            cached = self._cache[key]
            return dataclasses.replace(cached, source="cache")
        try:
            raw = self._call(SCORE_PROMPT.format(text=text))
            parsed = parse_score_response(raw or "", self.profile)
        except Exception:
            parsed = None
        if parsed is None:
            self.degraded_calls += 1
            parsed = self._fallback.score(text, category=category)
        self._cache[key] = parsed
        return parsed


# ---------------------------------------------------------------------------
# 启发式评分器（零依赖、确定性；用于测试与离线降级）
# ---------------------------------------------------------------------------

IDENTITY_KEYWORDS: Tuple[str, ...] = (
    "i am", "i always", "i never", "my name", "my principle", "my value",
    "commitment", "boundary", "refuse", "promise", "identity", "core",
    "我", "原则", "底线", "永远", "从不", "承诺", "身份",
)
SURPRISE_KEYWORDS: Tuple[str, ...] = (
    "surprised", "unexpected", "suddenly", "changed my mind", "actually",
    "turns out", "correction", "wrong", "mistake", "contradict",
    "意外", "突然", "其实", "纠正", "错", "改变",
)
AFFECT_KEYWORDS: Tuple[str, ...] = (
    "love", "hate", "afraid", "anxious", "grief", "angry", "proud", "ashamed",
    "happy", "hurt", "excited", "lonely", "despair", "joy",
    "爱", "恨", "害怕", "焦虑", "难过", "生气", "骄傲", "羞耻", "孤独", "开心",
)


class HeuristicScorer:
    """基于词面特征的确定性评分器。

    它不是「智能」的，但让整条流水线在没有 LLM 的情况下可跑、可断言、可回归。
    """

    def __init__(self, profile: Optional[ScoringProfile] = None) -> None:
        self.profile = (profile or ScoringProfile()).normalise()

    @staticmethod
    def _hits(text: str, needles: Sequence[str]) -> int:
        tl = text.lower()
        return sum(1 for n in needles if n in tl)

    def score(self, text: str, *, category: str = "fact") -> ScoreVector:
        tl = text.lower()
        length_factor = min(1.0, len(text) / 240.0)

        i_hits = self._hits(tl, IDENTITY_KEYWORDS)
        s_hits = self._hits(tl, SURPRISE_KEYWORDS)
        a_hits = self._hits(tl, AFFECT_KEYWORDS)

        # 1-10 原始分 -> 归一
        def to_norm(hits: int) -> float:
            raw = 1 + min(9, hits * 3 + int(round(length_factor * 2)))
            return _clamp01((raw - 1) / 9.0)

        i, s, a = to_norm(i_hits), to_norm(s_hits), to_norm(a_hits)
        return ScoreVector(
            importance=i,
            surprise=s,
            affect=a,
            composite=composite_score(i, s, a, self.profile),
            source="heuristic",
        )
