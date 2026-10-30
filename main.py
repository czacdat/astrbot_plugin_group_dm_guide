# -*- coding: utf-8 -*-
"""群聊高频提问引导插件。

两种模式：
  redirect_reply = true （默认）
      同一人在时间窗内问太多次后，不再在群里回他 —— 让模型正常生成回答，
      但把这条回答**私聊发给提问者**，群里最多留一句提示。
      私聊发不出去（对方不是好友）时，自动退回群里正常回复 + 一句提醒。

  redirect_reply = false
      老模式：群里发一句引导语，然后冷却期内完全不响应。

设计原则：
  · 只统计「机器人真要回话」的时机（on_llm_request），不数普通群聊；
  · 管理员、白名单、指定群可豁免；
  · dry_run 只记日志，不改发送行为。
"""

from __future__ import annotations

import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

DEFAULT_NUDGE = "……你问题有点多啦，群里说不太方便，加我好友私聊说吧。"
DEFAULT_GROUP_NOTICE = "（这里不方便说，私聊发你了）"
DEFAULT_FALLBACK = "（加我好友才能私聊哦）"
DEFAULT_BLOCKED_NOTICE = "（这些私聊说吧，群里不方便）"

# 标记本轮要转私聊
_EXTRA_REDIRECT = "_group_dm_guide_redirect"


