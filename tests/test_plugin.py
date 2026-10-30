# -*- coding: utf-8 -*-
"""离线单元测试 —— 不需要 AstrBot 环境。

用法：
    python tests/test_plugin.py

原理：测试前把 `astrbot.*` 相关模块塞进 sys.modules（stub），
再加载上一层的 main.py，这样就能在没有机器人的情况下验证逻辑。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# stub AstrBot
# --------------------------------------------------------------------------- #
def _install_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    api.logger = _Logger()

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # noqa: N801 - 与 AstrBot 同名
        pass

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

    class _Filter:
        @staticmethod
        def on_llm_request(priority=0):
            return lambda fn: fn

        @staticmethod
        def on_llm_response(priority=0):
            return lambda fn: fn

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.filter = _Filter()

    mc = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text=""):
            self.text = text

    mc.Plain = Plain

    prov = types.ModuleType("astrbot.api.provider")

    class ProviderRequest:
        pass

    class LLMResponse:
        def __init__(self, text=""):
            self.completion_text = text
            self.tools_call_args = []
            self.tools_call_name = []

    prov.ProviderRequest = ProviderRequest
    prov.LLMResponse = LLMResponse

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config

    def register(*args, **kwargs):
        return lambda cls: cls

    star_mod.Context = object
    star_mod.Star = Star
    star_mod.register = register

    astrbot.api = api
    for name, module in (
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event_mod),
        ("astrbot.api.message_components", mc),
        ("astrbot.api.provider", prov),
        ("astrbot.api.star", star_mod),
    ):
        sys.modules[name] = module


_install_stubs()


def _load_plugin():
    path = os.path.join(ROOT, "main.py")
    spec = importlib.util.spec_from_file_location("gdm_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()
GROUP_MOD = plugin


# --------------------------------------------------------------------------- #
# 假对象
# --------------------------------------------------------------------------- #
class FakeBot:
    """模拟 aiocqhttp 的 CQHttp。

    require_group=True 时只有带 group_id 才成功 —— 用来模拟「非好友，只有临时会话能发」。
    """

    def __init__(self, ok=True, require_group=True):
        self.ok = ok
        self.require_group = require_group
        self.calls = []

    async def send_private_msg(self, **params):
        self.calls.append(params)
        if not self.ok:
            raise RuntimeError("retcode=100 发送失败")
        if self.require_group and "group_id" not in params:
            raise RuntimeError("请先添加对方为好友")
        return {"status": "ok"}


class FakeContext:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def send_message(self, session, chain):
        self.calls.append((session, chain))
        return self.ok


class FakeResponse:
    def __init__(self, text, tool_calls=None):
        self.completion_text = text
        self.tools_call_args = list(tool_calls or [])
        self.tools_call_name = ["some_tool"] * len(tool_calls or [])


class FakeEvent:
    def __init__(self, group="100000", user="200000", bot=None, private=False, admin=False):
        self.group = group
        self.user = user
        self.bot = bot
        self.private = private
        self.admin = admin
        self.unified_msg_origin = f"test:GroupMessage:{group}"
        self.sent = []
        self.extras = {}
        self.blocked = None
        self.stopped = False

    def is_private_chat(self):
        return self.private

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.user

    def is_admin(self):
        return self.admin

    def should_call_llm(self, flag):
        self.blocked = flag

    def stop_event(self):
        self.stopped = True

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def plain_result(self, text):
        return ("plain", text)

    async def send(self, result):
        self.sent.append(result)


BASE_CONFIG = dict(
    enable=True,
    redirect_reply=True,
    enable_temp_session=True,
    min_redirect_chars=10,
    intercept_on_fail=True,
    blocked_notice="（私聊说吧）",
    group_notice="（私聊发你了）",
    fallback_notice="（加我好友）",
    trigger_count=3,
    window_minutes=10,
    cooldown_minutes=30,
    block_during_cooldown=True,
    message="问太多了",
    exempt_admins=True,
    whitelist=[],
    group_whitelist=[],
    dry_run=False,
)


def make_plugin(context=None, **overrides):
    cfg = dict(BASE_CONFIG)
    cfg.update(overrides)
    return plugin.GroupDmGuidePlugin(context or FakeContext(), cfg)


async def _trigger(instance, bot=None, **event_kwargs):
    """连问 trigger_count 次，返回最后一次的事件。"""
    event = None
    for _ in range(instance.trigger_count):
        event = FakeEvent(bot=bot, **event_kwargs)
        await instance.mark_redirect(event, object())
    return event


# --------------------------------------------------------------------------- #
# 测试
# --------------------------------------------------------------------------- #
class ThresholdTests(unittest.TestCase):
    def test_below_threshold_does_nothing(self):
        p = make_plugin()
        for _ in range(2):
            ev = FakeEvent()
            asyncio.run(p.mark_redirect(ev, object()))
            self.assertIsNone(ev.get_extra("_group_dm_guide_redirect"))

    def test_threshold_marks_redirect(self):
        p = make_plugin()
        ev = asyncio.run(_trigger(p))
        self.assertTrue(ev.get_extra("_group_dm_guide_redirect"))

    def test_cooldown_keeps_redirecting(self):
        p = make_plugin()
        asyncio.run(_trigger(p))
        ev = FakeEvent()
        asyncio.run(p.mark_redirect(ev, object()))
        self.assertTrue(ev.get_extra("_group_dm_guide_redirect"))


class RedirectTests(unittest.TestCase):
    def test_short_answer_stays_in_group(self):
        ctx = FakeContext()
        p = make_plugin(ctx)
        ev = asyncio.run(_trigger(p))
        resp = FakeResponse("嗯。")
        asyncio.run(p.redirect_to_private(ev, resp))
        self.assertEqual(resp.completion_text, "嗯。")
        self.assertFalse(ctx.calls)
        self.assertFalse(ev.sent)

    def test_long_answer_goes_private(self):
        ctx = FakeContext()
        p = make_plugin(ctx)
        ev = asyncio.run(_trigger(p))
        resp = FakeResponse("这是一个足够长的回答内容", tool_calls=[])
        asyncio.run(p.redirect_to_private(ev, resp))
        self.assertEqual(resp.completion_text, "")
        self.assertEqual(len(ctx.calls), 1)
        self.assertTrue(ev.sent)
        self.assertEqual(ev.sent[0][1], "（私聊发你了）")

    def test_tool_call_turn_is_skipped(self):
        ctx = FakeContext()
        p = make_plugin(ctx)
        ev = asyncio.run(_trigger(p))
        resp = FakeResponse("我查一下。", tool_calls=[{"query": "x"}])
        asyncio.run(p.redirect_to_private(ev, resp))
        self.assertEqual(resp.completion_text, "我查一下。")
        self.assertFalse(ctx.calls)
        self.assertFalse(ev.sent)


class DeliveryTests(unittest.TestCase):
    def test_temp_session_preferred(self):
        ctx, bot = FakeContext(), FakeBot(ok=True, require_group=True)
        p = make_plugin(ctx)
        ev = asyncio.run(_trigger(p, bot=bot))
        resp = FakeResponse("这是一个足够长的回答内容")
        asyncio.run(p.redirect_to_private(ev, resp))
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0]["group_id"], 100000)
        self.assertFalse(ctx.calls)

    def test_failure_intercepts_group_reply(self):
        ctx, bot = FakeContext(ok=False), FakeBot(ok=False)
        p = make_plugin(ctx)
        ev = asyncio.run(_trigger(p, bot=bot))
        resp = FakeResponse("这是一个足够长的回答内容")
        asyncio.run(p.redirect_to_private(ev, resp))
        self.assertEqual(resp.completion_text, "")
        self.assertEqual(ev.sent[0][1], "（私聊说吧）")

    def test_failure_can_fall_back_to_group(self):
        ctx, bot = FakeContext(ok=False), FakeBot(ok=False)
        p = make_plugin(ctx, intercept_on_fail=False)
        ev = asyncio.run(_trigger(p, bot=bot))
        resp = FakeResponse("这是一个足够长的回答内容")
        asyncio.run(p.redirect_to_private(ev, resp))
        self.assertEqual(resp.completion_text, "这是一个足够长的回答内容")
        self.assertEqual(ev.sent[0][1], "（加我好友）")

    def test_no_bot_uses_context(self):
        ctx = FakeContext(ok=True)
        p = make_plugin(ctx)
        ev = asyncio.run(_trigger(p, bot=None))
        resp = FakeResponse("这是一个足够长的回答内容")
        asyncio.run(p.redirect_to_private(ev, resp))
        self.assertEqual(len(ctx.calls), 1)
        self.assertEqual(ctx.calls[0][0], "test:FriendMessage:200000")


class ExemptionTests(unittest.TestCase):
    def test_admin_exempt(self):
        p = make_plugin()
        ev = None
        for _ in range(5):
            ev = FakeEvent(admin=True)
            asyncio.run(p.mark_redirect(ev, object()))
        self.assertIsNone(ev.get_extra("_group_dm_guide_redirect"))

    def test_whitelist_exempt(self):
        p = make_plugin(whitelist=["200000"])
        ev = None
        for _ in range(5):
            ev = FakeEvent()
            asyncio.run(p.mark_redirect(ev, object()))
        self.assertIsNone(ev.get_extra("_group_dm_guide_redirect"))

    def test_private_chat_ignored(self):
        p = make_plugin()
        ev = None
        for _ in range(5):
            ev = FakeEvent(private=True)
            asyncio.run(p.mark_redirect(ev, object()))
        self.assertIsNone(ev.get_extra("_group_dm_guide_redirect"))

    def test_group_whitelist_scope(self):
        p = make_plugin(group_whitelist=["999999"])
        ev = None
        for _ in range(5):
            ev = FakeEvent(group="100000")
            asyncio.run(p.mark_redirect(ev, object()))
        self.assertIsNone(ev.get_extra("_group_dm_guide_redirect"))


class ModeTests(unittest.TestCase):
    def test_dry_run_sends_nothing(self):
        ctx = FakeContext()
        p = make_plugin(ctx, dry_run=True)
        ev = asyncio.run(_trigger(p))
        resp = FakeResponse("这是一个足够长的回答内容")
        asyncio.run(p.redirect_to_private(ev, resp))
        self.assertFalse(ctx.calls)
        self.assertEqual(resp.completion_text, "这是一个足够长的回答内容")

    def test_nudge_mode_blocks(self):
        p = make_plugin(redirect_reply=False)
        ev = asyncio.run(_trigger(p))
        self.assertTrue(ev.blocked)
        self.assertTrue(ev.stopped)
        self.assertEqual(ev.sent[0][1], "问太多了")

    def test_disabled_does_nothing(self):
        p = make_plugin(enable=False)
        ev = None
        for _ in range(5):
            ev = FakeEvent()
            asyncio.run(p.mark_redirect(ev, object()))
        self.assertIsNone(ev.get_extra("_group_dm_guide_redirect"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
