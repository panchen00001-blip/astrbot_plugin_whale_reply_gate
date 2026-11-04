"""把 Jev 门控接入 AstrBot 的原生回复流程。"""

import re
from sys import maxsize
from time import monotonic

from mcp.types import CallToolResult
from typesafe_sdk import TypeSafeError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.tool import FunctionTool

from .decision import Decision, OptionalPolicy, Thresholds, decide
from .judge import JevJudge
from .presence import ChatMessage, GroupDialogue
from .settings import GateSettings


class WhaleReplyGate(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.groups: dict[tuple[str, str], GroupDialogue] = {}
        self.judge: JevJudge | None = None
        self.model_status = "模型设置尚未应用。"
        self.settings = GateSettings(self)
        self._configure_rules()

    def _configure_rules(self) -> None:
        """两个配置入口保存后，都用同一份配置更新运行参数。"""
        config = self.config
        self.test_group_ids = {str(group_id) for group_id in config["test_group_ids"]}
        self.thresholds = Thresholds(**config["thresholds"])
        self.optional_policy = OptionalPolicy(
            probability=config["optional_probability"],
            cooldown_seconds=config["optional_cooldown_seconds"],
            max_replies=config["optional_max_replies"],
        )
        aliases = "|".join(re.escape(name) for name in config["bot_names"])
        self.name_call = re.compile(
            rf"^(?:{aliases})(?:$|[\s，,：:！!？?、]|你好|在吗|帮|请|你|能|快|给)"
        )

    async def initialize(self) -> None:
        self.settings.register_routes()
        await self.apply_settings()

    async def apply_settings(self) -> None:
        self._configure_rules()
        await self.configure_judge()
        await self.settings.load_model_options()
        if self.config["log_enabled"]:
            self.logger.info("[小鲸鱼门控] 过程日志已开启，可在 AstrBot 日志页面查看。")

    async def configure_judge(self) -> None:
        """缺少配置是待填写状态；插件和设置页继续正常加载。"""
        if self.judge is not None:
            await self.judge.close()
            self.judge = None
        if not self.config["enabled"]:
            self.model_status = "门控未启用。"
            return
        if not self.config["typesafe_api_key"].strip():
            self.model_status = "请填写 API Key 后保存，门控会自动应用设置。"
            return
        if not self.config["model"].strip():
            self.model_status = "请选择或填写一个模型 ID 后保存。"
            return
        try:
            self.judge = JevJudge(
                self.config["typesafe_api_key"],
                self.config["model"],
                self.config["api_base"],
            )
        except (TypeSafeError, ValueError):
            self.model_status = "API Key 或接口网址格式不正确，请修改后保存。"
            return
        self.model_status = "模型已配置，等待群消息；真实判断效果待联调。"

    @filter.command("helloworld")
    async def helloworld(self, event: AstrMessageEvent):
        """检查插件是否已加载，不调用模型。"""
        yield event.plain_result(f"Hello, {event.get_sender_name()}!")

    def _log_event(
        self,
        event: AstrMessageEvent,
        action: str,
        **details: str | int | float | bool | None,
    ) -> None:
        """开关只控制门控过程日志，记录定位信息和判断数据。"""
        if not self.config["log_enabled"]:
            return
        self.logger.info(
            "[小鲸鱼门控] action=%s bot=%s group=%s sender=%s message=%s details=%s",
            action,
            event.get_self_id(),
            event.get_group_id(),
            event.get_sender_id(),
            event.message_obj.message_id,
            details,
        )

    def _handles(self, event: AstrMessageEvent) -> bool:
        return (
            self.judge is not None
            and event.get_platform_name() == "aiocqhttp"
            and bool(event.get_group_id())
            and str(event.get_group_id()) in self.test_group_ids
            and event.get_sender_id() != event.get_self_id()
        )

    def _group(self, event: AstrMessageEvent) -> GroupDialogue:
        key = (event.get_self_id(), event.get_group_id())
        if key not in self.groups:
            self.groups[key] = GroupDialogue()
        return self.groups[key]

    def _message(self, event: AstrMessageEvent) -> ChatMessage:
        parts = event.get_messages()
        quote = next((part for part in parts if isinstance(part, Reply)), None)
        return ChatMessage(
            id=str(event.message_obj.message_id),
            sender=event.get_sender_id(),
            name=event.get_sender_name(),
            text="".join(part.text for part in parts if isinstance(part, Plain)),
            received_at=monotonic(),
            mentions=[str(part.qq) for part in parts if isinstance(part, At)],
            reply_to_id=str(quote.id) if quote else None,
            reply_to_sender=str(quote.sender_id) if quote else None,
            quoted_text=(quote.message_str or "")[:200] if quote else "",
        )

    def _explicit_call(self, message: ChatMessage, bot_id: str) -> bool:
        if bot_id in message.mentions:
            return True
        if message.reply_to_sender == bot_id and message.text.strip():
            return True
        if message.mentions or message.reply_to_sender:
            return False
        return self.name_call.match(message.text.strip()) is not None

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=maxsize)
    async def collect_message(self, event: AstrMessageEvent) -> None:
        """先确认测试群配置，再让 AstrBot 自己记录群聊上下文。"""
        if not self._handles(event) or event.get_extra("handlers_parsed_params"):
            return

        settings = self.context.get_config(umo=event.unified_msg_origin)
        group_context = settings["provider_ltm_settings"]
        if group_context["active_reply"]["enable"]:
            raise ValueError("请在本群 AstrBot 配置中关闭内置主动回复，再启用门控。")
        if (
            not group_context["group_icl_enable"]
            or settings["platform_settings"]["unique_session"]
        ):
            raise ValueError("请开启群聊上下文，并关闭群内按成员隔离会话，再启用门控。")

        message = self._message(event)
        explicit = self._explicit_call(message, event.get_self_id())
        self._group(event).record(message)
        event.set_extra("whale_gate_message", message)
        event.set_extra("whale_gate_explicit", explicit)
        event.set_extra("enable_streaming", False)
        # 空 @ 和只叫名字也进入本群队列，不启动内置的 60 秒等待器。
        handlers = event.get_extra("activated_handlers", [])
        handlers[:] = [
            handler
            for handler in handlers
            if not (
                handler.handler_module_path == "astrbot.builtin_stars.astrbot.main"
                and handler.handler_name == "handle_empty_mention"
            )
        ]
        # 第三人称提及名字、@全体成员等交给 Jev，不触发原来的自动回复。
        event.is_at_or_wake_command = explicit
        self._log_event(event, "received", explicit=explicit)

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE, priority=maxsize - 3
    )
    async def gate_message(self, event: AstrMessageEvent):
        """原生群上下文入库后排队；只为 Jev 放行的消息创建新请求。"""
        message = event.get_extra("whale_gate_message")
        if message is None:
            return

        group = self._group(event)
        batch = await group.take_turn(message, event.get_extra("whale_gate_explicit"))
        if batch is None:
            self._log_event(event, "merged", reason="由同批次的另一条消息承接")
            event.stop_event()
            return
        if batch.explicit:
            event.set_extra("whale_gate_source", "explicit")
            event.is_at_or_wake_command = True
            if not event.message_str.strip():
                event.message_str = "用户叫了你，请自然地回应。"
            self._log_event(event, "explicit_reply", batch_size=len(batch.messages))
            return

        state = group.state(batch, monotonic(), self.config["dialogue_ttl_seconds"])
        state["bot"] = {
            "id": event.get_self_id(),
            "name": self.config["bot_names"][0],
            "aliases": self.config["bot_names"],
        }
        started_at = monotonic()
        try:
            judgement = await self.judge.evaluate(state)
        except TypeSafeError as error:
            # 服务错误留在管理日志里，本条群消息保持安静。
            logger.warning("[小鲸鱼门控] Jev 请求失败：%s", type(error).__name__)
            self._log_event(event, "judge_error", error=type(error).__name__)
            event.stop_event()
            return

        decision = decide(judgement, self.thresholds)
        self._log_event(
            event,
            "judge",
            relation=judgement.relation,
            relation_probability=judgement.relation_probability,
            response_needed=judgement.response_needed,
            interjection_welcome=judgement.interjection_welcome,
            confidence=judgement.confidence,
            decision=decision.value,
            group_active=state["group_dialogue"]["active"],
            context_count=len(state["recent_messages"]),
            batch_size=len(batch.messages),
            elapsed_ms=round((monotonic() - started_at) * 1000),
        )
        if decision == Decision.SKIP:
            event.stop_event()
            return
        if decision == Decision.OPTIONAL:
            allowed = group.budget.allow(monotonic(), self.optional_policy)
            self._log_event(
                event,
                "optional_check",
                allowed=allowed,
                probability=self.optional_policy.probability,
                cooldown_seconds=self.optional_policy.cooldown_seconds,
                max_replies=self.optional_policy.max_replies,
            )
            if not allowed:
                event.stop_event()
                return
            source = "ambient"
        else:
            source = "follow_up"

        event.set_extra("whale_gate_source", source)
        self._log_event(event, "request_reply", source=source)
        yield await self._reply_request(event)
        # 本条已经走过正式回复，结束事件，避免其他消息处理器再次生成。
        event.stop_event()

    async def _reply_request(self, event: AstrMessageEvent) -> ProviderRequest:
        """沿用当前会话的人格与历史，群上下文由 AstrBot 注入。"""
        manager = self.context.conversation_manager
        origin = event.unified_msg_origin
        cid = await manager.get_curr_conversation_id(origin)
        if cid is None:
            cid = await manager.new_conversation(origin, event.get_platform_id())
        conversation = await manager.get_conversation(origin, cid)
        images = [
            await part.convert_to_file_path()
            for part in event.get_messages()
            if isinstance(part, Image)
        ]
        return event.request_llm(
            prompt=event.message_str,
            image_urls=images,
            conversation=conversation,
        )

    @filter.on_llm_request(priority=-maxsize)
    async def reply_accepted(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """框架已构建正式请求并执行请求钩子，此时续期一次。"""
        source = event.get_extra("whale_gate_source")
        if source not in {"explicit", "follow_up"}:
            return
        if not event.get_extra("whale_gate_accepted", False):
            self._group(event).accept_reply(monotonic())
            event.set_extra("whale_gate_accepted", True)
            self._log_event(
                event,
                "reply_accepted",
                source=source,
                dialogue_ttl_seconds=self.config["dialogue_ttl_seconds"],
            )

    @filter.on_decorating_result(priority=-maxsize)
    async def before_reply_sent(self, event: AstrMessageEvent) -> None:
        """只统计本次正式回复的发送结果，不借用此前工具通知的成功标记。"""
        result = event.get_result()
        if event.get_extra("whale_gate_source") and result and result.is_llm_result():
            sent_by_tool = event.get_extra(
                "_send_message_to_user_current_session_plain_texts", []
            )
            if result.get_plain_text().strip() in sent_by_tool:
                return
            event.set_extra("whale_gate_prior_send", event._has_send_oper)
            event._has_send_oper = False

    @filter.after_message_sent()
    async def reply_sent(self, event: AstrMessageEvent) -> None:
        """成功发送后记录小鲸鱼的话；主动插话按一次完整回复计数。"""
        if not event.get_extra("whale_gate_source"):
            return
        result = event.get_result()
        if result is None or not result.is_llm_result():
            return
        sent = event._has_send_oper
        event._has_send_oper = sent or event.get_extra("whale_gate_prior_send", False)
        if not sent:
            self._log_event(event, "reply_not_sent")
            return
        self._record_sent_reply(event, result.get_plain_text())

    @filter.on_llm_tool_respond()
    async def tool_reply_sent(
        self,
        event: AstrMessageEvent,
        tool: FunctionTool,
        tool_args: dict | None,
        tool_result: CallToolResult | None,
    ) -> None:
        """发送工具已向本群投递的正文，也属于成功回复。"""
        if not event.get_extra("whale_gate_source"):
            return
        texts = event.get_extra("_send_message_to_user_current_session_plain_texts", [])
        seen = event.get_extra("whale_gate_tool_texts_seen", 0)
        for text in texts[seen:]:
            self._record_sent_reply(event, text)
        event.set_extra("whale_gate_tool_texts_seen", len(texts))

    def _record_sent_reply(self, event: AstrMessageEvent, text: str) -> None:
        """保存已送达正文；同一轮主动回复只占一次额度。"""
        if not text or text == event.get_extra("whale_gate_last_sent_text"):
            return

        group = self._group(event)
        now = monotonic()
        group.record(
            ChatMessage(
                id=f"bot:{event.message_obj.message_id}:{now}",
                sender=event.get_self_id(),
                name=self.config["bot_names"][0],
                text=text,
                received_at=now,
                is_bot=True,
            )
        )
        if event.get_extra("whale_gate_source") == "ambient" and not event.get_extra(
            "whale_gate_sent", False
        ):
            group.budget.record_sent(now)
        event.set_extra("whale_gate_last_sent_text", text)
        event.set_extra("whale_gate_sent", True)
        self._log_event(
            event,
            "reply_sent",
            source=event.get_extra("whale_gate_source"),
            characters=len(text),
        )

    async def terminate(self) -> None:
        self.settings.unregister_routes()
        for group in self.groups.values():
            group.close()
        if self.judge is not None:
            await self.judge.close()
