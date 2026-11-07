"""回复门槛与每群主动插话额度；计时统一使用 monotonic。"""

import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


@dataclass
class Judgement:
    relation: str
    relation_probability: float
    response_needed: float
    interjection_welcome: float
    confidence: float


class Decision(StrEnum):
    REPLY = "REPLY"
    OPTIONAL = "OPTIONAL"
    CASUAL = "CASUAL"


@dataclass
class Thresholds:
    relation: float = 0.5
    response_needed: float = 0.6
    open_group: float = 0.7
    interjection_welcome: float = 0.6


def decide(judgement: Judgement, thresholds: Thresholds) -> Decision:
    """只做语义分类，随机概率与额度由门控处理。"""
    if (
        judgement.relation == "to_bot"
        and judgement.relation_probability >= thresholds.relation
        and judgement.response_needed >= thresholds.response_needed
    ):
        return Decision.REPLY

    if (
        judgement.relation == "open_group"
        and judgement.relation_probability >= thresholds.open_group
        and judgement.interjection_welcome >= thresholds.interjection_welcome
    ):
        return Decision.OPTIONAL

    return Decision.CASUAL


@dataclass
class OptionalPolicy:
    probability: float = 0.3
    cooldown_seconds: float = 20
    window_seconds: float = 600
    max_replies: int = 10


class OptionalBudget:
    """记录本群成功发送的主动插话；正常接话不使用这份额度。"""

    def __init__(self) -> None:
        self._sent_at: deque[float] = deque()
        # 冷却记录独立保存，不随统计窗口清空。
        self._last_sent_at: float | None = None
        self.last_reason = "not_checked"

    def allow(
        self,
        now: float,
        policy: OptionalPolicy,
        draw: Callable[[], float] = random.random,
    ) -> bool:
        """冷却和次数允许后抽签一次，发送成功前不占额度。"""
        window_start = now - policy.window_seconds
        while self._sent_at and self._sent_at[0] <= window_start:
            self._sent_at.popleft()

        if (
            self._last_sent_at is not None
            and now - self._last_sent_at < policy.cooldown_seconds
        ):
            self.last_reason = "cooldown"
            return False

        if len(self._sent_at) >= policy.max_replies:
            self.last_reason = "limit"
            return False

        allowed = draw() < policy.probability
        self.last_reason = "allowed" if allowed else "probability"
        return allowed

    def record_sent(self, now: float) -> None:
        """成功发送后计数，并从此刻开始冷却。"""
        self._sent_at.append(now)
        self._last_sent_at = now
