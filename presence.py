"""本群对话状态、Jev 最近消息，以及按群合并的待处理消息。"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.api.message_components import Image

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
    images: list["Image"] = field(default_factory=list, repr=False)

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
            "image_count": len(self.images),
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
        self._window: asyncio.TimerHandle | None = None
        self._closed = False

    def accept_reply(self, now: float) -> None:
        self.last_accepted_at = now

    def record(self, message: ChatMessage) -> None:
        self.recent.append(message)

    def state(self, batch: MessageBatch, now: float, ttl: float) -> dict:
        elapsed = None if self.last_accepted_at is None else now - self.last_accepted_at
        remaining = 0 if elapsed is None else max(0, ttl - elapsed)
        current_ids = {message.id for message in batch.messages}
        batch_started_at = min(message.received_at for message in batch.messages)
        history = []
        characters_left = 2000
        for message in reversed(self.recent):
            # 历史只取本批开始前的消息。
            if (
                message.id in current_ids
                or message.received_at >= batch_started_at
                or now - message.received_at > 180
            ):
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
        self, message: ChatMessage, explicit: bool, wait_seconds: float = 1.0
    ) -> MessageBatch | None:
        """固定窗口收集消息，由最后一条消息的原任务承接。"""
        if self._closed:
            return None

        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        item = WaitingMessage(message, explicit, asyncio.current_task(), ready)
        if not self._waiting:
            self._window = loop.call_later(wait_seconds, self._window_elapsed)
        self._waiting.append(item)
        try:
            return await ready
        except asyncio.CancelledError:
            if item in self._waiting:
                self._waiting.remove(item)
                if not self._waiting and self._window is not None:
                    self._window.cancel()
                    self._window = None
            raise

    def _window_elapsed(self) -> None:
        self._window = None
        self._dispatch()

    def _dispatch(self) -> None:
        if self._closed or self._active_task is not None or self._window is not None:
            return
        waiting = [item for item in self._waiting if not item.ready.done()]
        self._waiting.clear()
        if not waiting:
            return

        # 冻结整批；后续消息进入下一轮窗口。
        leader = waiting[-1]
        batch = MessageBatch(
            [item.message for item in waiting], any(item.explicit for item in waiting)
        )
        self._active_task = leader.task
        leader.task.add_done_callback(self._finish_turn)
        for item in waiting[:-1]:
            item.ready.set_result(None)
        leader.ready.set_result(batch)

    def _finish_turn(self, task: asyncio.Task) -> None:
        self._active_task = None
        self._dispatch()

    def close(self) -> None:
        """卸载时结束等待，不取消已经开始的回复。"""
        self._closed = True
        if self._window is not None:
            self._window.cancel()
            self._window = None
        for item in self._waiting:
            if not item.ready.done():
                item.ready.set_result(None)
        self._waiting.clear()
