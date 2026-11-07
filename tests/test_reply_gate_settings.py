"""验证两个设置入口的字段一致性、配置持久化和模型选项共享。"""

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from typesafe_sdk import TypeSafeError

from astrbot.api import AstrBotConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_whale_reply_gate import settings as settings_module

PLUGIN_DIR = Path(__file__).resolve().parents[1]


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text())
        self.config = AstrBotConfig(str(self.directory / "config.json"), schema=schema)
        self.config["typesafe_api_key"] = "saved-test-key"
        self.config["enabled"] = True
        self.cache = {}
        self.plugin = SimpleNamespace(
            config=self.config,
            judge=object(),
            model_status="模型已配置",
            apply_settings=AsyncMock(),
            put_kv_data=AsyncMock(
                side_effect=lambda key, value: self.cache.update({key: value})
            ),
            get_kv_data=AsyncMock(
                side_effect=lambda key, default: self.cache.get(key, default)
            ),
            context=SimpleNamespace(register_web_api=Mock(), registered_web_apis=[]),
        )
        self.api = settings_module.GateSettings(self.plugin)
        self.request = SimpleNamespace(json=AsyncMock())
        self.enterContext(patch.object(settings_module, "request", self.request))

    def payload(self, **changes):
        values = {
            key: deepcopy(value)
            for key, value in self.config.items()
            if key != "typesafe_api_key"
        }
        return {**values, "api_key": "", **changes}

    async def test_unload_removes_only_this_instances_routes(self):
        other = ("/other", Mock(), ["GET"], "other")
        routes = self.plugin.context.registered_web_apis
        routes.extend(
            [other, ("/settings", self.api.get_settings, ["GET"], "settings")]
        )
        self.api.unregister_routes()
        self.assertEqual(routes, [other])

    async def test_panel_contains_every_native_setting_without_exposing_key(self):
        response = await self.api.get_settings()
        data = response["data"]
        self.assertTrue(data["has_api_key"])
        self.assertNotIn("saved-test-key", str(response))
        for key in self.config.schema:
            self.assertIn(key, data["schema"])
            if key != "typesafe_api_key":
                self.assertEqual(data[key], self.config[key])
        self.assertNotIn("typesafe_api_key", data)
        self.assertTrue(data["schema"]["typesafe_api_key"]["secret"])

    async def test_get_models_uses_draft_connection_without_saving_settings(self):
        before = deepcopy(dict(self.config))
        self.request.json.return_value = {
            "api_base": "https://gateway.example/v1",
            "api_key": "draft-key",
        }
        models = [{"id": "jev-latest", "description": "最新版"}]
        with patch.object(
            settings_module, "list_models", AsyncMock(return_value=models)
        ) as fetch:
            response = await self.api.fetch_models()
        fetch.assert_awaited_once_with("draft-key", "https://gateway.example/v1")
        self.assertEqual(response["data"]["models"], models)
        self.assertEqual(dict(self.config), before)
        self.assertEqual(self.cache["model_options"]["models"], models)

    async def test_get_models_can_reuse_saved_key(self):
        self.request.json.return_value = {
            "api_base": "https://api.typesafe.ai",
            "api_key": "",
        }
        with patch.object(
            settings_module, "list_models", AsyncMock(return_value=[])
        ) as fetch:
            response = await self.api.fetch_models()
        fetch.assert_awaited_once_with("saved-test-key", "https://api.typesafe.ai")
        self.assertEqual(response["data"]["models"], [])

    async def test_native_dropdown_and_panel_share_fetched_models_after_reload(self):
        models = [{"id": "jev-listed", "description": "可见模型"}]
        with patch.object(
            settings_module, "list_models", AsyncMock(return_value=models)
        ):
            self.request.json.return_value = {
                "api_base": self.config["api_base"],
                "api_key": "",
            }
            await self.api.fetch_models()
        self.assertEqual(self.config.schema["model"]["options"], ["jev-listed"])
        reloaded = settings_module.GateSettings(self.plugin)
        await reloaded.load_model_options()
        self.assertEqual(reloaded.settings()["models"], models)

    async def test_changing_url_clears_old_model_choices_but_keeps_selected_id(self):
        self.cache["model_options"] = {
            "api_base": "https://old.example",
            "models": [{"id": "old-model"}],
        }
        await self.api.load_model_options()
        self.assertEqual(self.api.models, [])
        self.assertEqual(self.config.schema["model"]["options"], [])

    async def test_missing_key_returns_actionable_message(self):
        self.config["typesafe_api_key"] = ""
        self.request.json.return_value = {
            "api_base": "https://api.typesafe.ai",
            "api_key": "",
        }
        response = await self.api.fetch_models()
        self.assertEqual(response["status"], "error")
        self.assertIn("请先填写 API Key", response["message"])

    async def test_button_failure_is_visible_without_exposing_key(self):
        self.request.json.return_value = {
            "api_base": self.config["api_base"],
            "api_key": "",
        }
        with patch.object(
            settings_module,
            "list_models",
            AsyncMock(side_effect=TypeSafeError("secret response")),
        ):
            response = await self.api.fetch_models()
        self.assertEqual(response["status"], "error")
        self.assertIn("模型接口配置", response["message"])
        self.assertNotIn("secret response", str(response))
        self.assertNotIn("saved-test-key", str(response))
        self.assertEqual((await self.api.get_settings())["status"], "ok")

    async def test_empty_model_response_does_not_add_current_selection(self):
        self.request.json.return_value = {
            "api_base": self.config["api_base"],
            "api_key": "",
        }
        with patch.object(settings_module, "list_models", AsyncMock(return_value=[])):
            response = await self.api.fetch_models()
        self.assertEqual(response["data"]["models"], [])
        self.assertEqual(self.config.schema["model"]["options"], [])

    async def test_save_all_fields_persists_values_readable_from_native_settings(self):
        body = self.payload(
            api_base="https://gateway.example/v1/",
            api_key="new-key",
            model="gateway/jev",
            enabled=False,
            log_enabled=True,
            test_group_ids=["10001", "10002"],
            bot_names=["小鲸鱼", "鲸鱼"],
            batch_wait_seconds=0.5,
            dialogue_ttl_seconds=180,
            optional_probability=0.2,
            casual_probability=0.1,
            optional_cooldown_seconds=60,
            optional_max_replies=3,
            thresholds={
                "relation": 0.8,
                "response_needed": 0.75,
                "open_group": 0.85,
                "interjection_welcome": 0.9,
            },
        )
        self.request.json.return_value = body
        response = await self.api.save_settings()
        self.assertEqual(response["status"], "ok")
        self.plugin.apply_settings.assert_awaited_once_with()
        reloaded = AstrBotConfig(self.config.config_path, schema=self.config.schema)
        for key in self.config.schema:
            expected = "new-key" if key == "typesafe_api_key" else body[key]
            if key == "api_base":
                expected = "https://gateway.example/v1"
            self.assertEqual(reloaded[key], expected, key)
        self.assertNotIn("new-key", str(response))

    async def test_saving_empty_key_keeps_saved_key(self):
        self.request.json.return_value = self.payload(model="jev-latest")
        await self.api.save_settings()
        self.assertEqual(self.config["typesafe_api_key"], "saved-test-key")

    async def test_invalid_form_does_not_change_stored_configuration(self):
        before = deepcopy(dict(self.config))
        for change in [
            {"model": ""},
            {"bot_names": []},
            {"optional_max_replies": "abc"},
        ]:
            self.request.json.return_value = self.payload(
                api_base="https://gateway.example", **change
            )
            response = await self.api.save_settings()
            self.assertEqual(response["status"], "error")
            self.assertEqual(dict(self.config), before)
        self.plugin.apply_settings.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
