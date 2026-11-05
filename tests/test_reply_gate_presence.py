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
        first = message(1, text="推荐两个方案", now=8)
        bot = message(2, sender="bot", text="第一个便宜，第二个性能更好", now=9)
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

    def test_history_stops_before_earliest_batch_message(self):
        for multiple in [False, True]:
            with self.subTest(multiple=multiple):
                group = GroupDialogue()
                earliest = message(2, now=10)
                later = message(3, now=12)
                # 批内顺序不影响历史边界。
                batch = MessageBatch(
                    [later, earliest] if multiple else [earliest], False
                )
                group.record(message(1, now=9))
                group.record(earliest)
                group.record(message(4, now=10))
                group.record(message(5, now=11))
                if multiple:
                    group.record(later)
                before = group.state(batch, 15, 120)
                self.assertEqual([m["id"] for m in before["recent_messages"]], ["1"])

                # 冻结后的新消息不能进入旧批次的历史，也不能挤占字数。
                group.record(message(6, now=16, text="后到消息" * 600))
                group.record(message(2, now=8))
                after = group.state(batch, 20, 120)
                self.assertEqual(after["recent_messages"], before["recent_messages"])
                key = "current_messages" if multiple else "current_message"
                self.assertEqual(after[key], before[key])

    def test_groups_do_not_share_history_or_state(self):
        first, second = GroupDialogue(), GroupDialogue()
        first.accept_reply(10)
        first.record(message(1, text="私有群约定"))
        batch = MessageBatch([message(2)], False)
        self.assertFalse(second.state(batch, 20, 120)["group_dialogue"]["active"])
        self.assertEqual(second.state(batch, 20, 120)["recent_messages"], [])

    def test_images_only_expose_count_and_have_independent_defaults(self):
        first, second = message(1, now=9), message(2)
        image = object()
        first.images.append(image)
        group = GroupDialogue()
        group.record(first)
        state = group.state(MessageBatch([second], False), 20, 120)
        self.assertEqual(state["recent_messages"][0]["image_count"], 1)
        self.assertEqual(state["current_message"]["image_count"], 0)
        self.assertNotIn("images", first.as_state())
        self.assertEqual(second.images, [])
        self.assertNotIn("images=", repr(first))


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks = []
        self.groups = []

    async def asyncTearDown(self):
        for group in self.groups:
            group.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def group(self):
        group = GroupDialogue()
        self.groups.append(group)
        return group

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def test_idle_window_merges_all_and_last_event_owns_batch(self):
        group = self.group()
        first = self.spawn(group.take_turn(message(1), False, 0.04))
        await asyncio.sleep(0.01)
        self.assertFalse(first.done())
        second = self.spawn(group.take_turn(message(2), False, 0.04))
        third = self.spawn(group.take_turn(message(3), False, 0.04))
        results = await asyncio.gather(first, second, third)
        self.assertEqual(results[:2], [None, None])
        self.assertEqual([m.id for m in results[2].messages], ["1", "2", "3"])
        self.assertFalse(results[2].explicit)

    async def test_later_arrival_does_not_restart_window(self):
        group = self.group()
        first = self.spawn(group.take_turn(message(1), False, 0.04))
        await asyncio.sleep(0.01)
        # 后到消息的窗口参数不能延长已开始的窗口。
        last = self.spawn(group.take_turn(message(2), False, 1.0))
        batch = await asyncio.wait_for(last, 0.2)
        self.assertIsNone(await first)
        self.assertEqual([m.id for m in batch.messages], ["1", "2"])

    async def test_multiple_explicit_calls_keep_entire_batch_and_last_event(self):
        group = self.group()
        tasks = [
            self.spawn(group.take_turn(message(i), explicit, 0.02))
            for i, explicit in enumerate([False, True, True, False], 1)
        ]
        results = await asyncio.gather(*tasks)
        self.assertEqual(results[:-1], [None, None, None])
        self.assertEqual([m.id for m in results[-1].messages], ["1", "2", "3", "4"])
        self.assertTrue(results[-1].explicit)

    async def test_arrivals_during_generation_form_next_batch_without_extra_wait(self):
        group = self.group()
        started, finish = asyncio.Event(), asyncio.Event()
        first_batches = []

        async def active():
            first_batches.append(await group.take_turn(message(1), False, 0.01))
            started.set()
            await finish.wait()

        first = self.spawn(active())
        await started.wait()
        second = self.spawn(group.take_turn(message(2), True, 0.15))
        third = self.spawn(group.take_turn(message(3), False, 0.15))
        await asyncio.sleep(0.2)
        self.assertFalse(second.done())
        self.assertFalse(third.done())
        self.assertEqual([m.id for m in first_batches[0].messages], ["1"])
        finish.set()
        await first
        batch = await asyncio.wait_for(third, 0.08)
        self.assertIsNone(await second)
        self.assertEqual([m.id for m in batch.messages], ["2", "3"])
        self.assertTrue(batch.explicit)

    async def test_next_batch_still_waits_for_its_window_after_generation(self):
        group = self.group()
        started, finish = asyncio.Event(), asyncio.Event()

        async def active():
            await group.take_turn(message(1), False, 0)
            started.set()
            await finish.wait()

        first = self.spawn(active())
        await started.wait()
        second = self.spawn(group.take_turn(message(2), False, 0.08))
        await asyncio.sleep(0)
        finish.set()
        await first
        await asyncio.sleep(0.01)
        self.assertFalse(second.done())
        self.assertEqual((await second).messages[0].id, "2")

    async def test_groups_have_independent_queues(self):
        first, second = self.group(), self.group()
        started, finish = asyncio.Event(), asyncio.Event()

        async def active():
            await first.take_turn(message(1), False, 0)
            started.set()
            await finish.wait()

        active_task = self.spawn(active())
        await started.wait()
        blocked = self.spawn(first.take_turn(message(2), True, 0.01))
        independent = self.spawn(second.take_turn(message(3), False, 0.01))
        batch = await asyncio.wait_for(independent, 0.2)
        self.assertEqual([m.id for m in batch.messages], ["3"])
        self.assertFalse(batch.explicit)
        self.assertFalse(blocked.done())
        finish.set()
        await active_task
        self.assertTrue((await blocked).explicit)

    async def test_cancelling_active_pipeline_releases_next_batch(self):
        group = self.group()
        started = asyncio.Event()

        async def active():
            await group.take_turn(message(1), False, 0)
            started.set()
            await asyncio.Future()

        first = self.spawn(active())
        await started.wait()
        waiting = self.spawn(group.take_turn(message(2), False, 0.01))
        await asyncio.sleep(0.02)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        self.assertEqual((await waiting).messages[0].id, "2")

    async def test_cancelling_last_waiter_preserves_other_messages(self):
        group = self.group()
        first = self.spawn(group.take_turn(message(1), False, 0.03))
        last = self.spawn(group.take_turn(message(2), True, 0.03))
        await asyncio.sleep(0)
        last.cancel()
        await asyncio.gather(last, return_exceptions=True)
        batch = await first
        self.assertEqual([m.id for m in batch.messages], ["1"])
        self.assertFalse(batch.explicit)

    async def test_cancelling_only_waiter_allows_fresh_window(self):
        group = self.group()
        cancelled = self.spawn(group.take_turn(message(1), False, 1.0))
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        fresh = self.spawn(group.take_turn(message(2), False, 0.01))
        self.assertEqual((await asyncio.wait_for(fresh, 0.2)).messages[0].id, "2")

    async def test_close_during_collection_releases_all_and_stays_closed(self):
        group = self.group()
        waiting = [self.spawn(group.take_turn(message(i), False, 0.02)) for i in [1, 2]]
        await asyncio.sleep(0)
        group.close()
        self.assertEqual(await asyncio.gather(*waiting), [None, None])
        await asyncio.sleep(0.03)
        self.assertIsNone(await group.take_turn(message(3), True, 0))

    async def test_close_keeps_active_pipeline_and_releases_next_batch(self):
        group = self.group()
        started, finish = asyncio.Event(), asyncio.Event()

        async def active():
            await group.take_turn(message(1), False, 0)
            started.set()
            await finish.wait()

        first = self.spawn(active())
        await started.wait()
        waiting = self.spawn(group.take_turn(message(2), False, 0.02))
        await asyncio.sleep(0)
        group.close()
        self.assertIsNone(await waiting)
        self.assertFalse(first.done())
        finish.set()
        await first
        self.assertIsNone(await group.take_turn(message(3), False, 0))


if __name__ == "__main__":
    unittest.main()
