"""完整设置与模型发现；原生设置和控制面板共用配置及模型列表。"""

from typing import TYPE_CHECKING

from openai import APIConnectionError, APIError, APIStatusError
from typesafe_sdk import TypeSafeAPIConnectionError, TypeSafeAPIError, TypeSafeError

from astrbot.api import logger
from astrbot.api.web import request
from astrbot.dashboard.services.config_service import validate_config

from .judge import list_models, normalize_api_base

if TYPE_CHECKING:
    from .main import WhaleReplyGate


API_PREFIX = "/astrbot_plugin_whale_reply_gate"


def model_error_message(error: APIError | TypeSafeError) -> str:
    """只返回用户能处理的错误，不回显请求内容或密钥。"""
    if isinstance(error, (APIConnectionError, TypeSafeAPIConnectionError)):
        return "连接模型接口失败，请检查网址和服务器网络。"
    if isinstance(error, (APIStatusError, TypeSafeAPIError)):
        status = (
            error.status_code if isinstance(error, APIStatusError) else error.status
        )
        if status in {401, 403}:
            return "API Key 无效或没有访问权限，请检查后重试。"
        if status == 404:
            return "没有找到 /models 接口，请检查接口网址是否包含正确的 /v1 前缀。"
        return f"模型接口返回 HTTP {status}，请稍后重试。"
    return "模型接口配置或返回格式不正确，请检查 API Key 和接口网址。"


class GateSettings:
    def __init__(self, plugin: "WhaleReplyGate") -> None:
        self.plugin = plugin
        self.models: list[dict[str, str]] = []

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        register(f"{API_PREFIX}/settings", self.get_settings, ["GET"], "读取门控设置")
        register(f"{API_PREFIX}/settings", self.save_settings, ["POST"], "保存门控设置")
        register(f"{API_PREFIX}/models", self.fetch_models, ["POST"], "获取可见模型")

    def unregister_routes(self) -> None:
        """卸载时移除本实例的接口，重载后由新实例注册。"""
        handlers = {self.get_settings, self.save_settings, self.fetch_models}
        routes = self.plugin.context.registered_web_apis
        routes[:] = [route for route in routes if route[1] not in handlers]

    def settings(self) -> dict:
        config = self.plugin.config
        return {
            **{key: config[key] for key in config.schema if key != "typesafe_api_key"},
            "schema": config.schema,
            "models": self.models,
            "has_api_key": bool(config["typesafe_api_key"].strip()),
            "ready": self.plugin.judge is not None,
            "status_message": self.plugin.model_status,
        }

    async def get_settings(self) -> dict:
        return {"status": "ok", "data": self.settings()}

    async def fetch_models(self) -> dict:
        data = await request.json()
        api_key = data["api_key"].strip() or self.plugin.config["typesafe_api_key"]
        return await self.discover_models(api_key, data["api_base"])

    async def discover_models(self, api_key: str, api_base: str) -> dict:
        if not api_key.strip():
            return {"status": "error", "message": "请先填写 API Key，再获取模型列表。"}
        try:
            api_base = normalize_api_base(api_base)
            models = await list_models(api_key, api_base)
        except ValueError as error:
            return {"status": "error", "message": str(error)}
        except (APIError, TypeSafeError) as error:
            logger.warning("[小鲸鱼门控] 获取模型列表失败：%s", type(error).__name__)
            return {"status": "error", "message": model_error_message(error)}
        await self.plugin.put_kv_data(
            "model_options", {"api_base": api_base, "models": models}
        )
        await self.load_model_options()
        return {"status": "ok", "data": {"models": models, "api_base": api_base}}

    async def load_model_options(self) -> None:
        """从 AstrBot 插件存储恢复上次获取的列表，供两个界面共同选择。"""
        config = self.plugin.config
        cache = await self.plugin.get_kv_data(
            "model_options", {"api_base": "", "models": []}
        )
        current_base = config["api_base"].strip().rstrip("/")
        self.models = cache["models"] if cache["api_base"] == current_base else []
        config.schema["model"]["options"] = [model["id"] for model in self.models]

    async def save_settings(self) -> dict:
        data = await request.json()
        try:
            api_base = normalize_api_base(data["api_base"])
        except ValueError as error:
            return {"status": "error", "message": str(error)}
        model = data["model"].strip()
        if not model:
            return {"status": "error", "message": "请选择或填写一个模型 ID。"}

        config = self.plugin.config
        values = {key: data[key] for key in config.schema if key != "typesafe_api_key"}
        values["api_base"] = api_base
        values["model"] = model
        values["typesafe_api_key"] = config["typesafe_api_key"]
        api_key = data["api_key"].strip()
        if api_key:
            values["typesafe_api_key"] = api_key
        errors, values = validate_config(values, config.schema, is_core=False)
        if errors:
            return {"status": "error", "message": "；".join(errors)}
        if not values["bot_names"]:
            return {"status": "error", "message": "请至少填写一个小鲸鱼的称呼。"}
        config.update(values)
        config.save_config()
        await self.plugin.apply_settings()
        return {
            "status": "ok",
            "message": "全部设置已保存并应用。",
            "data": self.settings(),
        }
