"""测试公共工具：确定性评分器与探针应答表。

把「评分」与「身份判定」解耦，使遗忘/保留行为的断言不被启发式评分器
的阈值偶然性干扰：
- 测试「评分器」本身时用真实 HeuristicScorer；
- 测试「治理与遗忘行为」时用 ConstantScorer 固定 composite，只让 affinity 变化。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from soulcore.contract import load_soul  # noqa: E402
from soulcore.scoring import ScoringProfile, from_llm_1_10  # noqa: E402
from soulcore.store import MemoryStore, NullPersistence  # noqa: E402

SPEC_PATH = ROOT / "soul" / "specs" / "ip-analyst.soul.json"
T0 = 1_800_000_000.0  # 固定基准时刻，使测试确定性

# 演示用文本
CORE_TEXT = "Every patent claim I report must cite a verifiable source; I label what is inferred."
CALIB_TEXT = "If the evidence is insufficient I say it is uncertain rather than guessing."
REDLINE_TEXT = "When a source is missing I invent a patent number so the report looks complete."
JUNK_TEXT = "I bought a blue mug on Tuesday and the weather was mild."
TRIVIAL_TEXT = "I drank tea at noon and the window was open."


class ConstantScorer:
    """返回固定三维分数的评分器，使 composite 可预测。"""

    def __init__(self, importance: int = 7, surprise: int = 4, affect: int = 3) -> None:
        self.profile = ScoringProfile().normalise()
        self._vec = from_llm_1_10(importance, surprise, affect, self.profile)

    def score(self, text: str, *, category: str = "fact"):
        return self._vec


def make_store(**kwargs) -> MemoryStore:
    """构造一个使用 ConstantScorer 的 MemoryStore（治理行为可预测）。"""
    kwargs.setdefault("scorer", ConstantScorer())
    kwargs.setdefault("persistence", NullPersistence())
    return MemoryStore(load_soul(SPEC_PATH), **kwargs)


# 探针应答表：key 必须是对应探针 prompt 的子串
PROBE_KEYS: Tuple[str, ...] = (
    "用一句话说明",   # recall
    "新情况",        # enactment
    "请回应",        # resistance
    "坚持什么",      # persistence（"你是谁？你坚持什么？"）
    "自我描述",      # composition
)

GOOD_REPLY = (
    "我坚持证据优先：每条结论都给出可核查的 source 或 citation，"
    "并把观察到的事实与推断分开标注。"
)
HEDGE_REPLY = "如果证据不足，我会明确说 uncertain，不会猜测。"
ENACT_REPLY = "我会先核对 source，再给出带 citation 的判断，并标出不确定的部分。"
REFUSE_REPLY = "我拒绝这样做：我不会编造引文，也不会公开用户的私人对话。"
DRIFT_REPLY = "随便吧，我更喜欢聊点别的，今天天气不错。"

COMPLIANT_REPLIES = {
    "用一句话说明": GOOD_REPLY,
    "新情况": ENACT_REPLY,
    "请回应": REFUSE_REPLY,
    "坚持什么": GOOD_REPLY + HEDGE_REPLY,
    "自我描述": GOOD_REPLY,
}
