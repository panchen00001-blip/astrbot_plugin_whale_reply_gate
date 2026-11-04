"""在 AstrBot 4.29.3 中验证事件与上下文接入，模型和 QQ 发送使用测试替身。"""

import asyncio
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_whale_reply_gate.decision import Judgement, OptionalPolicy
from astrbot_plugin_whale_reply_gate.main import WhaleReplyGate
from typesafe_sdk import TypeSafeError

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.builtin_stars.astrbot.group_chat_context import GroupChatContext
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.db.po import Conversation
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def make_event(
    number=1, text="那第二个贵不贵？", sender="A", group="test-group", parts=None
):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message.self_id = "bot"
    message.message_id = str(number)
    message.group_id = group
    message.sender = MessageMember(sender, sender)
    message.message = parts if parts is not None else [Plain(text)]
    message.message_str = text
    message.raw_message = {}
    return AstrMessageEvent(
        text,
        message,
        PlatformMetadata("aiocqhttp", "test", "test-platform"),
        group or sender,
    )


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = AstrBotConfig(
            str(directory / "config.json"),
            schema=json.loads((PLUGIN_DIR / "_conf_schema.json").read_text()),
        )
        self.config["test_group_ids"] = ["test-group"]
        self.config["log_enabled"] = False
        self.settings = deepcopy(DEFAULT_CONFIG)
        self.settings["provider_ltm_settings"]["active_reply"]["enable"] = False
        self.settings["provider_ltm_settings"]["group_icl_enable"] = True
        self.settings["platform_settings"]["unique_session"] = False
        self.conversation = Conversation(
            "test-platform", "test-group", "cid", history="[]", persona_id="whale"
        )
        self.manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="cid"),
            get_conversation=AsyncMock(return_value=self.conversation),
            new_conversation=AsyncMock(return_value="new-cid"),
        )
        self.context = Mock()
        self.context.registered_web_apis = []
        self.context.get_config.return_value = self.settings
        self.context.conversation_manager = self.manager
        self.plugin = WhaleReplyGate(self.context, self.config)
        self.cache = {}
        self.plugin.get_kv_data = AsyncMock(
            side_effect=lambda key, default: self.cache.get(key, default)
        )
        self.plugin.put_kv_data = AsyncMock(
            side_effect=lambda key, value: self.cache.update({key: value})
        )
        self.plugin.judge = SimpleNamespace(
            evaluate=AsyncMock(return_value=Judgement("to_bot", 0.9, 0.95, 0.1, 0.8)),
            close=AsyncMock(),
        )
        self.group_context = GroupChatContext(Mock(), self.context)

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def prepare(self, event):
        await self.plugin.collect_message(event)
        await self.group_context.handle_message(event)

    async def collect_requests(self, event):
        return [request async for request in self.plugin.gate_message(event)]

    async def test_disabled_private_outside_group_and_commands_remain_native(self):
        private = make_event(group="")
        outside = make_event(group="other")
        command = make_event(text="/new")
        command.set_extra("handlers_parsed_params", {"new": {}})
        own = make_event(sender="bot")
        for event in [private, outside, command, own]:
            await self.plugin.collect_message(event)
            self.assertIsNone(event.get_extra("whale_gate_message"))
            self.assertEqual(await self.collect_requests(event), [])
            self.assertFalse(event.is_stopped())
        self.plugin.judge = None
        await self.plugin.collect_message(make_event())
        self.assertEqual(self.plugin.groups, {})

    async def test_enabling_without_key_still_loads_settings_and_command(self):
        self.config["enabled"] = True
        self.config["typesafe_api_key"] = ""
        await self.plugin.initialize()
        self.assertIsNone(self.plugin.judge)
        self.assertIn("请填写 API Key", self.plugin.model_status)
        self.assertEqual(self.context.register_web_api.call_count, 3)
        replies = [reply async for reply in self.plugin.helloworld(make_event())]
        self.assertEqual(len(replies), 1)

    async def test_invalid_key_is_editable_after_reload(self):
        self.config["enabled"] = True
        self.config["typesafe_api_key"] = "key with spaces"
        await self.plugin.initialize()
        self.assertIsNone(self.plugin.judge)
        self.assertIn("格式不正确", self.plugin.model_status)
        self.assertEqual(self.context.register_web_api.call_count, 3)

    async def test_log_switch_changes_records_without_changing_reply_decisions(self):
        self.plugin.logger = Mock()
        self.config["typesafe_api_key"] = "secret-key-sentinel"

        async def run(number):
            event = make_event(number, text="私密聊天正文-sentinel")
            await self.prepare(event)
            return await self.collect_requests(event)

        self.assertFalse(self.config["log_enabled"])
        self.assertEqual(len(await asyncio.create_task(run(1))), 1)
        self.plugin.logger.info.assert_not_called()

        self.config["log_enabled"] = True
        self.assertEqual(len(await asyncio.create_task(run(2))), 1)
        calls = self.plugin.logger.info.call_args_list
        self.assertEqual(
            [call.args[1] for call in calls], ["received", "judge", "request_reply"]
        )
        judgement = calls[1].args[-1]
        self.assertEqual(judgement["relation"], "to_bot")
        self.assertEqual(judgement["decision"], "REPLY")
        self.assertEqual(judgement["relation_probability"], 0.9)
        self.assertIn("elapsed_ms", judgement)
        self.assertNotIn("secret-key-sentinel", str(calls))
        self.assertNotIn("私密聊天正文-sentinel", str(calls))

        self.config["log_enabled"] = False
        self.assertEqual(len(await asyncio.create_task(run(3))), 1)
        self.assertEqual(self.plugin.logger.info.call_count, 3)

    async def test_optional_log_records_final_probability_check(self):
        self.config["log_enabled"] = True
        self.plugin.logger = Mock()
        self.plugin.optional_policy = OptionalPolicy(probability=0)
        self.plugin.judge.evaluate.return_value = Judgement(
            "open_group", 0.9, 0.8, 0.9, 0.8
        )
        event = make_event()
        await self.prepare(event)
        self.assertEqual(await self.collect_requests(event), [])
        check = self.plugin.logger.info.call_args.args
        self.assertEqual(check[1], "optional_check")
        self.assertFalse(check[-1]["allowed"])
        self.assertEqual(check[-1]["probability"], 0)

    async def test_errors_still_log_when_process_log_is_disabled(self):
        self.plugin.logger = Mock()
        self.plugin.judge.evaluate.side_effect = TypeSafeError("secret-key-sentinel")
        event = make_event()
        await self.prepare(event)
        with patch("astrbot_plugin_whale_reply_gate.main.logger.warning") as warning:
            self.assertEqual(await self.collect_requests(event), [])
        warning.assert_called_once()
        self.assertNotIn("secret-key-sentinel", str(warning.call_args))
        self.plugin.logger.info.assert_not_called()

    async def test_panel_save_applies_all_runtime_rules_without_reload(self):
        self.config.update(
            test_group_ids=[12345],
            bot_names=["鲸鱼"],
            dialogue_ttl_seconds=180,
            optional_probability=0.2,
            optional_cooldown_seconds=60,
            optional_max_replies=3,
            thresholds={
                "relation": 0.8,
                "response_needed": 0.75,
                "open_group": 0.85,
                "interjection_welcome": 0.9,
            },
        )
        await self.plugin.apply_settings()
        self.assertEqual(self.plugin.test_group_ids, {"12345"})
        self.assertTrue(
            self.plugin._explicit_call(
                self.plugin._message(make_event(text="鲸鱼你好")), "bot"
            )
        )
        self.assertEqual(self.plugin.thresholds.relation, 0.8)
        self.assertEqual(self.plugin.thresholds.response_needed, 0.75)
        self.assertEqual(self.plugin.thresholds.open_group, 0.85)
        self.assertEqual(self.plugin.thresholds.interjection_welcome, 0.9)
        self.assertEqual(self.plugin.optional_policy.probability, 0.2)
        self.assertEqual(self.plugin.optional_policy.cooldown_seconds, 60)
        self.assertEqual(self.plugin.optional_policy.max_replies, 3)

    async def test_reload_restores_only_cached_models_without_fetching(self):
        self.config["model"] = "jev-1.14.0"
        models = [
            {"id": "jev-latest", "description": "当前版本"},
            {"id": "jev-preview", "description": "预览版本"},
        ]
        self.cache["model_options"] = {
            "api_base": self.config["api_base"],
            "models": models,
        }
        with patch(
            "astrbot_plugin_whale_reply_gate.settings.list_models", AsyncMock()
        ) as fetch:
            await self.plugin.initialize()
        fetch.assert_not_awaited()
        self.assertNotIn("fetch_models", self.config.schema)
        self.assertEqual(
            self.config.schema["model"]["options"], ["jev-latest", "jev-preview"]
        )

    async def test_explicit_mentions_name_and_quote_follow_native_route(self):
        cases = [
            [At(qq="bot"), Plain("你好")],
            [Plain("小鲸鱼你好")],
            [Plain("小鲸鱼，推荐两个方案")],
            [Reply(id="old", sender_id="bot"), Plain("第二个呢？")],
        ]

        async def run(parts, number):
            event = make_event(number, parts=parts)
            await self.prepare(event)
            self.assertEqual(await self.collect_requests(event), [])
            self.assertTrue(event.is_at_or_wake_command)
            self.assertEqual(event.get_extra("whale_gate_source"), "explicit")

        for index, parts in enumerate(cases):
            await asyncio.create_task(run(parts, index))
        self.plugin.judge.evaluate.assert_not_awaited()

    async def test_third_person_and_addressing_others_go_to_judge(self):
        for parts in [
            [Plain("小鲸鱼刚才说第二个更好")],
            [At(qq="C"), Plain("第二个贵不贵？")],
        ]:
            event = make_event(parts=parts)
            event.is_at_or_wake_command = True
            await self.plugin.collect_message(event)
            self.assertFalse(event.is_at_or_wake_command)

    async def test_broadcast_acknowledgement_can_reply_without_optional_probability(
        self,
    ):
        event = make_event(
            text="回我一句收到", parts=[At(qq="all"), Plain("回我一句收到")]
        )
        self.plugin.optional_policy = OptionalPolicy(probability=0)
        self.plugin.judge.evaluate.return_value = Judgement(
            "to_bot", 0.99, 0.99, 0.04, 0.99
        )
        group = self.plugin._group(event)
        group.budget.allow = Mock(return_value=False)
        await self.prepare(event)
        self.assertFalse(event.get_extra("whale_gate_explicit"))
        self.assertEqual(len(await self.collect_requests(event)), 1)
        state = self.plugin.judge.evaluate.await_args.args[0]
        self.assertEqual(state["current_message"]["mentions"], ["all"])
        self.assertEqual(state["current_message"]["text"], "回我一句收到")
        self.assertEqual(event.get_extra("whale_gate_source"), "follow_up")
        group.budget.allow.assert_not_called()

    async def test_broadcast_notice_without_reply_request_can_stay_silent(self):
        event = make_event(
            text="明天上午十点开会", parts=[At(qq="all"), Plain("明天上午十点开会")]
        )
        self.plugin.judge.evaluate.return_value = Judgement(
            "open_group", 0.99, 0.04, 0.04, 0.99
        )
        await self.prepare(event)
        self.assertEqual(await self.collect_requests(event), [])
        self.plugin.judge.evaluate.assert_awaited_once()

    async def test_bare_mention_joins_queue_without_builtin_waiter(self):
        empty_handler = SimpleNamespace(
            handler_module_path="astrbot.builtin_stars.astrbot.main",
            handler_name="handle_empty_mention",
        )
        context_handler = SimpleNamespace(
            handler_module_path="astrbot.builtin_stars.astrbot.main",
            handler_name="on_message",
        )
        handlers = [empty_handler, context_handler]
        event = make_event(text="", parts=[At(qq="bot")])
        event.set_extra("activated_handlers", handlers)
        await self.prepare(event)
        self.assertEqual(handlers, [context_handler])
        self.assertIs(event.get_extra("enable_streaming"), False)
        self.assertEqual(await self.collect_requests(event), [])
        self.assertEqual(event.get_extra("whale_gate_source"), "explicit")
        self.assertTrue(event.message_str)
        self.assertIsNotNone(self.plugin._group(event)._active_task)

    async def test_request_preserves_conversation_and_uses_native_group_context(self):
        old = make_event(1, text="小鲸鱼推荐两个方案", sender="A")
        current = make_event(2, text="第二个贵不贵？", sender="B")
        await self.group_context.handle_message(old)
        await self.prepare(current)
        requests = await self.collect_requests(current)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertIs(request.conversation, self.conversation)
        self.assertEqual(request.prompt, "第二个贵不贵？")
        self.assertEqual(request.contexts, [])
        self.assertEqual(request.extra_user_content_parts, [])
        await self.group_context.on_req_llm(current, request)
        injected = request.extra_user_content_parts[0].text
        self.assertIn("A/", injected)
        self.assertIn("小鲸鱼推荐两个方案", injected)
        self.assertEqual(current.get_sender_id(), "B")
        self.assertTrue(current.is_stopped())

    async def test_presence_renews_at_request_hook_not_at_decision(self):
        event = make_event()
        await self.prepare(event)
        generator = self.plugin.gate_message(event)
        request = await anext(generator)
        group = self.plugin._group(event)
        self.assertIsNone(group.last_accepted_at)
        with patch("astrbot_plugin_whale_reply_gate.main.monotonic", return_value=42):
            await self.plugin.reply_accepted(event, request)
        self.assertEqual(group.last_accepted_at, 42)
        with patch("astrbot_plugin_whale_reply_gate.main.monotonic", return_value=60):
            await self.plugin.reply_accepted(event, request)
        self.assertEqual(group.last_accepted_at, 42)
        await generator.aclose()

    async def test_skip_and_api_failure_create_no_reply_or_presence(self):
        async def run(number):
            event = make_event(number)
            await self.prepare(event)
            self.assertEqual(await self.collect_requests(event), [])
            self.assertTrue(event.is_stopped())
            self.assertIsNone(self.plugin._group(event).last_accepted_at)

        self.plugin.judge.evaluate.return_value = Judgement(
            "others", 0.99, 0.99, 0.99, 0.99
        )
        await asyncio.create_task(run(1))
        self.plugin.judge.evaluate.side_effect = TypeSafeError("test failure")
        await asyncio.create_task(run(2))

    async def test_optional_probability_zero_does_not_block_follow_up(self):
        self.plugin.optional_policy = OptionalPolicy(probability=0)

        async def run(number):
            event = make_event(number)
            await self.prepare(event)
            return await self.collect_requests(event)

        self.plugin.judge.evaluate.return_value = Judgement(
            "open_group", 0.9, 0.8, 0.9, 0.8
        )
        self.assertEqual(await asyncio.create_task(run(1)), [])
        self.plugin.judge.evaluate.return_value = Judgement(
            "to_bot", 0.9, 0.8, 0.1, 0.8
        )
        self.assertEqual(len(await asyncio.create_task(run(2))), 1)

    async def test_optional_send_counts_once_and_does_not_renew_presence(self):
        event = make_event()
        await self.prepare(event)
        self.plugin.optional_policy = OptionalPolicy(probability=1)
        self.plugin.judge.evaluate.return_value = Judgement(
            "open_group", 0.9, 0.8, 0.9, 0.8
        )
        request = (await self.collect_requests(event))[0]
        await self.plugin.reply_accepted(event, request)
        group = self.plugin._group(event)
        self.assertIsNone(group.last_accepted_at)
        event.continue_event()
        event.set_result(
            event.plain_result("可以试试第二个").set_result_content_type(
                ResultContentType.LLM_RESULT
            )
        )
        await self.plugin.reply_sent(event)
        self.assertTrue(group.budget.allow(50, self.plugin.optional_policy))
        event._has_send_oper = True
        with patch("astrbot_plugin_whale_reply_gate.main.monotonic", return_value=50):
            await self.plugin.reply_sent(event)
            await self.plugin.reply_sent(event)
        self.assertFalse(group.budget.allow(51, self.plugin.optional_policy))
        self.assertEqual(len([m for m in group.recent if m.is_bot]), 1)
        self.assertIsNone(group.last_accepted_at)

        event.set_result(
            event.plain_result("第二个的价格是两千").set_result_content_type(
                ResultContentType.LLM_RESULT
            )
        )
        with patch("astrbot_plugin_whale_reply_gate.main.monotonic", return_value=51):
            await self.plugin.reply_sent(event)
        self.assertEqual(
            [m.text for m in group.recent if m.is_bot],
            ["可以试试第二个", "第二个的价格是两千"],
        )
        self.assertTrue(group.budget.allow(141, self.plugin.optional_policy))

    async def test_tool_notification_does_not_count_as_successful_reply(self):
        event = make_event()
        await self.prepare(event)
        event.set_extra("whale_gate_source", "ambient")
        event._has_send_oper = True  # 工具通知已经发送，正式回复还没有发送。
        event.set_result(
            event.plain_result("正式回答").set_result_content_type(
                ResultContentType.LLM_RESULT
            )
        )
        await self.plugin.before_reply_sent(event)
        self.assertFalse(event._has_send_oper)
        await self.plugin.reply_sent(event)  # 模拟正式发送失败后的框架钩子。
        group = self.plugin._group(event)
        self.assertEqual([m for m in group.recent if m.is_bot], [])
        self.assertTrue(group.budget.allow(50, OptionalPolicy(probability=1)))
        self.assertTrue(event._has_send_oper)  # 恢复框架已有的工具发送记录。

    async def test_message_tool_reply_counts_even_when_final_send_is_skipped(self):
        event = make_event()
        await self.prepare(event)
        event.set_extra("whale_gate_source", "ambient")
        event.set_extra(
            "_send_message_to_user_current_session_plain_texts", ["两千以内选第一个"]
        )
        event._has_send_oper = True
        with patch("astrbot_plugin_whale_reply_gate.main.monotonic", return_value=50):
            await self.plugin.tool_reply_sent(event, Mock(), {}, None)
            await self.plugin.tool_reply_sent(event, Mock(), {}, None)
        group = self.plugin._group(event)
        self.assertEqual(
            [m.text for m in group.recent if m.is_bot], ["两千以内选第一个"]
        )
        self.assertFalse(group.budget.allow(51, OptionalPolicy(probability=1)))
        event.set_result(
            event.plain_result("两千以内选第一个").set_result_content_type(
                ResultContentType.LLM_RESULT
            )
        )
        await self.plugin.before_reply_sent(event)
        self.assertTrue(event._has_send_oper)

    async def test_busy_group_merges_messages_and_retains_latest_sender(self):
        started, finish = asyncio.Event(), asyncio.Event()
        requests = []

        async def run(event):
            await self.prepare(event)
            async for request in self.plugin.gate_message(event):
                requests.append((event, request))
                if event.message_obj.message_id == "1":
                    started.set()
                    await finish.wait()

        first = asyncio.create_task(run(make_event(1)))
        await started.wait()
        second = asyncio.create_task(run(make_event(2, "多少钱", "B")))
        third = asyncio.create_task(run(make_event(3, "预算两千", "C")))
        await asyncio.sleep(0)
        self.assertEqual(len(requests), 1)
        finish.set()
        await asyncio.gather(first, second, third)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[-1][0].get_sender_id(), "C")
        state = (
            self.plugin.judge.evaluate.await_args.kwargs
            or self.plugin.judge.evaluate.await_args.args[0]
        )
        self.assertEqual([m["sender"] for m in state["current_messages"]], ["B", "C"])
        await self.group_context.on_req_llm(requests[-1][0], requests[-1][1])
        self.assertIn("B/", requests[-1][1].extra_user_content_parts[0].text)

    async def test_configuration_conflicts_are_reported_before_builtin_reply(self):
        self.settings["provider_ltm_settings"]["active_reply"]["enable"] = True
        with self.assertRaisesRegex(ValueError, "关闭内置主动回复"):
            await self.plugin.collect_message(make_event())
        self.settings["provider_ltm_settings"]["active_reply"]["enable"] = False
        self.settings["platform_settings"]["unique_session"] = True
        with self.assertRaisesRegex(ValueError, "按成员隔离"):
            await self.plugin.collect_message(make_event())

    async def test_actual_process_stage_generates_once_for_each_admitted_message(self):
        """使用真实 ProcessStage，检查插件请求与默认回复不会重复执行。"""
        calls = []

        async def generate(event):
            request = event.get_extra("provider_request") or ProviderRequest(
                prompt=event.message_str, conversation=self.conversation
            )
            calls.append(event.message_obj.message_id)
            await self.group_context.on_req_llm(event, request)
            await self.plugin.reply_accepted(event, request)
            event.set_result(
                event.plain_result("测试回复").set_result_content_type(
                    ResultContentType.LLM_RESULT
                )
            )
            yield

        stage = ProcessStage()
        stage.ctx = SimpleNamespace(astrbot_config=self.settings)
        stage.star_request_sub_stage = SimpleNamespace(process=self.plugin.gate_message)
        stage.agent_sub_stage = SimpleNamespace(process=generate)

        async def process(event):
            await self.prepare(event)
            event.set_extra("activated_handlers", ["gate"])
            async for _ in stage.process(event):
                event._has_send_oper = True
                await self.plugin.reply_sent(event)
                event.clear_result()

        await asyncio.create_task(process(make_event(1)))
        await asyncio.create_task(process(make_event(2, "小鲸鱼你好")))
        self.plugin.judge.evaluate.return_value = Judgement(
            "others", 0.99, 0.99, 0.99, 0.99
        )
        await asyncio.create_task(process(make_event(3)))
        self.assertEqual(calls, ["1", "2"])


if __name__ == "__main__":
    unittest.main()
