"""群状态及消息合并；不启动 AstrBot、不调用模型。"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_whale_reply_gate.presence import (
    ChatMessage,
    GroupDialogue,
    MessageBatch,
)


def message(number, sender="A", text="那第二个呢？", now=10):
    return ChatMessage(str(number), sender, sender, text, now)


class DialogueTests(unittest.TestCase):
    def test_only_accepting_request_starts_and_renews_presence(self):
        group = GroupDialogue()
        current = message(1)
        group.record(current)
        batch = MessageBatch([current], True)
        self.assertFalse(group.state(batch, 10, 120)["group_dialogue"]["active"])
        group.accept_reply(15)
        state = group.state(batch, 35, 120)["group_dialogue"]
        self.assertEqual(state["remaining_seconds"], 100)
        self.assertEqual(state["seconds_since_last_accepted_reply_request"], 20)
        self.assertFalse(group.state(batch, 135, 120)["group_dialogue"]["active"])
        group.accept_reply(150)
        self.assertTrue(group.state(batch, 151, 120)["group_dialogue"]["active"])

    def test_context_keeps_other_members_bot_and_quote_relationship(self):
        group = GroupDialogue()
        first = message(1, text="推荐两个方案")
        bot = message(2, sender="bot", text="第一个便宜，第二个性能更好")
        bot.is_bot = True
        current = message(3, sender="B")
        current.reply_to_id = "2"
        current.reply_to_sender = "bot"
        for item in [first, bot, current]:
            group.record(item)
        state = group.state(MessageBatch([current], False), 20, 120)
        self.assertEqual([m["sender"] for m in state["recent_messages"]], ["A", "bot"])
        self.assertTrue(state["recent_messages"][1]["is_bot"])
        self.assertEqual(state["current_message"]["sender"], "B")
        self.assertEqual(state["current_message"]["reply_to_id"], "2")

    def test_history_obeys_age_count_and_text_limit(self):
        group = GroupDialogue()
        for number in range(20):
            group.record(message(number, text="文" * 500, now=number * 10))
        current = message(20, now=200)
        state = group.state(MessageBatch([current], False), 200, 120)
        history = state["recent_messages"]
        self.assertLessEqual(len(history), 10)
        self.assertLessEqual(sum(len(m["text"]) for m in history), 2000)
        self.assertTrue(all(int(m["id"]) >= 2 for m in history))
        group.record(message(21, now=201))
        self.assertEqual(
            group.state(MessageBatch([current], False), 400, 120)["recent_messages"], []
        )

    def test_groups_do_not_share_history_or_state(self):
        first, second = GroupDialogue(), GroupDialogue()
        first.accept_reply(10)
        first.record(message(1, text="私有群约定"))
        batch = MessageBatch([message(2)], False)
        self.assertFalse(second.state(batch, 20, 120)["group_dialogue"]["active"])
        self.assertEqual(second.state(batch, 20, 120)["recent_messages"], [])


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_pipeline_completion_and_merges_next_batch(self):
        group = GroupDialogue()
        first_started = asyncio.Event()
        finish_first = asyncio.Event()
        completed = []

        async def handle(number, explicit=False):
            batch = await group.take_turn(message(number, sender=str(number)), explicit)
            if number == 1:
                first_started.set()
                await finish_first.wait()
            completed.append((number, batch))

        first = asyncio.create_task(handle(1))
        await first_started.wait()
        second = asyncio.create_task(handle(2))
        third = asyncio.create_task(handle(3))
        await asyncio.sleep(0)
        self.assertEqual(completed, [])
        finish_first.set()
        await asyncio.gather(first, second, third)
        batches = dict(completed)
        self.assertIsNone(batches[2])
        self.assertEqual([m.sender for m in batches[3].messages], ["2", "3"])
        self.assertFalse(batches[3].explicit)

    async def test_explicit_caller_keeps_identity_and_later_messages_wait(self):
        group = GroupDialogue()
        first_started = asyncio.Event()
        finish_first = asyncio.Event()
        batches = []

        async def handle(number, explicit=False):
            batch = await group.take_turn(message(number, sender=str(number)), explicit)
            if number == 1:
                first_started.set()
                await finish_first.wait()
            if batch is not None:
                batches.append((number, batch))

        first = asyncio.create_task(handle(1))
        await first_started.wait()
        caller = asyncio.create_task(handle(2, explicit=True))
        other = asyncio.create_task(handle(3))
        await asyncio.sleep(0)
        finish_first.set()
        await asyncio.gather(first, caller, other)
        self.assertEqual([number for number, _ in batches], [1, 2, 3])
        self.assertTrue(batches[1][1].explicit)
        self.assertEqual(batches[1][1].messages[-1].sender, "2")
        self.assertFalse(batches[2][1].explicit)

    async def test_cancelling_active_pipeline_releases_waiters(self):
        group = GroupDialogue()
        started = asyncio.Event()

        async def active():
            await group.take_turn(message(1), False)
            started.set()
            await asyncio.Future()

        first = asyncio.create_task(active())
        await started.wait()
        waiting = asyncio.create_task(group.take_turn(message(2), False))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        self.assertEqual((await waiting).messages[0].id, "2")

    async def test_unload_releases_waiters_without_cancelling_active_pipeline(self):
        group = GroupDialogue()
        await group.take_turn(message(1), False)
        waiting = asyncio.create_task(group.take_turn(message(2), False))
        await asyncio.sleep(0)
        group.close()
        self.assertIsNone(await waiting)
        self.assertFalse(asyncio.current_task().cancelled())


if __name__ == "__main__":
    unittest.main()
