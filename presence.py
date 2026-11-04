"""本群对话状态、Jev 最近消息，以及按群合并的待处理消息。"""

import asyncio
from collections import deque
from dataclasses import dataclass, field

from .decision import OptionalBudget


@dataclass
class ChatMessage:
    id: str
    sender: str
    name: str
    text: str
    received_at: float
    is_bot: bool = False
    mentions: list[str] = field(default_factory=list)
    reply_to_id: str | None = None
    reply_to_sender: str | None = None
    quoted_text: str = ""

    def as_state(self) -> dict:
        """只提供聊天信息，计时字段留在插件内。"""
        return {
            "id": self.id,
            "sender": self.sender,
            "name": self.name,
            "text": self.text,
            "is_bot": self.is_bot,
            "mentions": self.mentions,
            "reply_to_id": self.reply_to_id,
            "reply_to_sender": self.reply_to_sender,
            "quoted_text": self.quoted_text,
        }


@dataclass
class MessageBatch:
    messages: list[ChatMessage]
    explicit: bool


@dataclass
class WaitingMessage:
    message: ChatMessage
    explicit: bool
    task: asyncio.Task
    ready: asyncio.Future[MessageBatch | None]


class GroupDialogue:
    """每个机器人账号＋群号一份；成员更换不会切断对话。"""

    def __init__(self) -> None:
        self.last_accepted_at: float | None = None
        self.recent: deque[ChatMessage] = deque(maxlen=10)
        self.budget = OptionalBudget()
        self._active_task: asyncio.Task | None = None
        self._waiting: list[WaitingMessage] = []

    def accept_reply(self, now: float) -> None:
        self.last_accepted_at = now

    def record(self, message: ChatMessage) -> None:
        self.recent.append(message)

    def state(self, batch: MessageBatch, now: float, ttl: float) -> dict:
        elapsed = None if self.last_accepted_at is None else now - self.last_accepted_at
        remaining = 0 if elapsed is None else max(0, ttl - elapsed)
        current_ids = {message.id for message in batch.messages}
        history = []
        characters_left = 2000
        for message in reversed(self.recent):
            if message.id in current_ids or now - message.received_at > 180:
                continue
            item = message.as_state()
            item["text"] = item["text"][:characters_left]
            characters_left -= len(item["text"])
            item["quoted_text"] = item["quoted_text"][:characters_left]
            characters_left -= len(item["quoted_text"])
            history.append(item)
            if characters_left == 0:
                break

        state = {
            "group_dialogue": {
                "active": remaining > 0,
                "seconds_since_last_accepted_reply_request": elapsed,
                "remaining_seconds": remaining,
            },
            "recent_messages": list(reversed(history)),
        }
        if len(batch.messages) == 1:
            state["current_message"] = batch.messages[0].as_state()
        else:
            state["current_messages"] = [m.as_state() for m in batch.messages]
        return state

    async def take_turn(
        self, message: ChatMessage, explicit: bool
    ) -> MessageBatch | None:
        """等待本轮结束；明确呼叫保留呼叫者的原事件。"""
        task = asyncio.current_task()
        if self._active_task is None:
            self._start_turn(task)
            return MessageBatch([message], explicit)

        ready = asyncio.get_running_loop().create_future()
        self._waiting.append(WaitingMessage(message, explicit, task, ready))
        return await ready

    def _start_turn(self, task: asyncio.Task) -> None:
        self._active_task = task
        # AstrBot 4.29.3 每条消息对应一个 pipeline task；结束时才轮到下一批。
        task.add_done_callback(self._finish_turn)

    def _finish_turn(self, task: asyncio.Task) -> None:
        self._active_task = None
        waiting = [item for item in self._waiting if not item.ready.done()]
        self._waiting.clear()
        if not waiting:
            return

        explicit_indexes = [i for i, item in enumerate(waiting) if item.explicit]
        leader_index = explicit_indexes[-1] if explicit_indexes else len(waiting) - 1
        current = waiting[: leader_index + 1]
        self._waiting.extend(waiting[leader_index + 1 :])
        leader = current[-1]
        batch = MessageBatch([item.message for item in current], leader.explicit)
        self._start_turn(leader.task)
        for item in current[:-1]:
            item.ready.set_result(None)
        leader.ready.set_result(batch)

    def close(self) -> None:
        """卸载时结束等待，不取消 AstrBot 已经开始的回复。"""
        for item in self._waiting:
            if not item.ready.done():
                item.ready.set_result(None)
        self._waiting.clear()