@register(
    "astrbot_plugin_group_dm_guide",
    "DSH",
    "群聊高频提问引导：同一人在群里问太多次后，把回答私聊发给他；可选冷却期不响应",
    "1.1.0",
)
class GroupDmGuidePlugin(Star):
    """把群里的高频提问者引导到私聊。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        cfg = dict(config or {})

        self.enable = bool(cfg.get("enable", True))
        self.trigger_count = max(1, int(cfg.get("trigger_count") or 3))
        self.window_minutes = max(1, int(cfg.get("window_minutes") or 10))
        self.cooldown_minutes = max(0, int(cfg.get("cooldown_minutes") or 30))
        self.exempt_admins = bool(cfg.get("exempt_admins", True))
        self.dry_run = bool(cfg.get("dry_run", False))

        self.redirect_reply = bool(cfg.get("redirect_reply", True))
        self.block_during_cooldown = bool(cfg.get("block_during_cooldown", True))
        # 好友私聊失败时，是否尝试「群临时会话」（send_private_msg + group_id）
        self.enable_temp_session = bool(cfg.get("enable_temp_session", True))

        self.message = str(cfg.get("message") or "").strip() or DEFAULT_NUDGE
        self.group_notice = (
            str(cfg.get("group_notice")).strip()
            if cfg.get("group_notice") is not None
            else DEFAULT_GROUP_NOTICE
        )
        self.fallback_notice = (
            str(cfg.get("fallback_notice")).strip()
            if cfg.get("fallback_notice") is not None
            else DEFAULT_FALLBACK
        )
        self.blocked_notice = (
            str(cfg.get("blocked_notice")).strip()
            if cfg.get("blocked_notice") is not None
            else DEFAULT_BLOCKED_NOTICE
        )
        # 回答短于这个字数就不转私聊（太短的没必要挪地方），0 = 不限制
        self.min_redirect_chars = max(0, int(cfg.get("min_redirect_chars") or 0))
        # 私聊发不出去时：仍然拦截群回复，只留提示（否则退回群里正常回答）
        self.intercept_on_fail = bool(cfg.get("intercept_on_fail", True))

        self.whitelist = {str(x).strip() for x in (cfg.get("whitelist") or []) if str(x).strip()}
        self.group_whitelist = {
            str(x).strip() for x in (cfg.get("group_whitelist") or []) if str(x).strip()
        }

        self._hits: dict[str, list[float]] = {}
        self._cooldown: dict[str, float] = {}
        self._last_prune = time.time()

        logger.info(
            "[群聊引导] 已加载: 模式=%s, 阈值=%s次/%s分钟, 冷却=%s分钟, 最短转私聊=%s字, 私聊失败拦截=%s, dry_run=%s",
            "转私聊" if self.redirect_reply else "发引导语",
            self.trigger_count,
            self.window_minutes,
            self.cooldown_minutes,
            self.min_redirect_chars,
            self.intercept_on_fail,
            self.dry_run,
        )

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #
    @staticmethod
    def _key(group_id: str, user_id: str) -> str:
        return f"{group_id}|{user_id}"

    def _render(self, count: int) -> str:
        try:
            return self.message.format(count=count, minutes=self.window_minutes)
        except (KeyError, IndexError, ValueError):
            return self.message

    def _prune(self, now: float) -> None:
        if now - self._last_prune < 600:
            return
        self._last_prune = now
        window = self.window_minutes * 60
        for key in list(self._hits):
            stamps = [t for t in self._hits[key] if now - t <= window]
            if stamps:
                self._hits[key] = stamps
            else:
                self._hits.pop(key, None)
        for key, until in list(self._cooldown.items()):
            if until <= now:
                self._cooldown.pop(key, None)

    def _is_exempt(self, event: AstrMessageEvent, user_id: str) -> bool:
        if user_id in self.whitelist:
            return True
        if self.exempt_admins:
            try:
                if event.is_admin():
                    return True
            except Exception:  # noqa: BLE001
                pass
        return False

    def _in_scope(self, group_id: str) -> bool:
        return True if not self.group_whitelist else group_id in self.group_whitelist

    @staticmethod
    def _private_umo(event: AstrMessageEvent, user_id: str) -> str:
        """由当前事件的 umo 推导同平台的私聊会话。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        platform = umo.split(":", 1)[0] if ":" in umo else ""
        return f"{platform}:FriendMessage:{user_id}" if platform else ""

    async def _send_private(
        self, event: AstrMessageEvent, user_id: str, group_id: str, text: str
    ) -> str:
        """尝试私聊送达：好友私聊 → 群临时会话 → 其它平台兜底。

        返回 'friend' / 'temp' / ''。
        注意：context.send_message() 的返回值只代表「找到了对应平台」，
        不代表真的发出去，所以 aiocqhttp 下优先用 bot 直连（失败会抛异常）。
        """
        payload = [{"type": "text", "data": {"text": text}}]
        bot = getattr(event, "bot", None)

        if bot is not None and user_id.isdigit() and hasattr(bot, "send_private_msg"):
            attempts: list[tuple[dict, str]] = []
            if group_id.isdigit() and self.enable_temp_session:
                # OneBot v11：send_private_msg 带 group_id = 群临时会话
                attempts.append(
                    (
                        {"user_id": int(user_id), "group_id": int(group_id), "message": payload},
                        "temp",
                    )
                )
            attempts.append(({"user_id": int(user_id), "message": payload}, "friend"))

            for params, channel in attempts:
                try:
                    await bot.send_private_msg(**params)
                    return channel
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[群聊引导] 私聊尝试失败(channel=%s group_id=%s): %s",
                        channel,
                        params.get("group_id"),
                        exc,
                    )

        # 非 aiocqhttp 平台，或上面都失败时用通用接口兜底
        umo = self._private_umo(event, user_id)
        if umo:
            try:
                if await self.context.send_message(umo, MessageChain([Plain(text)])):
                    return "friend"
            except Exception as exc:  # noqa: BLE001
                logger.debug("[群聊引导] 通用私聊接口失败: %s", exc)

        return ""

    # ------------------------------------------------------------------ #
    # 钩子 1：决定这一轮要不要转私聊
    # ------------------------------------------------------------------ #
    @filter.on_llm_request(priority=5)
    async def mark_redirect(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.enable:
            return
        try:
            if event.is_private_chat():
                return
            group_id = str(event.get_group_id() or "").strip()
            user_id = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            return
        if not group_id or not user_id:
            return
        if not self._in_scope(group_id) or self._is_exempt(event, user_id):
            return

        now = time.time()
        self._prune(now)
        key = self._key(group_id, user_id)

        # 冷却中：继续转私聊（而不是完全不理）
        if self._cooldown.get(key, 0.0) > now:
            if self.redirect_reply:
                event.set_extra(_EXTRA_REDIRECT, True)
                logger.info(
                    "[群聊引导] 冷却中，本轮转私聊: group=%s user=%s dry_run=%s",
                    group_id, user_id, self.dry_run,
                )
                return
            if self.block_during_cooldown:
                logger.info(
                    "[群聊引导] 冷却中，跳过群消息: group=%s user=%s dry_run=%s",
                    group_id, user_id, self.dry_run,
                )
                if not self.dry_run:
                    event.should_call_llm(True)
                    event.stop_event()
            return

        window = self.window_minutes * 60
        stamps = [t for t in self._hits.get(key, []) if now - t <= window]
        stamps.append(now)
        self._hits[key] = stamps

        if len(stamps) < self.trigger_count:
            return

        logger.info(
            "[群聊引导] 触发: group=%s user=%s %s分钟内第%s次 模式=%s dry_run=%s",
            group_id, user_id, self.window_minutes, len(stamps),
            "转私聊" if self.redirect_reply else "引导语", self.dry_run,
        )

        self._hits[key] = []
        if self.cooldown_minutes > 0:
            self._cooldown[key] = now + self.cooldown_minutes * 60

        if self.dry_run:
            return

        if self.redirect_reply:
            event.set_extra(_EXTRA_REDIRECT, True)
            return

        # 老模式：发引导语 + 拦截本轮
        try:
            await event.send(event.plain_result(self._render(len(stamps))))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[群聊引导] 发送引导语失败: %s", exc)
        event.should_call_llm(True)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 钩子 2：把这一轮的回答私聊发出去
    # ------------------------------------------------------------------ #
    @filter.on_llm_response(priority=-100)
    async def redirect_to_private(self, event: AstrMessageEvent, response: LLMResponse):
        if not self.enable or not self.redirect_reply:
            return
        if not event.get_extra(_EXTRA_REDIRECT):
            return
        event.set_extra(_EXTRA_REDIRECT, False)

        # 工具调用轮不是最终回答：跳过，等最后一条再转
        # （否则多步工具调用会把"我查一下"这类中间话也私聊发出去）
        if getattr(response, "tools_call_args", None) or getattr(
            response, "tools_call_name", None
        ):
            logger.info(
                "[群聊引导] 工具调用轮，先不转私聊: user=%s",
                event.get_sender_id(),
            )
            return

        text = str(getattr(response, "completion_text", "") or "").strip()
        if not text:
            return

        # 回答太短就不折腾了，正常留在群里
        if self.min_redirect_chars and len(text) < self.min_redirect_chars:
            logger.info(
                "[群聊引导] 回答只有 %s 字（< %s），不转私聊: user=%s",
                len(text),
                self.min_redirect_chars,
                event.get_sender_id(),
            )
            return

        try:
            user_id = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            return

        umo = self._private_umo(event, user_id)
        if not umo and not self.enable_temp_session:
            return

        if self.dry_run:
            logger.info("[群聊引导] dry_run：本应私聊发送 %s 字给 %s", len(text), user_id)
            return

        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:  # noqa: BLE001
            group_id = ""

        channel = await self._send_private(event, user_id, group_id, text)

        if channel:
            # 清空正文，群里不再重复这条回答
            response.completion_text = ""
            if self.group_notice:
                try:
                    await event.send(event.plain_result(self.group_notice))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[群聊引导] 群里提示发送失败: %s", exc)
            logger.info(
                "[群聊引导] 已转私聊(%s): user=%s 字数=%s",
                "好友" if channel == "friend" else "临时会话",
                user_id,
                len(text),
            )
            return

        if self.intercept_on_fail:
            # 私聊发不出去：仍然拦截，只留一句提示
            response.completion_text = ""
            notice = self.blocked_notice
            logger.info(
                "[群聊引导] 私聊未送达，已拦截群回复: user=%s 字数=%s",
                user_id,
                len(text),
            )
        else:
            # 退回群里正常回复
            notice = self.fallback_notice
            logger.info("[群聊引导] 私聊未送达，退回群里回复: user=%s", user_id)

        if notice:
            try:
                await event.send(event.plain_result(notice))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[群聊引导] 提示发送失败: %s", exc)
